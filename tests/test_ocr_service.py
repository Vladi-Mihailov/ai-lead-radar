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
        registration_owner_full_name="Ivanov Ivan Ivanovich",
        passport_full_name="Ivanov Ivan Ivanovich",
        power_of_attorney_owner_full_name=None,
        passport_number="AB1234567",
        citizenship="Georgia",
        category="passenger_car",
        registration_number="A123BC777", vin="JTMBR12345678901", chassis_number=None,
        manufacturer="Toyota", model="RAV4",
    )
    fields.update(overrides)
    return types.SimpleNamespace(**fields)


def _service() -> OcrService:
    return OcrService(api_key="test-key-not-real", model="gpt-5-mini")


async def test_extract_parses_full_result_with_matching_names(monkeypatch):
    """ФИО паспорта и техпаспорта совпадают: policyholder = это ФИО, оба
    same_as-флага True, driver_full_name не заполняется отдельно."""
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
    assert result.policyholder_full_name == "Ivanov Ivan Ivanovich"
    assert result.driver_full_name is None
    assert result.driver_same_as_policyholder is True
    assert result.owner_full_name is None
    assert result.owner_same_as_policyholder is True
    assert result.passport_number == "AB1234567"
    assert result.citizenship == "Georgia"
    assert result.category == "passenger_car"
    assert result.email is None
    assert result.phone is None
    assert result.fields_found_count == 8


async def test_extract_strips_whitespace_from_fields(monkeypatch):
    service = _service()
    fake_response = types.SimpleNamespace(output_parsed=_parsed(registration_number="  A123BC777  "))

    async def fake_parse(**kwargs):
        return fake_response

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    result = await service.extract([(b"bytes", "image/jpeg")])
    assert result.registration_number == "A123BC777"


async def test_extract_passes_through_latin_script_name_fields_unchanged(monkeypatch):
    """Латиница/транслитерация ФИО — правило самого prompt'а (см.
    reader/ocr/prompt.py), не код: OcrService не должен как-либо
    трогать/перекодировать значения, которые модель уже вернула латиницей —
    просто trim, как для любой другой строки (см. _clean())."""
    service = _service()
    fake_response = types.SimpleNamespace(
        output_parsed=_parsed(
            registration_owner_full_name="Ivanov Ivan Ivanovich",
            passport_full_name="  Petrov Petr Petrovich  ",
        )
    )

    async def fake_parse(**kwargs):
        return fake_response

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    result = await service.extract([(b"bytes", "image/jpeg")])

    # ФИО разные -> policyholder из техпаспорта, driver из паспорта (уже trim'нут).
    assert result.policyholder_full_name == "Ivanov Ivan Ivanovich"
    assert result.driver_full_name == "Petrov Petr Petrovich"


async def test_extract_partial_result_leaves_missing_fields_none(monkeypatch):
    service = _service()
    fake_response = types.SimpleNamespace(
        output_parsed=_parsed(
            vin=None, chassis_number=None,
            registration_owner_full_name=None, passport_full_name=None,
        )
    )

    async def fake_parse(**kwargs):
        return fake_response

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    result = await service.extract([(b"bytes", "image/jpeg")])

    assert result.registration_number == "A123BC777"
    assert result.vin is None
    assert result.chassis_number is None
    assert result.policyholder_full_name is None
    assert result.driver_full_name is None
    assert result.driver_same_as_policyholder is True
    assert result.fields_found_count == 6


async def test_extract_policyholder_is_none_for_legal_entity_owner(monkeypatch):
    """registration_owner_full_name = null, когда собственник в техпаспорте —
    юрлицо (см. reader/ocr/prompt.py) — без ФИО техпаспорта страхователя не
    определить, даже если ФИО паспорта распознано (не придумываем второе
    лицо, см. reader/ocr/models.py)."""
    service = _service()
    fake_response = types.SimpleNamespace(
        output_parsed=_parsed(registration_owner_full_name=None, passport_full_name="Petrov Petr Petrovich")
    )

    async def fake_parse(**kwargs):
        return fake_response

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    result = await service.extract([(b"bytes", "image/jpeg")])

    assert result.policyholder_full_name is None
    assert result.driver_full_name is None
    assert result.driver_same_as_policyholder is True


async def test_extract_passes_through_recognized_category(monkeypatch):
    service = _service()
    fake_response = types.SimpleNamespace(output_parsed=_parsed(category="motorcycle"))

    async def fake_parse(**kwargs):
        return fake_response

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    result = await service.extract([(b"bytes", "image/jpeg")])

    assert result.category == "motorcycle"


