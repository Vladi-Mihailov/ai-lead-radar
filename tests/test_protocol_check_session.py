"""Тесты VideosPoliceGeSession — csrf/retry (см. задачу "Проверить
протокол"). Реальная сеть не используется: httpx.AsyncClient подключён к
httpx.MockTransport (тот же приём, что и tests/test_police_ge_session.py).
"""

import sys
from pathlib import Path
from urllib.parse import parse_qs

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx
import pytest

from reader.public_bot.protocol_check_models import (
    ProtocolCheckSessionError,
)
from reader.public_bot.protocol_check_session import VideosPoliceGeSession

_NOT_FOUND_HTML = (
    '<form id="form"><input type="hidden" name="csrf_token" value="{token}"></form>'
    '<div class="value warning">Administrative violations have not been found.</div>'
)
_SESSION_EXPIRED_HTML = (
    '<form id="form"><input type="hidden" name="csrf_token" value="{token}"></form>'
    '<div class="value warning">Session expired please try again</div>'
)


def _page_html(token: str) -> str:
    return f'<form id="form"><input type="hidden" name="csrf_token" value="{token}"></form>'


class _CallLog:
    def __init__(self):
        self.calls: list[dict] = []


def _make_session(pages: list[str], posts: list[str], log: _CallLog) -> VideosPoliceGeSession:
    page_iter = iter(pages)
    post_iter = iter(posts)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            log.calls.append({"method": "GET", "path": request.url.path})
            return httpx.Response(200, text=next(page_iter))

        body = parse_qs(request.content.decode(), keep_blank_values=True)
        log.calls.append({
            "method": "POST",
            "path": request.url.path,
            "csrf_token": body.get("csrf_token", [None])[0],
            "fields": {k: v[0] for k, v in body.items()},
        })
        return httpx.Response(200, text=next(post_iter))

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(base_url="https://videos.police.ge", transport=transport)
    return VideosPoliceGeSession(client, request_timeout=5)


async def test_search_sends_get_then_post_with_csrf_token():
    log = _CallLog()
    session = _make_session(
        pages=[_page_html("token-1")],
        posts=[_NOT_FOUND_HTML.format(token="token-2")],
        log=log,
    )

    html = await session.search({"vehicleNo2": "AA001AA", "documentNo": "DOC1"})

    assert "have not been found" in html
    assert [c["method"] for c in log.calls] == ["GET", "POST"]
    assert log.calls[0]["path"] == "/index.php"
    assert log.calls[1]["path"] == "/submit-index.php"
    assert log.calls[1]["csrf_token"] == "token-1"


async def test_search_fills_unused_fields_with_empty_strings():
    """HTTP contract (см. задачу п.5): все 4 поля формы всегда отправляются,
    неиспользуемая пара — пустой строкой."""
    log = _CallLog()
    session = _make_session(
        pages=[_page_html("token-1")],
        posts=[_NOT_FOUND_HTML.format(token="token-1")],
        log=log,
    )

    await session.search({"vehicleNo2": "AA001AA", "documentNo": "DOC1"})

    fields = log.calls[1]["fields"]
    assert fields["vehicleNo2"] == "AA001AA"
    assert fields["documentNo"] == "DOC1"
    assert fields["protocolNo"] == ""
    assert fields["personalNo"] == ""
    assert fields["lang"] == "en"


async def test_search_protocol_variant_fills_correct_fields():
    log = _CallLog()
    session = _make_session(
        pages=[_page_html("token-1")],
        posts=[_NOT_FOUND_HTML.format(token="token-1")],
        log=log,
    )

    await session.search({"protocolNo": "PR1", "personalNo": "ID1"})

    fields = log.calls[1]["fields"]
    assert fields["protocolNo"] == "PR1"
    assert fields["personalNo"] == "ID1"
    assert fields["vehicleNo2"] == ""
    assert fields["documentNo"] == ""


async def test_session_expired_triggers_exactly_one_retry_then_succeeds():
    log = _CallLog()
    session = _make_session(
        pages=[_page_html("token-1"), _page_html("token-2")],
        posts=[
            _SESSION_EXPIRED_HTML.format(token="token-1"),
            _NOT_FOUND_HTML.format(token="token-2"),
        ],
        log=log,
    )

    html = await session.search({"vehicleNo2": "AA001AA", "documentNo": "DOC1"})

    assert "have not been found" in html
    assert [c["method"] for c in log.calls] == ["GET", "POST", "GET", "POST"]
    post_tokens = [c["csrf_token"] for c in log.calls if c["method"] == "POST"]
    assert post_tokens == ["token-1", "token-2"]


async def test_session_expired_twice_raises_error_without_further_retry():
    log = _CallLog()
    session = _make_session(
        pages=[_page_html("token-1"), _page_html("token-2")],
        posts=[
            _SESSION_EXPIRED_HTML.format(token="token-1"),
            _SESSION_EXPIRED_HTML.format(token="token-2"),
        ],
        log=log,
    )

    with pytest.raises(ProtocolCheckSessionError):
        await session.search({"vehicleNo2": "AA001AA", "documentNo": "DOC1"})

    # Ровно одна повторная попытка — GET/POST по два раза, не бесконечный retry.
    assert [c["method"] for c in log.calls] == ["GET", "POST", "GET", "POST"]


async def test_missing_csrf_token_on_page_raises_error():
    session = _make_session(pages=["<html>no token here</html>"], posts=[], log=_CallLog())

    with pytest.raises(ProtocolCheckSessionError):
        await session.search({"vehicleNo2": "AA001AA", "documentNo": "DOC1"})


async def test_each_search_call_fetches_a_fresh_csrf_token():
    """См. protocol_check_session.py докстрок — csrf_token НЕ кэшируется
    между разными search() вызовами (в отличие от PoliceGeSession) —
    каждый поиск получает свежий токен перед отправкой."""
    log = _CallLog()
    session = _make_session(
        pages=[_page_html("token-1"), _page_html("token-2")],
        posts=[
            _NOT_FOUND_HTML.format(token="token-1"),
            _NOT_FOUND_HTML.format(token="token-2"),
        ],
        log=log,
    )

    await session.search({"vehicleNo2": "AA001AA", "documentNo": "DOC1"})
    await session.search({"vehicleNo2": "BB002BB", "documentNo": "DOC2"})

    get_calls = [c for c in log.calls if c["method"] == "GET"]
    assert len(get_calls) == 2
