import logging
from typing import Protocol

from reader.commands.base import Command, CommandContext, CommandError, CommandResult
from reader.ocr.models import REPLY_FIELD_LABELS, OcrResult
from reader.ocr.service import OcrServiceError

logger = logging.getLogger(__name__)

_SUPPORTED_MIME_TYPES = ("image/jpeg", "image/png", "image/webp")

_USAGE_ERROR = "Используйте: insurance ocr (приложите фото или изображение документа)"
_NO_IMAGE_ERROR = "Не найдено изображение документа."
_UNSUPPORTED_MEDIA_ERROR = (
    "Неподдерживаемый формат файла. Пришлите фото или изображение документа (JPEG/PNG/WEBP)."
)
_OCR_FAILED_ERROR = "Не удалось получить ответ сервиса распознавания. Попробуйте ещё раз."
_NOTHING_RECOGNIZED_ERROR = "Не удалось распознать документ. Попробуйте прислать более чёткое фото."

_NOT_RECOGNIZED = "не распознано"

# Формат/порядок полей — см. reader/ocr/models.py::REPLY_FIELD_LABELS
# (общий источник правды с reader/checkout/parser.py, который разбирает тот
# же формат в обратную сторону — reply оператора с "pay"/исправленными
# полями, см. задачу про checkout).
_REPLY_FIELDS = REPLY_FIELD_LABELS


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


def _format_result(result: OcrResult) -> str:
    lines = ["Распознано:", ""]
    for label, attr in _REPLY_FIELDS:
        value = getattr(result, attr) or _NOT_RECOGNIZED
        lines.append(f"{label}: {value}")
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

    def __init__(self, ocr_service: OcrServiceLike, *, allowed_user_ids: list[int]):
        self._ocr_service = ocr_service
        self._allowed_user_ids = set(allowed_user_ids)

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
        сообщения в чате)."""
        trigger = self._find_trigger(events)
        if trigger is None:
            return

        # CommandDispatcher сам проверяет allowed_user_ids ДО вызова
        # handle() (см. reader/commands/dispatcher.py), но AlbumCollector
        # получает события в обход диспетчера — та же проверка нужна и
        # здесь, иначе альбом от неавторизованного отправителя был бы
        # обработан.
        if getattr(trigger, "sender_id", None) not in self._allowed_user_ids:
            logger.info(
                "Альбом insurance ocr проигнорирован: sender_id=%s не в allowed_user_ids",
                getattr(trigger, "sender_id", None),
            )
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
