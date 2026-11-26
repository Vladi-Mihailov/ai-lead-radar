"""Транспорт MAX-бота: webhook (production) или long polling (только
локальная разработка — MAX ограничивает его скорость и срок хранения
событий), общий диспетчер и отправка ответов.

Webhook: aiohttp на 127.0.0.1, снаружи — reverse proxy с TLS на 443 (MAX
доставляет только на https://, порт 443, сертификат доверенного УЦ, один
endpoint на бота). Каждый POST сверяет заголовок X-Max-Bot-Api-Secret и
отвечает 200 сразу после постановки апдейта в очередь — медленные проверки
(police.ge, GİB/KGM, Georgian Card) идут в воркерах, иначе MAX счёл бы
доставку неудачной (лимит 30 секунд) и прислал бы апдейт повторно.

Повторная доставка: ключ апдейта (updates.MaxEvent.dedup_key) фиксируется в
max_processed_updates ДО обработки — повторный POST, повтор после рестарта и
двойной polling не обрабатываются дважды. Цена — at-most-once: если процесс
упадёт посреди обработки, нажатие придётся повторить; дубль добавления
машины или второй сессии Georgian Card хуже.

Отвечает бот ТОЛЬКО в личном диалоге: сообщения из групп и каналов
игнорируются (личные сведения о штрафах в общие чаты не уходят).
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from aiohttp import web

from reader.max_bot import keyboards
from reader.max_bot.api import MaxApi, MaxApiError
from reader.max_bot.formatting import plain_to_html, split_text, telethon_md_to_html
from reader.max_bot.storage import MaxBotStorage
from reader.max_bot.updates import SUPPORTED_UPDATE_TYPES, MaxEvent, parse_update

logger = logging.getLogger(__name__)

SECRET_HEADER = "X-Max-Bot-Api-Secret"
_MAX_WEBHOOK_BODY = 256 * 1024
_QUEUE_SIZE = 1000
_WORKERS = 4
_POLL_ERROR_PAUSE_SECONDS = 5.0
_PURGE_INTERVAL_SECONDS = 6 * 3600
ERROR_TEXT = "⚠️ Не удалось выполнить действие. Попробуйте ещё раз чуть позже."
BUSY_TEXT = "⏳ Предыдущее действие ещё выполняется — дождитесь результата."


@dataclass(frozen=True)
class Reply:
    """text — в разметке общих Telegram-текстов (Telethon md), если
    markdown=True; иначе обычный текст. extra_texts — следующие сообщения
    (длинные списки), клавиатура — под последним."""

    text: str
    keyboard: keyboards.Keyboard | None = None
    extra_texts: tuple[str, ...] = field(default_factory=tuple)
    markdown: bool = True


def _render(reply: Reply) -> list[str]:
    convert = telethon_md_to_html if reply.markdown else plain_to_html
    chunks: list[str] = []
    for text in (reply.text, *reply.extra_texts):
        chunks.extend(split_text(convert(text)))
    return chunks


class MaxResponder:
    """error_keyboard — что показать вместо «⏳», если медленное действие
    упало (обычно кнопка главного меню бота)."""

    def __init__(self, api: MaxApi, *, error_keyboard: keyboards.Keyboard | None = None):
        self._api = api
        self._error_keyboard = error_keyboard

    async def send(self, user_id: int, reply: Reply, *, notify: bool = True) -> str | None:
        """Новое сообщение (одно или несколько) в диалог с user_id. mid
        последнего — для последующей правки."""
        chunks = _render(reply)
        mid = None
        for index, chunk in enumerate(chunks):
            last = index == len(chunks) - 1
            mid = await self._api.send_message(
                user_id=user_id, text=chunk,
                attachments=keyboards.attachments(reply.keyboard) if last else None, notify=notify,
            )
        return mid

    async def answer(self, event: MaxEvent, reply: Reply) -> None:
        """Ответ на событие: нажатие кнопки правит сообщение с этой кнопкой
        (как edit-in-place у Telegram-ботов), текст — новым сообщением."""
        if event.kind == "callback" and event.callback_id:
            chunks = _render(reply)
            first_keyboard = reply.keyboard if len(chunks) == 1 else None
            await self._api.answer_callback(
                event.callback_id,
                message={"text": chunks[0], "format": "html", "attachments": keyboards.attachments(first_keyboard)},
                pace_key=f"u{event.user.user_id}",
            )
            for index, chunk in enumerate(chunks[1:], start=1):
                last = index == len(chunks) - 1
                await self._api.send_message(
                    user_id=event.user.user_id, text=chunk,
                    attachments=keyboards.attachments(reply.keyboard) if last else None,
                )
            return
        await self.send(event.user.user_id, reply)

    async def notify(self, event: MaxEvent, text: str) -> None:
        """Короткое всплывающее уведомление на нажатие без изменения сообщения."""
        if event.kind == "callback" and event.callback_id:
            await self._api.answer_callback(event.callback_id, notification=text, pace_key=f"u{event.user.user_id}")
        else:
            await self.send(event.user.user_id, Reply(text=text, markdown=False))

    async def run_slow(
        self, event: MaxEvent, progress_text: str, work: Callable[[], Awaitable[Reply | None]],
    ) -> None:
        """«⏳ ...» сразу, затем результат в том же сообщении. Для нажатия —
        ответ на callback (MAX ждёт его быстро), для текста — новое
        сообщение. Если правка не удалась — результат новым сообщением.
        work() вернул None — ничего не меняется (прогресс убирать нечем).
        Ошибка внутри work() не оставляет «⏳» навсегда: сообщение заменяется
        текстом ошибки (на уже отвеченный callback второй ответ не послать)."""
        progress = Reply(text=progress_text, markdown=False)
        mid = None
        if event.kind == "callback" and event.callback_id:
            await self.answer(event, progress)
            mid = event.message_id
        else:
            mid = await self.send(event.user.user_id, progress)
        try:
            result = await work()
        except Exception:
            logger.exception("MAX: медленное действие завершилось ошибкой")
            result = Reply(text=ERROR_TEXT, keyboard=self._error_keyboard, markdown=False)
        if result is None:
            return
        chunks = _render(result)
        if mid is not None and len(chunks) == 1:
            try:
                await self._api.edit_message(
                    mid, text=chunks[0], attachments=keyboards.attachments(result.keyboard),
                    pace_key=f"u{event.user.user_id}",
                )
                return
            except MaxApiError:
                logger.warning("MAX: не удалось заменить «⏳» результатом — отправляю новым сообщением")
        await self.send(event.user.user_id, result)


class MaxNotificationSender:
    """Уведомления по инициативе бота (новые штрафы, увеличения, изменения
    задолженности): отправка в диалог; заблокировавший бота пользователь
    помечается в max_bot_users."""

    def __init__(self, responder: MaxResponder, storage: MaxBotStorage):
        self._responder = responder
        self._storage = storage

    async def send_notification(self, max_user_id: int, text: str, keyboard) -> None:
        await self._responder.send(max_user_id, Reply(text=text, keyboard=keyboard))

    def mark_blocked(self, max_user_id: int) -> None:
        self._storage.mark_blocked(max_user_id)


class EventHandler(Protocol):
    async def handle(self, event: MaxEvent, responder: MaxResponder) -> None: ...


class MaxDispatcher:
    """Очередь апдейтов + воркеры. События одного пользователя
    обрабатываются строго по очереди (lock на user_id). Нажатие кнопки, пока
    предыдущее действие того же пользователя ещё идёт, не ставится в очередь,
    а сразу получает «⏳ ещё выполняется»: двойное нажатие не запускает вторую
    живую проверку и не занимает воркер, нужный другим пользователям."""

    def __init__(
        self, storage: MaxBotStorage, handler: EventHandler, responder: MaxResponder, *,
        workers: int = _WORKERS, queue_size: int = _QUEUE_SIZE,
    ):
        self._storage = storage
        self._handler = handler
        self._responder = responder
        self._workers = workers
        self._queue: asyncio.Queue[MaxEvent] = asyncio.Queue(maxsize=queue_size)
        # user_id -> [lock, сколько событий его держат или ждут]; запись
        # удаляется, когда счётчик доходит до нуля.
        self._user_locks: dict[int, list] = {}

    def submit(self, raw: dict) -> bool:
        """False — очередь переполнена: webhook отвечает 503 и MAX повторит
        доставку позже (ключ апдейта в этом случае НЕ фиксируется)."""
        event = parse_update(raw)
        if event is None:
            return True
        if self._queue.full():
            return False
        if not self._storage.claim_update(event.dedup_key):
            logger.info("MAX %s: повторная доставка апдейта %s — пропускаю", self._storage.bot, event.update_type)
            return True
        self._queue.put_nowait(event)
        return True

    async def run_forever(self) -> None:
        await asyncio.gather(*(self._worker() for _ in range(self._workers)), self._maintenance())

    async def process(self, event: MaxEvent) -> None:
        """Обработка одного события (воркер; тесты вызывают напрямую)."""
        if event.kind == "other" or event.user is None:
            return
        if not event.is_private:
            logger.info("MAX %s: событие не из личного диалога — игнорирую", self._storage.bot)
            return
        if event.user.is_bot:
            return
        user_id = event.user.user_id
        entry = self._user_locks.get(user_id)
        if event.kind == "callback" and entry is not None and entry[0].locked():
            try:
                await self._responder.notify(event, BUSY_TEXT)
            except Exception:
                logger.warning("MAX %s: не удалось ответить на повторное нажатие", self._storage.bot)
            return
        if entry is None:
            entry = self._user_locks[user_id] = [asyncio.Lock(), 0]
        entry[1] += 1
        try:
            async with entry[0]:
                await self._process_locked(event)
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                self._user_locks.pop(user_id, None)

    async def _process_locked(self, event: MaxEvent) -> None:
        if event.kind == "stopped":
            self._storage.mark_blocked(event.user.user_id)
            return
        self._storage.record_user(event.user, chat_id=event.chat_id)
        try:
            await self._handler.handle(event, self._responder)
        except Exception:
            logger.exception("MAX %s: ошибка обработки %s", self._storage.bot, event.update_type)
            try:
                await self._responder.notify(event, ERROR_TEXT)
            except Exception:
                logger.warning("MAX %s: не удалось сообщить пользователю об ошибке", self._storage.bot)

    async def _worker(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                await self.process(event)
            finally:
                self._queue.task_done()

    async def _maintenance(self) -> None:
        while True:
            try:
                purged = self._storage.purge_processed_updates()
                if purged:
                    logger.info("MAX %s: удалено %d старых ключей апдейтов", self._storage.bot, purged)
            except Exception:
                logger.exception("MAX %s: очистка ключей апдейтов не удалась", self._storage.bot)
            await asyncio.sleep(_PURGE_INTERVAL_SECONDS)


def build_webhook_app(dispatcher: MaxDispatcher, *, secret: str, path: str) -> web.Application:
    if not secret:
        raise ValueError("webhook secret обязателен")

    async def webhook(request: web.Request) -> web.Response:
        received = request.headers.get(SECRET_HEADER, "")
        if not hmac.compare_digest(received.encode("utf-8"), secret.encode("utf-8")):
            logger.warning("MAX webhook: запрос без верного секрета отклонён")
            return web.Response(status=401)
        try:
            raw = await request.json()
        except ValueError:
            return web.Response(status=400)
        if not dispatcher.submit(raw):
            return web.Response(status=503)
        return web.json_response({"ok": True})

    async def health(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application(client_max_size=_MAX_WEBHOOK_BODY)
    app.router.add_post(path, webhook)
    app.router.add_get("/healthz", health)
    return app


async def run_webhook_forever(app: web.Application, *, host: str, port: int) -> None:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    logger.info("MAX webhook слушает %s:%d", host, port)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


async def run_polling_forever(api: MaxApi, dispatcher: MaxDispatcher) -> None:
    """Только для локальной разработки (MAX: long polling не для production,
    и он не работает, пока у бота есть webhook-подписка)."""
    marker = None
    while True:
        try:
            updates, marker = await api.get_updates(marker=marker, timeout=30, types=SUPPORTED_UPDATE_TYPES)
        except MaxApiError as exc:
            logger.warning("MAX polling: %s", exc)
            await asyncio.sleep(_POLL_ERROR_PAUSE_SECONDS)
            continue
        for raw in updates:
            while not dispatcher.submit(raw):
                await asyncio.sleep(1.0)
