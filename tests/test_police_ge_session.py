"""
Тесты PoliceGeSession — управление HTTP-сессией/csrf/retry. Реальная
сеть не используется: httpx.AsyncClient подключён к httpx.MockTransport,
который отдаёт заранее заготовленные ответы и запоминает, что именно было
запрошено (метод, url, отправленный csrf_token).
"""

import sys
from pathlib import Path
from urllib.parse import parse_qs

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402
import pytest  # noqa: E402

from reader.fines.police_ge_circuit_breaker import PoliceGeCircuitBreaker  # noqa: E402
from reader.fines.police_ge_session import PoliceGeSession  # noqa: E402
from reader.fines.provider import (  # noqa: E402
    FineProviderError,
    PoliceGeUnavailableError,
)

_PAGE_URL = "https://police.ge/protocol/index.php?lang=en"


def _page_html(csrf_token: str | None) -> str:
    token_input = (
        f'<input type="hidden" name="csrf_token" value="{csrf_token}">'
        if csrf_token is not None
        else ""
    )
    return f"<html><body>{token_input}</body></html>"


class _CallLog:
    def __init__(self):
        self.calls: list[dict] = []


def _make_session(
    pages: list[str], posts: list[httpx.Response], log: _CallLog,
    *, circuit_breaker: PoliceGeCircuitBreaker | None = None,
) -> PoliceGeSession:
    page_iter = iter(pages)
    post_iter = iter(posts)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            log.calls.append({"method": "GET"})
            return httpx.Response(200, text=next(page_iter))

        sent_csrf = parse_qs(request.content.decode())["csrf_token"][0]
        log.calls.append({"method": "POST", "csrf_token": sent_csrf})
        return next(post_iter)

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(base_url="https://police.ge/protocol/", transport=transport)
    return PoliceGeSession(
        client, page_url=_PAGE_URL, request_timeout=5, circuit_breaker=circuit_breaker,
    )


def _json_response(payload: dict) -> httpx.Response:
    return httpx.Response(200, json=payload)


def _html_response(csrf_token: str | None) -> httpx.Response:
    """То, что реально отдаёт police.ge вместо JSON при протухшем csrf/сессии."""
    return httpx.Response(200, text=_page_html(csrf_token))


async def test_search_by_plate_success_on_first_try():
    log = _CallLog()
    session = _make_session(
        pages=[_page_html("token-1")],
        posts=[_json_response({"success": True, "data": {"count": 0, "results": []}})],
        log=log,
    )

    result = await session.search_by_plate("AA001AA")

    assert result == {"success": True, "data": {"count": 0, "results": []}}
    assert [c["method"] for c in log.calls] == ["GET", "POST"]
    assert log.calls[1]["csrf_token"] == "token-1"


async def test_empty_results_is_not_treated_as_failure():
    log = _CallLog()
    session = _make_session(
        pages=[_page_html("token-1")],
        posts=[_json_response({"success": True, "data": {"count": 0, "results": []}})],
        log=log,
    )

    result = await session.search_by_plate("AA001AA")

    # Один GET, один POST — не должно быть скрытого повторного запроса на
    # легитимный "штрафов нет".
    assert len([c for c in log.calls if c["method"] == "GET"]) == 1
    assert result["data"]["results"] == []


async def test_session_is_reused_across_multiple_searches():
    log = _CallLog()
    session = _make_session(
        pages=[_page_html("token-1")],
        posts=[
            _json_response({"success": True, "data": {"count": 0, "results": []}}),
            _json_response({"success": True, "data": {"count": 0, "results": []}}),
        ],
        log=log,
    )

    await session.search_by_plate("AA001AA")
    await session.search_by_plate("BB002BB")

    get_calls = [c for c in log.calls if c["method"] == "GET"]
    post_calls = [c for c in log.calls if c["method"] == "POST"]
    assert len(get_calls) == 1  # один GET на весь проход, а не на каждый номер
    assert len(post_calls) == 2
    assert all(c["csrf_token"] == "token-1" for c in post_calls)


