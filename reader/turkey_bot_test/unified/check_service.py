"""UnifiedTurkeyCheckService — единственная точка входа для проверки
номера через ВСЕ три провайдера (GIB/Avrasya/KGM), используется ОДИНАКОВО
и для ручной "🔎 Проверить сейчас" (reader/turkey_bot_test/conversation.py),
и для планового мониторинга 13:00/21:00 (reader/turkey_bot_test/monitoring/
monitoring_service.py) — см. design report п.12: "Manual check vs
monitoring... Оба должны использовать ОДИН unified checking service".

Провайдеры полностью независимы (asyncio.gather(..., return_exceptions=True))
— ошибка/исключение одного НЕ мешает остальным (см. design report п.4).

CAPTCHA — см. captcha_resolver.py: РОВНО одна попытка на провайдера через
CaptchaResolver, БЕЗ retry/refresh-циклов здесь (см. design report решение
п.7) — недоступный/отклонённый код -> ProviderCheckResult(status=ERROR),
НИКОГДА не NO_DEBT."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol

import httpx

from reader.turkey_bot_test.avrasya.models import AvrasyaSubmitOutcome
from reader.turkey_bot_test.avrasya.provider import AvrasyaProvider
from reader.turkey_bot_test.avrasya.session import (
    AvrasyaRateLimitedError,
    AvrasyaSession,
    AvrasyaTransportError,
)
from reader.turkey_bot_test.gib.models import GibSubmitOutcome
from reader.turkey_bot_test.gib.provider import GibProvider
from reader.turkey_bot_test.gib.session import GibSession, GibTransportError
from reader.turkey_bot_test.kgm.models import KgmSubmitOutcome
from reader.turkey_bot_test.kgm.provider import KgmProvider
from reader.turkey_bot_test.kgm.session import KgmSession, KgmTransportError
from reader.turkey_bot_test.unified.captcha_resolver import (
    CaptchaResolver,
    DefaultCaptchaResolver,
)
from reader.turkey_bot_test.unified.models import (
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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _error_result(provider: str, error_type: str, *, checked_at: datetime) -> ProviderCheckResult:
    return ProviderCheckResult(
        provider=provider, status=ProviderStatus.ERROR, debt_count=0,
        principal_amount=None, penalty_amount=None, total_amount=Decimal(0),
        items=(), error_type=error_type, checked_at=checked_at,
    )


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
                description=fine.violation_description or fine.description,
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
        # AvrasyaDebtItem не несёт стабильного id (см. design report решение
        # п.5) — reference всегда None, item-level diff для Avrasya невозможен.
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
        # KgmDebtItem не несёт стабильного id (см. design report решение
        # п.5) — reference всегда None.
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
    ):
        self._gib_check_factory = gib_check_factory
        self._avrasya_check_factory = avrasya_check_factory
        self._kgm_check_factory = kgm_check_factory
        self._captcha_resolver = captcha_resolver or DefaultCaptchaResolver()

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
