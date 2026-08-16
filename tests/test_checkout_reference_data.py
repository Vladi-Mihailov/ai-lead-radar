"""Тесты reader/checkout/reference_data.py — реальная сеть не используется,
httpx.AsyncClient подключён к httpx.MockTransport (тот же приём, что и
tests/test_police_ge_session.py)."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402
import pytest  # noqa: E402

from reader.checkout.reference_data import (  # noqa: E402
    ReferenceDataError,
    TplReferenceDataClient,
    parse_policy_period,
)

_CATEGORIES_RESPONSE = [
    {
        "id": 7,
        "key": "მსუბუქი/PassengerCar",
        "products": [
            {"productId": 1, "period": 15, "periodType": "D", "price": 30.0},
            {"productId": 2, "period": 30, "periodType": "D", "price": 50.0},
            {"productId": 4, "period": 1, "periodType": "Y", "price": 295.0},
        ],
    },
    {
        "id": 10,
        "key": "მოტოციკლი/Motorcycle",
        "products": [
            {"productId": 5, "period": 15, "periodType": "D", "price": 20.0},
        ],
    },
]

_MANUFACTURERS_RESPONSE = [
    {"id": 147, "name": "TOYOTA"},
    {"id": 2, "name": "AC"},
    {"id": 3, "name": "ALFA ROMEO"},
]

_MODELS_RESPONSE = [
    {"id": 10574, "name": "TOYOTA CAMRY"},
    {"id": 10484, "name": "TOYOTA KLUGER"},
]

_COUNTRIES_RESPONSE = [
    {"id": 1, "name": "Georgia", "number": "00001", "isPopular": False},
    {"id": 52, "name": "Russia Federation", "number": "00005", "isPopular": True},
    {"id": 51, "name": "Armenia", "number": "00004", "isPopular": True},
]


class _CallLog:
    def __init__(self):
        self.requests: list[httpx.Request] = []


def _make_client(handler, log: _CallLog) -> TplReferenceDataClient:
    def logging_handler(request: httpx.Request) -> httpx.Response:
        log.requests.append(request)
        return handler(request)

    transport = httpx.MockTransport(logging_handler)
    client = httpx.AsyncClient(transport=transport)
    return TplReferenceDataClient(client)


def _default_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/core/categories":
        return httpx.Response(200, json=_CATEGORIES_RESPONSE)
    if request.url.path == "/api/core/vehicles/manufacturers":
        return httpx.Response(200, json=_MANUFACTURERS_RESPONSE)
    if request.url.path == "/api/core/vehicles/manufacturers/147/models":
        return httpx.Response(200, json=_MODELS_RESPONSE)
    if request.url.path == "/api/core/countries":
        return httpx.Response(200, json=_COUNTRIES_RESPONSE)
    return httpx.Response(404)


# ---- parse_policy_period ----


def test_parse_policy_period_days():
    assert parse_policy_period("30-D") == (30, "D")


def test_parse_policy_period_year():
    assert parse_policy_period("1-Y") == (1, "Y")


# ---- category_product ----


async def test_category_product_returns_matching_category_and_product():
    log = _CallLog()
    reference_data = _make_client(_default_handler, log)

    result = await reference_data.category_product("passenger_car", "30-D")

    assert result.vehicle_category_id == 7
    assert result.product_id == 2
    assert result.price == 50.0


async def test_category_product_matches_by_key_suffix_not_hardcoded_id():
    """motorcycle -> id=10 в тестовых данных (в реальном research это было
    10 тоже, но важно, что сопоставление идёт по суффиксу key, а не по
    хардкоженному id — см. docstring модуля)."""
    log = _CallLog()
    reference_data = _make_client(_default_handler, log)

    result = await reference_data.category_product("motorcycle", "15-D")

    assert result.vehicle_category_id == 10
    assert result.product_id == 5


async def test_category_product_caches_categories_response():
    log = _CallLog()
    reference_data = _make_client(_default_handler, log)

    await reference_data.category_product("passenger_car", "30-D")
    await reference_data.category_product("passenger_car", "1-Y")

    categories_calls = [r for r in log.requests if r.url.path == "/api/core/categories"]
    assert len(categories_calls) == 1


async def test_category_product_raises_for_unknown_category():
    reference_data = _make_client(_default_handler, _CallLog())
    with pytest.raises(ReferenceDataError):
        await reference_data.category_product("truck", "30-D")


async def test_category_product_raises_when_period_not_offered():
    reference_data = _make_client(_default_handler, _CallLog())
    with pytest.raises(ReferenceDataError):
        await reference_data.category_product("passenger_car", "90-D")


async def test_category_product_raises_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    reference_data = _make_client(handler, _CallLog())
    with pytest.raises(ReferenceDataError):
        await reference_data.category_product("passenger_car", "30-D")


async def test_category_product_raises_on_invalid_json():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    reference_data = _make_client(handler, _CallLog())
    with pytest.raises(ReferenceDataError):
        await reference_data.category_product("passenger_car", "30-D")


# ---- resolve_manufacturer ----


async def test_resolve_manufacturer_exact_match_case_insensitive():
    reference_data = _make_client(_default_handler, _CallLog())
    match = await reference_data.resolve_manufacturer("toyota")
    assert match.id == 147
    assert match.name == "TOYOTA"


async def test_resolve_manufacturer_fuzzy_match_typo():
    reference_data = _make_client(_default_handler, _CallLog())
    match = await reference_data.resolve_manufacturer("TOYOTAA")
    assert match.id == 147


async def test_resolve_manufacturer_returns_none_when_not_found():
    reference_data = _make_client(_default_handler, _CallLog())
    match = await reference_data.resolve_manufacturer("SOME UNKNOWN BRAND XYZ")
    assert match is None


async def test_resolve_manufacturer_caches_manufacturers_response():
    log = _CallLog()
    reference_data = _make_client(_default_handler, log)

    await reference_data.resolve_manufacturer("TOYOTA")
    await reference_data.resolve_manufacturer("AC")

    manufacturer_calls = [r for r in log.requests if r.url.path == "/api/core/vehicles/manufacturers"]
    assert len(manufacturer_calls) == 1


# ---- resolve_model ----


async def test_resolve_model_exact_match():
    reference_data = _make_client(_default_handler, _CallLog())
    match = await reference_data.resolve_model(147, "TOYOTA CAMRY")
    assert match.id == 10574


async def test_resolve_model_returns_none_when_not_found():
    reference_data = _make_client(_default_handler, _CallLog())
    match = await reference_data.resolve_model(147, "COMPLETELY UNKNOWN MODEL 12345")
    assert match is None


async def test_resolve_model_caches_per_manufacturer():
    log = _CallLog()
    reference_data = _make_client(_default_handler, log)

    await reference_data.resolve_model(147, "TOYOTA CAMRY")
    await reference_data.resolve_model(147, "TOYOTA KLUGER")

    model_calls = [r for r in log.requests if r.url.path == "/api/core/vehicles/manufacturers/147/models"]
    assert len(model_calls) == 1


# ---- resolve_country (гражданство страхователя, checkout tpl.ge) ----


async def test_resolve_country_exact_match():
    reference_data = _make_client(_default_handler, _CallLog())
    match = await reference_data.resolve_country("Georgia")
    assert match.id == 1
    assert match.name == "Georgia"


async def test_resolve_country_exact_match_is_case_insensitive():
    reference_data = _make_client(_default_handler, _CallLog())
    match = await reference_data.resolve_country("russia federation")
    assert match.id == 52


async def test_resolve_country_fuzzy_match_typo():
    reference_data = _make_client(_default_handler, _CallLog())
    match = await reference_data.resolve_country("Georgiaa")
    assert match.id == 1


async def test_resolve_country_returns_none_for_unknown_country():
    """Не выдумываем id для страны, которой нет в справочнике tpl.ge (см.
    задачу: "не придумывай ID")."""
    reference_data = _make_client(_default_handler, _CallLog())
    match = await reference_data.resolve_country("Narnia")
    assert match is None


async def test_resolve_country_caches_countries_response():
    log = _CallLog()
    reference_data = _make_client(_default_handler, log)

    await reference_data.resolve_country("Georgia")
    await reference_data.resolve_country("Armenia")

    country_calls = [r for r in log.requests if r.url.path == "/api/core/countries"]
    assert len(country_calls) == 1