async def test_extract_category_is_none_when_not_reliably_determined(monkeypatch):
    """category = null не заменяется программно на passenger_car — модель
    сама решает, что не может надёжно определить категорию."""
    service = _service()
    fake_response = types.SimpleNamespace(output_parsed=_parsed(category=None))

    async def fake_parse(**kwargs):
        return fake_response

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    result = await service.extract([(b"bytes", "image/jpeg")])

    assert result.category is None


async def test_extract_passes_through_passport_number_and_citizenship(monkeypatch):
    service = _service()
    fake_response = types.SimpleNamespace(
        output_parsed=_parsed(passport_number="  PN0011223  ", citizenship="Russia Federation")
    )

    async def fake_parse(**kwargs):
        return fake_response

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    result = await service.extract([(b"bytes", "image/jpeg")])

    assert result.passport_number == "PN0011223"  # trim, как и остальные строковые поля
    assert result.citizenship == "Russia Federation"


async def test_extract_leaves_passport_number_and_citizenship_none_when_no_passport_document(monkeypatch):
    service = _service()
    fake_response = types.SimpleNamespace(output_parsed=_parsed(passport_number=None, citizenship=None))

    async def fake_parse(**kwargs):
        return fake_response

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    result = await service.extract([(b"bytes", "image/jpeg")])

    assert result.passport_number is None
    assert result.citizenship is None


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


# ---- разделение ролей ФИО: паспорт vs техпаспорт (см. reader/ocr/models.py) ----


async def test_matching_names_set_both_same_as_flags_true(monkeypatch):
    """ФИО паспорта и техпаспорта совпадают (регистр/пробелы не важны) —
    оба same_as-флага True, отдельный driver_full_name не заполняется."""
    service = _service()
    fake_response = types.SimpleNamespace(
        output_parsed=_parsed(
            registration_owner_full_name="Ivan   Ivanov",
            passport_full_name="  ivan ivanov  ",
        )
    )

    async def fake_parse(**kwargs):
        return fake_response

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    result = await service.extract([(b"bytes", "image/jpeg")])

    assert result.policyholder_full_name == "Ivan   Ivanov"
    assert result.driver_full_name is None
    assert result.driver_same_as_policyholder is True
    assert result.owner_same_as_policyholder is True
    assert result.owner_full_name is None


async def test_different_names_split_policyholder_from_registration_and_driver_from_passport(monkeypatch):
    """ФИО распознаны в обоих документах и различаются: policyholder — из
    техпаспорта, driver — из паспорта, driver_same_as_policyholder=False,
    owner_same_as_policyholder остаётся True (текущая бизнес-логика —
    владелец отдельно не распознаётся)."""
    service = _service()
    fake_response = types.SimpleNamespace(
        output_parsed=_parsed(
            registration_owner_full_name="Ivanov Ivan",
            passport_full_name="Petrov Petr",
        )
    )

    async def fake_parse(**kwargs):
        return fake_response

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    result = await service.extract([(b"bytes", "image/jpeg")])

    assert result.policyholder_full_name == "Ivanov Ivan"
    assert result.driver_full_name == "Petrov Petr"
    assert result.driver_same_as_policyholder is False
    assert result.owner_same_as_policyholder is True
    assert result.owner_full_name is None


# ---- driver_same_as_policyholder: сравнение только по фамилии+имени ----


async def test_driver_same_as_policyholder_true_when_passport_name_omits_patronymic(monkeypatch):
    """Полное ФИО техпаспорта vs фамилия+имя паспорта/прав (без отчества) —
    один и тот же человек, отдельный водитель не выводится."""
    service = _service()
    fake_response = types.SimpleNamespace(
        output_parsed=_parsed(
            registration_owner_full_name="Buivolenko Viktor Igorevich",
            passport_full_name="Buivolenko Viktor",
        )
    )

    async def fake_parse(**kwargs):
        return fake_response

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    result = await service.extract([(b"bytes", "image/jpeg")])

    assert result.driver_same_as_policyholder is True
    assert result.driver_full_name is None
    # Страхователь остаётся полным ФИО, не усечённым до фамилии+имени.
    assert result.policyholder_full_name == "Buivolenko Viktor Igorevich"


