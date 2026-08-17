from dataclasses import replace
from datetime import date, datetime
from typing import Callable, Protocol

from reader.commands.base import Command, CommandContext, CommandError, CommandResult
from reader.ocr.models import REPLY_SECTIONS, OcrResult
from reader.ocr.service import OcrServiceError
from reader.time_display import TBILISI_TZ

_SUPPORTED_MIME_TYPES = ("image/jpeg", "image/png", "image/webp")

_USAGE_ERROR = "Используйте: insurance ocr (приложите фото или изображение документа)"
_NO_IMAGE_ERROR = "Не найдено изображение документа."
_UNSUPPORTED_MEDIA_ERROR = (
    "Неподдерживаемый формат файла. Пришлите фото или изображение документа (JPEG/PNG/WEBP)."
)
_OCR_FAILED_ERROR = "Не удалось получить ответ сервиса распознавания. Попробуйте ещё раз."
_NOTHING_RECOGNIZED_ERROR = "Не удалось распознать документ. Попробуйте прислать более чёткое фото."

_NOT_RECOGNIZED = "не распознано"

# Default-значения новой checkout-заявки (см. reader/ocr/models.py::OcrResult)
# — НЕ из config, фиксированы бизнес-требованием: банк-эквайер и период
# полиса оператор чаще всего не меняет, дата начала периода — календарная
# дата создания ИМЕННО ЭТОГО OCR-черновика (см. _process_and_reply).
_DEFAULT_PAYMENT_BANK = "bog"
_DEFAULT_POLICY_PERIOD = "15"
_PERIOD_START_FORMAT = "%d.%m.%Y"


class OcrServiceLike(Protocol):
    """Ровно то, что нужно InsuranceOcrCommand от OcrService (см.
    reader/ocr/service.py) — не импортируем сам класс в тесты, которым
    нужен только фейк (без реального openai-клиента)."""

    async def extract(self, images: list[tuple[bytes, str]]) -> OcrResult: ...


def _media_mime_type(event) -> str | None:
    """None — на этом сообщении нет пригодного изображения (нет медиа
    вовсе, либо есть, но не photo/поддерживаемый image-document)."""
    if getattr(event, "photo", None) is not None:
        return "image/jpeg"  # Telegram Photo — всегда JPEG

    document = getattr(event, "document", None)
    if document is not None:
        mime_type = getattr(document, "mime_type", None)
        if mime_type in _SUPPORTED_MIME_TYPES:
            return mime_type

    return None


def _has_any_media(event) -> bool:
    return getattr(event, "media", None) is not None


def _is_ocr_trigger(text: str | None) -> bool:
    """Тот же формат, что и у CommandDispatcher (text.strip().split()) —
    используется AlbumCollector-веткой (handle_album), которая получает
    "сырые" события в обход CommandDispatcher.handle_event для сообщений
    без caption (остальные части альбома, см. reader/commands/album_collector.py)."""
    parts = (text or "").strip().split()
    return len(parts) >= 2 and parts[0].lower() == "insurance" and parts[1].lower() == "ocr"


def _with_draft_defaults(
    result: OcrResult,
    *,
    default_email: str | None,
    default_phone: str | None,
    today: date,
) -> OcrResult:
    """email/phone/payment_bank/policy_period/period_start не распознаются
    OCR (см. reader/ocr/models.py::OcrResult) — заполняются default-
    значениями ДО того, как Telegram-текст сформирован, чтобы оператор мог
    их скорректировать тем же correction-reply, что и остальные поля
    (единственная запись о распознанных/эффективных полях — сам текст
    сообщения, см. reader/checkout/parser.py).

    today — календарная дата создания ИМЕННО ЭТОГО черновика (см.
    _process_and_reply, вызывается один раз за обработку), а не today()
    заново при каждом обращении — иначе "Начало периода" молча съезжало бы,
    если оператор подтвердит заявку позже/после полуночи (см. задачу)."""
    return replace(
        result,
        email=result.email or default_email,
        phone=result.phone or default_phone,
        payment_bank=result.payment_bank or _DEFAULT_PAYMENT_BANK,
        policy_period=result.policy_period or _DEFAULT_POLICY_PERIOD,
        period_start=result.period_start or today.strftime(_PERIOD_START_FORMAT),
    )


def _format_result(result: OcrResult) -> str:
    lines = ["Распознано:", ""]
    for section in REPLY_SECTIONS:
        for field in section:
            value = getattr(result, field.attr)
            if field.is_flag:
                rendered = "+" if value else "-"
            elif field.empty_when_none:
                # Водитель/Владелец: None означает "совпадает со
                # страхователем", а не "не удалось распознать" — никогда не
                # показываем "не распознано" для этих двух полей (см.
                # reader/ocr/models.py::ReplyField.empty_when_none).
                rendered = value or ""
            else:
                rendered = value or _NOT_RECOGNIZED
            lines.append(f"{field.label}: {rendered}")
        lines.append("")
    lines.append("Проверь данные.")
    return "\n".join(lines)


