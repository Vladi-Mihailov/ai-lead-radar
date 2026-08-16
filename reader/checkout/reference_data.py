"""Клиент публичных справочников tpl.ge (категории/тарифы, производители,
модели, страны) — используется reader/checkout/service.py, чтобы
сопоставить свободный текст из OCR (category/manufacturer/model/citizenship)
с числовыми id/productId, которые реально ожидает POST /api/policies (см.
reader/checkout/models.py::TplPolicyPayload).

Все четыре endpoint'а зафиксированы browser research'ом:
- GET /api/core/categories?embed=products
- GET /api/core/vehicles/manufacturers
- GET /api/core/vehicles/manufacturers/{id}/models
- GET /api/core/countries

Публичные, без авторизации/CSRF (см. research-отчёт) — httpx.AsyncClient
без дополнительных заголовков, тот же стиль, что и
reader/fines/police_ge_session.py."""

from __future__ import annotations

import difflib
from dataclasses import dataclass

import httpx

_CATEGORIES_URL = "https://web-back.tpl.ge/api/core/categories"
_MANUFACTURERS_URL = "https://web-back.tpl.ge/api/core/vehicles/manufacturers"
_COUNTRIES_URL = "https://web-back.tpl.ge/api/core/countries"

# Порог уверенности fuzzy-match производителя/модели (difflib.SequenceMatcher
# ratio) — ниже этого значения считаем, что tpl.ge не содержит такого
# значения в справочнике, и просим оператора уточнить, а не подставляем
# случайно похожее совпадение (модель — единственное поле, где справочник
# tpl.ge это тысячи свободных строк на одну марку, см. research-отчёт).
_MATCH_CUTOFF = 0.72


class ReferenceDataError(Exception):
    """Не удалось получить/сопоставить данные со справочником tpl.ge —
    str(exc) показывается оператору (см. reader/checkout/service.py)."""


@dataclass(frozen=True)
class CategoryProduct:
    vehicle_category_id: int
    product_id: int
    price: float


@dataclass(frozen=True)
class ManufacturerMatch:
    id: int
    name: str


@dataclass(frozen=True)
class ModelMatch:
    id: int
    name: str


@dataclass(frozen=True)
class CountryMatch:
    id: int
    name: str


def parse_policy_period(policy_period: str) -> tuple[int, str]:
    """"30-D" -> (30, "D"), "1-Y" -> (1, "Y") — формат тот же, что и period/
    periodType в ответе /api/core/categories?embed=products (см. research-
    отчёт). Валидация самого значения из конфига — забота
    reader/settings.py (fail-fast при загрузке), здесь только разбор."""
    value_str, _, period_type = policy_period.partition("-")
    return int(value_str), period_type


class TplReferenceDataClient:
    def __init__(self, client: httpx.AsyncClient):
        self._client = client
        self._categories_cache: list[dict] | None = None
        self._manufacturers_cache: list[dict] | None = None
        self._models_cache: dict[int, list[dict]] = {}
        self._countries_cache: list[dict] | None = None

    async def category_product(self, category: str, policy_period: str) -> CategoryProduct:
        from reader.checkout.mapping import CATEGORY_KEY_SUFFIX  # локальный импорт — без цикла

        suffix = CATEGORY_KEY_SUFFIX.get(category)
        if suffix is None:
            raise ReferenceDataError(f"Неизвестная категория '{category}' — нет сопоставления с tpl.ge")

        categories = await self._get_categories()
        entry = next((c for c in categories if c.get("key", "").endswith(suffix)), None)
        if entry is None:
            raise ReferenceDataError(f"tpl.ge не вернул категорию, соответствующую '{category}'")

        period_value, period_type = parse_policy_period(policy_period)
        product = next(
            (
                p
                for p in entry.get("products", [])
                if p.get("period") == period_value and p.get("periodType") == period_type
            ),
            None,
        )
        if product is None:
            raise ReferenceDataError(
                f"tpl.ge не вернул тариф для категории '{category}' и периода '{policy_period}'"
            )

        return CategoryProduct(
            vehicle_category_id=entry["id"],
            product_id=product["productId"],
            price=product["price"],
        )

    async def resolve_manufacturer(self, name: str) -> ManufacturerMatch | None:
        manufacturers = await self._get_manufacturers()
        match = _best_match(name, manufacturers)
        if match is None:
            return None
        return ManufacturerMatch(id=match["id"], name=match["name"])

    async def resolve_model(self, manufacturer_id: int, name: str) -> ModelMatch | None:
        models = await self._get_models(manufacturer_id)
        match = _best_match(name, models)
        if match is None:
            return None
        return ModelMatch(id=match["id"], name=match["name"])

    async def resolve_country(self, name: str) -> CountryMatch | None:
        """name — гражданство, как его вернул OCR (см.
        reader/ocr/prompt.py::SYSTEM_PROMPT — ожидается название страны на
        английском, например "Georgia"/"Russia Federation") — сопоставляется
        с реальным справочником tpl.ge, ничего не хардкодится (см. задачу:
        "не придумывай ID")."""
        countries = await self._get_countries()
        match = _best_match(name, countries)
        if match is None:
            return None
        return CountryMatch(id=match["id"], name=match["name"])

    async def _get_categories(self) -> list[dict]:
        if self._categories_cache is None:
            response = await self._client.get(_CATEGORIES_URL, params={"embed": "products"})
            self._categories_cache = _json_list(response, "categories")
        return self._categories_cache

    async def _get_manufacturers(self) -> list[dict]:
        if self._manufacturers_cache is None:
            response = await self._client.get(_MANUFACTURERS_URL)
            self._manufacturers_cache = _json_list(response, "manufacturers")
        return self._manufacturers_cache

    async def _get_models(self, manufacturer_id: int) -> list[dict]:
        if manufacturer_id not in self._models_cache:
            response = await self._client.get(f"{_MANUFACTURERS_URL}/{manufacturer_id}/models")
            self._models_cache[manufacturer_id] = _json_list(response, "models")
        return self._models_cache[manufacturer_id]

    async def _get_countries(self) -> list[dict]:
        if self._countries_cache is None:
            response = await self._client.get(_COUNTRIES_URL)
            self._countries_cache = _json_list(response, "countries")
        return self._countries_cache


def _json_list(response: httpx.Response, what: str) -> list[dict]:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ReferenceDataError(f"tpl.ge вернул ошибку при запросе справочника {what}: {exc}") from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise ReferenceDataError(f"tpl.ge вернул невалидный JSON для справочника {what}") from exc

    if not isinstance(data, list):
        raise ReferenceDataError(f"tpl.ge вернул неожиданный формат для справочника {what}")
    return data


def _best_match(query: str, items: list[dict]) -> dict | None:
    if not query:
        return None

    normalized_query = query.strip().casefold()

    for item in items:
        if str(item.get("name", "")).strip().casefold() == normalized_query:
            return item

    names = [str(item.get("name", "")) for item in items]
    close = difflib.get_close_matches(query, names, n=1, cutoff=_MATCH_CUTOFF)
    if not close:
        return None

    best_name = close[0]
    return next(item for item in items if str(item.get("name", "")) == best_name)
