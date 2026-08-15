"""
Тесты reader/ocr/service.py::OcrService — ни один тест не обращается к
настоящему OpenAI API: client.responses.parse подменяется напрямую (по
аналогии с auto-insurance/tests/test_ocr_provider.py), сеть не используется
вовсе."""

import sys
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402
import openai  # noqa: E402
import pytest  # noqa: E402

from reader.ocr.service import OcrService, OcrServiceError  # noqa: E402

_REQUEST = httpx.Request("POST", "https://api.openai.com/v1/responses")


def _parsed(**overrides):
    fields = dict(
        owner_full_name="Иванов Иван Иванович",
        driver_full_name="Петров Пётр Петрович",
        policyholder_full_name="Петров Пётр Петрович",
        registration_number="A123BC777", vin="JTMBR12345678901", chassis_number=None,
        manufacturer="Toyota", model="RAV4",
    )
    fields.update(overrides)
    return types.SimpleNamespace(**fields)


def _service() -> OcrService:
    return OcrService(api_key="test-key-not-real", model="gpt-5-mini")


async def test_extract_parses_full_result(monkeypatch):
    service = _service()
    fake_response = types.SimpleNamespace(output_parsed=_parsed())

    async def fake_parse(**kwargs):
        return fake_response

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    result = await service.extract([(b"fake-image-bytes", "image/jpeg")])

    assert result.registration_number == "A123BC777"
    assert result.vin == "JTMBR12345678901"
    assert result.chassis_number is None
    assert result.manufacturer == "Toyota"
    assert result.model == "RAV4"
    assert result.owner_full_name == "Иванов Иван Иванович"
    assert result.driver_full_name == "Петров Пётр Петрович"
    assert result.policyholder_full_name == "Петров Пётр Петрович"
    assert result.fields_found_count == 7


async def test_extract_strips_whitespace_from_fields(monkeypatch):
    service = _service()
    fake_response = types.SimpleNamespace(output_parsed=_parsed(registration_number="  A123BC777  "))

    async def fake_parse(**kwargs):
        return fake_response

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    result = await service.extract([(b"bytes", "image/jpeg")])
    assert result.registration_number == "A123BC777"


async def test_extract_partial_result_leaves_missing_fields_none(monkeypatch):
    service = _service()
    fake_response = types.SimpleNamespace(
        output_parsed=_parsed(
            vin=None, chassis_number=None,
            owner_full_name=None, driver_full_name=None, policyholder_full_name=None,
        )
    )

    async def fake_parse(**kwargs):
        return fake_response

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    result = await service.extract([(b"bytes", "image/jpeg")])

    assert result.registration_number == "A123BC777"
    assert result.vin is None
    assert result.chassis_number is None
    assert result.owner_full_name is None
    assert result.driver_full_name is None
    assert result.policyholder_full_name is None
    assert result.fields_found_count == 3


async def test_extract_owner_full_name_is_none_for_legal_entity_owner(monkeypatch):
    """owner_full_name = null, когда собственник в техпаспорте — юрлицо (см.
    reader/ocr/prompt.py) — на уровне OcrService это просто передача через
    None, сама классификация "юрлицо vs физлицо" — задача модели/промпта,
    не кода."""
    service = _service()
    fake_response = types.SimpleNamespace(output_parsed=_parsed(owner_full_name=None))

    async def fake_parse(**kwargs):
        return fake_response

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    result = await service.extract([(b"bytes", "image/jpeg")])

    assert result.owner_full_name is None
    assert result.driver_full_name == "Петров Пётр Петрович"
    assert result.policyholder_full_name == "Петров Пётр Петрович"


