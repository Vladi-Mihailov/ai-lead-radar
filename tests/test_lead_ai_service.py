"""Тесты reader/lead_ai/service.py::LeadAiService — ни один тест не
обращается к настоящему OpenAI API: client.responses.parse подменяется
напрямую (по аналогии с tests/test_ocr_service.py), сеть не используется
вовсе.

Как и раньше (см. tests/test_ocr_service.py), это тесты СЕРВИСА, а не
самой модели OpenAI: analyze() мокается на ОЖИДАЕМЫЙ результат — тест
документирует/фиксирует, какую классификацию reader/lead_ai/prompt.py
должен вызывать у модели для каждого сценария (полноценно проверить
реальное поведение GPT можно только вручную/на проде), и проверяет, что
сервис корректно нормализует/пробрасывает этот результат.

Покрывает регрессионные сценарии из задачи: штрафы (оплата/проверка),
денежные переводы Россия->Грузия, обменник/курс/цена в долларах ->
irrelevant, страховка (Грузия/Турция/Армения/без страны/медицинская), а
также повышенный recall страховых лидов — планирование автомобильной
поездки само по себе является потенциальным лидом, keyword ("перев") не
доказательство (может быть "перевал", не иметь отношения к переводу денег)."""

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
        reason="хочет оплатить штраф",
        suggested_messages=["Добрый день", "В этой же группе помогают с оплатой штрафа"],
    )
    monkeypatch.setattr(service._client.responses, "parse", _fake_parse_returning(analysis))

    result = await service.analyze("Кто может помочь оплатить штраф?")

    assert result.relevant is True
    assert result.lead_type == "fine_payment"


async def test_fine_check_message_is_fine_check(monkeypatch):
    service = _service()
    analysis = LeadAiAnalysis(
        relevant=True, lead_type="fine_check",
        reason="хочет проверить штрафы",
        suggested_messages=["Добрый день", "Мы тут проверяли"],
    )
    monkeypatch.setattr(service._client.responses, "parse", _fake_parse_returning(analysis))

    result = await service.analyze("Можете посмотреть штрафы по номеру?")

    assert result.relevant is True
    assert result.lead_type == "fine_check"


async def test_fine_check_link_question_is_fine_check(monkeypatch):
    """Регрессия из задачи: «можно ссылку проверить наличие штрафа?»."""
    service = _service()
    analysis = LeadAiAnalysis(
        relevant=True, lead_type="fine_check",
        reason="просит ссылку/способ проверить штраф",
        suggested_messages=["Добрый день", "Мы тут проверяли"],
    )
    monkeypatch.setattr(service._client.responses, "parse", _fake_parse_returning(analysis))

    result = await service.analyze("Можно ссылку посмотреть наличие штрафа?")

    assert result.relevant is True
    assert result.lead_type == "fine_check"


async def test_money_transfer_ru_ge_message_is_money_transfer_ru_ge(monkeypatch):
    service = _service()
    analysis = LeadAiAnalysis(
        relevant=True, lead_type="money_transfer_ru_ge",
        reason="хочет перевести деньги из России на грузинскую карту",
        suggested_messages=["Добрый день", "Мы так переводили", "Подскажите сумму"],
    )
    monkeypatch.setattr(service._client.responses, "parse", _fake_parse_returning(analysis))

    result = await service.analyze("Можно перевести вам на российскую карту, а вы отправите на BOG?")

    assert result.relevant is True
    assert result.lead_type == "money_transfer_ru_ge"


async def test_currency_exchange_office_message_is_irrelevant(monkeypatch):
    service = _service()
    analysis = LeadAiAnalysis(
        relevant=False, lead_type="irrelevant",
        reason="спрашивает про обменник, не про перевод Россия->Грузия", suggested_messages=[],
    )
    monkeypatch.setattr(service._client.responses, "parse", _fake_parse_returning(analysis))

    result = await service.analyze("Где хороший обменник в Батуми?")

    assert result.relevant is False
    assert result.lead_type == "irrelevant"


async def test_usd_rate_question_is_irrelevant(monkeypatch):
    service = _service()
    analysis = LeadAiAnalysis(
        relevant=False, lead_type="irrelevant",
        reason="спрашивает курс доллара, без намерения перевода", suggested_messages=[],
    )
    monkeypatch.setattr(service._client.responses, "parse", _fake_parse_returning(analysis))

    result = await service.analyze("Какой сегодня курс доллара?")

    assert result.relevant is False


