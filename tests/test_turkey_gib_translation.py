"""
Тесты reader/turkey_bot/gib/translation.py — TurkeyFineTranslationService.
Ни один тест не обращается к настоящему OpenAI API: client.responses.parse
подменяется напрямую (тот же приём, что и
tests/test_fine_translation_service.py — грузинский аналог), сеть не
используется вовсе.
"""

import sys
import types
from datetime import date
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402
import openai  # noqa: E402
import pytest  # noqa: E402

from reader.turkey_bot.gib.models import GibFineRecord  # noqa: E402
from reader.turkey_bot.gib.translation import (  # noqa: E402
    FineTranslationError,
    TurkeyFineTranslationService,
    _TranslatedFineItem,
    _TranslatedFinesBatch,
)

_REQUEST = httpx.Request("POST", "https://api.openai.com/v1/responses")


def _service() -> TurkeyFineTranslationService:
    return TurkeyFineTranslationService(api_key="test-key-not-real", model="gpt-5-mini")


def _fine(**overrides) -> GibFineRecord:
    defaults = {
        "protocol_no": "MC00000000",
        "plate": "34ABC123",
        "amount": Decimal("1000.00"),
        "description": "raw description",
        "violation_date": date(2026, 8, 8),
        "authority": "EMNİYET GENEL MÜDÜRLÜĞÜ",
        "late_fee": None,
        "discount": None,
        "location": "Örnek konum",
        "law_article": "51/2-B-2",
        "violation_description": "Örnek ihlal",
        "location_ru": None,
        "violation_description_ru": None,
    }
    defaults.update(overrides)
    return GibFineRecord(**defaults)


async def _instant_sleep(_seconds):
    return None


# ---- translate_fines(): не отправляем на перевод без необходимости ----


async def test_no_fine_has_structured_fields_makes_no_api_call(monkeypatch):
    service = _service()
    calls = {"count": 0}

    async def fake_parse(**kwargs):
        calls["count"] += 1
        return types.SimpleNamespace(output_parsed=_TranslatedFinesBatch(items=[]))

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    fines = (_fine(location=None, violation_description=None),)
    result = await service.translate_fines(fines)

    assert calls["count"] == 0
    assert result == fines


async def test_translates_location_and_violation_description_in_one_call():
    service = _service()
    calls = []
    batch = _TranslatedFinesBatch(
        items=[_TranslatedFineItem(index=0, location_ru="Место РУ", violation_description_ru="Нарушение РУ")]
    )

    async def fake_parse(**kwargs):
        calls.append(kwargs)
        return types.SimpleNamespace(output_parsed=batch)

    service._client.responses.parse = fake_parse

    fines = (_fine(),)
    result = await service.translate_fines(fines)

    assert len(calls) == 1
    assert result[0].location_ru == "Место РУ"
    assert result[0].violation_description_ru == "Нарушение РУ"
    # Турецкие оригиналы и остальные поля не тронуты.
    assert result[0].location == "Örnek konum"
    assert result[0].protocol_no == "MC00000000"
    assert result[0].amount == Decimal("1000.00")

    user_content = calls[0]["input"][0]["content"]
    assert "МЕСТО" in user_content
    assert "НАРУШЕНИЕ" in user_content


async def test_batches_multiple_fines_into_a_single_request():
    service = _service()
    calls = []
    batch = _TranslatedFinesBatch(
        items=[
            _TranslatedFineItem(index=0, location_ru="Место 1"),
            _TranslatedFineItem(index=1, location_ru="Место 2"),
        ]
    )

    async def fake_parse(**kwargs):
        calls.append(kwargs)
        return types.SimpleNamespace(output_parsed=batch)

    service._client.responses.parse = fake_parse

    fines = (_fine(location="Yer 1", violation_description=None), _fine(location="Yer 2", violation_description=None))
    result = await service.translate_fines(fines)

    assert len(calls) == 1  # ОДИН запрос на ВСЕ штрафы, не по одному на каждый
    assert result[0].location_ru == "Место 1"
    assert result[1].location_ru == "Место 2"


async def test_fine_without_any_structured_field_is_skipped_from_request_but_kept_in_result():
    service = _service()
    calls = []
    batch = _TranslatedFinesBatch(items=[_TranslatedFineItem(index=0, location_ru="Место 1")])

    async def fake_parse(**kwargs):
        calls.append(kwargs)
        return types.SimpleNamespace(output_parsed=batch)

    service._client.responses.parse = fake_parse

    translatable = _fine(location="Yer 1", violation_description=None)
    not_translatable = _fine(location=None, violation_description=None, protocol_no="SKIP0000")
    result = await service.translate_fines((translatable, not_translatable))

    assert len(result) == 2
    assert result[0].location_ru == "Место 1"
    assert result[1].location_ru is None  # не отправлялся - не переведён, но сохранён как есть
    assert result[1].protocol_no == "SKIP0000"

    user_content = calls[0]["input"][0]["content"]
    assert "Yer 1" in user_content
    assert "SKIP0000" not in user_content  # protocol_no никогда не отправляется


