"""KGM relay — тонкий HTTP-ретранслятор на грузинском сервере (REDNET), см.
docs/kgm-relay.md. С 2026-11-11 webihlaltakip.kgm.gov.tr не отвечает
production-серверу в Германии (из сети Hetzner таймаутят и DNS, и TCP 443),
а с грузинского сервера сайт доступен. Поэтому в Грузии работает ТОЛЬКО
транспорт — тот же самый reader/turkey_bot/kgm/session.py::KgmSession (без
изменений и без копии), а OCR CAPTCHA и бизнес-разбор ответа
(parser.parse_submit_response) остаются на production (см. relay_client.py
::RelayKgmSession).

Одна relay-сессия = один KgmSession + его собственный httpx.AsyncClient на
всё время проверки одного номера: cookie ASP.NET_SessionId живёт в client,
__VIEWSTATE*/hdnx — в KgmSession, поэтому обновление CAPTCHA и повторный
submit после "rejected" обязаны идти через ТЕ ЖЕ объекты. Relay держит их в
памяти по session_id (secrets.token_hex(16)) до DELETE или 120 с простоя,
затем client закрывается. После любой ошибки KGM сессия закрывается сразу:
состояние ASP.NET тогда неизвестно, а production после transport_error всё
равно прекращает проверку KGM (unified/check_service.py::_check_kgm).

Защита: запросы только с адресов allowed_networks (сеть WireGuard, см.
relay_main.py) и только с HMAC-подписью с nonce (relay_auth.py), тело не
больше 16 KiB, не больше 2 открытых сессий (при заполнении закрывается давно
не использованная свободная, 503 — только если свободных нет), token bucket
на создание сессий (одна в 2 с, burst 3), не больше 600 сессий за сутки UTC,
asyncio.Semaphore(1) — к KGM в каждый момент идёт не больше одной операции.

Журнал — только метод, шаблон маршрута, статус, длительность, размер ответа
и категория ошибки. Госномер, CAPTCHA-код, cookies, ViewState, тело ответа
KGM, session_id, адрес клиента, секрет и подпись в журнал не пишутся никогда;
ответы с ошибкой ничего из запроса не повторяют."""

import asyncio
import base64
import contextlib
import ipaddress
import json
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import TypeVar

import httpx
from aiohttp import web

from reader.turkey_bot.kgm import relay_auth
from reader.turkey_bot.kgm.relay_version import RELAY_PROTOCOL_VERSION, kgm_code_fingerprint
from reader.turkey_bot.kgm.session import (
    _MIN_REQUEST_INTERVAL_SECONDS,
    KgmSession,
    KgmTransportError,
    _describe_transport_error,
)

logger = logging.getLogger(__name__)

SESSIONS_PATH = "/internal/kgm/sessions"
HEALTH_PATH = "/internal/kgm/health"

MAX_REQUEST_BODY_SIZE = 16 * 1024
SESSION_TTL_SECONDS = 120.0
MAX_OPEN_SESSIONS = 2
SESSION_RATE_PER_SECOND = 0.5
SESSION_BURST = 3
DAILY_SESSION_CAP = 600
CLEANUP_INTERVAL_SECONDS = 15.0

# Те же User-Agent и таймаут запроса, что у прямого маршрута
# (unified/check_service.py::_build_client): для KGM relay выглядит так же,
# как production до блокировки. check_service здесь не импортируется — он
# тянет OCR-зависимости, которых на грузинском сервере нет.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
KGM_REQUEST_TIMEOUT_SECONDS = 30.0

# Сроки операций (включая ожидание семафора) выводятся из таймаута одного
# запроса к KGM и паузы KgmSession._throttle между запросами: start() — GET
# страницы + пауза + GET CAPTCHA, captcha и submit — пауза + один запрос;
# DEADLINE_MARGIN_SECONDS — запас. Read-таймауты relay_client.py больше
# соответствующих сроков: relay всегда отвечает раньше, чем production
# бросит запрос, и не оставляет сессию, о которой production не узнал.
DEADLINE_MARGIN_SECONDS = 3.5
START_DEADLINE_SECONDS = 2 * KGM_REQUEST_TIMEOUT_SECONDS + _MIN_REQUEST_INTERVAL_SECONDS + DEADLINE_MARGIN_SECONDS
STEP_DEADLINE_SECONDS = KGM_REQUEST_TIMEOUT_SECONDS + _MIN_REQUEST_INTERVAL_SECONDS + DEADLINE_MARGIN_SECONDS
OPERATION_DEADLINES = {
    "start": START_DEADLINE_SECONDS,
    "captcha": STEP_DEADLINE_SECONDS,
    "submit": STEP_DEADLINE_SECONDS,
}

