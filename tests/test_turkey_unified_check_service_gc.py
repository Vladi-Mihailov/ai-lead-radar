"""Тесты "reclaim Turkey OCR memory after checks" — один explicit
gc.collect() ПОСЛЕ каждого полного UnifiedTurkeyCheckService.check()
(reader/turkey_bot/unified/check_service.py, production module). Offline
A/B (см. отчёт) установил: ни onnxruntime, ни OpenCV сами по себе не
удерживают память — полный RapidOCR pipeline накапливает исключительно
GC-collectible Python-циклы, и один gc.collect() после check() надёжно
возвращает RSS почти к baseline.

Большинство тестов подменяют check_service.gc.collect() счётчиком-фейком
(НЕ вызывая реальный full GC без необходимости, см. задачу) — ровно один
тест (test_real_gc_collect_runs_and_logs_debug) использует настоящий
gc.collect(), чтобы доказать сквозную работу end-to-end."""

import asyncio
import logging
import time
from decimal import Decimal

import pytest

import reader.turkey_bot.unified.check_service as check_service_module
from reader.turkey_bot.avrasya.models import AvrasyaSubmitOutcome
from reader.turkey_bot.gib.models import GibSubmitOutcome
from reader.turkey_bot.gib.session import GibTransportError
from reader.turkey_bot.kgm.models import KgmSubmitOutcome
from reader.turkey_bot.unified.check_service import UnifiedTurkeyCheckService
from reader.turkey_bot.unified.models import ProviderStatus

pytestmark = pytest.mark.asyncio

_NO_DEBT_GIB = GibSubmitOutcome(kind="no_debt", messages=(), raw_data=None)
_NO_DEBT_AVRASYA = AvrasyaSubmitOutcome(kind="no_debt", status_code=404, messages=(), raw_data=None)
_NO_DEBT_KGM = KgmSubmitOutcome(kind="no_debt", kgm_total=Decimal(0), yid_total=Decimal(0), grand_total=Decimal(0))


class _FakeCloseable:
    async def aclose(self) -> None:
        pass


class _FakeCaptchaResolver:
    async def resolve(self, *, provider, image_png):
        return "ABCDE"


class _InstantProvider:
    def __init__(self, outcome):
        self._outcome = outcome
        image_attr = "image_png"
        self._challenge = type("Challenge", (), {image_attr: b"PNG", "image_id": "cid"})()

    async def start(self):
        return self._challenge

    async def submit(self, **kwargs):
        return self._outcome


class _RaisingProvider:
    """GİB транспортная ошибка на первой же попытке — check() сам НЕ
    бросает (см. _check_gib: transport_error становится terminal
    ProviderCheckResult), это provider-error путь (item 2)."""

    async def start(self):
        raise GibTransportError("boom")

    async def submit(self, **kwargs):
        raise AssertionError("submit() must never be reached")


class _HangingProvider:
    async def start(self):
        await asyncio.sleep(3600)

    async def submit(self, **kwargs):
        raise AssertionError("submit() must never be reached in the hanging-provider test")


class _CountingGc:
    """Фейк-счётчик вместо реального gc.collect() — большинство тестов
    здесь про ФАКТ вызова/момент вызова, не про поведение самого GC."""

    def __init__(self):
        self.call_count = 0

    def __call__(self):
        self.call_count += 1
        return 0


def _make_service(*, gib=None, avrasya=None, kgm=None) -> UnifiedTurkeyCheckService:
    gib = gib or _InstantProvider(_NO_DEBT_GIB)
    avrasya = avrasya or _InstantProvider(_NO_DEBT_AVRASYA)
    kgm = kgm or _InstantProvider(_NO_DEBT_KGM)
    return UnifiedTurkeyCheckService(
        lambda: (_FakeCloseable(), gib),
        lambda: (_FakeCloseable(), avrasya),
        lambda: (_FakeCloseable(), kgm),
        captcha_resolver=_FakeCaptchaResolver(),
    )


@pytest.fixture
def fake_gc(monkeypatch):
    counter = _CountingGc()
    monkeypatch.setattr(check_service_module.gc, "collect", counter)
    return counter


# ---- 1. gc.collect() called exactly once after a successful check ----


async def test_gc_collect_called_exactly_once_after_successful_check(fake_gc):
    service = _make_service()

    result = await service.check("AA001AA")

    assert result.plate == "AA001AA"
    assert fake_gc.call_count == 1


# ---- 2. gc.collect() called after a provider transport-error path ----