async def test_success_false_triggers_one_retry_then_succeeds():
    log = _CallLog()
    session = _make_session(
        pages=[_page_html("token-1"), _page_html("token-2")],
        posts=[
            _json_response({"success": False, "message": None, "data": []}),
            _json_response({"success": True, "data": {"count": 0, "results": []}}),
        ],
        log=log,
    )

    result = await session.search_by_plate("AA001AA")

    assert result["success"] is True
    assert [c["method"] for c in log.calls] == ["GET", "POST", "GET", "POST"]
    post_tokens = [c["csrf_token"] for c in log.calls if c["method"] == "POST"]
    assert post_tokens == ["token-1", "token-2"]  # второй POST — уже с новым токеном


async def test_invalid_json_response_triggers_retry_like_success_false():
    log = _CallLog()
    session = _make_session(
        pages=[_page_html("token-1"), _page_html("token-2")],
        posts=[
            _html_response("token-1"),  # протухший csrf — сайт вернул страницу, не JSON
            _json_response({"success": True, "data": {"count": 0, "results": []}}),
        ],
        log=log,
    )

    result = await session.search_by_plate("AA001AA")

    assert result["success"] is True
    assert [c["method"] for c in log.calls] == ["GET", "POST", "GET", "POST"]


async def test_repeated_failure_after_retry_raises_error():
    log = _CallLog()
    session = _make_session(
        pages=[_page_html("token-1"), _page_html("token-2")],
        posts=[
            _json_response({"success": False, "message": None, "data": []}),
            _json_response({"success": False, "message": None, "data": []}),
        ],
        log=log,
    )

    with pytest.raises(FineProviderError):
        await session.search_by_plate("AA001AA")

    # Ровно одна повторная попытка — GET/POST по два раза, не бесконечный retry.
    assert [c["method"] for c in log.calls] == ["GET", "POST", "GET", "POST"]


async def test_missing_csrf_token_on_page_raises_error():
    session = _make_session(pages=[_page_html(None)], posts=[], log=_CallLog())

    with pytest.raises(FineProviderError):
        await session.search_by_plate("AA001AA")


# ---- Production bug (O127PX62, 2026-10-21): transport-level httpx
# exceptions (ConnectTimeout и т.п.) должны оборачиваться в
# FineProviderError, а не падать unhandled мимо check_task()'s except
# FineProviderError — см. FineProvider.search_by_plate докстрок: "Бросает
# FineProviderError при сбое запроса к источнику" (сеть ИЛИ формат) ----