async def test_price_in_dollars_without_transfer_intent_is_irrelevant(monkeypatch):
    service = _service()
    analysis = LeadAiAnalysis(
        relevant=False, lead_type="irrelevant",
        reason="обсуждает цену товара в долларах, не перевод денег", suggested_messages=[],
    )
    monkeypatch.setattr(service._client.responses, "parse", _fake_parse_returning(analysis))

    result = await service.analyze("Продаю телефон за 200 долларов")

    assert result.relevant is False


async def test_insurance_georgia_message(monkeypatch):
    service = _service()
    analysis = LeadAiAnalysis(
        relevant=True, lead_type="insurance_georgia",
        reason="нужна страховка для Грузии",
        suggested_messages=["Добрый день", "https://t.me/car_ins_georgia", "Оформили нам быстро"],
    )
    monkeypatch.setattr(service._client.responses, "parse", _fake_parse_returning(analysis))

    result = await service.analyze("Нужна страховка на машину в Грузии")

    assert result.lead_type == "insurance_georgia"


async def test_insurance_turkey_message(monkeypatch):
    service = _service()
    analysis = LeadAiAnalysis(
        relevant=True, lead_type="insurance_turkey",
        reason="нужна страховка для Турции",
        suggested_messages=["Добрый день", "Оформили нам быстро"],
    )
    monkeypatch.setattr(service._client.responses, "parse", _fake_parse_returning(analysis))

    result = await service.analyze("Еду в Турцию на машине, нужна страховка")

    assert result.lead_type == "insurance_turkey"


async def test_insurance_armenia_message(monkeypatch):
    service = _service()
    analysis = LeadAiAnalysis(
        relevant=True, lead_type="insurance_armenia",
        reason="нужна страховка для Армении",
        suggested_messages=["Добрый день", "Оформили нам быстро"],
    )
    monkeypatch.setattr(service._client.responses, "parse", _fake_parse_returning(analysis))

    result = await service.analyze("Нужна автостраховка в Армению")

    assert result.lead_type == "insurance_armenia"


async def test_insurance_without_country_is_insurance_general(monkeypatch):
    service = _service()
    analysis = LeadAiAnalysis(
        relevant=True, lead_type="insurance_general",
        reason="нужна страховка, страна неясна",
        suggested_messages=["Добрый день", "Для какой страны нужна страховка?"],
    )
    monkeypatch.setattr(service._client.responses, "parse", _fake_parse_returning(analysis))

    result = await service.analyze("Нужна автомобильная страховка")

    assert result.lead_type == "insurance_general"


async def test_medical_insurance_is_irrelevant(monkeypatch):
    service = _service()
    analysis = LeadAiAnalysis(
        relevant=False, lead_type="irrelevant",
        reason="медицинская страховка, не автомобильная", suggested_messages=[],
    )
    monkeypatch.setattr(service._client.responses, "parse", _fake_parse_returning(analysis))

    result = await service.analyze("Нужна медицинская страховка для поездки")

    assert result.relevant is False


# ---- повышенный recall: поездка на автомобиле сама по себе — страховой лид ----


async def test_car_trip_to_georgia_with_mountain_pass_question_is_insurance_georgia(monkeypatch):
    """Регрессия из задачи: человек прямо про страховку не спрашивает, но
    сам едет в Грузию на автомобиле — это insurance_georgia, не irrelevant."""
    service = _service()
    analysis = LeadAiAnalysis(
        relevant=True, lead_type="insurance_georgia",
        reason="человек сам планирует поездку в Грузию на автомобиле",
        suggested_messages=["Добрый день", "Мы тут вчера делали", "Оформили нам быстро"],
    )
    monkeypatch.setattr(service._client.responses, "parse", _fake_parse_returning(analysis))

    result = await service.analyze(
        "Добрый день! Думаем съездить в Тбилиси в начале октября. Какая дорога "
        "будет на перевале, может быть снег? На летней тогда не проехать?"
    )

    assert result.relevant is True
    assert result.lead_type == "insurance_georgia"