_SESSION_ID_RE = re.compile(r"[0-9a-f]{32}")
_PLATE_RE = re.compile(r"[A-Za-z0-9 ]{1,20}")
_MAX_CAPTCHA_CODE_LENGTH = 128
_SUBMIT_FIELDS = frozenset({"plate", "captcha_code"})

_HTTP_ERROR_NAMES = {404: "not_found", 405: "method_not_allowed"}

_T = TypeVar("_T")

KgmSessionFactory = Callable[[], tuple[httpx.AsyncClient, KgmSession]]
IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def default_session_factory() -> tuple[httpx.AsyncClient, KgmSession]:
    client = httpx.AsyncClient(timeout=KGM_REQUEST_TIMEOUT_SECONDS, headers={"User-Agent": _USER_AGENT})
    return client, KgmSession(client, request_timeout=KGM_REQUEST_TIMEOUT_SECONDS)


class _TokenBucket:
    def __init__(self, *, rate_per_second: float, burst: int, clock: Callable[[], float]):
        self._rate = rate_per_second
        self._burst = float(burst)
        self._tokens = float(burst)
        self._clock = clock
        self._updated_at = clock()

    def try_take(self) -> bool:
        now = self._clock()
        self._tokens = min(self._burst, self._tokens + max(0.0, now - self._updated_at) * self._rate)
        self._updated_at = now
        if self._tokens < 1.0:
            return False
        self._tokens -= 1.0
        return True


class _DailyCounter:
    """Сколько сессий создано за текущие сутки UTC (считается каждая попытка,
    дошедшая до KGM, — лимит про нагрузку на KGM, а не про успех)."""

    def __init__(self, *, cap: int, wall_clock: Callable[[], float]):
        self.cap = cap
        self._wall_clock = wall_clock
        self._day: date | None = None
        self._count = 0

    def count(self) -> int:
        today = datetime.fromtimestamp(self._wall_clock(), tz=timezone.utc).date()
        if today != self._day:
            self._day = today
            self._count = 0
        return self._count

    def has_room(self) -> bool:
        return self.count() < self.cap

    def add(self) -> None:
        self.count()
        self._count += 1


@dataclass(eq=False)
class _RelaySession:
    client: httpx.AsyncClient
    kgm: KgmSession
    last_used: float
    # Пока идёт операция, сессию не закрывают ни TTL, ни DELETE, ни
    # вытеснение: закрытие откладывается до её конца (discarded), client не
    # закрывается под работающим запросом.
    in_flight: int = 0
    discarded: bool = False


class _RelayError(Exception):
    def __init__(self, response: web.Response):
        super().__init__(response.status)
        self.response = response


def _error(status: int, error: str, *, detail: str | None = None) -> web.Response:
    payload = {"error": error}
    if detail is not None:
        payload["detail"] = detail
    return web.json_response(payload, status=status)


def _transport_detail(exc: KgmTransportError) -> str:
    """Безопасная деталь для 502 — та же _describe_transport_error, что и в
    логах KgmSession (тип + HTTP-статус или errno, без URL, тела и cookie).
    KgmTransportError без httpx-причины (неожиданный Content-Type) — только
    тип: текст такого исключения содержит заголовок ответа KGM."""
    cause = exc.__cause__
    if isinstance(cause, httpx.HTTPError):
        return _describe_transport_error(cause)
    return type(exc).__name__