def _make_session_raising(
    exc: Exception, *, on_method: str = "GET", circuit_breaker: PoliceGeCircuitBreaker | None = None,
) -> PoliceGeSession:
    """on_method — на каком именно запросе (первом GET за csrf_token или
    последующем POST поиска) эмулируется сбой сети."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == on_method:
            raise exc
        return httpx.Response(200, text=_page_html("token-1"))

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(base_url="https://police.ge/protocol/", transport=transport)
    return PoliceGeSession(
        client, page_url=_PAGE_URL, request_timeout=5, circuit_breaker=circuit_breaker,
    )


async def test_connect_timeout_on_csrf_page_load_raises_fine_provider_error():
    """ТОЧНЫЙ production repro — httpx.ConnectTimeout на GET csrf-страницы
    (см. production traceback: police_ge_session.py:47 search_by_plate ->
    _refresh_csrf_token -> self._client.get(...))."""
    session = _make_session_raising(httpx.ConnectTimeout("connect timed out"), on_method="GET")

    with pytest.raises(FineProviderError) as excinfo:
        await session.search_by_plate("O127PX62")

    # Exception chaining сохранён (см. задачу "существующая logging/error
    # handling должна сохранить exception context") — не "проглочено".
    assert isinstance(excinfo.value.__cause__, httpx.ConnectTimeout)


async def test_connect_timeout_on_search_post_raises_fine_provider_error():
    """ТОЧНЫЙ production repro — httpx.ConnectTimeout на POST поиска (см.
    production traceback: police_ge_session.py:72 _try_search ->
    self._client.post(...))."""
    session = _make_session_raising(httpx.ConnectTimeout("connect timed out"), on_method="POST")

    with pytest.raises(FineProviderError) as excinfo:
        await session.search_by_plate("O127PX62")

    assert isinstance(excinfo.value.__cause__, httpx.ConnectTimeout)


async def test_read_timeout_raises_fine_provider_error():
    """Не только ConnectTimeout — ЛЮБОЙ httpx.HTTPError (см.
    reader/turkey_bot/gib/session.py — тот же проверенный паттерн, что уже
    используется для Turkey-провайдеров)."""
    session = _make_session_raising(httpx.ReadTimeout("read timed out"), on_method="POST")

    with pytest.raises(FineProviderError):
        await session.search_by_plate("O127PX62")


async def test_connect_error_raises_fine_provider_error():
    session = _make_session_raising(
        httpx.ConnectError("connection refused"), on_method="GET",
    )

    with pytest.raises(FineProviderError):
        await session.search_by_plate("O127PX62")


async def test_http_status_error_from_raise_for_status_raises_fine_provider_error():
    """5xx-ответ (raise_for_status() бросает httpx.HTTPStatusError,
    подкласс httpx.HTTPError) — та же обёртка, что и у транспортных сбоев."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=_page_html("token-1"))
        return httpx.Response(503, text="Service Unavailable")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(base_url="https://police.ge/protocol/", transport=transport)
    session = PoliceGeSession(client, page_url=_PAGE_URL, request_timeout=5)

    with pytest.raises(FineProviderError):
        await session.search_by_plate("O127PX62")


# ---- Circuit breaker integration (см. задачу "back off when police.ge
# is unavailable", production incident 2026-10-21) ----


async def test_transport_failure_records_against_circuit_breaker():
    breaker = PoliceGeCircuitBreaker(failure_threshold=3)
    session = _make_session_raising(
        httpx.ConnectTimeout("connect timed out"), on_method="GET", circuit_breaker=breaker,
    )

    with pytest.raises(FineProviderError):
        await session.search_by_plate("O127PX62")

    assert breaker.consecutive_failures == 1
    assert breaker.is_open is False


async def test_three_transport_failures_open_the_circuit_via_session():
    breaker = PoliceGeCircuitBreaker(failure_threshold=3)
    for _ in range(3):
        session = _make_session_raising(
            httpx.ConnectTimeout("connect timed out"), on_method="GET", circuit_breaker=breaker,
        )
        with pytest.raises(FineProviderError):
            await session.search_by_plate("O127PX62")

    assert breaker.is_open is True


async def test_open_circuit_rejects_immediately_without_any_http_call():
    """Главный regression-критерий (п.9.2): "while OPEN -> provider not
    called" — ни одного GET/POST не уходит, PoliceGeUnavailableError
    бросается СРАЗУ."""
    breaker = PoliceGeCircuitBreaker(failure_threshold=3)
    for _ in range(3):
        breaker.record_transport_failure()
    assert breaker.is_open is True

    log = _CallLog()
    session = _make_session(pages=["unused"], posts=[], log=log, circuit_breaker=breaker)

    with pytest.raises(PoliceGeUnavailableError):
        await session.search_by_plate("O127PX62")

    assert log.calls == []  # ни единого HTTP-запроса


async def test_parser_business_error_does_not_record_transport_failure():
    """П.9.8: "parser error does NOT open circuit" — csrf_token-не-найден
    это parser/business-ошибка (страница реально загрузилась), НЕ
    transport-сбой (см. задачу п.2: "Do NOT classify parser/business
    errors as connectivity failures")."""
    breaker = PoliceGeCircuitBreaker(failure_threshold=3)
    session = _make_session(pages=[_page_html(None)], posts=[], log=_CallLog(), circuit_breaker=breaker)

    with pytest.raises(FineProviderError):
        await session.search_by_plate("O127PX62")

    assert breaker.consecutive_failures == 0
    assert breaker.is_open is False