async def test_driver_same_as_policyholder_true_when_both_have_different_patronymics(monkeypatch):
    """Отчества присутствуют в обоих документах, но различаются —
    фамилия+имя совпадают, поэтому это всё равно один и тот же человек."""
    service = _service()
    fake_response = types.SimpleNamespace(
        output_parsed=_parsed(
            registration_owner_full_name="Buivolenko Viktor Igorevich",
            passport_full_name="Buivolenko Viktor Petrovich",
        )
    )

    async def fake_parse(**kwargs):
        return fake_response

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    result = await service.extract([(b"bytes", "image/jpeg")])

    assert result.driver_same_as_policyholder is True
    assert result.driver_full_name is None
    assert result.policyholder_full_name == "Buivolenko Viktor Igorevich"


async def test_driver_same_as_policyholder_false_when_first_name_differs_same_surname(monkeypatch):
    """Фамилия совпадает, имя — нет: разные люди, водитель выводится
    отдельно и полностью, как распознано."""
    service = _service()
    fake_response = types.SimpleNamespace(
        output_parsed=_parsed(
            registration_owner_full_name="Buivolenko Viktor Igorevich",
            passport_full_name="Buivolenko Petr",
        )
    )

    async def fake_parse(**kwargs):
        return fake_response

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    result = await service.extract([(b"bytes", "image/jpeg")])

    assert result.driver_same_as_policyholder is False
    assert result.driver_full_name == "Buivolenko Petr"
    assert result.policyholder_full_name == "Buivolenko Viktor Igorevich"


async def test_driver_same_as_policyholder_false_when_surname_differs_same_first_name(monkeypatch):
    """Имя совпадает, фамилия — нет: тоже разные люди."""
    service = _service()
    fake_response = types.SimpleNamespace(
        output_parsed=_parsed(
            registration_owner_full_name="Buivolenko Viktor Igorevich",
            passport_full_name="Sidorov Viktor",
        )
    )

    async def fake_parse(**kwargs):
        return fake_response

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    result = await service.extract([(b"bytes", "image/jpeg")])

    assert result.driver_same_as_policyholder is False
    assert result.driver_full_name == "Sidorov Viktor"


async def test_power_of_attorney_owner_logic_unaffected_by_relaxed_driver_name_matching(monkeypatch):
    """Регресс: смягчение сравнения для driver никак не должно влиять на
    независимую owner/доверенность-логику (см. reader/ocr/models.py)."""
    service = _service()
    fake_response = types.SimpleNamespace(
        output_parsed=_parsed(
            registration_owner_full_name="Buivolenko Viktor Igorevich",
            passport_full_name="Buivolenko Viktor",
            power_of_attorney_owner_full_name="Sidorov Semyon",
        )
    )

    async def fake_parse(**kwargs):
        return fake_response

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    result = await service.extract([(b"bytes", "image/jpeg")])

    assert result.driver_same_as_policyholder is True
    assert result.driver_full_name is None
    assert result.owner_same_as_policyholder is False
    assert result.owner_full_name == "Sidorov Semyon"


async def test_only_registration_certificate_name_available_uses_it_as_policyholder(monkeypatch):
    """Единственный надёжный источник — техпаспорт: используем его ФИО как
    policyholder, не изобретаем отдельного водителя."""
    service = _service()
    fake_response = types.SimpleNamespace(
        output_parsed=_parsed(registration_owner_full_name="Ivanov Ivan", passport_full_name=None)
    )

    async def fake_parse(**kwargs):
        return fake_response

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    result = await service.extract([(b"bytes", "image/jpeg")])

    assert result.policyholder_full_name == "Ivanov Ivan"
    assert result.driver_full_name is None
    assert result.driver_same_as_policyholder is True


async def test_only_passport_name_available_yields_no_policyholder_no_driver(monkeypatch):
    """Единственный источник — паспорт (техпаспортное ФИО не распознано):
    ФИО из паспорта НЕ соответствует правилу источника для policyholder
    (см. reader/ocr/prompt.py) — не придумываем страхователя из одного
    паспорта, второе лицо тоже не изобретаем."""
    service = _service()
    fake_response = types.SimpleNamespace(
        output_parsed=_parsed(registration_owner_full_name=None, passport_full_name="Petrov Petr")
    )

    async def fake_parse(**kwargs):
        return fake_response

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    result = await service.extract([(b"bytes", "image/jpeg")])

    assert result.policyholder_full_name is None
    assert result.driver_full_name is None
    assert result.driver_same_as_policyholder is True


