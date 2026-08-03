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

from reader.fines.police_ge_session import PoliceGeSession  # noqa: E402
from reader.fines.provider import FineProviderError  # noqa: E402

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


def _make_session(pages: list[str], posts: list[httpx.Response], log: _CallLog) -> PoliceGeSession:
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
    return PoliceGeSession(client, page_url=_PAGE_URL, request_timeout=5)


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
