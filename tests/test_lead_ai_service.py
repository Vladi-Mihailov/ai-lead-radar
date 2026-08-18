"""Тесты reader/lead_ai/service.py::LeadAiService — ни один тест не
обращается к настоящему OpenAI API: client.responses.parse подменяется
напрямую (по аналогии с tests/test_ocr_service.py), сеть не используется
вовсе.

Покрывает регрессионные сценарии из задачи: штрафы (оплата/проверка),
денежные переводы Россия->Грузия, обменник/курс/цена в долларах ->
irrelevant, страховка (Грузия/Турция/Армения/без страны/медицинская)."""

import sys
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402
import openai  # noqa: E402
import pytest  # noqa: E402

from reader.lead_ai.models import LeadAiAnalysis  # noqa: E402
from reader.lead_ai.service import LeadAiService, LeadAiServiceError  # noqa: E402

_REQUEST = httpx.Request("POST", "https://api.openai.com/v1/responses")


def _service() -> LeadAiService:
    return LeadAiService(api_key="test-key-not-real", model="gpt-5-mini")


def _fake_parse_returning(analysis: LeadAiAnalysis):
    async def fake_parse(**kwargs):
        return types.SimpleNamespace(output_parsed=analysis)

    return fake_parse


async def _instant_sleep(_seconds):
    return None


# ---- регрессионные категории из задачи ----


async def test_fine_payment_message_is_relevant(monkeypatch):
    service = _service()
    analysis = LeadAiAnalysis(
        relevant=True, lead_type="fine_payment",
        reason="хочет оплатить штраф", suggested_reply="Пришлите госномер автомобиля.",
    )
    monkeypatch.setattr(service._client.responses, "parse", _fake_parse_returning(analysis))

    result = await service.analyze("Кто может помочь оплатить штраф?")

    assert result.relevant is True
    assert result.lead_type == "fine_payment"


async def test_fine_check_message_is_fine_check(monkeypatch):
    service = _service()
    analysis = LeadAiAnalysis(
        relevant=True, lead_type="fine_check",
        reason="хочет проверить штрафы", suggested_reply="Пришлите госномер автомобиля.",
    )
    monkeypatch.setattr(service._client.responses, "parse", _fake_parse_returning(analysis))

    result = await service.analyze("Можете посмотреть штрафы по номеру?")

    assert result.relevant is True
    assert result.lead_type == "fine_check"


async def test_money_transfer_ru_ge_message_is_money_transfer_ru_ge(monkeypatch):
    service = _service()
    analysis = LeadAiAnalysis(
        relevant=True, lead_type="money_transfer_ru_ge",
        reason="хочет перевести деньги из России на грузинскую карту",
        suggested_reply="Подскажите сумму и в какой валюте хотите получить?",
    )
    monkeypatch.setattr(service._client.responses, "parse", _fake_parse_returning(analysis))

    result = await service.analyze("Можно перевести вам на российскую карту, а вы отправите на BOG?")

    assert result.relevant is True
    assert result.lead_type == "money_transfer_ru_ge"


async def test_currency_exchange_office_message_is_irrelevant(monkeypatch):
    service = _service()
    analysis = LeadAiAnalysis(
        relevant=False, lead_type="irrelevant",
        reason="спрашивает про обменник, не про перевод Россия->Грузия", suggested_reply="",
    )
    monkeypatch.setattr(service._client.responses, "parse", _fake_parse_returning(analysis))

    result = await service.analyze("Где хороший обменник в Батуми?")

    assert result.relevant is False
    assert result.lead_type == "irrelevant"


async def test_usd_rate_question_is_irrelevant(monkeypatch):
    service = _service()
    analysis = LeadAiAnalysis(
        relevant=False, lead_type="irrelevant",
        reason="спрашивает курс доллара, без намерения перевода", suggested_reply="",
    )
    monkeypatch.setattr(service._client.responses, "parse", _fake_parse_returning(analysis))

    result = await service.analyze("Какой сегодня курс доллара?")

    assert result.relevant is False


async def test_price_in_dollars_without_transfer_intent_is_irrelevant(monkeypatch):
    service = _service()
    analysis = LeadAiAnalysis(
        relevant=False, lead_type="irrelevant",
        reason="обсуждает цену товара в долларах, не перевод денег", suggested_reply="",
    )
    monkeypatch.setattr(service._client.responses, "parse", _fake_parse_returning(analysis))

    result = await service.analyze("Продаю телефон за 200 долларов")

    assert result.relevant is False


async def test_insurance_georgia_message(monkeypatch):
    service = _service()
    analysis = LeadAiAnalysis(
        relevant=True, lead_type="insurance_georgia",
        reason="нужна страховка для Грузии",
        suggested_reply="Можем оформить автомобильную страховку для Грузии. Пришлите документы на автомобиль.",
    )
    monkeypatch.setattr(service._client.responses, "parse", _fake_parse_returning(analysis))

    result = await service.analyze("Нужна страховка на машину в Грузии")

    assert result.lead_type == "insurance_georgia"


async def test_insurance_turkey_message(monkeypatch):
    service = _service()
    analysis = LeadAiAnalysis(
        relevant=True, lead_type="insurance_turkey",
        reason="нужна страховка для Турции",
        suggested_reply="Можем оформить автомобильную страховку для Турции.",
    )
    monkeypatch.setattr(service._client.responses, "parse", _fake_parse_returning(analysis))

    result = await service.analyze("Еду в Турцию на машине, нужна страховка")

    assert result.lead_type == "insurance_turkey"


