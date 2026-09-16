"""ConversationController Turkey-бота — линейный, одноразовый цикл
"номер -> CAPTCHA -> код -> результат" (см. design report Stage 3), ТЕПЕРЬ
с ДВУМЯ независимыми провайдерами (см. design report Stage 2B):
  - GIB (🚔 Проверить штрафы) — штрафы/задолженность, dijital.gib.gov.tr;
  - Avrasya Tüneli (🛣 Проверить платные дороги) — неоплаченные проезды,
    avrasyatuneli.com (см. reader/turkey_bot/avrasya/*).
Оба провайдера используют ОДИН И ТОТ ЖЕ линейный конечный автомат (номер
-> CAPTCHA -> код -> результат) и ОДНУ И ТУ ЖЕ CAPTCHA-обязательность (см.
задачу: "CAPTCHA remains mandatory... never bypassed/automated" — ни для
одного провайдера) — конкретный провайдер выбирается ДО первого запроса
(либо явной кнопкой меню, либо гаражом) и хранится в payload персистентного
состояния (см. _STEP_AWAITING_PLATE/_STEP_AWAITING_CODE ниже), а не
угадывается по содержимому сообщения.

НИЧЕГО не знает про Telethon (та же граница, что и
reader/public_bot/conversation.py) — reader/turkey_bot/handlers.py
конвертирует BotReply в реальные Telegram-вызовы (в частности,
photo_png -> отправка фото, чего у Георгии нет вовсе).

Ключевой архитектурный принцип (см.
reader/turkey_bot/live_session_registry.py и design report Stage 3):
ЖИВАЯ сессия провайдера (httpx.AsyncClient/cookies) существует ТОЛЬКО в
памяти этого процесса, никогда в БД — ЭТО ВЕРНО ДЛЯ ОБОИХ провайдеров (см.
reader/turkey_bot/avrasya/live_session_registry.py — отдельный, но
структурно идентичный реестр для Avrasya, см. design report Stage 2B: "do
not force Avrasya into GIB-specific abstractions... never serialize a
live HTTP session into SQLite"). bot_conversation_state хранит только
"какой номер проверяли, у какого провайдера и какой image_id был
последним показан" (image_id — ТОЛЬКО для GIB, у Avrasya его нет вовсе,
см. avrasya/models.py) — этого достаточно, чтобы после рестарта/протухания
сессии молча запросить НОВУЮ CAPTCHA для того же номера у ТОГО ЖЕ
провайдера, но НЕДОСТАТОЧНО и не может быть достаточно для восстановления
самой сессии.

Единый per-chat lock (см. design report Stage 2B: "Use one common
per-chat lock across GİB and Avrasya so the two flows cannot race") —
LiveGibSessionRegistry.lock_for() (сам по себе провайдер-агностичный
метод, не трогающий _checks/GibProvider) переиспользуется КАК ЕСТЬ и для
Avrasya-веток — второй, отдельный lock здесь НЕ заводится (это создало бы
ровно ту гонку, которую единый lock должен предотвращать)."""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx

from reader.turkey_bot import texts
from reader.turkey_bot.avrasya.live_session_registry import (
    LiveAvrasyaCheck,
    LiveAvrasyaSessionRegistry,
)
from reader.turkey_bot.avrasya.models import (
    AvrasyaCaptchaChallenge,
    AvrasyaSubmitOutcome,
)
from reader.turkey_bot.avrasya.provider import AvrasyaProvider
from reader.turkey_bot.avrasya.session import (
    AvrasyaRateLimitedError,
    AvrasyaSession,
    AvrasyaTransportError,
)
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
from reader.turkey_bot.toll_check_repository import TurkeyTollCheckRepository
from reader.turkey_bot.user_cars_repository import TurkeyUserCarsRepository
from reader.turkey_bot.validation import normalize_plate

logger = logging.getLogger(__name__)