async def test_extract_sends_one_content_part_per_image(monkeypatch):
    service = _service()
    captured = {}

    async def fake_parse(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(output_parsed=_parsed())

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    await service.extract([(b"one", "image/jpeg"), (b"two", "image/png")])

    content = captured["input"][0]["content"]
    image_parts = [c for c in content if c["type"] == "input_image"]
    assert len(image_parts) == 2
    assert content[0]["type"] == "input_text"


async def test_extract_raises_when_model_returns_no_parsed_output(monkeypatch):
    service = _service()

    async def fake_parse(**kwargs):
        return types.SimpleNamespace(output_parsed=None)

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    with pytest.raises(OcrServiceError):
        await service.extract([(b"bytes", "image/jpeg")])


async def test_extract_retries_once_on_rate_limit_then_succeeds(monkeypatch):
    service = _service()
    resp = httpx.Response(429, request=_REQUEST)
    calls = {"count": 0}

    async def fake_parse(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise openai.RateLimitError("rate limited", response=resp, body=None)
        return types.SimpleNamespace(output_parsed=_parsed())

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)
    monkeypatch.setattr("reader.ocr.service.asyncio.sleep", _instant_sleep)

    result = await service.extract([(b"bytes", "image/jpeg")])

    assert calls["count"] == 2
    assert result.registration_number == "A123BC777"


async def test_extract_gives_up_after_one_retry_on_persistent_rate_limit(monkeypatch):
    service = _service()
    resp = httpx.Response(429, request=_REQUEST)

    async def fake_parse(**kwargs):
        raise openai.RateLimitError("rate limited", response=resp, body=None)

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)
    monkeypatch.setattr("reader.ocr.service.asyncio.sleep", _instant_sleep)

    with pytest.raises(OcrServiceError):
        await service.extract([(b"bytes", "image/jpeg")])


async def test_extract_does_not_retry_non_5xx_status_error(monkeypatch):
    service = _service()
    resp = httpx.Response(400, request=_REQUEST)
    calls = {"count": 0}

    async def fake_parse(**kwargs):
        calls["count"] += 1
        raise openai.APIStatusError("bad request", response=resp, body=None)

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)
    monkeypatch.setattr("reader.ocr.service.asyncio.sleep", _instant_sleep)

    with pytest.raises(OcrServiceError):
        await service.extract([(b"bytes", "image/jpeg")])

    assert calls["count"] == 1


async def test_extract_retries_once_on_5xx_then_succeeds(monkeypatch):
    service = _service()
    resp = httpx.Response(500, request=_REQUEST)
    calls = {"count": 0}

    async def fake_parse(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise openai.APIStatusError("server error", response=resp, body=None)
        return types.SimpleNamespace(output_parsed=_parsed())

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)
    monkeypatch.setattr("reader.ocr.service.asyncio.sleep", _instant_sleep)

    result = await service.extract([(b"bytes", "image/jpeg")])
    assert calls["count"] == 2
    assert result.registration_number == "A123BC777"


async def test_extract_wraps_generic_openai_error(monkeypatch):
    service = _service()

    async def fake_parse(**kwargs):
        raise openai.AuthenticationError(
            "invalid api key", response=httpx.Response(401, request=_REQUEST), body=None,
        )

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    with pytest.raises(OcrServiceError):
        await service.extract([(b"bytes", "image/jpeg")])


async def test_extract_never_leaks_exception_text_into_ocr_service_error(monkeypatch):
    """Не логируем/не пробрасываем str(exc) дальше — сообщение оператору
    формируется отдельно, generic (см. reader/commands/insurance_ocr.py),
    и не должно содержать текст исходной ошибки провайдера."""
    service = _service()
    secret_message = "leaked-secret-detail-should-not-propagate"

    async def fake_parse(**kwargs):
        raise openai.AuthenticationError(
            secret_message, response=httpx.Response(401, request=_REQUEST), body=None,
        )

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    with pytest.raises(OcrServiceError) as exc_info:
        await service.extract([(b"bytes", "image/jpeg")])

    assert secret_message not in str(exc_info.value)


async def _instant_sleep(_seconds):
    return None