async def test_repeated_success_false_does_not_open_circuit():
    """success:false retry (та же parser/business категория) — тоже НЕ
    transport-сбой, даже после двух подряд (см.
    test_repeated_failure_after_retry_raises_error выше)."""
    breaker = PoliceGeCircuitBreaker(failure_threshold=3)
    session = _make_session(
        pages=[_page_html("token-1"), _page_html("token-2")],
        posts=[
            _json_response({"success": False, "message": None, "data": []}),
            _json_response({"success": False, "message": None, "data": []}),
        ],
        log=_CallLog(),
        circuit_breaker=breaker,
    )

    with pytest.raises(FineProviderError):
        await session.search_by_plate("AA001AA")

    assert breaker.consecutive_failures == 0
    assert breaker.is_open is False


async def test_successful_search_resets_circuit_breaker():
    """П.9.9/п.2: "A successful request resets circuit to CLOSED" — даже
    после пары transport-сбоев (не дошедших до порога), успешный поиск
    сбрасывает счётчик."""
    breaker = PoliceGeCircuitBreaker(failure_threshold=3)
    breaker.record_transport_failure()
    breaker.record_transport_failure()
    assert breaker.consecutive_failures == 2

    session = _make_session(
        pages=[_page_html("token-1")],
        posts=[_json_response({"success": True, "data": {"count": 0, "results": []}})],
        log=_CallLog(),
        circuit_breaker=breaker,
    )

    result = await session.search_by_plate("AA001AA")

    assert result["success"] is True
    assert breaker.consecutive_failures == 0
    assert breaker.is_open is False


# ==== fetch_published_protocols — публичный feed (см. задачу "Georgia
# fines incremental public-feed monitoring") ====


async def test_fetch_published_protocols_returns_parsed_json():
    log = _CallLog()
    session = _make_session(
        pages=[_page_html("token-1")],
        posts=[_json_response({"success": True, "data": {"count": 311185, "results": [{"protocolAuto": "AA001AA"}]}})],
        log=log,
    )

    result = await session.fetch_published_protocols(first_result=0)

    assert result["success"] is True
    assert result["data"]["count"] == 311185
    assert result["data"]["results"] == [{"protocolAuto": "AA001AA"}]


async def test_fetch_published_protocols_reuses_csrf_token_from_search_by_plate():
    """ТА ЖЕ сессия/csrf_token — переключение между обычным поиском и
    feed-страницами внутри одного прохода scheduler'а безопасно (см.
    PoliceGeSession докстрок про "общий csrf_token просто переиспользуется")."""
    log = _CallLog()
    session = _make_session(
        pages=[_page_html("token-1")],
        posts=[
            _json_response({"success": True, "data": {"count": 1, "results": []}}),
            _json_response({"success": True, "data": {"count": 1, "results": []}}),
        ],
        log=log,
    )

    await session.search_by_plate("AA001AA")
    await session.fetch_published_protocols(first_result=0)

    # Один GET за csrf (не два) — второй вызов переиспользовал токен.
    get_calls = [c for c in log.calls if c["method"] == "GET"]
    assert len(get_calls) == 1
    post_tokens = [c["csrf_token"] for c in log.calls if c["method"] == "POST"]
    assert post_tokens[0] == post_tokens[1]


async def test_fetch_published_protocols_sends_first_result_param():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=_page_html("token-1"))
        body = parse_qs(request.content.decode())
        captured["firstResult"] = body["firstResult"][0]
        return httpx.Response(200, json={"success": True, "data": {"count": 0, "results": []}})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(base_url="https://police.ge/protocol/", transport=transport)
    session = PoliceGeSession(client, page_url=_PAGE_URL, request_timeout=5)

    await session.fetch_published_protocols(first_result=90)

    assert captured["firstResult"] == "90"


