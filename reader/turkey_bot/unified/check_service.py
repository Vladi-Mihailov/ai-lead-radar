"""UnifiedTurkeyCheckService — единственная точка входа для проверки
номера через ВСЕ три провайдера (GIB/Avrasya/KGM), используется ОДИНАКОВО
и для ручной "🔎 Проверить сейчас" (reader/turkey_bot/conversation.py),
и для планового мониторинга 13:00/21:00 (reader/turkey_bot/monitoring/
monitoring_service.py) — перенесено из reader/turkey_bot_test/unified/
check_service.py (см. задачу "Перенос Unified Turkey функционала в
production"): GİB/Avrasya/KGM providers НЕ дублируются — импортируются
из reader/turkey_bot/{gib,avrasya,kgm}/*, которые уже байт-в-байт
идентичны своим test-аналогам (см. READ-ONLY аудит).

Провайдеры полностью независимы (asyncio.gather(..., return_exceptions=True))
— ошибка/исключение одного НЕ мешает остальным.

CAPTCHA — см. captcha_resolver.py: РОВНО одна попытка на провайдера через
CaptchaResolver, БЕЗ retry/refresh-циклов здесь — недоступный/отклонённый
код -> ProviderCheckResult(status=ERROR), НИКОГДА NO_DEBT. Резолвер (OCR)
переносится БЕЗ ИЗМЕНЕНИЙ из test — "существующая абстракция", не новая
и не улучшенная (см. задачу п.3).

ОТЛИЧИЕ от test-версии — сохранение регрессии, найденной READ-ONLY
аудитом (см. задачу п.4): production ДО этого переноса переводил
location/violation_description GIB-штрафов на русский через
TurkeyFineTranslationService (reader/turkey_bot/conversation.py::
_translate_fines). Test unified-flow этот вызов никогда не делал.
Здесь — необязательный `gib_translator` (см. GibFineTranslatorLike
ниже, тот же Protocol-приём, что и FineTranslatorLike в
reader/turkey_bot/conversation.py) вызывается ТОЛЬКО для has_debt,
ТОЛЬКО что полученного (raw) GibSubmitOutcome.fines, ДО построения
DebtItem — fail-open (см. _check_gib): сбой перевода никогда не
превращает успешную проверку в ошибку, просто показывает турецкий
оригинал (тот же принцип, что и в production conversation.py)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol

import httpx

from reader.turkey_bot.avrasya.models import AvrasyaSubmitOutcome
from reader.turkey_bot.avrasya.provider import AvrasyaProvider
from reader.turkey_bot.avrasya.session import (
    AvrasyaRateLimitedError,
    AvrasyaSession,
    AvrasyaTransportError,
)
from reader.turkey_bot.gib.models import GibFineRecord, GibSubmitOutcome
from reader.turkey_bot.gib.provider import GibProvider
from reader.turkey_bot.gib.session import GibSession, GibTransportError
from reader.turkey_bot.gib.translation import FineTranslationError
from reader.turkey_bot.kgm.models import KgmSubmitOutcome
from reader.turkey_bot.kgm.provider import KgmProvider
from reader.turkey_bot.kgm.session import KgmSession, KgmTransportError
from reader.turkey_bot.unified.captcha_resolver import (
    CaptchaResolver,
    DefaultCaptchaResolver,
)
from reader.turkey_bot.unified.models import (
    DebtItem,
    ProviderCheckResult,
    ProviderStatus,
    UnifiedCheckResult,
    derive_overall_status,
    total_amount_for,
)

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_REQUEST_TIMEOUT_SECONDS = 30.0


def _build_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS, headers={"User-Agent": _USER_AGENT})


def default_gib_check_factory() -> tuple[httpx.AsyncClient, GibProvider]:
    client = _build_client()
    return client, GibProvider(GibSession(client))


def default_avrasya_check_factory() -> tuple[httpx.AsyncClient, AvrasyaProvider]:
    client = _build_client()
    return client, AvrasyaProvider(AvrasyaSession(client))


def default_kgm_check_factory() -> tuple[httpx.AsyncClient, KgmProvider]:
    client = _build_client()
    return client, KgmProvider(KgmSession(client))


class _AsyncCloseable(Protocol):
    async def aclose(self) -> None: ...


GibCheckFactory = Callable[[], tuple[_AsyncCloseable, GibProvider]]
AvrasyaCheckFactory = Callable[[], tuple[_AsyncCloseable, AvrasyaProvider]]
KgmCheckFactory = Callable[[], tuple[_AsyncCloseable, KgmProvider]]


class GibFineTranslatorLike(Protocol):
    """Ровно то, что нужно отсюда от TurkeyFineTranslationService (см.
    reader/turkey_bot/gib/translation.py) — тот же Protocol-приём, что и
    FineTranslatorLike в reader/turkey_bot/conversation.py (сознательно
    НЕ импортируется оттуда: unified/check_service.py не должен зависеть
    от conversation.py, во избежание циклического импорта — conversation.py
    сам импортирует check_service.py)."""

    async def translate_fines(
        self, fines: tuple[GibFineRecord, ...],
    ) -> tuple[GibFineRecord, ...]: ...


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _error_result(provider: str, error_type: str, *, checked_at: datetime) -> ProviderCheckResult:
    return ProviderCheckResult(
        provider=provider, status=ProviderStatus.ERROR, debt_count=0,
        principal_amount=None, penalty_amount=None, total_amount=Decimal(0),
        items=(), error_type=error_type, checked_at=checked_at,
    )


def _gib_fine_description(fine: GibFineRecord) -> str | None:
    """Собирает ОДНУ строку описания штрафа для DebtItem.description —
    используются *_ru поля, КОГДА они заполнены (см. reader/turkey_bot/
    gib/translation.py::translate_fines), иначе исходный турецкий текст
    (тот же fallback-принцип "location_ru or location", что и в
    reader/turkey_bot/texts.py::_format_fine_block — сохраняет production
    перевод, найденный READ-ONLY аудитом как regression относительно
    test unified-flow, см. модуль docstring)."""
    location = fine.location_ru or fine.location
    violation = fine.violation_description_ru or fine.violation_description or fine.description
    if location and violation:
        return f"{location} — {violation}"
    return violation or location


def _gib_outcome_to_result(outcome: GibSubmitOutcome, *, checked_at: datetime) -> ProviderCheckResult:
    if outcome.kind == "no_debt":
        return ProviderCheckResult(
            provider="gib", status=ProviderStatus.NO_DEBT, debt_count=0,
            principal_amount=Decimal(0), penalty_amount=Decimal(0), total_amount=Decimal(0),
            items=(), error_type=None, checked_at=checked_at,
        )
    if outcome.kind == "has_debt":
        # Та же формула, что и texts.py::format_has_debt_messages (сумма
        # amount + late_fee - discount по каждому штрафу) — не изобретаю
        # новую математику для той же величины.
        principal = Decimal(0)
        penalty = Decimal(0)
        total = Decimal(0)
        items = []
        for fine in outcome.fines:
            if fine.amount is not None:
                principal += fine.amount
                total += fine.amount
            if fine.late_fee is not None:
                penalty += fine.late_fee
                total += fine.late_fee
            if fine.discount is not None:
                total -= fine.discount
            items.append(DebtItem(
                reference=fine.protocol_no, amount=fine.amount or Decimal(0),
                description=_gib_fine_description(fine),
            ))
        return ProviderCheckResult(
            provider="gib", status=ProviderStatus.HAS_DEBT, debt_count=len(outcome.fines),
            principal_amount=principal, penalty_amount=penalty, total_amount=total,
            items=tuple(items), error_type=None, checked_at=checked_at,
        )
    if outcome.kind == "rejected":
        return _error_result("gib", "captcha_rejected", checked_at=checked_at)
    return _error_result("gib", "unexpected", checked_at=checked_at)


def _avrasya_outcome_to_result(
    outcome: AvrasyaSubmitOutcome, *, checked_at: datetime,
) -> ProviderCheckResult:
    if outcome.kind == "no_debt":
        return ProviderCheckResult(
            provider="avrasya", status=ProviderStatus.NO_DEBT, debt_count=0,
            principal_amount=Decimal(0), penalty_amount=Decimal(0), total_amount=Decimal(0),
            items=(), error_type=None, checked_at=checked_at,
        )
    if outcome.kind == "has_debt":
        # Та же формула, что и texts.py::format_avrasya_has_debt_message.
        principal = sum((item.principal_amount for item in outcome.debt_items), Decimal(0))
        penalty = sum(
            (item.penalty_amount for item in outcome.debt_items if item.penalty_amount is not None),
            Decimal(0),
        )
        total = sum((item.total_amount for item in outcome.debt_items), Decimal(0))
        # AvrasyaDebtItem не несёт стабильного id — reference всегда None,
        # item-level diff для Avrasya невозможен.
        items = tuple(
            DebtItem(reference=None, amount=item.total_amount, description=item.service_file_type)
            for item in outcome.debt_items
        )
        return ProviderCheckResult(
            provider="avrasya", status=ProviderStatus.HAS_DEBT, debt_count=len(outcome.debt_items),
            principal_amount=principal, penalty_amount=penalty, total_amount=total,
            items=items, error_type=None, checked_at=checked_at,
        )
    if outcome.kind == "rejected":
        return _error_result("avrasya", "captcha_rejected", checked_at=checked_at)
    return _error_result("avrasya", "unexpected", checked_at=checked_at)


def _kgm_outcome_to_result(outcome: KgmSubmitOutcome, *, checked_at: datetime) -> ProviderCheckResult:
    if outcome.kind == "no_debt":
        return ProviderCheckResult(
            provider="kgm", status=ProviderStatus.NO_DEBT, debt_count=0,
            principal_amount=Decimal(0), penalty_amount=Decimal(0), total_amount=Decimal(0),
            items=(), error_type=None, checked_at=checked_at,
        )
    if outcome.kind == "has_debt":
        all_items = [item for operator in outcome.operators for item in operator.items]
        principal = sum((item.base_toll for item in all_items), Decimal(0))
        total = outcome.grand_total if outcome.grand_total is not None else Decimal(0)
        penalty = total - principal if total > principal else Decimal(0)
        # KgmDebtItem не несёт стабильного id — reference всегда None.
        items = tuple(
            DebtItem(reference=None, amount=item.payable_amount, description=operator.operator_name)
            for operator in outcome.operators
            for item in operator.items
        )
        return ProviderCheckResult(
            provider="kgm", status=ProviderStatus.HAS_DEBT, debt_count=len(all_items),
            principal_amount=principal, penalty_amount=penalty, total_amount=total,
            items=items, error_type=None, checked_at=checked_at,
        )
    if outcome.kind == "rejected":
        return _error_result("kgm", "captcha_rejected", checked_at=checked_at)
    return _error_result("kgm", "unexpected", checked_at=checked_at)


class UnifiedTurkeyCheckService:
    def __init__(
        self,
        gib_check_factory: GibCheckFactory,
        avrasya_check_factory: AvrasyaCheckFactory,
        kgm_check_factory: KgmCheckFactory,
        *,
        captcha_resolver: CaptchaResolver | None = None,
        gib_translator: GibFineTranslatorLike | None = None,
    ):
        self._gib_check_factory = gib_check_factory
        self._avrasya_check_factory = avrasya_check_factory
        self._kgm_check_factory = kgm_check_factory
        self._captcha_resolver = captcha_resolver or DefaultCaptchaResolver()
        self._gib_translator = gib_translator

    async def check(self, plate: str) -> UnifiedCheckResult:
        started_at = _now()
        results = await asyncio.gather(
            self._check_gib(plate),
            self._check_avrasya(plate),
            self._check_kgm(plate),
            return_exceptions=True,
        )
        providers = tuple(
            result if isinstance(result, ProviderCheckResult)
            else _error_result(provider_name, "internal_error", checked_at=_now())
            for provider_name, result in zip(("gib", "avrasya", "kgm"), results)
        )
        for provider_name, result in zip(("gib", "avrasya", "kgm"), results):
            if isinstance(result, Exception):
                logger.exception(
                    "Turkey unified check: непойманное исключение в провайдере %s (plate=%s)",
                    provider_name, plate, exc_info=result,
                )
        finished_at = _now()
        overall_status = derive_overall_status(providers)
        return UnifiedCheckResult(
            plate=plate, started_at=started_at, finished_at=finished_at,
            overall_status=overall_status, total_amount=total_amount_for(providers),
            providers=providers,
        )

    async def _translate_gib_fines(
        self, fines: tuple[GibFineRecord, ...],
    ) -> tuple[GibFineRecord, ...]:
        """Fail-open (см. reader/turkey_bot/conversation.py::
        _translate_fines — идентичный принцип): нет translator'а ИЛИ сбой
        перевода -> исходные (турецкие) fines без изменений, НИКОГДА не
        превращает успешную проверку в ошибку. Логируется только факт
        сбоя, НИКОГДА исходный/переведённый текст."""
        if self._gib_translator is None:
            return fines
        try:
            return await self._gib_translator.translate_fines(fines)
        except FineTranslationError:
            logger.warning(
                "Turkey unified GIB: перевод недоступен, показываю оригинальный турецкий текст",
            )
            return fines

    async def _check_gib(self, plate: str) -> ProviderCheckResult:
        client, provider = self._gib_check_factory()
        try:
            try:
                challenge = await provider.start()
            except GibTransportError:
                return _error_result("gib", "transport_error", checked_at=_now())

            captcha_code = await self._captcha_resolver.resolve(
                provider="gib", image_png=challenge.image_png,
            )
            if not captcha_code:
                return _error_result("gib", "captcha_unavailable", checked_at=_now())

            try:
                outcome = await provider.submit(
                    plate=plate, image_id=challenge.image_id, captcha_code=captcha_code,
                )
            except GibTransportError:
                return _error_result("gib", "transport_error", checked_at=_now())

            if outcome.kind == "has_debt":
                translated_fines = await self._translate_gib_fines(outcome.fines)
                outcome = replace(outcome, fines=translated_fines)

            return _gib_outcome_to_result(outcome, checked_at=_now())
        finally:
            await client.aclose()

    async def _check_avrasya(self, plate: str) -> ProviderCheckResult:
        client, provider = self._avrasya_check_factory()
        try:
            try:
                challenge = await provider.start()
            except AvrasyaRateLimitedError:
                return _error_result("avrasya", "rate_limited", checked_at=_now())
            except AvrasyaTransportError:
                return _error_result("avrasya", "transport_error", checked_at=_now())

            captcha_code = await self._captcha_resolver.resolve(
                provider="avrasya", image_png=challenge.image_png,
            )
            if not captcha_code:
                return _error_result("avrasya", "captcha_unavailable", checked_at=_now())

            try:
                outcome = await provider.submit(plate=plate, captcha_code=captcha_code)
            except AvrasyaRateLimitedError:
                return _error_result("avrasya", "rate_limited", checked_at=_now())
            except AvrasyaTransportError:
                return _error_result("avrasya", "transport_error", checked_at=_now())

            return _avrasya_outcome_to_result(outcome, checked_at=_now())
        finally:
            await client.aclose()

    async def _check_kgm(self, plate: str) -> ProviderCheckResult:
        client, provider = self._kgm_check_factory()
        try:
            try:
                challenge = await provider.start()
            except KgmTransportError:
                return _error_result("kgm", "transport_error", checked_at=_now())

            captcha_code = await self._captcha_resolver.resolve(
                provider="kgm", image_png=challenge.image_png,
            )
            if not captcha_code:
                return _error_result("kgm", "captcha_unavailable", checked_at=_now())

            try:
                outcome = await provider.submit(plate=plate, captcha_code=captcha_code)
            except KgmTransportError:
                return _error_result("kgm", "transport_error", checked_at=_now())

            return _kgm_outcome_to_result(outcome, checked_at=_now())
        finally:
            await client.aclose()
