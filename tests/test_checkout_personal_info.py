"""Тесты reader/checkout/personal_info.py — OcrPersonalInfoProvider
(реальный источник identification_number/citizenship/phone/email) и
NoPersonalInfoProvider (fail-closed default). reference_data — фейковый, ни
один реальный HTTP-запрос не выполняется."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.checkout.personal_info import (  # noqa: E402
    NoPersonalInfoProvider,
    OcrPersonalInfoProvider,
)
from reader.checkout.reference_data import CountryMatch  # noqa: E402
from reader.ocr.models import OcrResult  # noqa: E402

_PHONE = "925000000000"
_EMAIL = "tplgee@mail.ru"


def _effective(**overrides) -> OcrResult:
    fields = dict(
        owner_full_name="Ivanov Ivan", driver_full_name="Petrov Petr", policyholder_full_name="Petrov Petr",
        passport_number="AB1234567", citizenship="Georgia",
        category="passenger_car", registration_number="AA001AA",
        vin="WVWZZZ1KZAW123456", chassis_number=None,
        manufacturer="Toyota", model="Camry",
    )
    fields.update(overrides)
    return OcrResult(**fields)


_DEFAULT_COUNTRY = CountryMatch(id=1, name="Georgia")


class _FakeReferenceData:
    def __init__(self, *, country: CountryMatch | None = _DEFAULT_COUNTRY):
        self._country = country
        self.resolve_country_calls: list[str] = []

    async def resolve_country(self, name):
        self.resolve_country_calls.append(name)
        return self._country


def _provider(reference_data=None) -> tuple[OcrPersonalInfoProvider, _FakeReferenceData]:
    reference_data = reference_data or _FakeReferenceData()
    return OcrPersonalInfoProvider(reference_data=reference_data, phone=_PHONE, email=_EMAIL), reference_data


# ---- happy path ----


async def test_resolve_fills_identification_number_from_passport():
    provider, _ref = _provider()
    resolution = await provider.resolve(_effective(passport_number="XY0001112"))

    assert resolution.is_complete
    assert resolution.info.insurer.identification_number == "XY0001112"
    assert resolution.info.driver.identification_number == "XY0001112"
    assert resolution.info.owner.identification_number == "XY0001112"


async def test_resolve_fills_citizenship_id_via_reference_data():
    reference_data = _FakeReferenceData(country=CountryMatch(id=52, name="Russia Federation"))
    provider, _ref = _provider(reference_data)

    resolution = await provider.resolve(_effective(citizenship="Russia Federation"))

    assert resolution.is_complete
    assert resolution.info.insurer.citizenship_id == 52
    assert resolution.info.driver.citizenship_id == 52
    assert resolution.info.owner.citizenship_id == 52
    assert reference_data.resolve_country_calls == ["Russia Federation"]


async def test_resolve_fills_phone_and_email_from_constructor_args_not_ocr():
    """phone/email приходят из settings (переданы в конструктор), а не из
    OcrResult — у OcrResult вообще нет таких полей (см. задачу: "Phone/email
    не нужно OCR-ить")."""
    provider, _ref = _provider()
    resolution = await provider.resolve(_effective())

    for role in (resolution.info.insurer, resolution.info.driver, resolution.info.owner):
        assert role.phone == _PHONE
        assert role.email == _EMAIL


async def test_resolve_uses_same_person_for_all_three_roles():
    """Бизнес-решение задачи: один и тот же identification_number/
    citizenship/phone/email для страхователя/водителя/собственника."""
    provider, _ref = _provider()
    resolution = await provider.resolve(_effective())

    assert resolution.info.insurer == resolution.info.driver == resolution.info.owner


# ---- отсутствие/неизвестные значения блокируют ----


async def test_resolve_blocks_when_passport_number_missing():
    provider, _ref = _provider()
    resolution = await provider.resolve(_effective(passport_number=None))

    assert not resolution.is_complete
    assert any("паспорта" in item for item in resolution.missing)


async def test_resolve_blocks_when_citizenship_missing():
    provider, _ref = _provider()
    resolution = await provider.resolve(_effective(citizenship=None))

    assert not resolution.is_complete
    assert any("гражданство" in item for item in resolution.missing)


async def test_resolve_blocks_when_citizenship_not_found_in_tpl_ge_catalog():
    reference_data = _FakeReferenceData(country=None)
    provider, _ref = _provider(reference_data)

    resolution = await provider.resolve(_effective(citizenship="Narnia"))

    assert not resolution.is_complete
    assert any("Narnia" in item for item in resolution.missing)


async def test_resolve_does_not_call_reference_data_when_citizenship_is_none():
    reference_data = _FakeReferenceData()
    provider, _ref = _provider(reference_data)

    await provider.resolve(_effective(citizenship=None))

    assert reference_data.resolve_country_calls == []


async def test_resolve_reports_both_missing_fields_together():
    provider, _ref = _provider()
    resolution = await provider.resolve(_effective(passport_number=None, citizenship=None))

    assert not resolution.is_complete
    assert len(resolution.missing) == 2


# ---- NoPersonalInfoProvider (fail-closed default) ----


async def test_no_personal_info_provider_always_blocks():
    provider = NoPersonalInfoProvider()
    resolution = await provider.resolve(_effective())

    assert not resolution.is_complete
    assert len(resolution.missing) == 3  # страхователь/водитель/собственник
