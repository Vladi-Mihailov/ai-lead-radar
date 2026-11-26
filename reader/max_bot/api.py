"""HTTP-клиент MAX Bot API (https://dev.max.ru/docs-api/).

Токен передаётся ТОЛЬКО в заголовке Authorization (query-параметр
access_token MAX больше не принимает) и никогда не попадает в логи,
исключения и repr: URL запросов его не содержит.

Ошибки MAX приходят в двух видах: HTTP 4xx/5xx с {"code", "message"} и
HTTP 200 с {"success": false, "code", "message"} (PUT/DELETE /messages,
/subscriptions) — оба превращаются в MaxApiError. Повтор — только для 429
(запрос не выполнен, повтор безопасен); 5xx и таймауты не повторяются здесь
(отправка не идемпотентна: повтор мог бы продублировать сообщение) —
решает вызывающий код (у доставок — свой backoff).

Лимиты платформы: ~30 запросов/с на бота и 2 сообщения/с в один диалог
(отправка, правка, ответ на callback) — _Pacer держит паузы с запасом.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://platform-api2.max.ru"
MAX_TEXT_LENGTH = 4000

_PER_CHAT_INTERVAL_SECONDS = 0.55
_GLOBAL_INTERVAL_SECONDS = 1 / 25
_RATE_LIMIT_RETRIES = 2
_RATE_LIMIT_DELAY_SECONDS = 1.0
_DEFAULT_TIMEOUT_SECONDS = 30.0
# Long polling держит соединение до timeout секунд — HTTP-таймаут должен
# быть больше, иначе пустой ответ превращался бы в ошибку.
_POLL_EXTRA_TIMEOUT_SECONDS = 15.0


class MaxApiError(Exception):
    """status — HTTP-статус (None — запрос не дошёл: сеть/таймаут), code —
    машинный код MAX ("not.found", "chat.denied", ...) или "transport"."""

    def __init__(self, *, method: str, path: str, status: int | None, code: str | None, message: str | None):
        self.method = method
        self.path = path
        self.status = status
        self.code = code
        self.message = message
        super().__init__(f"MAX API {method} {path}: status={status} code={code} message={message}")

    @property
    def recipient_unavailable(self) -> bool:
        """Пользователь заблокировал бота/удалил диалог — повторять бессмысленно."""
        return self.status in (403, 404) or self.code in ("chat.denied", "not.found", "user.blocked")


class MaxRateLimitedError(MaxApiError):
    pass


class _Pacer:
    """Пауза между запросами: глобальная (все запросы бота) и по диалогу."""

    def __init__(self, *, clock: Callable[[], float], sleep: Callable[[float], Awaitable[None]]):
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._last_global = float("-inf")
        self._last_by_key: dict[str, float] = {}

    async def wait(self, key: str | None) -> None:
        """Слот резервируется под lock, ждём — уже без него: пауза одного
        диалога не задерживает запросы в другие диалоги."""
        async with self._lock:
            now = self._clock()
            ready_at = max(now, self._last_global + _GLOBAL_INTERVAL_SECONDS)
            if key is not None:
                ready_at = max(ready_at, self._last_by_key.get(key, float("-inf")) + _PER_CHAT_INTERVAL_SECONDS)
            self._last_global = ready_at
            if key is not None:
                if len(self._last_by_key) > 10_000:
                    self._last_by_key = {k: v for k, v in self._last_by_key.items() if v > now}
                self._last_by_key[key] = ready_at
        if ready_at > now:
            await self._sleep(ready_at - now)


def _recipient_params(*, user_id: int | None, chat_id: int | None) -> dict:
    if (user_id is None) == (chat_id is None):
        raise ValueError("нужен ровно один из user_id/chat_id")
    return {"user_id": user_id} if user_id is not None else {"chat_id": chat_id}


def _pace_key(*, user_id: int | None = None, chat_id: int | None = None) -> str:
    return f"u{user_id}" if user_id is not None else f"c{chat_id}"


class MaxApi:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
        verify: bool | str = True,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not token:
            raise ValueError("MAX token пуст")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=base_url, verify=verify, timeout=timeout)
        self._headers = {"Authorization": token}
        self._timeout = timeout
        self._sleep = sleep
        self._pacer = _Pacer(clock=clock, sleep=sleep)

    def __repr__(self) -> str:
        return "MaxApi(<token hidden>)"

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request(
        self, method: str, path: str, *, params: dict | None = None, json: dict | None = None,
        pace_key: str | None = None, timeout: float | None = None,
    ) -> dict:
        attempt = 0
        while True:
            await self._pacer.wait(pace_key)
            try:
                response = await self._client.request(
                    method, path, params=params, json=json, headers=self._headers,
                    timeout=timeout or self._timeout,
                )
            except httpx.HTTPError as exc:
                raise MaxApiError(
                    method=method, path=path, status=None, code="transport", message=type(exc).__name__,
                ) from None
            body = _json_or_none(response)
            if response.status_code == 429:
                if attempt < _RATE_LIMIT_RETRIES:
                    attempt += 1
                    await self._sleep(_RATE_LIMIT_DELAY_SECONDS * attempt)
                    continue
                raise MaxRateLimitedError(
                    method=method, path=path, status=429, code=_field(body, "code"), message=_field(body, "message"),
                )
            if response.status_code != 200:
                raise MaxApiError(
                    method=method, path=path, status=response.status_code,
                    code=_field(body, "code"), message=_field(body, "message"),
                )
            if isinstance(body, dict) and body.get("success") is False:
                raise MaxApiError(
                    method=method, path=path, status=200, code=_field(body, "code"), message=_field(body, "message"),
                )
            return body if isinstance(body, dict) else {}

    async def get_me(self) -> dict:
        return await self._request("GET", "/me")

    async def send_message(
        self, *, text: str, user_id: int | None = None, chat_id: int | None = None,
        attachments: list[dict] | None = None, fmt: str | None = "html", notify: bool = True,
    ) -> str | None:
        """Возвращает mid отправленного сообщения (None — MAX его не вернул)."""
        if len(text) > MAX_TEXT_LENGTH:
            raise ValueError(f"текст длиннее {MAX_TEXT_LENGTH} символов — разбейте его (formatting.split_text)")
        params = {**_recipient_params(user_id=user_id, chat_id=chat_id), "disable_link_preview": "true"}
        body = _message_body(text=text, attachments=attachments, fmt=fmt, notify=notify)
        response = await self._request(
            "POST", "/messages", params=params, json=body, pace_key=_pace_key(user_id=user_id, chat_id=chat_id),
        )
        message = response.get("message") or {}
        return (message.get("body") or {}).get("mid")

    async def edit_message(
        self, message_id: str, *, text: str, attachments: list[dict] | None = None, fmt: str | None = "html",
        pace_key: str | None = None,
    ) -> None:
        """attachments=None — клавиатура остаётся прежней, [] — убирается."""
        body = _message_body(text=text, attachments=None, fmt=fmt, notify=False)
        if attachments is not None:
            body["attachments"] = attachments
        await self._request("PUT", "/messages", params={"message_id": message_id}, json=body, pace_key=pace_key)

    async def answer_callback(
        self, callback_id: str, *, message: dict | None = None, notification: str | None = None,
        pace_key: str | None = None,
    ) -> None:
        """message — новое содержимое сообщения с нажатой кнопкой (правка на
        месте), notification — короткое всплывающее уведомление."""
        body: dict = {}
        if message is not None:
            body["message"] = message
        if notification is not None:
            body["notification"] = notification
        await self._request("POST", "/answers", params={"callback_id": callback_id}, json=body, pace_key=pace_key)

    async def get_updates(
        self, *, marker: int | None, timeout: int = 30, limit: int = 100, types: list[str] | None = None,
    ) -> tuple[list[dict], int | None]:
        params: dict = {"timeout": timeout, "limit": limit}
        if marker is not None:
            params["marker"] = marker
        if types:
            params["types"] = ",".join(types)
        body = await self._request(
            "GET", "/updates", params=params, timeout=timeout + _POLL_EXTRA_TIMEOUT_SECONDS,
        )
        return list(body.get("updates") or []), body.get("marker", marker)

    async def subscribe(self, url: str, *, secret: str, update_types: list[str]) -> None:
        await self._request("POST", "/subscriptions", json={"url": url, "secret": secret, "update_types": update_types})

    async def unsubscribe(self, url: str) -> None:
        await self._request("DELETE", "/subscriptions", params={"url": url})

    async def subscriptions(self) -> list[dict]:
        body = await self._request("GET", "/subscriptions")
        return list(body.get("subscriptions") or [])

    async def set_commands(self, commands: list[tuple[str, str]]) -> None:
        await self._request(
            "PATCH", "/me/commands",
            json={"commands": [{"name": name, "description": description} for name, description in commands]},
        )


def _message_body(*, text: str, attachments: list[dict] | None, fmt: str | None, notify: bool) -> dict:
    body: dict = {"text": text, "notify": notify}
    if attachments:
        body["attachments"] = attachments
    if fmt:
        body["format"] = fmt
    return body


def _json_or_none(response: httpx.Response):
    try:
        return response.json()
    except ValueError:
        return None


def _field(body, name: str) -> str | None:
    if isinstance(body, dict):
        value = body.get(name)
        return str(value) if value is not None else None
    return None
