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
from typing import Protocol

import httpx

from reader.turkey_bot import texts
from reader.turkey_bot.check_repository import TurkeyCheckRepository
from reader.turkey_bot.conversation_state_repository import (
    TurkeyConversationStateRepository,
)
from reader.turkey_bot.gib.models import CaptchaChallenge, GibSubmitOutcome
from reader.turkey_bot.gib.provider import GibProvider
from reader.turkey_bot.gib.session import GibSession, GibTransportError
from reader.turkey_bot.live_session_registry import LiveGibCheck, LiveGibSessionRegistry
from reader.turkey_bot.models import ConversationState
from reader.turkey_bot.validation import normalize_plate

logger = logging.getLogger(__name__)

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
    связь."""

    text: str
    photo_png: bytes | None = None
    show_cancel_button: bool = False


class _AsyncCloseable(Protocol):
    """Ровно то, что LiveGibSessionRegistry требует от .client (см.
    reader/turkey_bot/live_session_registry.py::pop_and_close) — тот же
    Protocol-приём, что и FineTranslatorLike в reader/fines/check_service.py,
    чтобы тесты могли подменить httpx.AsyncClient лёгким фейком без
    реальной сети."""

    async def aclose(self) -> None: ...


CheckFactory = Callable[[], tuple[_AsyncCloseable, GibProvider]]


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
        *,
        check_factory: CheckFactory = _default_check_factory,
    ):
        self._states = conversation_state_repository
        self._checks = check_repository
        self._registry = session_registry
        self._check_factory = check_factory

    async def handle_text(self, text: str, *, chat_id: int, telegram_user_id: int) -> BotReply:
        stripped = text.strip()
        lowered = stripped.lower()

        if lowered == "/start":
            async with self._registry.lock_for(chat_id):
                await self._registry.pop_and_close(chat_id)
                self._states.clear(chat_id)
            return BotReply(text=texts.WELCOME_TEXT)

        if lowered == "/cancel":
            return await self.handle_cancel(chat_id=chat_id)

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
                return BotReply(text=texts.NOTHING_TO_CANCEL_TEXT)

            await self._registry.pop_and_close(chat_id)
            self._states.clear(chat_id)
            return BotReply(text=texts.CANCEL_CONFIRM_TEXT)

    async def _handle_new_plate(
        self, raw_plate: str, *, chat_id: int, telegram_user_id: int,
    ) -> BotReply:
        plate = normalize_plate(raw_plate)
        if plate is None:
            return BotReply(text=texts.INVALID_PLATE_TEXT)

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
                return BotReply(text=texts.CAPTCHA_FETCH_FAILED_TEXT)

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
                    return BotReply(text=texts.SESSION_LOST_TEXT)

                challenge = await self._open_new_check(chat_id, telegram_user_id, plate)
                if challenge is None:
                    self._states.clear(chat_id)
                    return BotReply(text=texts.CAPTCHA_FETCH_FAILED_TEXT)

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
                return BotReply(text=texts.TRANSPORT_ERROR_TEXT)

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
                return BotReply(text=texts.TRANSPORT_ERROR_TEXT)

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
            await self._finish(
                chat_id, telegram_user_id=telegram_user_id, check=check,
                status="no_debt", gib_message_text=message_text, raw_response=raw_response,
            )
            return BotReply(text=texts.no_debt_text(check.plate))

        if outcome.kind == "has_debt":
            await self._finish(
                chat_id, telegram_user_id=telegram_user_id, check=check,
                status="has_debt", gib_message_text=message_text, raw_response=raw_response,
            )
            # См. reader/turkey_bot/texts.py::has_debt_text - НИКОГДА не
            # показывает raw_data пользователю (схема неизвестна, см.
            # design report Stage 2) - только безопасная заглушка.
            return BotReply(text=texts.has_debt_text(check.plate))

        # "unexpected"
        await self._finish(
            chat_id, telegram_user_id=telegram_user_id, check=check,
            status="unexpected", gib_message_text=message_text, raw_response=raw_response,
        )
        logger.warning(
            "Turkey GIB: unexpected response shape (chat_id=%s, messages=%r)",
            chat_id, [(m.type, m.text) for m in outcome.messages],
        )
        return BotReply(text=texts.UNEXPECTED_ERROR_TEXT)

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