async def test_only_location_present_omits_violation_description_from_request():
    service = _service()
    calls = []

    async def fake_parse(**kwargs):
        calls.append(kwargs)
        return types.SimpleNamespace(
            output_parsed=_TranslatedFinesBatch(items=[_TranslatedFineItem(index=0, location_ru="X")])
        )

    service._client.responses.parse = fake_parse

    await service.translate_fines((_fine(location="Yer", violation_description=None),))

    user_content = calls[0]["input"][0]["content"]
    assert "МЕСТО" in user_content
    assert "НАРУШЕНИЕ" not in user_content


async def test_mismatched_index_in_response_is_ignored_defensively():
    """Индекс вне диапазона входных штрафов не должен ничего портить -
    сопоставление по index (см. reader/turkey_bot/gib/translation.py),
    не по позиции."""
    service = _service()
    batch = _TranslatedFinesBatch(items=[_TranslatedFineItem(index=99, location_ru="Nowhere")])

    async def fake_parse(**kwargs):
        return types.SimpleNamespace(output_parsed=batch)

    service._client.responses.parse = fake_parse

    fines = (_fine(),)
    result = await service.translate_fines(fines)

    assert result[0].location_ru is None  # индекс 99 не совпал ни с чем


# ---- никакие чувствительные/нерелевантные поля не отправляются ----


async def test_never_sends_plate_amount_protocol_or_authority_to_openai():
    service = _service()
    calls = []

    async def fake_parse(**kwargs):
        calls.append(kwargs)
        return types.SimpleNamespace(
            output_parsed=_TranslatedFinesBatch(items=[_TranslatedFineItem(index=0)])
        )

    service._client.responses.parse = fake_parse

    fine = _fine(
        plate="SECRET-PLATE-000",
        protocol_no="SECRET-PROTOCOL",
        amount=Decimal("999999.99"),
        authority="SECRET-AUTHORITY",
    )
    await service.translate_fines((fine,))

    user_content = calls[0]["input"][0]["content"]
    for forbidden in ("SECRET-PLATE-000", "SECRET-PROTOCOL", "999999.99", "SECRET-AUTHORITY"):
        assert forbidden not in user_content


# ---- транспорт/ошибки — тот же приём, что и FineTranslationService ----


async def test_raises_when_model_returns_no_parsed_output():
    service = _service()

    async def fake_parse(**kwargs):
        return types.SimpleNamespace(output_parsed=None)

    service._client.responses.parse = fake_parse

    with pytest.raises(FineTranslationError):
        await service.translate_fines((_fine(),))


async def test_retries_once_on_rate_limit_then_succeeds(monkeypatch):
    service = _service()
    resp = httpx.Response(429, request=_REQUEST)
    calls = {"count": 0}
    batch = _TranslatedFinesBatch(items=[_TranslatedFineItem(index=0, location_ru="Место")])

    async def fake_parse(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise openai.RateLimitError("rate limited", response=resp, body=None)
        return types.SimpleNamespace(output_parsed=batch)

    service._client.responses.parse = fake_parse
    monkeypatch.setattr("reader.turkey_bot.gib.translation.asyncio.sleep", _instant_sleep)

    result = await service.translate_fines((_fine(),))

    assert calls["count"] == 2
    assert result[0].location_ru == "Место"


async def test_gives_up_after_one_retry_on_persistent_failure(monkeypatch):
    service = _service()
    resp = httpx.Response(429, request=_REQUEST)

    async def fake_parse(**kwargs):
        raise openai.RateLimitError("rate limited", response=resp, body=None)

    service._client.responses.parse = fake_parse
    monkeypatch.setattr("reader.turkey_bot.gib.translation.asyncio.sleep", _instant_sleep)

    with pytest.raises(FineTranslationError):
        await service.translate_fines((_fine(),))


async def test_does_not_retry_non_5xx_status_error():
    service = _service()
    resp = httpx.Response(400, request=_REQUEST)
    calls = {"count": 0}

    async def fake_parse(**kwargs):
        calls["count"] += 1
        raise openai.APIStatusError("bad request", response=resp, body=None)

    service._client.responses.parse = fake_parse

    with pytest.raises(FineTranslationError):
        await service.translate_fines((_fine(),))

    assert calls["count"] == 1
