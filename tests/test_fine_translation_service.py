"""
Тесты reader/fines/translation.py — FineTranslationService/contains_georgian.
Ни один тест не обращается к настоящему OpenAI API: client.responses.parse
подменяется напрямую (тот же приём, что и tests/test_lead_ai_service.py/
tests/test_ocr_service.py), сеть не используется вовсе.
"""

import sys
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402
import openai  # noqa: E402
import pytest  # noqa: E402

from reader.fines.translation import (  # noqa: E402
    FineTranslationError,
    FineTranslationService,
    TranslatedFineText,
    contains_georgian,
)

_REQUEST = httpx.Request("POST", "https://api.openai.com/v1/responses")


def _service() -> FineTranslationService:
    return FineTranslationService(api_key="test-key-not-real", model="gpt-5-mini")


def _fake_parse_returning(result: TranslatedFineText):
    async def fake_parse(**kwargs):
        return types.SimpleNamespace(output_parsed=result)

    return fake_parse


async def _instant_sleep(_seconds):
    return None


# ---- contains_georgian: определяем, что реально нужно переводить ----


def test_contains_georgian_true_for_georgian_text():
    assert contains_georgian("სამტრედია-გრიგოლეთი 26კმ") is True


def test_contains_georgian_false_for_russian_text():
    assert contains_georgian("Тбилиси, проспект Руставели") is False


def test_contains_georgian_false_for_latin_text():
    assert contains_georgian("Tbilisi, Rustaveli Ave") is False


def test_contains_georgian_false_for_none():
    assert contains_georgian(None) is False


def test_contains_georgian_false_for_empty_string():
    assert contains_georgian("") is False


def test_contains_georgian_true_for_mixed_text_with_some_georgian():
    assert contains_georgian("125-1-1 ასკ მუხლი") is True


# ---- translate(): не отправляем на перевод без необходимости ----


async def test_translate_with_both_fields_none_makes_no_api_call(monkeypatch):
    service = _service()
    calls = {"count": 0}

    async def fake_parse(**kwargs):
        calls["count"] += 1
        return types.SimpleNamespace(output_parsed=TranslatedFineText())

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    result = await service.translate(place=None, violation_description=None)

    assert calls["count"] == 0
    assert result == TranslatedFineText()


async def test_translate_both_fields_in_one_request(monkeypatch):
    """"Для одного штрафа желательно переводить place + violation_description
    одним запросом" — один вызов parse(), оба поля в результате."""
    service = _service()
    calls = []
    result = TranslatedFineText(
        place_ru="Самтредиа-Григолети 26км", violation_description_ru="Статья 125-1-1",
    )

    async def fake_parse(**kwargs):
        calls.append(kwargs)
        return types.SimpleNamespace(output_parsed=result)

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    translated = await service.translate(
        place="სამტრედია-გრიგოლეთი 26კმ", violation_description="ასკ 125-ე მუხლის პირველის პრიმა ნაწილი",
    )

    assert len(calls) == 1
    assert translated.place_ru == "Самтредиа-Григолети 26км"
    assert translated.violation_description_ru == "Статья 125-1-1"
    user_content = calls[0]["input"][0]["content"]
    assert "МЕСТО" in user_content
    assert "НАРУШЕНИЕ" in user_content


async def test_translate_place_only_omits_violation_description_from_request(monkeypatch):
    service = _service()
    calls = []

    async def fake_parse(**kwargs):
        calls.append(kwargs)
        return types.SimpleNamespace(output_parsed=TranslatedFineText(place_ru="Место"))

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    await service.translate(place="ადგილი", violation_description=None)

    user_content = calls[0]["input"][0]["content"]
    assert "МЕСТО" in user_content
    assert "НАРУШЕНИЕ" not in user_content


# ---- транспорт/ошибки — тот же приём, что и LeadAiService/OcrService ----


async def test_translate_raises_when_model_returns_no_parsed_output(monkeypatch):
    service = _service()

    async def fake_parse(**kwargs):
        return types.SimpleNamespace(output_parsed=None)

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    with pytest.raises(FineTranslationError):
        await service.translate(place="ტექსტი", violation_description=None)


async def test_translate_retries_once_on_rate_limit_then_succeeds(monkeypatch):
    service = _service()
    resp = httpx.Response(429, request=_REQUEST)
    calls = {"count": 0}
    result = TranslatedFineText(place_ru="Место")

    async def fake_parse(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise openai.RateLimitError("rate limited", response=resp, body=None)
        return types.SimpleNamespace(output_parsed=result)

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)
    monkeypatch.setattr("reader.fines.translation.asyncio.sleep", _instant_sleep)

    translated = await service.translate(place="ადგილი", violation_description=None)

    assert calls["count"] == 2
    assert translated.place_ru == "Место"


async def test_translate_gives_up_after_one_retry_on_persistent_failure(monkeypatch):
    service = _service()
    resp = httpx.Response(429, request=_REQUEST)

    async def fake_parse(**kwargs):
        raise openai.RateLimitError("rate limited", response=resp, body=None)

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)
    monkeypatch.setattr("reader.fines.translation.asyncio.sleep", _instant_sleep)

    with pytest.raises(FineTranslationError):
        await service.translate(place="ადგილი", violation_description=None)


async def test_translate_does_not_retry_non_5xx_status_error(monkeypatch):
    service = _service()
    resp = httpx.Response(400, request=_REQUEST)
    calls = {"count": 0}

    async def fake_parse(**kwargs):
        calls["count"] += 1
        raise openai.APIStatusError("bad request", response=resp, body=None)

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    with pytest.raises(FineTranslationError):
        await service.translate(place="ადგილი", violation_description=None)

    assert calls["count"] == 1