async def test_driving_to_georgia_is_insurance_georgia(monkeypatch):
    service = _service()
    analysis = LeadAiAnalysis(
        relevant=True, lead_type="insurance_georgia",
        reason="едет в Грузию на своей машине",
        suggested_messages=["Добрый день", "Мы тут вчера делали"],
    )
    monkeypatch.setattr(service._client.responses, "parse", _fake_parse_returning(analysis))

    result = await service.analyze("Едем в Грузию на машине")

    assert result.relevant is True
    assert result.lead_type == "insurance_georgia"


async def test_driving_to_turkey_is_insurance_turkey(monkeypatch):
    service = _service()
    analysis = LeadAiAnalysis(
        relevant=True, lead_type="insurance_turkey",
        reason="едет в Турцию на своей машине",
        suggested_messages=["Добрый день", "Оформили нам быстро"],
    )
    monkeypatch.setattr(service._client.responses, "parse", _fake_parse_returning(analysis))

    result = await service.analyze("На машине в Турцию")

    assert result.relevant is True
    assert result.lead_type == "insurance_turkey"


async def test_driving_to_armenia_is_insurance_armenia(monkeypatch):
    service = _service()
    analysis = LeadAiAnalysis(
        relevant=True, lead_type="insurance_armenia",
        reason="едет в Армению на своей машине",
        suggested_messages=["Добрый день", "Оформили нам быстро"],
    )
    monkeypatch.setattr(service._client.responses, "parse", _fake_parse_returning(analysis))

    result = await service.analyze("Едем в Армению на машине")

    assert result.relevant is True
    assert result.lead_type == "insurance_armenia"


async def test_weather_question_without_car_trip_signal_is_irrelevant(monkeypatch):
    """Регрессия из задачи: просто вопрос про погоду в Тбилиси, БЕЗ
    признака собственной автомобильной поездки — не должен превращаться в
    страховой лид (keyword — не доказательство, см. задачу)."""
    service = _service()
    analysis = LeadAiAnalysis(
        relevant=False, lead_type="irrelevant",
        reason="просто вопрос про погоду, без признака поездки на автомобиле",
        suggested_messages=[],
    )
    monkeypatch.setattr(service._client.responses, "parse", _fake_parse_returning(analysis))

    result = await service.analyze("Какая погода в Тбилиси?")

    assert result.relevant is False
    assert result.lead_type == "irrelevant"


async def test_mountain_pass_word_is_not_classified_as_money_transfer(monkeypatch):
    """Регрессия из задачи: "перевал" может ложно совпасть по keyword
    "перев" с "перевод", но по смыслу это дорога, а не перевод денег —
    money_transfer_ru_ge здесь неверно в любом случае (сообщение — про
    поездку/страховку, см. test_car_trip_to_georgia_with_mountain_pass_
    question_is_insurance_georgia, а не про деньги)."""
    service = _service()
    analysis = LeadAiAnalysis(
        relevant=True, lead_type="insurance_georgia",
        reason="про перевал/дорогу в рамках поездки в Грузию, не про деньги",
        suggested_messages=["Добрый день", "Мы тут вчера делали"],
    )
    monkeypatch.setattr(service._client.responses, "parse", _fake_parse_returning(analysis))

    result = await service.analyze("Как сейчас дорога через перевал в Верхнем Ларсе?")

    assert result.lead_type != "money_transfer_ru_ge"


# ---- inconsistent model result нормализуется Python-кодом ----


async def test_inconsistent_model_result_is_normalized(monkeypatch):
    """Модель вернула relevant=True, lead_type=irrelevant — service должен
    вернуть уже нормализованный (безопасный) результат."""
    service = _service()
    analysis = LeadAiAnalysis(
        relevant=True, lead_type="irrelevant",
        reason="противоречиво", suggested_messages=["что-то"],
    )
    monkeypatch.setattr(service._client.responses, "parse", _fake_parse_returning(analysis))

    result = await service.analyze("любой текст")

    assert result.relevant is False
    assert result.lead_type == "irrelevant"
    assert result.suggested_messages == []


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
    analysis = LeadAiAnalysis(relevant=False, lead_type="irrelevant", reason="r", suggested_messages=[])

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
    analysis = LeadAiAnalysis(relevant=False, lead_type="irrelevant", reason="r", suggested_messages=[])

    async def fake_parse(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(output_parsed=analysis)

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    long_text = "a" * 5000
    await service.analyze(long_text)

    sent_content = captured["input"][0]["content"]
    assert len(sent_content) < 5000