async def test_neither_name_available_yields_no_policyholder_no_driver(monkeypatch):
    service = _service()
    fake_response = types.SimpleNamespace(
        output_parsed=_parsed(registration_owner_full_name=None, passport_full_name=None)
    )

    async def fake_parse(**kwargs):
        return fake_response

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    result = await service.extract([(b"bytes", "image/jpeg")])

    assert result.policyholder_full_name is None
    assert result.driver_full_name is None
    assert result.driver_same_as_policyholder is True
    assert result.owner_same_as_policyholder is True
    assert result.owner_full_name is None


# ---- доверенность: отдельный владелец (см. reader/ocr/models.py) ----


async def test_no_power_of_attorney_yields_owner_same_as_policyholder(monkeypatch):
    """Без доверенности (модель вернула null) — владелец по умолчанию
    совпадает со страхователем, техпаспорт для owner повторно не
    используется."""
    service = _service()
    fake_response = types.SimpleNamespace(
        output_parsed=_parsed(power_of_attorney_owner_full_name=None)
    )

    async def fake_parse(**kwargs):
        return fake_response

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    result = await service.extract([(b"bytes", "image/jpeg")])

    assert result.owner_same_as_policyholder is True
    assert result.owner_full_name is None


async def test_confident_power_of_attorney_owner_sets_separate_owner(monkeypatch):
    """Доверенность с уверенно определённым владельцем — owner_full_name из
    доверенности, owner_same_as_policyholder=False, независимо от
    driver-логики (страхователь/водитель совпадают в этом сценарии)."""
    service = _service()
    fake_response = types.SimpleNamespace(
        output_parsed=_parsed(power_of_attorney_owner_full_name="Sidorov Semyon")
    )

    async def fake_parse(**kwargs):
        return fake_response

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    result = await service.extract([(b"bytes", "image/jpeg")])

    assert result.owner_same_as_policyholder is False
    assert result.owner_full_name == "Sidorov Semyon"
    # Не зависит от driver-роли — водитель по-прежнему совпадает со страхователем.
    assert result.driver_same_as_policyholder is True


async def test_power_of_attorney_owner_takes_priority_even_when_driver_also_differs(monkeypatch):
    """Приоритет доверенности над default owner_same_as_policyholder=True не
    зависит от driver-ветки — проверяем на сценарии, где ФИО паспорта и
    техпаспорта тоже различаются (driver отдельный)."""
    service = _service()
    fake_response = types.SimpleNamespace(
        output_parsed=_parsed(
            registration_owner_full_name="Ivanov Ivan",
            passport_full_name="Petrov Petr",
            power_of_attorney_owner_full_name="Sidorov Semyon",
        )
    )

    async def fake_parse(**kwargs):
        return fake_response

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    result = await service.extract([(b"bytes", "image/jpeg")])

    assert result.driver_same_as_policyholder is False
    assert result.driver_full_name == "Petrov Petr"
    assert result.owner_same_as_policyholder is False
    assert result.owner_full_name == "Sidorov Semyon"


async def test_ambiguous_power_of_attorney_without_confident_person_yields_owner_same_as_policyholder(monkeypatch):
    """Доверенность с несколькими ФИО, где нельзя уверенно определить
    владельца — модель обязана вернуть null (см. reader/ocr/prompt.py), а не
    угадывать; на уровне OcrService это неотличимо от "доверенности нет" —
    владелец остаётся same_as-страхователем, пустой owner_full_name."""
    service = _service()
    fake_response = types.SimpleNamespace(
        output_parsed=_parsed(power_of_attorney_owner_full_name=None)
    )

    async def fake_parse(**kwargs):
        return fake_response

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    result = await service.extract([(b"bytes", "image/jpeg")])

    assert result.owner_same_as_policyholder is True
    assert result.owner_full_name is None


async def test_power_of_attorney_owner_name_is_trimmed_like_other_name_fields(monkeypatch):
    service = _service()
    fake_response = types.SimpleNamespace(
        output_parsed=_parsed(power_of_attorney_owner_full_name="  Sidorov Semyon  ")
    )

    async def fake_parse(**kwargs):
        return fake_response

    monkeypatch.setattr(service._client.responses, "parse", fake_parse)

    result = await service.extract([(b"bytes", "image/jpeg")])

    assert result.owner_full_name == "Sidorov Semyon"


async def _instant_sleep(_seconds):
    return None