def _parse_submit_body(body: bytes) -> tuple[str, str] | None:
    """Строго: JSON-объект ровно с полями plate и captcha_code, оба строки;
    plate — до 20 символов [A-Za-z0-9 ] (validation.normalize_plate отдаёт
    [A-Z0-9]{5,9}); captcha_code — 1..128 печатных символов. Unicode в коде
    допустим: OCR (captcha_solver._parse_kgm_expression) склеивает хвост
    через str.isalnum(), а неверный код должен дойти до KGM и вернуться
    "rejected" (повтор с новой CAPTCHA), а не стать transport_error."""
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or set(data) != _SUBMIT_FIELDS:
        return None
    plate, captcha_code = data["plate"], data["captcha_code"]
    if not isinstance(plate, str) or not _PLATE_RE.fullmatch(plate):
        return None
    if not isinstance(captcha_code, str) or not captcha_code.isprintable():
        return None
    if not 0 < len(captcha_code) <= _MAX_CAPTCHA_CODE_LENGTH:
        return None
    return plate, captcha_code


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


async def _close_quietly(client: httpx.AsyncClient) -> None:
    try:
        await client.aclose()
    except Exception as exc:
        logger.warning("KGM relay: не удалось закрыть httpx-клиент (%s)", type(exc).__name__)


