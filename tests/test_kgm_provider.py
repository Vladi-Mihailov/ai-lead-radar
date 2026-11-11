"""
Тесты KgmProvider (см. reader/turkey_bot/kgm/provider.py) — диагностическая
строка лога на kind == "unexpected" (см. задачу "KGM diagnostic logging"):
категория/код/безопасная деталь/время ответа сервера/размер ответа — да;
госномер, CAPTCHA, message (может нести текст сайта) и сам ответ — никогда.
Результат проверки возвращается без изменений. Реальной сети нет — fake
session вместо KgmSession.
"""

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.turkey_bot.kgm.parser import parse_submit_response  # noqa: E402
from reader.turkey_bot.kgm.provider import KgmProvider  # noqa: E402

_FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures"
_SUCCESS_DELTA = (_FIXTURES_DIR / "kgm_sorgulama_m295yb196_delta.txt").read_text(encoding="utf-8")
_NO_DEBT_DELTA = (_FIXTURES_DIR / "kgm_sorgulama_a123aa180_no_debt_delta.txt").read_text(encoding="utf-8")

_LOGGER = "reader.turkey_bot.kgm.provider"
_PLATE = "34ABC123"
_CAPTCHA = "54 32109"


class _FakeSession:
    """Только то, что KgmProvider.submit() берёт у KgmSession: submit() и
    last_submit_seconds (выставляется самим submit(), как в KgmSession)."""

    def __init__(self, response_text: str, *, server_seconds: float | None = 9.51):
        self._response_text = response_text
        self._server_seconds = server_seconds
        self.last_submit_seconds: float | None = None

    async def submit(self, *, plate: str, captcha_code: str) -> str:
        self.last_submit_seconds = self._server_seconds
        return self._response_text


def _panel(html: str) -> str:
    return f"{len(html)}|updatePanel|pnlSayfa|{html}|"


def _provider_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == _LOGGER and r.levelno == logging.WARNING]


async def test_unexpected_logs_one_safe_line_and_returns_outcome_unchanged(caplog):
    site_text = f"Sistem hatası, daha sonra tekrar deneyiniz ({_PLATE})"
    response = _panel(f'<span id="lblHata">{site_text}</span>')
    provider = KgmProvider(_FakeSession(response))

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        outcome = await provider.submit(plate=_PLATE, captcha_code=_CAPTCHA)

    assert outcome == parse_submit_response(response)
    assert outcome.kind == "unexpected"
    assert _provider_warnings(caplog) == [
        "KGM unexpected response: category=provider_message code=lblhata_unrecognized "
        f"detail=chars={len(site_text)} digits=5 keywords=hata+tekrar_deneyin+sistem "
        f"server_seconds=9.51 response_chars={len(response)}"
    ]


async def test_unexpected_log_never_contains_plate_captcha_site_text_or_html(caplog):
    redirect_url = f"/Hata.aspx?p={_PLATE}"
    responses = [
        _panel(f'<span id="lblHata">Sistem hatası ({_PLATE})</span>'),
        f"<html><head><title>Request Rejected</title></head><body>{_PLATE}</body></html>",
        f"1|#||4|{len(redirect_url)}|pageRedirect||{redirect_url}|",
    ]

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        for response in responses:
            await KgmProvider(_FakeSession(response)).submit(plate=_PLATE, captcha_code=_CAPTCHA)

    messages = _provider_warnings(caplog)
    assert len(messages) == len(responses)
    for message in messages:
        for forbidden in (_PLATE, _CAPTCHA, "Sistem hatası", "Request Rejected", "Hata.aspx", "<", "'"):
            assert forbidden not in message, f"{forbidden!r} leaked into log: {message!r}"


async def test_unexpected_without_server_time_logs_dash(caplog):
    response = "garbage not a delta response"
    provider = KgmProvider(_FakeSession(response, server_seconds=None))

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        await provider.submit(plate=_PLATE, captcha_code=_CAPTCHA)

    assert _provider_warnings(caplog) == [
        "KGM unexpected response: category=response_format code=delta_malformed "
        "detail=chars=28 shape=other server_seconds=- response_chars=28"
    ]


async def test_has_debt_no_debt_and_rejected_log_nothing(caplog):
    responses = [_SUCCESS_DELTA, _NO_DEBT_DELTA, _panel('<span id="lblHata">metin hatası</span>')]

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        kinds = [
            (await KgmProvider(_FakeSession(response)).submit(plate=_PLATE, captcha_code=_CAPTCHA)).kind
            for response in responses
        ]

    assert kinds == ["has_debt", "no_debt", "rejected"]
    assert _provider_warnings(caplog) == []