async def test_gc_collect_called_after_provider_error_path(fake_gc):
    service = _make_service(gib=_RaisingProvider())

    result = await service.check("AA001AA")

    gib_result = next(p for p in result.providers if p.provider == "gib")
    assert gib_result.status == ProviderStatus.ERROR
    assert gib_result.error_type == "transport_error"
    assert fake_gc.call_count == 1


# ---- 3. gc.collect() called after a partial result (mixed success/error) ----


async def test_gc_collect_called_after_partial_result(fake_gc):
    service = _make_service(gib=_RaisingProvider())  # gib errors, avrasya/kgm succeed -> PARTIAL-ish mixed result

    result = await service.check("AA001AA")

    statuses = {p.provider: p.status for p in result.providers}
    assert statuses["gib"] == ProviderStatus.ERROR
    assert statuses["avrasya"] == ProviderStatus.NO_DEBT
    assert statuses["kgm"] == ProviderStatus.NO_DEBT
    assert fake_gc.call_count == 1


async def test_gc_collect_called_after_cancellation(fake_gc):
    """См. задачу п.1: "после cancellation cleanup, если это безопасно" —
    finally оборачивает ВЕСЬ метод, включая lock acquisition/gather, так
    что даже отменённый снаружи check() (asyncio.wait_for timeout)
    по-прежнему запускает ровно один gc.collect()."""
    service = _make_service(gib=_HangingProvider())

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(service.check("AA001AA"), timeout=0.05)

    assert fake_gc.call_count == 1


# ---- 4. result returned unchanged (real gc.collect() cannot invalidate it) ----


async def test_result_returned_unchanged_despite_real_gc():
    """Реальный (непатченный) gc.collect() — единственный тест здесь,
    который его вызывает по-настоящему, чтобы доказать сквозную работу:
    возвращаемый результат не искажается, и DEBUG-лог формируется
    корректно (см. _collect_after_check)."""
    service = _make_service()

    result = await service.check("AA001AA")

    assert result.plate == "AA001AA"
    assert result.overall_status is not None
    assert len(result.providers) == 3


async def test_real_gc_collect_runs_and_logs_debug(caplog):
    service = _make_service()

    with caplog.at_level(logging.DEBUG, logger="reader.turkey_bot.unified.check_service"):
        await service.check("AA001AA")

    debug_lines = [r.message for r in caplog.records if "GC collected objects" in r.message]
    assert len(debug_lines) == 1
    assert "plate=AA001AA" in debug_lines[0]


# ---- 5. provider concurrency within one check is still parallel ----


async def test_providers_within_one_check_still_run_concurrently(fake_gc):
    class _SlowProvider:
        def __init__(self, outcome, delay):
            self._outcome = outcome
            self._delay = delay
            image_attr = "image_png"
            self._challenge = type("Challenge", (), {image_attr: b"PNG", "image_id": "cid"})()

        async def start(self):
            await asyncio.sleep(self._delay)
            return self._challenge

        async def submit(self, **kwargs):
            return self._outcome

    delay = 0.1
    service = _make_service(
        gib=_SlowProvider(_NO_DEBT_GIB, delay),
        avrasya=_SlowProvider(_NO_DEBT_AVRASYA, delay),
        kgm=_SlowProvider(_NO_DEBT_KGM, delay),
    )

    started = time.monotonic()
    await service.check("AA001AA")
    elapsed = time.monotonic() - started

    assert elapsed < delay * 2.5
    assert fake_gc.call_count == 1


# ---- 6. global lock still serializes concurrent check() calls ----


async def test_global_lock_still_serializes_with_gc_wrapper(fake_gc):
    class _ConcurrencyProbeProvider:
        def __init__(self, outcome, *, hold_seconds=0.05):
            self._outcome = outcome
            self._hold_seconds = hold_seconds
            self.inside = False
            self.violation = False
            image_attr = "image_png"
            self._challenge = type("Challenge", (), {image_attr: b"PNG", "image_id": "cid"})()

        async def start(self):
            if self.inside:
                self.violation = True
            self.inside = True
            await asyncio.sleep(self._hold_seconds)
            self.inside = False
            return self._challenge

        async def submit(self, **kwargs):
            return self._outcome

    probe = _ConcurrencyProbeProvider(_NO_DEBT_GIB)
    service = _make_service(gib=probe)

    await asyncio.gather(service.check("AA001AA"), service.check("BB002BB"))

    assert probe.violation is False
    assert fake_gc.call_count == 2  # one per logical check, not per provider attempt
