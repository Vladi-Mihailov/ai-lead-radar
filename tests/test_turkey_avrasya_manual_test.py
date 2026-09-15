"""
Тесты reader/turkey_bot/avrasya/manual_test.py — единственного места, где
человек вручную проходит цикл plate -> CAPTCHA -> код -> результат (см.
design report Stage 1). Реальная сеть НЕ используется: httpx.AsyncClient
конструируется поверх httpx.MockTransport (тот же приём, что и
tests/test_turkey_avrasya_session.py) — manual_test.httpx.AsyncClient
подменяется на фабрику, игнорирующую timeout/headers, но реально
использующую MockTransport, чтобы не трогать сеть вовсе. input()
подменяется, чтобы не блокироваться на реальной клавиатуре — сам код
CAPTCHA НИКОГДА не должен появляться в stdout/stderr (см. задачу).
"""

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402
import pytest  # noqa: E402

import reader.turkey_bot.avrasya.session as avrasya_session  # noqa: E402
from reader.turkey_bot.avrasya import manual_test  # noqa: E402

_PAGE_URL = "https://www.avrasyatuneli.com/ihlalli-gecis-odemesi/"
_CAPTCHA_URL = "https://www.avrasyatuneli.com/Captcha.ashx?uid=DebtQueryByPlate"
_QUERY_URL = "https://www.avrasyatuneli.com/api/debt/query"

_SAMPLE_JPEG = bytes.fromhex("ffd8ffe000104a4649460001010100480048" + "00" * 20 + "ffd9")

_SECRET_CAPTCHA_CODE = "s3cr3t-code-42"


@pytest.fixture(autouse=True)
def _no_throttle_delay(monkeypatch):
    monkeypatch.setattr(avrasya_session, "_MIN_REQUEST_INTERVAL_SECONDS", 0.0)


def _patch_client_transport(monkeypatch, handler) -> None:
    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        return real_async_client(transport=transport)

    monkeypatch.setattr(manual_test.httpx, "AsyncClient", fake_client)


def _happy_handler(*, submit_status: int, submit_body):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == _PAGE_URL:
            return httpx.Response(200, text="<html></html>")
        if url == _CAPTCHA_URL:
            return httpx.Response(200, content=_SAMPLE_JPEG, headers={"content-type": "image/jpeg"})
        if url == _QUERY_URL:
            if isinstance(submit_body, dict):
                return httpx.Response(submit_status, json=submit_body)
            return httpx.Response(submit_status, text=str(submit_body))
        raise AssertionError(f"unexpected request to {url}")

    return handler


async def test_run_saves_captcha_prints_only_path_and_never_the_code(monkeypatch, capsys):
    _patch_client_transport(
        monkeypatch,
        _happy_handler(
            submit_status=400,
            submit_body={
                "Messages": [
                    {
                        "PropertyName": "Captcha",
                        "ErrorMessage": "Güvenlik kodunu doğru girdiğinizden emin olunuz.",
                    }
                ]
            },
        ),
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: _SECRET_CAPTCHA_CODE)

    exit_code = await manual_test._run("A123AA123")

    out = capsys.readouterr().out
    assert exit_code == 0
    assert _SECRET_CAPTCHA_CODE not in out

    lines = out.splitlines()
    saved_path = Path(lines[0])
    assert saved_path.exists()
    assert saved_path.read_bytes() == _SAMPLE_JPEG
    saved_path.unlink()

    assert "status_code: 400" in out
    assert "kind: rejected" in out
    assert "property_name='Captcha'" in out


async def test_run_normalizes_cyrillic_russian_plate_via_shared_validation(monkeypatch):
    """См. задачу: "Russian plate normalization must reuse the existing
    Turkey bot normalize_plate() implementation" — не своя копия."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == _PAGE_URL:
            return httpx.Response(200, text="<html></html>")
        if url == _CAPTCHA_URL:
            return httpx.Response(200, content=_SAMPLE_JPEG, headers={"content-type": "image/jpeg"})
        if url == _QUERY_URL:
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(400, json={"Messages": []})
        raise AssertionError(f"unexpected request to {url}")

    _patch_client_transport(monkeypatch, handler)
    monkeypatch.setattr("builtins.input", lambda _prompt: "123456")

    await manual_test._run("А123АА123")

    assert captured["body"]["queryValue"] == "A123AA123"


async def test_run_rejects_implausible_plate_before_any_network_call(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        raise AssertionError("should not make any network call for an invalid plate")

    _patch_client_transport(monkeypatch, handler)

    exit_code = await manual_test._run("!!!")

    assert exit_code == 2
    assert calls == []


async def test_run_aborts_when_no_code_entered(monkeypatch, capsys):
    _patch_client_transport(
        monkeypatch, _happy_handler(submit_status=400, submit_body={"Messages": []}),
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "   ")

    exit_code = await manual_test._run("A123AA123")

    assert exit_code == 1
    # Ни submit, ни его результат не должны были случиться.
    assert "status_code" not in capsys.readouterr().out


async def test_run_reports_rate_limit_without_leaking_body(monkeypatch, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == _PAGE_URL:
            return httpx.Response(200, text="<html></html>")
        if url == _CAPTCHA_URL:
            return httpx.Response(200, content=_SAMPLE_JPEG, headers={"content-type": "image/jpeg"})
        if url == _QUERY_URL:
            return httpx.Response(429, text="API calls quota exceeded! maximum admitted 1 per Second.")
        raise AssertionError(f"unexpected request to {url}")

    _patch_client_transport(monkeypatch, handler)
    monkeypatch.setattr("builtins.input", lambda _prompt: "123456")

    exit_code = await manual_test._run("A123AA123")

    assert exit_code == 1
    assert "Rate limited" in capsys.readouterr().err


async def test_run_reports_transport_error_when_captcha_fetch_fails(monkeypatch, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == _PAGE_URL:
            return httpx.Response(500, text="boom")
        raise AssertionError(f"unexpected request to {url}")

    _patch_client_transport(monkeypatch, handler)

    exit_code = await manual_test._run("A123AA123")

    assert exit_code == 1
    assert "Transport error" in capsys.readouterr().err
