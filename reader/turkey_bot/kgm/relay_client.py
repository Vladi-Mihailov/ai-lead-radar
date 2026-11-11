"""RelayKgmSession — KGM через relay на грузинском сервере (см.
docs/kgm-relay.md и relay_server.py): тот же интерфейс, что KgmProvider
ожидает от KgmSession (start / refresh_captcha / submit /
last_submit_seconds), плюс aclose(). Один объект — и "сессия" для
KgmProvider, и closeable для unified/check_service.py::_check_kgm (finally:
await client.aclose()), поэтому ни check_service, ни KgmProvider не меняются.

На production остаётся всё, кроме транспорта: картинка CAPTCHA приходит
сюда (base64) и решается OCR как раньше, а submit() возвращает СЫРОЙ текст
ответа KGM, который KgmProvider отдаёт parser.parse_submit_response — ровно
как с прямым KgmSession.

Любой сбой — сеть, таймаут, любой статус кроме ожидаемого (401/404/429/
502/503/...), неразборчивый JSON или base64 — превращается в
KgmTransportError (-> ProviderStatus.ERROR "transport_error", GİB/Avrasya
не затрагиваются) и ровно одну WARNING-строку: операция, безопасная деталь,
длительность. Госномер, CAPTCHA-код, тела запроса/ответа, секрет и подпись в
журнал не попадают. Автоматического перехода на прямой KGM нет (route.py)."""

import base64
import json
import logging
import math
import re
import time
from collections.abc import Callable
from typing import Any, TypeVar

import httpx

from reader.turkey_bot.kgm import relay_auth
from reader.turkey_bot.kgm.models import KgmCaptchaChallenge
from reader.turkey_bot.kgm.session import KgmTransportError, _describe_transport_error

logger = logging.getLogger(__name__)

# Те же пути, что в relay_server.py (не импортируется отсюда: тянул бы
# aiohttp.web в процесс бота ради двух строк).
SESSIONS_PATH = "/internal/kgm/sessions"
HEALTH_PATH = "/internal/kgm/health"

_CONNECT_TIMEOUT_SECONDS = 5.0
# Больше срока операции на relay (relay_server.OPERATION_TIMEOUT_SECONDS =
# 35 с): relay всегда отвечает раньше, чем истечёт этот таймаут.
_READ_TIMEOUT_SECONDS = 40.0
# DELETE в aclose() — best effort: при недоступном relay не задерживать
# завершение проверки, сессию relay закроет сам по TTL.
_CLOSE_TIMEOUT_SECONDS = 5.0

_SESSION_ID_RE = re.compile(r"[0-9a-f]{32}")
_ERROR_CODE_RE = re.compile(r"[a-z_]{1,32}")
# Из detail relay (session._describe_transport_error: "ConnectError errno=111
# Connection refused", "HTTPStatusError status=500") в журнал идут только тип
# исключения и числа — текст ответа по сети не доверяется.
_DETAIL_PREFIX_RE = re.compile(r"[A-Za-z]{1,40}(?: status=\d{3})?(?: errno=-?\d{1,6})?")

_T = TypeVar("_T")


class _MalformedRelayResponse(Exception):
    """Ожидаемый статус, но ответ не той формы — текст для лога безопасен."""


def _relay_error_suffix(response: httpx.Response) -> str:
    """" error=busy detail=ConnectTimeout" из JSON-ответа relay: код ошибки —
    только из фиксированного алфавита, от детали — только префикс "тип
    [status=N] [errno=N]". Произвольный текст ответа (например, HTML-страница
    чужого прокси) в журнал не идёт."""
    try:
        data = response.json()
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    parts = []
    error = data.get("error")
    if isinstance(error, str) and _ERROR_CODE_RE.fullmatch(error):
        parts.append(f"error={error}")
    detail = data.get("detail")
    match = _DETAIL_PREFIX_RE.match(detail) if isinstance(detail, str) else None
    if match:
        parts.append(f"detail={match.group(0)}")
    return " " + " ".join(parts) if parts else ""


def _parse_captcha(data: dict[str, Any]) -> KgmCaptchaChallenge:
    encoded = data.get("captcha_png_b64")
    if not isinstance(encoded, str):
        raise _MalformedRelayResponse("captcha missing")
    try:
        image = base64.b64decode(encoded, validate=True)
    except ValueError:
        raise _MalformedRelayResponse("invalid base64") from None
    return KgmCaptchaChallenge(image_png=image)


def _parse_created(data: dict[str, Any]) -> tuple[str, KgmCaptchaChallenge]:
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        raise _MalformedRelayResponse("malformed session_id")
    return session_id, _parse_captcha(data)


def _parse_submitted(data: dict[str, Any]) -> tuple[str, float]:
    response_text = data.get("response_text")
    if not isinstance(response_text, str):
        raise _MalformedRelayResponse("response_text missing")
    submit_seconds = data.get("submit_seconds")
    if (
        isinstance(submit_seconds, bool)
        or not isinstance(submit_seconds, (int, float))
        or not math.isfinite(submit_seconds)
    ):
        raise _MalformedRelayResponse("malformed submit_seconds")
    return response_text, float(submit_seconds)