async def test_fetch_published_protocols_raises_police_ge_unavailable_when_circuit_open():
    breaker = PoliceGeCircuitBreaker(failure_threshold=3)
    for _ in range(3):
        breaker.record_transport_failure()
    assert breaker.is_open is True

    log = _CallLog()
    session = _make_session(pages=["unused"], posts=[], log=log, circuit_breaker=breaker)

    with pytest.raises(PoliceGeUnavailableError):
        await session.fetch_published_protocols(first_result=0)

    assert log.calls == []  # ни единого HTTP-запроса


async def test_fetch_published_protocols_connect_timeout_raises_fine_provider_error():
    session = _make_session_raising(httpx.ConnectTimeout("connect timed out"), on_method="POST")

    with pytest.raises(FineProviderError) as excinfo:
        await session.fetch_published_protocols(first_result=0)

    assert isinstance(excinfo.value.__cause__, httpx.ConnectTimeout)


async def test_fetch_published_protocols_transport_failure_records_against_circuit_breaker():
    breaker = PoliceGeCircuitBreaker(failure_threshold=3)
    session = _make_session_raising(
        httpx.ConnectTimeout("connect timed out"), on_method="POST", circuit_breaker=breaker,
    )

    with pytest.raises(FineProviderError):
        await session.fetch_published_protocols(first_result=0)

    assert breaker.consecutive_failures == 1


# ---- with_timeout() (см. задачу "fix Georgia Check Now local timeout") --
# reader/public_bot/main.py::build_check_now_provider() uses this to give
# AUTO's local leg a short 3s probe without touching the shared/global
# request_timeout used everywhere else. ----

async def test_with_timeout_passes_new_timeout_to_requests():
    captured: list[float | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        timeout_info = request.extensions.get("timeout")
        captured.append(timeout_info["connect"] if timeout_info else None)
        if request.method == "GET":
            return httpx.Response(200, text=_page_html("tok"))
        return _json_response({"success": True, "data": {"count": 0, "results": []}})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(base_url="https://police.ge/protocol/", transport=transport)
    session = PoliceGeSession(client, page_url=_PAGE_URL, request_timeout=30)

    short_session = session.with_timeout(3.0)
    await short_session.search_by_plate("O127PX62")

    assert captured == [3.0, 3.0]  # GET (csrf) + POST (search), both at the new timeout


async def test_with_timeout_does_not_mutate_the_original_session():
    log = _CallLog()
    session = _make_session(
        pages=[_page_html("tok")],
        posts=[_json_response({"success": True, "data": {"count": 0, "results": []}})],
        log=log,
    )  # constructed with request_timeout=5 (see _make_session)

    session.with_timeout(3.0)  # must not affect `session` itself
    await session.search_by_plate("O127PX62")  # still usable at its own original timeout

    assert len(log.calls) == 2  # GET + POST happened normally on the ORIGINAL session


async def test_with_timeout_shares_the_same_circuit_breaker():
    """The whole point of sharing (not duplicating) the breaker: a failure
    recorded via one timeout variant must open the circuit for the OTHER
    variant too -- "if the local circuit breaker is already open, REDNET
    fallback remains immediate" only holds if there is exactly one breaker
    state, not two independent ones."""
    breaker = PoliceGeCircuitBreaker(failure_threshold=3)
    for _ in range(3):
        breaker.record_transport_failure()
    assert breaker.is_open is True

    log = _CallLog()
    session = _make_session(pages=["unused"], posts=[], log=log, circuit_breaker=breaker)
    short_session = session.with_timeout(3.0)

    with pytest.raises(PoliceGeUnavailableError):
        await short_session.search_by_plate("O127PX62")

    assert log.calls == []  # rejected before any HTTP call, via the SHARED breaker