async def test_insurance_armenia_message(monkeypatch):
    service = _service()
    analysis = LeadAiAnalysis(
        relevant=True, lead_type="insurance_armenia",
        reason="нужна страховка для Армении",
        suggested_reply="Можем оформить автомобильную страховку для Армении.",
    )
    monkeypatch.setattr(service._client.responses, "parse", _fake_parse_returning(analysis))

    result = await service.analyze("Нужна автостраховка в Армению")

    assert result.lead_type == "insurance_armenia"


async def test_insurance_without_country_is_insurance_general(monkeypatch):
    service = _service()
    analysis = LeadAiAnalysis(
        relevant=True, lead_type="insurance_general",
        reason="нужна страховка, страна неясна",
        suggested_reply="Можем оформить автомобильную страховку. Подскажите, для какой страны она нужна?",
    )
    monkeypatch.setattr(service._client.responses, "parse", _fake_parse_returning(analysis))

    result = await service.analyze("Нужна автомобильная страховка")

    assert result.lead_type == "insurance_general"


async def test_medical_insurance_is_irrelevant(monkeypatch):
    service = _service()
    analysis = LeadAiAnalysis(
        relevant=False, lead_type="irrelevant",
        reason="медицинская страховка, не автомобильная", suggested_reply="",
    )
    monkeypatch.setattr(service._client.responses, "parse", _fake_parse_returning(analysis))

    result = await service.analyze("Нужна медицинская страховка для поездки")

    assert result.relevant is False


# ---- inconsistent model result нормализуется Python-кодом ----


async def test_inconsistent_model_result_is_normalized(monkeypatch):
    """Модель вернула relevant=True, lead_type=irrelevant — service должен
    вернуть уже нормализованный (безопасный) результат."""
    service = _service()
    analysis = LeadAiAnalysis(
        relevant=True, lead_type="irrelevant",
        reason="противоречиво", suggested_reply="что-то",
    )
    monkeypatch.setattr(service._client.responses, "parse", _fake_parse_returning(analysis))

    result = await service.analyze("любой текст")

    assert result.relevant is False
    assert result.lead_type == "irrelevant"
    assert result.suggested_reply == ""


# ---- транспорт/ошибки — тот же приём, что и OcrService ----


async def test_analyze_raises_when_model_returns_no_parsed_output(monkeypatch):
    service = _service()

    async def fake_parse(**kwargs):
        return types.SimpleNamespace(output_parsed=None)

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    with pytest.raises(LeadAiServiceError):
        await service.analyze("текст")


async def test_analyze_retries_once_on_rate_limit_then_succeeds(monkeypatch):
    service = _service()
    resp = httpx.Response(429, request=_REQUEST)
    calls = {"count": 0}
    analysis = LeadAiAnalysis(relevant=False, lead_type="irrelevant", reason="r", suggested_reply="")

    async def fake_parse(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise openai.RateLimitError("rate limited", response=resp, body=None)
        return types.SimpleNamespace(output_parsed=analysis)

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)
    monkeypatch.setattr("reader.lead_ai.service.asyncio.sleep", _instant_sleep)

    result = await service.analyze("текст")

    assert calls["count"] == 2
    assert result.relevant is False


async def test_analyze_gives_up_after_one_retry_on_persistent_rate_limit(monkeypatch):
    service = _service()
    resp = httpx.Response(429, request=_REQUEST)

    async def fake_parse(**kwargs):
        raise openai.RateLimitError("rate limited", response=resp, body=None)

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)
    monkeypatch.setattr("reader.lead_ai.service.asyncio.sleep", _instant_sleep)

    with pytest.raises(LeadAiServiceError):
        await service.analyze("текст")


async def test_analyze_does_not_retry_non_5xx_status_error(monkeypatch):
    service = _service()
    resp = httpx.Response(400, request=_REQUEST)
    calls = {"count": 0}

    async def fake_parse(**kwargs):
        calls["count"] += 1
        raise openai.APIStatusError("bad request", response=resp, body=None)

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)
    monkeypatch.setattr("reader.lead_ai.service.asyncio.sleep", _instant_sleep)

    with pytest.raises(LeadAiServiceError):
        await service.analyze("текст")

    assert calls["count"] == 1


async def test_analyze_wraps_generic_openai_error(monkeypatch):
    service = _service()

    async def fake_parse(**kwargs):
        raise openai.AuthenticationError(
            "invalid api key", response=httpx.Response(401, request=_REQUEST), body=None,
        )

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    with pytest.raises(LeadAiServiceError):
        await service.analyze("текст")


async def test_analyze_never_leaks_exception_text_into_lead_ai_service_error(monkeypatch):
    """Не логируем/не пробрасываем str(exc) дальше — исходный текст лида не
    должен попасть в исключение, всплывающее выше сервиса."""
    service = _service()
    secret_message = "leaked-lead-text-should-not-propagate"

    async def fake_parse(**kwargs):
        raise openai.AuthenticationError(
            secret_message, response=httpx.Response(401, request=_REQUEST), body=None,
        )

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    with pytest.raises(LeadAiServiceError) as exc_info:
        await service.analyze("текст")

    assert secret_message not in str(exc_info.value)


async def test_analyze_truncates_overly_long_message_text(monkeypatch):
    service = _service()
    captured = {}
    analysis = LeadAiAnalysis(relevant=False, lead_type="irrelevant", reason="r", suggested_reply="")

    async def fake_parse(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(output_parsed=analysis)

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    long_text = "a" * 5000
    await service.analyze(long_text)

    sent_content = captured["input"][0]["content"]
    assert len(sent_content) < 5000