class _KgmRelay:
    """Состояние одного процесса relay: открытые сессии, лимиты, семафор KGM
    и фоновая очистка. Обработчики маршрутов — методы этого класса."""

    def __init__(
        self,
        *,
        session_factory: KgmSessionFactory,
        clock: Callable[[], float],
        wall_clock: Callable[[], float],
        session_ttl_seconds: float,
        max_open_sessions: int,
        session_rate_per_second: float,
        session_burst: int,
        daily_session_cap: int,
        start_deadline_seconds: float,
        step_deadline_seconds: float,
        cleanup_interval_seconds: float,
        kgm_code: str,
    ):
        self._session_factory = session_factory
        self._clock = clock
        self._ttl = session_ttl_seconds
        self._max_open = max_open_sessions
        self._start_deadline = start_deadline_seconds
        self._step_deadline = step_deadline_seconds
        self._cleanup_interval = cleanup_interval_seconds
        self._kgm_code = kgm_code
        self._bucket = _TokenBucket(rate_per_second=session_rate_per_second, burst=session_burst, clock=clock)
        self._daily = _DailyCounter(cap=daily_session_cap, wall_clock=wall_clock)
        self._sessions: dict[str, _RelaySession] = {}
        self._pending_creations = 0
        self._kgm_semaphore = asyncio.Semaphore(1)
        self._cleanup_task: asyncio.Task | None = None

    # ---- жизненный цикл сессий ----

    async def _discard(self, session_id: str) -> None:
        entry = self._sessions.pop(session_id, None)
        if entry is None:
            return
        entry.discarded = True
        if entry.in_flight == 0:
            await _close_quietly(entry.client)

    async def purge_expired(self) -> int:
        now = self._clock()
        expired = [
            session_id for session_id, entry in self._sessions.items()
            if entry.in_flight == 0 and now - entry.last_used > self._ttl
        ]
        for session_id in expired:
            await self._discard(session_id)
        if expired:
            logger.info("KGM relay: закрыто просроченных сессий: %d", len(expired))
        return len(expired)

    def _least_recently_used_idle(self) -> str | None:
        """Самая давно не использованная сессия без текущей операции; сессии
        в процессе создания в self._sessions ещё не попали и сюда не входят."""
        idle = [(entry.last_used, session_id) for session_id, entry in self._sessions.items() if entry.in_flight == 0]
        return min(idle)[1] if idle else None

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(self._cleanup_interval)
            try:
                await self.purge_expired()
            except Exception as exc:
                logger.warning("KGM relay: фоновая очистка не удалась (%s)", type(exc).__name__)

    async def on_startup(self, app: web.Application) -> None:
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def on_cleanup(self, app: web.Application) -> None:
        task, self._cleanup_task = self._cleanup_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        open_sessions = len(self._sessions)
        for session_id in list(self._sessions):
            await self._discard(session_id)
        if open_sessions:
            logger.info("KGM relay: при остановке закрыто сессий: %d", open_sessions)

    def _new_session_id(self) -> str:
        while True:
            session_id = secrets.token_hex(16)
            if session_id not in self._sessions:
                return session_id

    def _lookup(self, request: web.Request) -> tuple[str, _RelaySession] | None:
        session_id = request.match_info.get("session_id", "")
        if not _SESSION_ID_RE.fullmatch(session_id):
            return None
        entry = self._sessions.get(session_id)
        return (session_id, entry) if entry is not None else None

    # ---- операции с KGM ----

    async def _run_kgm(self, operation: Callable[[], Awaitable[_T]], *, deadline: float) -> _T:
        try:
            async with asyncio.timeout(deadline):
                async with self._kgm_semaphore:
                    return await operation()
        except KgmTransportError as exc:
            raise _RelayError(_error(502, "kgm_transport", detail=_transport_detail(exc))) from None
        except TimeoutError:
            logger.warning("KGM relay: операция с KGM не уложилась в %.1f с", deadline)
            raise _RelayError(_error(502, "kgm_transport", detail="TimeoutError")) from None

    async def _use_session(
        self, session_id: str, entry: _RelaySession, operation: Callable[[], Awaitable[_T]],
    ) -> _T:
        entry.in_flight += 1
        entry.last_used = self._clock()
        succeeded = False
        try:
            result = await self._run_kgm(operation, deadline=self._step_deadline)
            succeeded = True
            return result
        finally:
            entry.in_flight -= 1
            entry.last_used = self._clock()
            if not succeeded:
                if self._sessions.get(session_id) is entry:
                    del self._sessions[session_id]
                entry.discarded = True
            if entry.discarded and entry.in_flight == 0:
                await _close_quietly(entry.client)

    # ---- маршруты ----

    async def create_session(self, request: web.Request) -> web.Response:
        await self.purge_expired()
        victim: str | None = None
        if len(self._sessions) + self._pending_creations >= self._max_open:
            victim = self._least_recently_used_idle()
            if victim is None:
                return _error(503, "busy")
        if not self._daily.has_room():
            logger.warning("KGM relay: исчерпан дневной лимит сессий (%d за сутки UTC)", self._daily.cap)
            return _error(429, "rate_limited")
        if not self._bucket.try_take():
            return _error(429, "rate_limited")
        self._daily.add()

        # Между выбором victim и этим местом нет ни одного await — никакой
        # другой запрос не мог занять её или начать на ней операцию.
        self._pending_creations += 1
        client: httpx.AsyncClient | None = None
        stored = False
        try:
            if victim is not None:
                # Обычно это брошенная сессия (production не прислал DELETE);
                # закрыть её лучше, чем отказать новой проверке.
                logger.info("KGM relay: лимит открытых сессий — закрыта давно не использованная (evicted_idle)")
                await self._discard(victim)
            client, kgm = self._session_factory()
            challenge = await self._run_kgm(kgm.start, deadline=self._start_deadline)
            session_id = self._new_session_id()
            self._sessions[session_id] = _RelaySession(client=client, kgm=kgm, last_used=self._clock())
            stored = True
        except _RelayError as exc:
            return exc.response
        finally:
            self._pending_creations -= 1
            if not stored and client is not None:
                await _close_quietly(client)
        return web.json_response(
            {"session_id": session_id, "captcha_png_b64": _b64(challenge.image_png), "expires_in": int(self._ttl)},
            status=201,
        )

    async def refresh_captcha(self, request: web.Request) -> web.Response:
        await self.purge_expired()
        found = self._lookup(request)
        if found is None:
            return _error(404, "session_not_found")
        session_id, entry = found
        try:
            challenge = await self._use_session(session_id, entry, entry.kgm.refresh_captcha)
        except _RelayError as exc:
            return exc.response
        return web.json_response({"captcha_png_b64": _b64(challenge.image_png)})

    async def submit(self, request: web.Request) -> web.Response:
        """Сырой текст ответа KGM (ASP.NET AJAX delta) возвращается как есть:
        разбирает его production (KgmProvider -> parser.parse_submit_response).
        Захват __VIEWSTATE/hdnx из этого ответа для следующего запроса уже
        сделал сам KgmSession.submit() — состояние осталось в сессии relay."""
        parsed = _parse_submit_body(await request.read())
        if parsed is None:
            return _error(400, "bad_request")
        plate, captcha_code = parsed
        await self.purge_expired()
        found = self._lookup(request)
        if found is None:
            return _error(404, "session_not_found")
        session_id, entry = found
        try:
            response_text = await self._use_session(
                session_id, entry, lambda: entry.kgm.submit(plate=plate, captcha_code=captcha_code),
            )
        except _RelayError as exc:
            return exc.response
        submit_seconds = entry.kgm.last_submit_seconds
        return web.json_response({
            "response_text": response_text,
            "submit_seconds": round(submit_seconds, 3) if submit_seconds is not None else 0.0,
        })

    async def delete_session(self, request: web.Request) -> web.Response:
        """Идемпотентно: 204 и для уже закрытой/неизвестной сессии."""
        found = self._lookup(request)
        if found is not None:
            await self._discard(found[0])
        await self.purge_expired()
        return web.Response(status=204)

    async def health(self, request: web.Request) -> web.Response:
        await self.purge_expired()
        return web.json_response({
            "ok": True,
            "open_sessions": len(self._sessions),
            "sessions_today": self._daily.count(),
            "version": RELAY_PROTOCOL_VERSION,
            "kgm_code": self._kgm_code,
        })