class InsuranceOcrCommand(Command):
    """`insurance ocr` — оператор прикладывает фото/документ(ы) автомобиля
    к сообщению с этим текстом, ai-lead-radar скачивает вложение(я),
    отправляет в OcrService (см. reader/ocr/service.py) и отвечает reply на
    исходное сообщение распознанными полями.

    Полностью независима от auto-insurance — ничего оттуда не
    импортируется, это отдельная реализация только с тем же общим
    архитектурным подходом (Structured Outputs), см. задачу.

    Ничего не пишет в БД, не создаёт заказов/оплат — единственный эффект
    команды - один reply-текст оператору. Изображения обрабатываются
    только в памяти (см. reader/ocr/service.py) и нигде не сохраняются.

    Одно фото/документ — обрабатывается прямо в handle() (вызывается
    CommandDispatcher). Альбом (Telegram грузит несколько фото одним
    сообщением как ГРУППУ отдельных апдейтов с общим grouped_id, подпись —
    только на одном из них) — handle() для такого сообщения ничего не
    делает (см. ниже): его целиком собирает и обрабатывает AlbumCollector
    (см. reader/commands/album_collector.py), вызывающий handle_album()
    ПОСЛЕ того как соберёт все части группы — иначе OCR получил бы только
    одно фото из нескольких и/или сообщение обработалось бы дважды."""

    name = "insurance"

    def __init__(
        self,
        ocr_service: OcrServiceLike,
        *,
        default_email: str | None = None,
        default_phone: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self._ocr_service = ocr_service
        self._default_email = default_email
        self._default_phone = default_phone
        # Тбилисская дата, а не UTC/naive — тот же часовой пояс, что и у
        # остального оператор-фейсинг вывода (см. reader/time_display.py).
        self._clock = clock or (lambda: datetime.now(TBILISI_TZ))

    async def handle(self, ctx: CommandContext) -> CommandResult:
        if not ctx.args or ctx.args[0].lower() != "ocr":
            raise CommandError(_USAGE_ERROR)

        event = ctx.event
        if getattr(event, "grouped_id", None) is not None:
            # Часть Telegram-альбома — реальная обработка происходит в
            # AlbumCollector.on_group_ready -> self.handle_album (см.
            # докстрок класса и reader/main.py) один раз на всю группу,
            # после того как соберутся остальные фото. Здесь — намеренный
            # no-op, чтобы не отправить reply дважды и не обработать
            # только это одно фото из нескольких.
            return CommandResult(text="")

        await self._process_and_reply([event], trigger=event)
        return CommandResult(text="")

    async def handle_album(self, events: list) -> None:
        """Вызывается AlbumCollector'ом ОДИН раз, когда все части альбома
        собраны (см. reader/commands/album_collector.py). Если ни одно из
        событий группы не несёт "insurance ocr" — это не наш альбом,
        игнорируем молча (как и CommandDispatcher игнорирует произвольные
        сообщения в чате).

        Допуск по отправителю НЕ проверяется (см. задачу: "внутри
        настроенного OCR-чата документы может отправлять любой участник") —
        единственное требование это сам чат, а его уже гарантирует
        AlbumCollector.start(), зарегистрированный с chats=[тот же чат]
        (см. reader/commands/album_collector.py и reader/main.py)."""
        trigger = self._find_trigger(events)
        if trigger is None:
            return

        await self._process_and_reply(events, trigger=trigger)

    @staticmethod
    def _find_trigger(events: list):
        for event in events:
            if _is_ocr_trigger(getattr(event, "raw_text", None)):
                return event
        return None

    async def _process_and_reply(self, events: list, *, trigger) -> None:
        images, unsupported_found = await self._extract_images(events)

        if not images:
            await trigger.reply(_UNSUPPORTED_MEDIA_ERROR if unsupported_found else _NO_IMAGE_ERROR)
            return

        try:
            result = await self._ocr_service.extract(images)
        except OcrServiceError:
            await trigger.reply(_OCR_FAILED_ERROR)
            return

        if result.fields_found_count == 0:
            await trigger.reply(_NOTHING_RECOGNIZED_ERROR)
            return

        # Дата вычисляется РОВНО ОДИН РАЗ здесь (не заново при "pay" —
        # см. reader/checkout/mapping.py::resolve_period_start) и с этого
        # момента живёт только в тексте сообщения, как и остальные поля.
        result = _with_draft_defaults(
            result,
            default_email=self._default_email,
            default_phone=self._default_phone,
            today=self._clock().date(),
        )
        await trigger.reply(_format_result(result))

    @staticmethod
    async def _extract_images(events: list) -> tuple[list[tuple[bytes, str]], bool]:
        images: list[tuple[bytes, str]] = []
        unsupported_found = False

        for event in events:
            mime_type = _media_mime_type(event)
            if mime_type is None:
                if _has_any_media(event):
                    unsupported_found = True
                continue

            # Telethon скачивает напрямую в память (file=bytes) — ни один
            # temp-файл на диске не создаётся (см. задачу).
            data = await event.download_media(file=bytes)
            if data:
                images.append((data, mime_type))

        return images, unsupported_found