# Модульный singleton (см. B008) - default для ConversationController(tz=...),
# когда вызывающий код (тесты, не рассчитанные на статистику) не передаёт
# tz вовсе; main.py всегда передаёт реальный settings.fine_monitor.timezone.
_DEFAULT_TZ = ZoneInfo("UTC")

_STEP_AWAITING_CODE = "awaiting_captcha_code"
# Новый шаг (см. design report Stage 2B) — между нажатием CHECK_FINES_LABEL/
# CHECK_TOLLS_LABEL в главном меню и вводом самого номера: payload несёт
# ТОЛЬКО {"provider": "gib"|"avrasya"} - какой провайдер обслужит
# СЛЕДУЮЩИЙ введённый текст как номер. Голый ввод номера без этого шага
# (см. handle_text) по-прежнему трактуется как GIB напрямую — regression-
# safe (см. design report: "bare plate still defaults to GİB").
_STEP_AWAITING_PLATE = "awaiting_plate"

_PROVIDER_GIB = "gib"
_PROVIDER_AVRASYA = "avrasya"

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
    "гараж пуст" — пустой tuple — от "это вообще не гараж").

    cta_buttons — коммерческие CTA-кнопки (label, url), показываемые
    ТОЛЬКО после подтверждённого Avrasya has_debt (см. design report:
    "append these CTAs only after a confirmed has_debt result") — ТА ЖЕ
    форма (label, url) и ТЕ ЖЕ реальные значения (метки/URL), что и у
    reader/public_bot/conversation.py::BotReply.cta_buttons/
    ConversationController._owner_cta_buttons и reader/public_bot/
    keyboards.py::owner_fine_cta_buttons — воспроизведены здесь локально
    (см. _avrasya_debt_cta_buttons ниже), а НЕ импортированы оттуда: Turkey
    остаётся полностью отдельным процессом, не зависящим от
    reader/public_bot/* (см. reader/turkey_bot/main.py про эту границу).
    conversation.py намеренно НЕ импортирует Telethon Button здесь (тот же
    принцип, что и у Георгии) — только handlers.py конвертирует эти пары
    в реальные Button.url(...)."""

    text: str
    photo_png: bytes | None = None
    show_cancel_button: bool = False
    extra_texts: tuple[str, ...] = ()
    show_main_menu: bool = False
    garage_cars: tuple[TurkeyUserCar, ...] | None = None
    cta_buttons: tuple[tuple[str, str], ...] | None = None


class _AsyncCloseable(Protocol):
    """Ровно то, что LiveGibSessionRegistry/LiveAvrasyaSessionRegistry
    требуют от .client (см. reader/turkey_bot/live_session_registry.py::
    pop_and_close) — тот же Protocol-приём, что и FineTranslatorLike в
    reader/fines/check_service.py, чтобы тесты могли подменить
    httpx.AsyncClient лёгким фейком без реальной сети."""

    async def aclose(self) -> None: ...


CheckFactory = Callable[[], tuple[_AsyncCloseable, GibProvider]]
AvrasyaCheckFactory = Callable[[], tuple[_AsyncCloseable, AvrasyaProvider]]


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


def _default_avrasya_check_factory() -> tuple[httpx.AsyncClient, AvrasyaProvider]:
    """Реальная Avrasya-сессия (см. design report Stage 2B) — тот же
    общий _build_client() (одинаковый User-Agent/timeout), что и у GIB —
    не отдельная настройка без причины."""
    client = _build_client()
    return client, AvrasyaProvider(AvrasyaSession(client))


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


def _sanitize_avrasya_outcome_for_storage(outcome: AvrasyaSubmitOutcome) -> str:
    """Аналог _sanitize_outcome_for_storage() для Avrasya (см. design
    report Stage 2B: "Store sanitized response server-side") — идёт
    ТОЛЬКО в TurkeyTollCheckRepository, никогда пользователю (см.
    conversation.py::_handle_avrasya_submit_outcome и texts.py::
    AVRASYA_UNEXPECTED_TEXT). Никогда не содержит captcha_code/cookies —
    AvrasyaSubmitOutcome их не несёт (см. avrasya/models.py)."""
    return json.dumps(
        {
            "kind": outcome.kind,
            "status_code": outcome.status_code,
            "messages": [
                {"property_name": m.property_name, "error_message": m.error_message}
                for m in outcome.messages
            ],
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
        avrasya_session_registry: LiveAvrasyaSessionRegistry,
        toll_check_repository: TurkeyTollCheckRepository,
        *,
        check_factory: CheckFactory = _default_check_factory,
        avrasya_check_factory: AvrasyaCheckFactory = _default_avrasya_check_factory,
        translator: FineTranslatorLike | None = None,
        trusted_operator_user_ids: frozenset[int] = frozenset(),
        tz: ZoneInfo = _DEFAULT_TZ,
        payment_help_contact_username: str = "tplgee",
    ):
        self._states = conversation_state_repository
        self._checks = check_repository
        self._registry = session_registry
        self._garage = garage_repository
        self._statistics = statistics_service
        self._avrasya_registry = avrasya_session_registry
        self._toll_checks = toll_check_repository
        self._check_factory = check_factory
        self._avrasya_check_factory = avrasya_check_factory
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
        # Destination Avrasya has_debt CTA-кнопок (см. BotReply.cta_buttons
        # докстрок) — ТОТ ЖЕ config (settings.public_bot.
        # payment_help_contact_username), что и у @ProtocolGEbot/
        # reader/public_bot/conversation.py::_owner_cta_buttons — не
        # hardcoded, не вводит новую настройку (default "tplgee" совпадает
        # с default'ом там же, см. reader/public_bot/conversation.py).
        self._payment_help_contact_username = payment_help_contact_username

    def _avrasya_debt_cta_buttons(self) -> tuple[tuple[str, str], ...]:
        """ТЕ ЖЕ метки и та же destination, что и у Георгии (см.
        BotReply.cta_buttons докстрок) — воспроизведены буквально, не
        придуманы заново."""
        url = f"https://t.me/{self._payment_help_contact_username}"
        return (("💳 Оплатить в рублях", url), ("🚗 ОСАГО Грузии", url))

    def _is_trusted(self, telegram_user_id: int) -> bool:
        """Единственная проверка авторизации trusted-режима — ТОЛЬКО по
        numeric telegram_user_id (тот же принцип, что и
        reader/public_bot/conversation.py::_is_trusted), никогда по
        username. Trusted даёт доступ ТОЛЬКО к статистике (см. design
        report: "Trusted status grants access to statistics only — it
        does not grant managers access to another user's Turkey garage or
        checks") — гараж (для ОБОИХ провайдеров) всегда фильтруется по
        РЕАЛЬНОМУ telegram_user_id вызывающего, независимо от
        trusted-статуса."""
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
                await self._avrasya_registry.pop_and_close(chat_id)
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

        if stripped == texts.CHECK_FINES_LABEL:
            return await self._handle_check_button(
                chat_id=chat_id, telegram_user_id=telegram_user_id, provider=_PROVIDER_GIB,
            )

        if stripped == texts.CHECK_TOLLS_LABEL:
            return await self._handle_check_button(
                chat_id=chat_id, telegram_user_id=telegram_user_id, provider=_PROVIDER_AVRASYA,
            )

        state = self._states.get(chat_id)

        if state is not None and state.step == _STEP_AWAITING_PLATE:
            provider = (state.payload or {}).get("provider", _PROVIDER_GIB)
            return await self._handle_plate_for_provider(
                stripped, chat_id=chat_id, telegram_user_id=telegram_user_id, provider=provider,
            )

        if state is not None and state.step == _STEP_AWAITING_CODE:
            provider = (state.payload or {}).get("provider", _PROVIDER_GIB)
            if provider == _PROVIDER_AVRASYA:
                return await self._handle_avrasya_captcha_code(
                    stripped, chat_id=chat_id, telegram_user_id=telegram_user_id, state=state,
                )
            return await self._handle_captcha_code(
                stripped, chat_id=chat_id, telegram_user_id=telegram_user_id, state=state,
            )

        return await self._handle_new_plate(stripped, chat_id=chat_id, telegram_user_id=telegram_user_id)

    async def handle_cancel(self, *, chat_id: int) -> BotReply:
        """Общий путь и для текстовой команды /cancel, и для inline
        "❌ Отмена" (см. reader/turkey_bot/handlers.py) — оба ведут сюда.
        Отменяет ЛЮБУЮ живую проверку — GIB и/или Avrasya (в норме активна
        не больше одной сразу, см. design report: единый lock, но
        pop_and_close на обеих — дешёвая защита от рассинхронизации)."""
        async with self._registry.lock_for(chat_id):
            had_live_check = (
                await self._registry.get(chat_id) is not None
                or await self._avrasya_registry.get(chat_id) is not None
            )
            had_state = self._states.get(chat_id) is not None
            if not had_live_check and not had_state:
                return BotReply(text=texts.NOTHING_TO_CANCEL_TEXT, show_main_menu=True)

            await self._registry.pop_and_close(chat_id)
            await self._avrasya_registry.pop_and_close(chat_id)
            self._states.clear(chat_id)
            return BotReply(text=texts.CANCEL_CONFIRM_TEXT, show_main_menu=True)

    async def _handle_garage(self, *, chat_id: int, telegram_user_id: int) -> BotReply:
        """🚗 Мои авто — доступно ВСЕМ (см. design report), в отличие от
        статистики. Список ВСЕГДА фильтруется по РЕАЛЬНОМУ telegram_user_id
        вызывающего (см. TurkeyUserCarsRepository.list_cars) — нет
        отдельного "trusted"-режима просмотра чужого гаража (см. design
        report: "Trusted status grants access to statistics only"). ОДИН
        и тот же гараж для ОБОИХ провайдеров (см. design report Stage 2B:
        "Reuse the existing turkey_bot_user_cars; no separate Avrasya
        garage")."""
        async with self._registry.lock_for(chat_id):
            # Открытие "🚗 Мои авто" - явная навигация, как и /cancel -
            # прошлый незавершённый диалог (если был) отбрасывается (тот
            # же принцип, что и у reader/public_bot/conversation.py про
            # пункты меню).
            await self._registry.pop_and_close(chat_id)
            await self._avrasya_registry.pop_and_close(chat_id)
            self._states.clear(chat_id)

        cars = tuple(self._garage.list_cars(telegram_user_id))
        if not cars:
            return BotReply(text=texts.EMPTY_GARAGE_TEXT, show_main_menu=True)
        return BotReply(text=texts.GARAGE_HEADER, garage_cars=cars)

    async def _handle_statistics(self, *, chat_id: int) -> BotReply:
        """Вызывающий код (handle_text) уже проверил is_trusted() —
        см. design report: единственная проверка авторизации для
        статистики, здесь не повторяется. Avrasya НЕ включена в
        статистику (см. design report Stage 2B: "Do not add Avrasya to
        Статистика yet") — TurkeyStatisticsService не тронут в этой
        задаче."""
        async with self._registry.lock_for(chat_id):
            await self._registry.pop_and_close(chat_id)
            await self._avrasya_registry.pop_and_close(chat_id)
            self._states.clear(chat_id)

        stats = self._statistics.get_statistics(now=datetime.now(timezone.utc), tz=self._tz)
        user_messages = texts.format_user_list_messages(self._statistics.list_known_users())
        return BotReply(
            text=texts.format_statistics(stats),
            extra_texts=tuple(user_messages),
            show_main_menu=True,
        )

    async def _handle_check_button(
        self, *, chat_id: int, telegram_user_id: int, provider: str,
    ) -> BotReply:
        """Нажатие CHECK_FINES_LABEL/CHECK_TOLLS_LABEL в главном меню —
        armит provider для СЛЕДУЮЩЕГО введённого номера (см.
        _STEP_AWAITING_PLATE) и, как и остальные пункты меню, отбрасывает
        любой прошлый незавершённый диалог."""
        async with self._registry.lock_for(chat_id):
            await self._registry.pop_and_close(chat_id)
            await self._avrasya_registry.pop_and_close(chat_id)
            self._states.set(
                chat_id, telegram_user_id=telegram_user_id, step=_STEP_AWAITING_PLATE,
                payload={"provider": provider},
            )
        text = (
            texts.ASK_PLATE_FOR_TOLLS_TEXT
            if provider == _PROVIDER_AVRASYA
            else texts.ASK_PLATE_FOR_FINES_TEXT
        )
        return BotReply(text=text, show_main_menu=True)

    async def handle_garage_check(
        self, car_id: int, *, chat_id: int, telegram_user_id: int, provider: str,
    ) -> BotReply | None:
        """None — car_id не существует ИЛИ принадлежит другому
        пользователю (см. TurkeyUserCarsRepository.get_owned_car) —
        reader/turkey_bot/handlers.py должен показать общий
        "неизвестная кнопка" alert и ничего не начинать (тот же принцип,
        что и у reader/public_bot про чужой subscription_id, см. design
        report: "A normal user must not be able to inspect another
        user's garage by forging callback data"). provider выбирает,
        какую из двух проверок начать для НАЙДЕННОГО и ПОДТВЕРЖДЁННОГО
        своего автомобиля — сам по себе ничего не авторизует (см.
        keyboards.py модуль docstring)."""
        car = self._garage.get_owned_car(car_id, telegram_user_id=telegram_user_id)
        if car is None:
            return None
        return await self._start_check_for_plate(
            car.car_number, chat_id=chat_id, telegram_user_id=telegram_user_id, provider=provider,
        )

    async def _handle_new_plate(
        self, raw_plate: str, *, chat_id: int, telegram_user_id: int,
    ) -> BotReply:
        """Голый ввод номера БЕЗ предварительного нажатия CHECK_FINES_LABEL/
        CHECK_TOLLS_LABEL — всегда GIB (см. design report: "bare plate
        still defaults to GİB" — исходное, ещё Stage 3, поведение бота, не
        новое)."""
        return await self._handle_plate_for_provider(
            raw_plate, chat_id=chat_id, telegram_user_id=telegram_user_id, provider=_PROVIDER_GIB,
        )

    async def _handle_plate_for_provider(
        self, raw_plate: str, *, chat_id: int, telegram_user_id: int, provider: str,
    ) -> BotReply:
        plate = normalize_plate(raw_plate)
        if plate is None:
            return BotReply(text=texts.INVALID_PLATE_TEXT, show_main_menu=True)

        return await self._start_check_for_plate(
            plate, chat_id=chat_id, telegram_user_id=telegram_user_id, provider=provider,
        )

    async def _start_check_for_plate(
        self, plate: str, *, chat_id: int, telegram_user_id: int, provider: str,
    ) -> BotReply:
        """Общее ядро для "ввёл номер вручную"/"нажал кнопку меню" (см.
        _handle_new_plate/_handle_plate_for_provider) И "нажал машину в
        гараже" (см. handle_garage_check) — CAPTCHA обязательна во ВСЕХ
        случаях одинаково, для ОБОИХ провайдеров (см. design report:
        "CAPTCHA remains mandatory. Do not bypass or automate it"),
        пользователю никогда не нужно вводить номер повторно, потому что
        plate сюда приходит уже готовым (либо из ввода, либо из
        TurkeyUserCarsRepository), а не запрашивается заново."""
        async with self._registry.lock_for(chat_id):
            # Этот путь достигается ТОЛЬКО из состояния IDLE/awaiting_plate
            # (см. handle_text — пока step == awaiting_captcha_code, ЛЮБОЙ
            # текст трактуется как код, а не как новый номер: коды CAPTCHA
            # и номера визуально слишком похожи, чтобы угадывать намерение
            # пользователя, см. design report Stage 3 — новый номер вместо
            # кода НЕ поддерживается, вместо этого явный /cancel). Тем не
            # менее pop_and_close() здесь — дешёвая защита на случай, если
            # registry и persisted-состояние когда-либо разойдутся
            # (например, после ошибки в предыдущем цикле) — обычно no-op.
            await self._registry.pop_and_close(chat_id)
            await self._avrasya_registry.pop_and_close(chat_id)
            self._states.clear(chat_id)

            if provider == _PROVIDER_AVRASYA:
                try:
                    challenge = await self._open_new_avrasya_check(chat_id, telegram_user_id, plate)
                except AvrasyaRateLimitedError:
                    logger.warning(
                        "Turkey Avrasya: rate limited while starting session (chat_id=%s)", chat_id,
                    )
                    return BotReply(text=texts.AVRASYA_RATE_LIMITED_TEXT, show_main_menu=True)
                except AvrasyaTransportError:
                    logger.warning(
                        "Turkey Avrasya: failed to start session/captcha (chat_id=%s)", chat_id,
                    )
                    return BotReply(text=texts.AVRASYA_CAPTCHA_FETCH_FAILED_TEXT, show_main_menu=True)
            else:
                challenge = await self._open_new_check(chat_id, telegram_user_id, plate)
                if challenge is None:
                    return BotReply(text=texts.CAPTCHA_FETCH_FAILED_TEXT, show_main_menu=True)

            return BotReply(
                text=texts.ASK_CAPTCHA_TEXT, photo_png=challenge.image_png, show_cancel_button=True,
            )

    # ---- GIB (см. reader/turkey_bot/gib/*) — НЕИЗМЕНЁННАЯ логика Stage 3/4 ----

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
                payload={"plate": check.plate, "image_id": challenge.image_id, "provider": _PROVIDER_GIB},
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
            # turkey_bot_user_cars (ОБЩИЙ гараж и для Avrasya, см.
            # _handle_avrasya_submit_outcome).
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
            payload={"plate": plate, "image_id": challenge.image_id, "provider": _PROVIDER_GIB},
        )
        return challenge

    # ---- Avrasya Tüneli (см. reader/turkey_bot/avrasya/*) — Stage 2B ----

    async def _handle_avrasya_captcha_code(
        self, code: str, *, chat_id: int, telegram_user_id: int, state: ConversationState,
    ) -> BotReply:
        """Структурная копия _handle_captcha_code() (см. выше) для
        Avrasya — та же "restart recovery"/rejected-retry семантика, но
        через LiveAvrasyaSessionRegistry/AvrasyaProvider и без image_id
        (см. avrasya/models.py — Avrasya его не несёт)."""
        async with self._registry.lock_for(chat_id):
            check = await self._avrasya_registry.get(chat_id)

            if check is None:
                # Тот же принцип, что и у GIB-ветки: рестарт/idle-TTL/
                # устаревшая сессия — не различаются, молча запрашиваем
                # новую CAPTCHA для уже сохранённого номера (см. design
                # report: "Missing/expired live Avrasya session → create a
                # fresh session/CAPTCHA for the persisted plate").
                plate = (state.payload or {}).get("plate")
                if not plate:
                    self._states.clear(chat_id)
                    return BotReply(text=texts.SESSION_LOST_TEXT, show_main_menu=True)

                try:
                    challenge = await self._open_new_avrasya_check(chat_id, telegram_user_id, plate)
                except AvrasyaRateLimitedError:
                    self._states.clear(chat_id)
                    logger.warning(
                        "Turkey Avrasya: rate limited during restart recovery (chat_id=%s)", chat_id,
                    )
                    return BotReply(text=texts.AVRASYA_RATE_LIMITED_TEXT, show_main_menu=True)
                except AvrasyaTransportError:
                    self._states.clear(chat_id)
                    logger.warning(
                        "Turkey Avrasya: failed to start session/captcha (chat_id=%s)", chat_id,
                    )
                    return BotReply(text=texts.AVRASYA_CAPTCHA_FETCH_FAILED_TEXT, show_main_menu=True)

                return BotReply(
                    text=texts.SESSION_EXPIRED_RETRY_TEXT,
                    photo_png=challenge.image_png,
                    show_cancel_button=True,
                )

            check.submit_attempts += 1
            try:
                outcome = await check.provider.submit(plate=check.plate, captcha_code=code)
            except AvrasyaRateLimitedError:
                await self._finish_avrasya(
                    chat_id, telegram_user_id=telegram_user_id, check=check,
                    status="error", raw_response=None,
                )
                logger.warning("Turkey Avrasya submit: rate limited (chat_id=%s)", chat_id)
                return BotReply(text=texts.AVRASYA_RATE_LIMITED_TEXT, show_main_menu=True)
            except AvrasyaTransportError:
                await self._finish_avrasya(
                    chat_id, telegram_user_id=telegram_user_id, check=check,
                    status="error", raw_response=None,
                )
                logger.warning("Turkey Avrasya submit: transport error (chat_id=%s)", chat_id)
                return BotReply(text=texts.AVRASYA_TRANSPORT_ERROR_TEXT, show_main_menu=True)

            return await self._handle_avrasya_submit_outcome(
                outcome, chat_id=chat_id, telegram_user_id=telegram_user_id, check=check,
            )

    async def _handle_avrasya_submit_outcome(
        self, outcome: AvrasyaSubmitOutcome, *, chat_id: int, telegram_user_id: int,
        check: LiveAvrasyaCheck,
    ) -> BotReply:
        if outcome.kind == "rejected":
            # См. design report Stage 2B: "Wrong Avrasya CAPTCHA → obtain
            # a fresh CAPTCHA and remain in the Avrasya flow" — та же
            # семантика, что и у GIB rejected (см. _handle_submit_outcome).
            try:
                challenge = await check.provider.refresh_captcha()
            except AvrasyaRateLimitedError:
                await self._finish_avrasya(
                    chat_id, telegram_user_id=telegram_user_id, check=check,
                    status="error", raw_response=None,
                )
                logger.warning(
                    "Turkey Avrasya refresh_captcha: rate limited (chat_id=%s)", chat_id,
                )
                return BotReply(text=texts.AVRASYA_RATE_LIMITED_TEXT, show_main_menu=True)
            except AvrasyaTransportError:
                await self._finish_avrasya(
                    chat_id, telegram_user_id=telegram_user_id, check=check,
                    status="error", raw_response=None,
                )
                logger.warning(
                    "Turkey Avrasya refresh_captcha: transport error (chat_id=%s)", chat_id,
                )
                return BotReply(text=texts.AVRASYA_TRANSPORT_ERROR_TEXT, show_main_menu=True)

            self._states.set(
                chat_id, telegram_user_id=telegram_user_id, step=_STEP_AWAITING_CODE,
                payload={"plate": check.plate, "provider": _PROVIDER_AVRASYA},
            )
            return BotReply(
                text=texts.CAPTCHA_REJECTED_RETRY_TEXT, photo_png=challenge.image_png,
                show_cancel_button=True,
            )

        raw_response = _sanitize_avrasya_outcome_for_storage(outcome)

        if outcome.kind in ("no_debt", "has_debt"):
            # ОБЩИЙ гараж (см. reader/turkey_bot/user_cars_repository.py) —
            # ТОЛЬКО для реально завершённых no_debt/has_debt, ТОЧНО ТАК
            # ЖЕ, как и у GIB (см. _handle_submit_outcome выше) — ни
            # rejected/unexpected/error сюда не попадают (см. design
            # report: "successful Avrasya check adding/reusing the same
            # saved vehicle").
            self._garage.record_successful_check(
                telegram_user_id=telegram_user_id, car_number=check.plate,
            )

        if outcome.kind == "no_debt":
            await self._finish_avrasya(
                chat_id, telegram_user_id=telegram_user_id, check=check,
                status="no_debt", raw_response=raw_response,
            )
            return BotReply(text=texts.avrasya_no_debt_text(check.plate), show_main_menu=True)

        if outcome.kind == "has_debt":
            # См. reader/turkey_bot/texts.py::format_avrasya_has_debt_message
            # (design report Stage 2C) — строит текст ТОЛЬКО из уже
            # типизированных AvrasyaDebtItem (см. avrasya/models.py),
            # conversation.py здесь НЕ интерпретирует raw JSON вообще (тот
            # же принцип, что и у GIB has_debt выше). Полный сырой ответ
            # по-прежнему уходит ТОЛЬКО в TurkeyTollCheckRepository
            # (raw_response выше), никогда в текст пользователю.
            await self._finish_avrasya(
                chat_id, telegram_user_id=telegram_user_id, check=check,
                status="has_debt", raw_response=raw_response,
            )
            return BotReply(
                text=texts.format_avrasya_has_debt_message(check.plate, outcome.debt_items),
                show_main_menu=True,
                # См. design report: CTA-кнопки ТОЛЬКО после подтверждённого
                # has_debt — никогда для no_debt/rejected/unexpected/error
                # (см. BotReply.cta_buttons докстрок).
                cta_buttons=self._avrasya_debt_cta_buttons(),
            )

        # "unexpected" — НЕ трогает гараж (см. design report: "unexpected
        # response does not add car").
        await self._finish_avrasya(
            chat_id, telegram_user_id=telegram_user_id, check=check,
            status="unexpected", raw_response=raw_response,
        )
        logger.warning(
            "Turkey Avrasya: unexpected response shape (chat_id=%s, status_code=%s)",
            chat_id, outcome.status_code,
        )
        return BotReply(text=texts.AVRASYA_UNEXPECTED_TEXT, show_main_menu=True)

    async def _finish_avrasya(
        self,
        chat_id: int,
        *,
        telegram_user_id: int,
        check: LiveAvrasyaCheck,
        status: str,
        raw_response: str | None,
    ) -> None:
        """Аналог _finish() (см. выше) для Avrasya — закрывает и убирает
        живую Avrasya-сессию, очищает персистентное состояние диалога,
        пишет запись в TurkeyTollCheckRepository (ОТДЕЛЬНАЯ таблица, см.
        reader/turkey_bot/toll_check_repository.py — turkey_fine_checks
        не трогается)."""
        await self._avrasya_registry.pop_and_close(chat_id)
        self._states.clear(chat_id)
        self._toll_checks.record_result(
            telegram_user_id=telegram_user_id,
            telegram_chat_id=chat_id,
            provider=_PROVIDER_AVRASYA,
            plate=check.plate,
            captcha_attempts=check.submit_attempts,
            status=status,
            message_text=None,
            raw_response=raw_response,
        )

    async def _open_new_avrasya_check(
        self, chat_id: int, telegram_user_id: int, plate: str,
    ) -> AvrasyaCaptchaChallenge:
        """Аналог _open_new_check() (см. выше) для Avrasya — В ОТЛИЧИЕ от
        него, НЕ проглатывает AvrasyaRateLimitedError/AvrasyaTransportError
        сама, а закрывает client и поднимает исключение дальше (см.
        session.py: AvrasyaRateLimitedError — подкласс
        AvrasyaTransportError) — вызывающий код (см. _start_check_for_plate/
        _handle_avrasya_captcha_code) различает rate-limit от обычного
        сбоя транспорта и показывает разный текст пользователю (см. design
        report: у GIB такого различия нет, поэтому _open_new_check его и
        не делает — не унифицируется искусственно)."""
        client, provider = self._avrasya_check_factory()
        try:
            challenge = await provider.start()
        except AvrasyaTransportError:
            await client.aclose()
            raise

        check = LiveAvrasyaCheck(client=client, provider=provider, plate=plate)
        await self._avrasya_registry.put(chat_id, check)
        self._states.set(
            chat_id, telegram_user_id=telegram_user_id, step=_STEP_AWAITING_CODE,
            payload={"plate": plate, "provider": _PROVIDER_AVRASYA},
        )
        return challenge