def _route_template(request: web.Request) -> str:
    """Шаблон маршрута ("/internal/kgm/sessions/{session_id}/submit"), а не
    путь запроса — в пути session_id. Для несовпавшего маршрута — "-"."""
    resource = getattr(request.match_info.route, "resource", None)
    return resource.canonical if resource is not None else "-"


def _error_category(status: int, body: bytes) -> str:
    """Категория ошибки — поле "error" ответа, который relay построил сам
    (_error()); для успешных ответов — "-"."""
    if status < 400:
        return "-"
    try:
        error = json.loads(body).get("error")
    except (ValueError, AttributeError):
        return "-"
    return error if isinstance(error, str) else "-"


@web.middleware
async def _access_log_middleware(request: web.Request, handler) -> web.StreamResponse:
    started = time.monotonic()
    try:
        response = await handler(request)
    except web.HTTPRequestEntityTooLarge:
        response = _error(413, "payload_too_large")
    except web.HTTPMethodNotAllowed as exc:
        response = _error(405, "method_not_allowed")
        allow = exc.headers.get("Allow")
        if allow:
            response.headers["Allow"] = allow
    except web.HTTPException as exc:
        response = _error(exc.status, _HTTP_ERROR_NAMES.get(exc.status, "http_error"))
    except Exception as exc:
        # Только тип: текст исключения может нести данные запроса.
        logger.error("KGM relay: необработанное исключение %s", type(exc).__name__)
        response = _error(500, "internal")
    body = response.body if isinstance(response, web.Response) and isinstance(response.body, bytes) else b""
    logger.info(
        "KGM relay: %s %s -> %d in %.3fs, %d bytes, error=%s",
        request.method, _route_template(request), response.status,
        time.monotonic() - started, len(body), _error_category(response.status, body),
    )
    return response


def _remote_allowed(remote: str | None, networks: tuple[IpNetwork, ...]) -> bool:
    if not remote:
        return False
    try:
        address = ipaddress.ip_address(remote)
    except ValueError:
        return False
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address in network for network in networks)


def _build_network_middleware(networks: tuple[IpNetwork, ...]):
    @web.middleware
    async def network_middleware(request: web.Request, handler) -> web.StreamResponse:
        # Раньше HMAC и раньше чтения тела: чужой адрес не получает даже 413.
        if not _remote_allowed(request.remote, networks):
            logger.warning("KGM relay: запрос отклонён (remote_not_allowed)")
            return _error(401, "unauthorized")
        return await handler(request)

    return network_middleware


