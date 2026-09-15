"""ConversationController Turkey-бота — линейный, одноразовый цикл
"номер -> CAPTCHA -> код -> результат" (см. design report Stage 3).
НИЧЕГО не знает про Telethon (та же граница, что и
reader/public_bot/conversation.py) — reader/turkey_bot/handlers.py
конвертирует BotReply в реальные Telegram-вызовы (в частности,
photo_png -> отправка фото, чего у Георгии нет вовсе).

Ключевой архитектурный принцип (см.
reader/turkey_bot/live_session_registry.py и design report Stage 3):
ЖИВАЯ GIB-сессия (httpx.AsyncClient/cookies) существует ТОЛЬКО в памяти
этого процесса, никогда в БД. bot_conversation_state хранит только "какой
номер проверяли и какой image_id был последним показан" — этого
достаточно, чтобы после рестарта/протухания сессии молча запросить НОВУЮ
CAPTCHA для того же номера, но НЕДОСТАТОЧНО и не может быть достаточно для
восстановления самой сессии. Каждый обработчик, который может изменить
состояние конкретного chat_id, серилизован через
LiveGibSessionRegistry.lock_for(chat_id) (см. design report: "per-chat
locking") — защита от гонки между двумя почти одновременными сообщениями
одного и того же диалога.
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx

from reader.turkey_bot import texts
from reader.turkey_bot.check_repository import TurkeyCheckRepository
from reader.turkey_bot.conversation_state_repository import (
    TurkeyConversationStateRepository,
)
from reader.turkey_bot.gib.models import (
    CaptchaChallenge,
    GibFineRecord,
    GibSubmitOutcome,
)
from reader.turkey_bot.gib.provider import GibProvider
from reader.turkey_bot.gib.session import GibSession, GibTransportError
from reader.turkey_bot.gib.translation import FineTranslationError
from reader.turkey_bot.live_session_registry import LiveGibCheck, LiveGibSessionRegistry
from reader.turkey_bot.models import ConversationState, TurkeyUserCar
from reader.turkey_bot.statistics_service import TurkeyStatisticsService
from reader.turkey_bot.user_cars_repository import TurkeyUserCarsRepository
from reader.turkey_bot.validation import normalize_plate

logger = logging.getLogger(__name__)

# Модульный singleton (см. B008) - default для ConversationController(tz=...),
# когда вызывающий код (тесты, не рассчитанные на статистику) не передаёт
# tz вовсе; main.py всегда передаёт реальный settings.fine_monitor.timezone.
_DEFAULT_TZ = ZoneInfo("UTC")

_STEP_AWAITING_CODE = "awaiting_captcha_code"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_REQUEST_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class BotReply:
    """Что показать пользователю. photo_png не None ТОЛЬКО когда нужно
    отправить (новую) CAPTCHA-картинку — handlers.py решает, отправлять ли
    фото или обычный текст, исходя из этого поля. show_cancel_button —
    прикрепить ли inline "❌ Отмена" (см. reader/turkey_bot/keyboards.py) —
    всегда True одновременно с photo_png (пока идёт диалог, отмена должна
    быть доступна), но выражено отдельным полем, а не выведено из
    photo_png is not None, чтобы handlers.py не должен был знать про эту
    связь.

    extra_texts — ДОПОЛНИТЕЛЬНЫЕ сообщения, отправляемые ПОСЛЕ text (без
    фото/кнопок, кроме случая show_main_menu — см. ниже) — has_debt со
    многими штрафами (см. reader/turkey_bot/texts.py::
    format_has_debt_messages) и список пользователей для 📊 Статистика
    (см. format_user_list_messages) — оба уже возвращают готовый список
    сообщений с сохранёнными границами. Пусто во всех остальных случаях
    (см. задачу: "CAPTCHA flow ... must remain unchanged").

    show_main_menu — прикрепить ли персистентную reply-клавиатуру (см.
    reader/turkey_bot/keyboards.py::main_menu_keyboard) к ПОСЛЕДНЕМУ
    отправленному сообщению (text, если extra_texts пуст, иначе —
    последний elements extra_texts) — handlers.py решает это, а не
    conversation.py (который ничего не знает про Telethon-клавиатуры).
    Игнорируется, если show_cancel_button/garage_cars тоже установлены —
    Telethon не может прикрепить два разных вида клавиатуры к одному
    сообщению, а показывать reply-меню ПОКА идёт диалог (CAPTCHA) или
    рядом с inline-гаражом не нужно.

    garage_cars — записи "гаража" ТЕКУЩЕГО пользователя (см.
    reader/turkey_bot/user_cars_repository.py) для inline-клавиатуры
    "🚗 Мои автомобили" (см. reader/turkey_bot/keyboards.py::
    garage_keyboard) — None, когда это не список гаража вовсе (отличает
    "гараж пуст" — пустой tuple — от "это вообще не гараж")."""

    text: str
    photo_png: bytes | None = None
    show_cancel_button: bool = False
    extra_texts: tuple[str, ...] = ()
    show_main_menu: bool = False
    garage_cars: tuple[TurkeyUserCar, ...] | None = None


class _AsyncCloseable(Protocol):
    """Ровно то, что LiveGibSessionRegistry требует от .client (см.
    reader/turkey_bot/live_session_registry.py::pop_and_close) — тот же
    Protocol-приём, что и FineTranslatorLike в reader/fines/check_service.py,
    чтобы тесты могли подменить httpx.AsyncClient лёгким фейком без
    реальной сети."""

    async def aclose(self) -> None: ...


CheckFactory = Callable[[], tuple[_AsyncCloseable, GibProvider]]


class FineTranslatorLike(Protocol):
    """Ровно то, что нужно отсюда от TurkeyFineTranslationService (см.
    reader/turkey_bot/gib/translation.py) — тот же Protocol-приём, что и
    FineTranslatorLike в reader/fines/check_service.py (грузинский
    аналог), чтобы тесты могли подменить реальный OpenAI-клиент лёгким
    фейком без сети."""

    async def translate_fines(
        self, fines: tuple[GibFineRecord, ...],
    ) -> tuple[GibFineRecord, ...]: ...


def _build_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS, headers={"User-Agent": _USER_AGENT})


def _default_check_factory() -> tuple[httpx.AsyncClient, GibProvider]:
    """Реальная GIB-сессия (см. design report Stage 3) — единственный
    вызывающий код в production (reader/turkey_bot/main.py) полагается на
    значение по умолчанию; тесты передают свой check_factory (см.
    tests/test_turkey_bot_conversation.py) вместо реальной сети."""
    client = _build_client()
    return client, GibProvider(GibSession(client))


def _sanitize_outcome_for_storage(outcome: GibSubmitOutcome) -> str:
    """См. модуль docstring reader/turkey_bot/texts.py и задачу: то, что
    здесь сериализуется, идёт ТОЛЬКО в TurkeyCheckRepository (server-side),
    никогда пользователю. Никогда не содержит captcha_code/cookies — их
    здесь просто нет: GibSubmitOutcome их не несёт (см.
    reader/turkey_bot/gib/models.py)."""
    return json.dumps(
        {
            "kind": outcome.kind,
            "messages": [{"type": m.type, "text": m.text} for m in outcome.messages],
            "raw_data": outcome.raw_data,
        },
        ensure_ascii=False,
        default=str,
    )


class ConversationController:
    def __init__(
        self,
        conversation_state_repository: TurkeyConversationStateRepository,
        check_repository: TurkeyCheckRepository,
        session_registry: LiveGibSessionRegistry,
        garage_repository: TurkeyUserCarsRepository,
        statistics_service: TurkeyStatisticsService,
        *,
        check_factory: CheckFactory = _default_check_factory,
        translator: FineTranslatorLike | None = None,
        trusted_operator_user_ids: frozenset[int] = frozenset(),
        tz: ZoneInfo = _DEFAULT_TZ,
    ):
        self._states = conversation_state_repository
        self._checks = check_repository
        self._registry = session_registry
        self._garage = garage_repository
        self._statistics = statistics_service
        self._check_factory = check_factory
        # None — как и everywhere в проекте (см. FineTranslatorLike в
        # reader/fines/check_service.py) — означает "перевод недоступен"
        # (нет OPENAI_API_KEY, см. main.py), не ошибку: клиент увидит
        # оригинальный турецкий текст вместо перевода (см. _translate_fines).
        self._translator = translator
        # frozenset(...) на входе — на случай, если вызывающий код (см.
        # reader/turkey_bot/main.py) передал обычный list из config.yaml
        # (тот же приём, что и reader/public_bot/conversation.py). ТА ЖЕ
        # настройка, что и у @ProtocolGEbot (settings.public_bot.
        # trusted_operator_user_ids) — см. design report: "reuse the same
        # trusted manager IDs/configuration ... do not duplicate/hardcode
        # a second manager list".
        self._trusted_operator_user_ids = frozenset(trusted_operator_user_ids)
        self._tz = tz

    def _is_trusted(self, telegram_user_id: int) -> bool:
        """Единственная проверка авторизации trusted-режима — ТОЛЬКО по
        numeric telegram_user_id (тот же принцип, что и
        reader/public_bot/conversation.py::_is_trusted), никогда по
        username. Trusted даёт доступ ТОЛЬКО к статистике (см. design
        report: "Trusted status grants access to statistics only — it
        does not grant managers access to another user's Turkey garage or
        checks") — гараж всегда фильтруется по РЕАЛЬНОМУ telegram_user_id
        вызывающего, независимо от trusted-статуса."""
        return telegram_user_id in self._trusted_operator_user_ids

    def is_trusted(self, telegram_user_id: int) -> bool:
        """Публичная обёртка — нужна reader/turkey_bot/handlers.py, чтобы
        решить, показывать ли STATISTICS_LABEL в главном меню (см.
        reader/turkey_bot/keyboards.py::main_menu_keyboard)."""
        return self._is_trusted(telegram_user_id)

    async def handle_text(self, text: str, *, chat_id: int, telegram_user_id: int) -> BotReply:
        stripped = text.strip()
        lowered = stripped.lower()

        if lowered == "/start":
            async with self._registry.lock_for(chat_id):
                await self._registry.pop_and_close(chat_id)
                self._states.clear(chat_id)
            return BotReply(text=texts.WELCOME_TEXT, show_main_menu=True)

        if lowered == "/cancel":
            return await self.handle_cancel(chat_id=chat_id)

        # Пункты reply-меню (см. reader/turkey_bot/keyboards.py::
        # main_menu_keyboard) проверяются С ПРИОРИТЕТОМ НАД шагом диалога
        # (тот же порядок, что и /start/​/cancel выше, и тот же принцип, что
        # и у reader/public_bot/conversation.py::_handle_menu_label) —
        # нажатие кнопки меню всегда навигирует, а не тихо трактуется как
        # (заведомо неверный) код CAPTCHA.
        if stripped == texts.GARAGE_LABEL:
            return await self._handle_garage(chat_id=chat_id, telegram_user_id=telegram_user_id)

        if stripped == texts.STATISTICS_LABEL and self._is_trusted(telegram_user_id):
            return await self._handle_statistics(chat_id=chat_id)

        state = self._states.get(chat_id)
        if state is not None and state.step == _STEP_AWAITING_CODE:
            return await self._handle_captcha_code(
                stripped, chat_id=chat_id, telegram_user_id=telegram_user_id, state=state,
            )

        return await self._handle_new_plate(stripped, chat_id=chat_id, telegram_user_id=telegram_user_id)

    async def handle_cancel(self, *, chat_id: int) -> BotReply:
        """Общий путь и для текстовой команды /cancel, и для inline
        "❌ Отмена" (см. reader/turkey_bot/handlers.py) — оба ведут сюда."""
        async with self._registry.lock_for(chat_id):
            had_live_check = await self._registry.get(chat_id) is not None
            had_state = self._states.get(chat_id) is not None
            if not had_live_check and not had_state:
                return BotReply(text=texts.NOTHING_TO_CANCEL_TEXT, show_main_menu=True)

            await self._registry.pop_and_close(chat_id)
            self._states.clear(chat_id)
            return BotReply(text=texts.CANCEL_CONFIRM_TEXT, show_main_menu=True)

    async def _handle_garage(self, *, chat_id: int, telegram_user_id: int) -> BotReply:
        """🚗 Мои авто — доступно ВСЕМ (см. design report), в отличие от
        статистики. Список ВСЕГДА фильтруется по РЕАЛЬНОМУ telegram_user_id
        вызывающего (см. TurkeyUserCarsRepository.list_cars) — нет
        отдельного "trusted"-режима просмотра чужого гаража (см. design
        report: "Trusted status grants access to statistics only")."""
        async with self._registry.lock_for(chat_id):
            # Открытие "🚗 Мои авто" - явная навигация, как и /cancel -
            # прошлый незавершённый диалог (если был) отбрасывается (тот
            # же принцип, что и у reader/public_bot/conversation.py про
            # пункты меню).
            await self._registry.pop_and_close(chat_id)
            self._states.clear(chat_id)

        cars = tuple(self._garage.list_cars(telegram_user_id))
        if not cars:
            return BotReply(text=texts.EMPTY_GARAGE_TEXT, show_main_menu=True)
        return BotReply(text=texts.GARAGE_HEADER, garage_cars=cars)

    async def _handle_statistics(self, *, chat_id: int) -> BotReply:
        """Вызывающий код (handle_text) уже проверил is_trusted() —
        см. design report: единственная проверка авторизации для
        статистики, здесь не повторяется."""
        async with self._registry.lock_for(chat_id):
            await self._registry.pop_and_close(chat_id)
            self._states.clear(chat_id)

        stats = self._statistics.get_statistics(now=datetime.now(timezone.utc), tz=self._tz)
        user_messages = texts.format_user_list_messages(self._statistics.list_known_users())
        return BotReply(
            text=texts.format_statistics(stats),
            extra_texts=tuple(user_messages),
            show_main_menu=True,
        )

    async def handle_garage_check(
        self, car_id: int, *, chat_id: int, telegram_user_id: int,
    ) -> BotReply | None:
        """None — car_id не существует ИЛИ принадлежит другому
        пользователю (см. TurkeyUserCarsRepository.get_owned_car) —
        reader/turkey_bot/handlers.py должен показать общий
        "неизвестная кнопка" alert и ничего не начинать (тот же принцип,
        что и у reader/public_bot про чужой subscription_id, см. design
        report: "A normal user must not be able to inspect another
        user's garage by forging callback data")."""
        car = self._garage.get_owned_car(car_id, telegram_user_id=telegram_user_id)
        if car is None:
            return None
        return await self._start_check_for_plate(
            car.car_number, chat_id=chat_id, telegram_user_id=telegram_user_id,
        )

    async def _handle_new_plate(
        self, raw_plate: str, *, chat_id: int, telegram_user_id: int,
    ) -> BotReply:
        plate = normalize_plate(raw_plate)
        if plate is None:
            return BotReply(text=texts.INVALID_PLATE_TEXT, show_main_menu=True)

        return await self._start_check_for_plate(plate, chat_id=chat_id, telegram_user_id=telegram_user_id)

    async def _start_check_for_plate(
        self, plate: str, *, chat_id: int, telegram_user_id: int,
    ) -> BotReply:
        """Общее ядро для "ввёл номер вручную" (см. _handle_new_plate) И
        "нажал машину в гараже" (см. handle_garage_check) — CAPTCHA
        обязательна в обоих случаях одинаково (см. design report: "CAPTCHA
        remains mandatory. Do not bypass or automate it"), пользователю
        никогда не нужно вводить номер повторно во втором случае, потому
        что plate сюда приходит уже готовым (либо из ввода, либо из
        TurkeyUserCarsRepository), а не запрашивается заново."""
        async with self._registry.lock_for(chat_id):
            # Этот путь достигается ТОЛЬКО из состояния IDLE (см.
            # handle_text — пока step == awaiting_captcha_code, ЛЮБОЙ текст
            # трактуется как код, а не как новый номер: коды CAPTCHA и
            # номера визуально слишком похожи, чтобы угадывать намерение
            # пользователя, см. design report Stage 3 — новый номер вместо
            # кода НЕ поддерживается, вместо этого явный /cancel). Тем не
            # менее pop_and_close() здесь — дешёвая защита на случай, если
            # registry и persisted-состояние когда-либо разойдутся
            # (например, после ошибки в предыдущем цикле) — обычно no-op.
            await self._registry.pop_and_close(chat_id)
            self._states.clear(chat_id)

            challenge = await self._open_new_check(chat_id, telegram_user_id, plate)
            if challenge is None:
                return BotReply(text=texts.CAPTCHA_FETCH_FAILED_TEXT, show_main_menu=True)

            return BotReply(
                text=texts.ASK_CAPTCHA_TEXT, photo_png=challenge.image_png, show_cancel_button=True,
            )

    async def _handle_captcha_code(
        self, code: str, *, chat_id: int, telegram_user_id: int, state: ConversationState,
    ) -> BotReply:
        async with self._registry.lock_for(chat_id):
            check = await self._registry.get(chat_id)

            if check is None:
                # Рестарт процесса, естественная idle-TTL эвикция, или
                # устаревший challenge — reader/turkey_bot/
                # live_session_registry.py не различает эти случаи, и мы
                # тоже не должны: во всех — молча запросить новую CAPTCHA
                # для номера, уже сохранённого в payload (см. design
                # report Stage 3, "restart recovery").
                plate = (state.payload or {}).get("plate")
                if not plate:
                    self._states.clear(chat_id)
                    return BotReply(text=texts.SESSION_LOST_TEXT, show_main_menu=True)

                challenge = await self._open_new_check(chat_id, telegram_user_id, plate)
                if challenge is None:
                    self._states.clear(chat_id)
                    return BotReply(text=texts.CAPTCHA_FETCH_FAILED_TEXT, show_main_menu=True)

                return BotReply(
                    text=texts.SESSION_EXPIRED_RETRY_TEXT,
                    photo_png=challenge.image_png,
                    show_cancel_button=True,
                )

            check.submit_attempts += 1
            try:
                outcome = await check.provider.submit(
                    plate=check.plate, image_id=check.image_id, captcha_code=code,
                )
            except GibTransportError:
                await self._finish(
                    chat_id, telegram_user_id=telegram_user_id, check=check,
                    status="error", gib_message_text=None, raw_response=None,
                )
                logger.warning("Turkey GIB submit: transport error (chat_id=%s)", chat_id)
                return BotReply(text=texts.TRANSPORT_ERROR_TEXT, show_main_menu=True)

            return await self._handle_submit_outcome(
                outcome, chat_id=chat_id, telegram_user_id=telegram_user_id, check=check,
            )

    async def _handle_submit_outcome(
        self, outcome: GibSubmitOutcome, *, chat_id: int, telegram_user_id: int, check: LiveGibCheck,
    ) -> BotReply:
        if outcome.kind == "rejected":
            try:
                challenge = await check.provider.refresh_captcha()
            except GibTransportError:
                await self._finish(
                    chat_id, telegram_user_id=telegram_user_id, check=check,
                    status="error", gib_message_text=None, raw_response=None,
                )
                logger.warning("Turkey GIB refresh_captcha: transport error (chat_id=%s)", chat_id)
                return BotReply(text=texts.TRANSPORT_ERROR_TEXT, show_main_menu=True)

            check.image_id = challenge.image_id
            self._states.set(
                chat_id, telegram_user_id=telegram_user_id, step=_STEP_AWAITING_CODE,
                payload={"plate": check.plate, "image_id": challenge.image_id},
            )
            return BotReply(
                text=texts.CAPTCHA_REJECTED_RETRY_TEXT, photo_png=challenge.image_png,
                show_cancel_button=True,
            )

        message_text = outcome.messages[0].text if outcome.messages else None
        raw_response = _sanitize_outcome_for_storage(outcome)

        if outcome.kind == "no_debt":
            # "Гараж" (см. reader/turkey_bot/user_cars_repository.py) —
            # ТОЛЬКО для реально завершённых no_debt/has_debt (см. design
            # report: "regardless of whether the result was has_debt /
            # no_debt... CAPTCHA/rejected/unexpected/abandoned checks must
            # not pollute the garage") — этот вызов и его аналог в ветке
            # has_debt ниже единственные места, где вообще пишется в
            # turkey_bot_user_cars.
            self._garage.record_successful_check(
                telegram_user_id=telegram_user_id, car_number=check.plate,
            )
            await self._finish(
                chat_id, telegram_user_id=telegram_user_id, check=check,
                status="no_debt", gib_message_text=message_text, raw_response=raw_response,
            )
            return BotReply(text=texts.no_debt_text(check.plate), show_main_menu=True)

        if outcome.kind == "has_debt":
            self._garage.record_successful_check(
                telegram_user_id=telegram_user_id, car_number=check.plate,
            )
            # Перевод location/violation_description на русский (см.
            # reader/turkey_bot/gib/translation.py) - fail-open, см.
            # _translate_fines: недоступный/сбойный перевод НИКОГДА не
            # мешает показать результат (см. задачу).
            fines = await self._translate_fines(outcome.fines)
            await self._finish(
                chat_id, telegram_user_id=telegram_user_id, check=check,
                status="has_debt", gib_message_text=message_text, raw_response=raw_response,
            )
            # См. reader/turkey_bot/texts.py::format_has_debt_messages -
            # строит текст ТОЛЬКО из уже типизированных GibFineRecord (см.
            # reader/turkey_bot/gib/fine_parser.py) - conversation.py здесь
            # НЕ интерпретирует raw JSON вообще (см. задачу). Список
            # сообщений (см. задачу про лимит Telegram) - первое идёт как
            # основной ответ, остальные - extra_texts.
            rendered_messages = texts.format_has_debt_messages(check.plate, fines)
            return BotReply(
                text=rendered_messages[0], extra_texts=tuple(rendered_messages[1:]),
                show_main_menu=True,
            )

        # "unexpected" — НЕ трогает гараж (см. design report: "unexpected
        # response does not add car").
        await self._finish(
            chat_id, telegram_user_id=telegram_user_id, check=check,
            status="unexpected", gib_message_text=message_text, raw_response=raw_response,
        )
        logger.warning(
            "Turkey GIB: unexpected response shape (chat_id=%s, messages=%r)",
            chat_id, [(m.type, m.text) for m in outcome.messages],
        )
        return BotReply(text=texts.UNEXPECTED_ERROR_TEXT, show_main_menu=True)

    async def _translate_fines(
        self, fines: tuple[GibFineRecord, ...],
    ) -> tuple[GibFineRecord, ...]:
        """None translator (нет OPENAI_API_KEY, см. main.py), либо любой
        сбой перевода (см. FineTranslationError) - fail-open: возвращает
        fines БЕЗ ИЗМЕНЕНИЙ (турецкий текст остаётся, см.
        reader/turkey_bot/texts.py: `location_ru or location`) - см.
        задачу: "Translation must NEVER make a successful GIB check
        fail". Логируется только факт сбоя (тип исключения), НИКОГДА
        исходный/переведённый текст."""
        if self._translator is None:
            return fines
        try:
            return await self._translator.translate_fines(fines)
        except FineTranslationError:
            logger.warning("Turkey fine translation unavailable, showing original Turkish text")
            return fines

    async def _finish(
        self,
        chat_id: int,
        *,
        telegram_user_id: int,
        check: LiveGibCheck,
        status: str,
        gib_message_text: str | None,
        raw_response: str | None,
    ) -> None:
        """Общий эпилог для каждого терминального исхода (no_debt/has_debt/
        unexpected/error) - закрывает и убирает живую сессию, очищает
        персистентное состояние диалога, пишет запись в
        TurkeyCheckRepository (см. модуль docstring про то, что там
        хранится и почему это безопасно)."""
        await self._registry.pop_and_close(chat_id)
        self._states.clear(chat_id)
        self._checks.record_result(
            telegram_user_id=telegram_user_id,
            telegram_chat_id=chat_id,
            plate=check.plate,
            captcha_attempts=check.submit_attempts,
            status=status,
            gib_message_text=gib_message_text,
            raw_response=raw_response,
        )

    async def _open_new_check(
        self, chat_id: int, telegram_user_id: int, plate: str,
    ) -> CaptchaChallenge | None:
        """None - сбой транспорта при первом обращении к GIB (см.
        GibTransportError) - httpx.AsyncClient уже закрыт здесь же, вызывающий
        код ничего дополнительно закрывать не должен. Успех - живая
        проверка уже зарегистрирована в LiveGibSessionRegistry И
        persisted в bot_conversation_state - вызывающему коду остаётся
        только отправить challenge.image_png пользователю."""
        client, provider = self._check_factory()
        try:
            challenge = await provider.start()
        except GibTransportError:
            await client.aclose()
            logger.warning(
                "Turkey GIB: failed to start session/captcha (chat_id=%s)", chat_id,
            )
            return None

        check = LiveGibCheck(
            client=client, provider=provider, plate=plate, image_id=challenge.image_id,
        )
        await self._registry.put(chat_id, check)
        self._states.set(
            chat_id, telegram_user_id=telegram_user_id, step=_STEP_AWAITING_CODE,
            payload={"plate": plate, "image_id": challenge.image_id},
        )
        return challenge