def _parse_health(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("ok") is not True:
        raise _MalformedRelayResponse("health is not ok")
    return data


class RelayKgmSession:
    """Один экземпляр — одна проверка номера (как KgmSession): свой
    httpx.AsyncClient к relay и одна relay-сессия, которую aclose()
    закрывает на relay (DELETE) перед закрытием client."""

    def __init__(
        self,
        base_url: str,
        hmac_secret: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        wall_clock: Callable[[], float] = time.time,
    ):
        relay_auth.require_secret(hmac_secret)
        self._secret = hmac_secret
        self._wall_clock = wall_clock
        # trust_env=False: подписанные запросы идут только напрямую через
        # WireGuard, а не через HTTP(S)_PROXY из окружения процесса бота.
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(_READ_TIMEOUT_SECONDS, connect=_CONNECT_TIMEOUT_SECONDS),
            transport=transport,
            trust_env=False,
        )
        self._session_id: str | None = None
        self._closed = False
        # Как у KgmSession — читает KgmProvider для лога unexpected; здесь это
        # submit_seconds, измеренный на relay (длительность POST к KGM без
        # пути production -> relay).
        self.last_submit_seconds: float | None = None

    async def start(self) -> KgmCaptchaChallenge:
        if self._session_id is not None:
            await self._delete_session()
        session_id, challenge = await self._call(
            "POST", SESSIONS_PATH, operation="start", expected_status=201, parse=_parse_created,
        )
        self._session_id = session_id
        return challenge

    async def refresh_captcha(self) -> KgmCaptchaChallenge:
        session_id = self._require_session("captcha")
        return await self._call(
            "POST", f"{SESSIONS_PATH}/{session_id}/captcha",
            operation="captcha", expected_status=200, parse=_parse_captcha,
        )

    async def submit(self, *, plate: str, captcha_code: str) -> str:
        session_id = self._require_session("submit")
        self.last_submit_seconds = None
        body = json.dumps({"plate": plate, "captcha_code": captcha_code}).encode("utf-8")
        response_text, submit_seconds = await self._call(
            "POST", f"{SESSIONS_PATH}/{session_id}/submit",
            operation="submit", expected_status=200, body=body, parse=_parse_submitted,
        )
        self.last_submit_seconds = submit_seconds
        return response_text

    async def health(self) -> dict[str, Any]:
        """Только для ручной проверки связи (route.py, python -m ...): к KGM
        relay при этом не обращается."""
        return await self._call("GET", HEALTH_PATH, operation="health", expected_status=200, parse=_parse_health)

    async def aclose(self) -> None:
        if self._closed:
            return
        try:
            await self._delete_session()
        finally:
            self._closed = True
            await self._client.aclose()

    async def _delete_session(self) -> None:
        session_id, self._session_id = self._session_id, None
        if session_id is None:
            return
        try:
            await self._call(
                "DELETE", f"{SESSIONS_PATH}/{session_id}",
                operation="close", expected_status=204, timeout=_CLOSE_TIMEOUT_SECONDS,
            )
        except KgmTransportError:
            pass  # WARNING уже записан в _call(); relay закроет сессию по TTL

    def _require_session(self, operation: str) -> str:
        if self._session_id is None:
            raise self._failure(operation, "relay session is not started", time.monotonic())
        return self._session_id

    def _failure(self, operation: str, detail: str, started: float) -> KgmTransportError:
        logger.warning("KGM relay %s: %s after %.2fs", operation, detail, time.monotonic() - started)
        return KgmTransportError(f"KGM relay {operation} failed: {detail}")

    async def _call(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        expected_status: int,
        body: bytes = b"",
        parse: Callable[[dict[str, Any]], _T] | None = None,
        timeout: float | None = None,
    ) -> _T | None:
        started = time.monotonic()
        if self._closed:
            raise self._failure(operation, "client is closed", started)
        try:
            request = self._client.build_request(
                method, path, content=body,
                headers={"Content-Type": "application/json"} if body else None,
                timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
            )
            # Подписывается ровно тот путь и те байты тела, что уйдут по сети,
            # со свежим nonce на каждый запрос (повтор relay отклонит).
            request.headers.update(relay_auth.signed_headers(
                self._secret, method, request.url.raw_path.decode("ascii"), body, now=self._wall_clock(),
            ))
            response = await self._client.send(request)
        except httpx.HTTPError as exc:
            raise self._failure(operation, _describe_transport_error(exc), started) from exc
        except Exception as exc:
            raise self._failure(operation, type(exc).__name__, started) from exc

        if response.status_code != expected_status:
            raise self._failure(operation, f"HTTP {response.status_code}{_relay_error_suffix(response)}", started)
        if parse is None:
            return None
        try:
            data = response.json()
        except ValueError:
            raise self._failure(operation, "malformed JSON", started) from None
        if not isinstance(data, dict):
            raise self._failure(operation, "malformed JSON", started)
        try:
            return parse(data)
        except _MalformedRelayResponse as exc:
            raise self._failure(operation, str(exc), started) from None