def _build_auth_middleware(
    hmac_secret: str, wall_clock: Callable[[], float], uptime_clock: Callable[[], float],
):
    nonce_cache = relay_auth.NonceCache()
    started_uptime = uptime_clock()

    @web.middleware
    async def auth_middleware(request: web.Request, handler) -> web.StreamResponse:
        # read() сам соблюдает client_max_size: больше 16 KiB ->
        # HTTPRequestEntityTooLarge, его превращает в 413 _access_log_middleware.
        body = await request.read()
        now = wall_clock()
        # Момент старта процесса в ТЕКУЩЕЙ шкале wall clock, на каждом запросе:
        # если часы после старта шагнули (NTP поправил убежавшие вперёд),
        # граница сдвигается вместе с ними и не отклоняет корректные запросы.
        not_before = int(now - (uptime_clock() - started_uptime))
        is_valid, reason = relay_auth.verify(
            hmac_secret,
            method=request.method,
            path=request.raw_path,
            body=body,
            timestamp=request.headers.get(relay_auth.TIMESTAMP_HEADER),
            nonce=request.headers.get(relay_auth.NONCE_HEADER),
            signature=request.headers.get(relay_auth.SIGNATURE_HEADER),
            nonce_cache=nonce_cache,
            now=now,
            not_before=not_before,
        )
        if not is_valid:
            logger.warning("KGM relay: запрос отклонён (%s)", reason)
            return _error(401, "unauthorized")
        return await handler(request)

    return auth_middleware


def build_kgm_relay_app(
    *,
    hmac_secret: str,
    allowed_networks: Iterable[str | IpNetwork],
    session_factory: KgmSessionFactory = default_session_factory,
    clock: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], float] = time.time,
    uptime_clock: Callable[[], float] = time.monotonic,
    session_ttl_seconds: float = SESSION_TTL_SECONDS,
    max_open_sessions: int = MAX_OPEN_SESSIONS,
    session_rate_per_second: float = SESSION_RATE_PER_SECOND,
    session_burst: int = SESSION_BURST,
    daily_session_cap: int = DAILY_SESSION_CAP,
    start_deadline_seconds: float = START_DEADLINE_SECONDS,
    step_deadline_seconds: float = STEP_DEADLINE_SECONDS,
    cleanup_interval_seconds: float = CLEANUP_INTERVAL_SECONDS,
) -> web.Application:
    """allowed_networks обязателен и не может быть пустым: relay_main.py
    передаёт KGM_RELAY_ALLOWED_NETWORK (сеть WireGuard), тесты — 127.0.0.0/8.
    Запросы, подписанные раньше старта процесса (сборки приложения),
    отклоняются; граница — wall_clock() - (uptime_clock() - uptime при
    старте), на каждом запросе. clock — монотонные часы для TTL и token
    bucket, uptime_clock — отдельные монотонные часы только для этой границы
    (тесты TTL замораживают clock, и граница не должна идти за wall clock),
    wall_clock — время для подписи и суток UTC. Часы, сроки операций и лимиты
    подменяются только в тестах."""
    relay_auth.require_secret(hmac_secret)
    networks = tuple(ipaddress.ip_network(network) for network in allowed_networks)
    if not networks:
        raise ValueError("allowed_networks must not be empty")
    relay = _KgmRelay(
        session_factory=session_factory,
        clock=clock,
        wall_clock=wall_clock,
        session_ttl_seconds=session_ttl_seconds,
        max_open_sessions=max_open_sessions,
        session_rate_per_second=session_rate_per_second,
        session_burst=session_burst,
        daily_session_cap=daily_session_cap,
        start_deadline_seconds=start_deadline_seconds,
        step_deadline_seconds=step_deadline_seconds,
        cleanup_interval_seconds=cleanup_interval_seconds,
        kgm_code=kgm_code_fingerprint(),
    )
    app = web.Application(
        middlewares=[
            _access_log_middleware,
            _build_network_middleware(networks),
            _build_auth_middleware(hmac_secret, wall_clock, uptime_clock),
        ],
        client_max_size=MAX_REQUEST_BODY_SIZE,
        # Штатный access log aiohttp пишет строку запроса целиком, а в пути
        # session_id, — выключен для любого runner'а (relay_main.py,
        # TestServer); вместо него _access_log_middleware.
        handler_args={"access_log": None},
    )
    app.router.add_post(SESSIONS_PATH, relay.create_session)
    app.router.add_post(SESSIONS_PATH + "/{session_id}/captcha", relay.refresh_captcha)
    app.router.add_post(SESSIONS_PATH + "/{session_id}/submit", relay.submit)
    app.router.add_delete(SESSIONS_PATH + "/{session_id}", relay.delete_session)
    app.router.add_get(HEALTH_PATH, relay.health)
    app.on_startup.append(relay.on_startup)
    app.on_cleanup.append(relay.on_cleanup)
    return app
