"""Тесты "reduce Turkey OCR memory pressure" (FIX 2) — process-wide
asyncio.Lock внутри UnifiedTurkeyCheckService.check() (reader/turkey_bot/
unified/check_service.py, production module). Полностью локальные —
никакого HTTP/CAPTCHA/OCR, fake providers, тайминг проверяется через
asyncio.Event/monotonic()."""

import asyncio
import time
from decimal import Decimal

import pytest

from reader.turkey_bot.avrasya.models import AvrasyaSubmitOutcome
from reader.turkey_bot.gib.models import GibSubmitOutcome
from reader.turkey_bot.gib.session import GibTransportError
from reader.turkey_bot.kgm.models import KgmSubmitOutcome
from reader.turkey_bot.unified.check_service import UnifiedTurkeyCheckService

pytestmark = pytest.mark.asyncio

_NO_DEBT_GIB = GibSubmitOutcome(kind="no_debt", messages=(), raw_data=None)
_NO_DEBT_AVRASYA = AvrasyaSubmitOutcome(kind="no_debt", status_code=404, messages=(), raw_data=None)
_NO_DEBT_KGM = KgmSubmitOutcome(kind="no_debt", kgm_total=Decimal(0), yid_total=Decimal(0), grand_total=Decimal(0))


class _FakeCloseable:
    async def aclose(self) -> None:
        pass


class _FakeCaptchaResolver:
    """Возвращает фиксированный код для любого провайдера — никакого OCR
    (см. tests/test_turkey_bot_unified_check_service.py, тот же приём) —
    без этого фейка check() шёл бы в РЕАЛЬНЫЙ DefaultCaptchaResolver/
    RapidOCR на невалидные PNG-байты фейковых провайдеров, что не имеет
    отношения к тесту lock'а и просто вызвало бы captcha_unavailable."""

    async def resolve(self, *, provider, image_png):
        return "ABCDE"


class _InstantProvider:
    """Немедленно возвращает no_debt — для провайдеров, чьё поведение не
    относится к тесту (только один провайдер в связке "инструментирован")."""

    def __init__(self, outcome):
        self._outcome = outcome
        image_attr = "image_png"
        self._challenge = type("Challenge", (), {image_attr: b"PNG", "image_id": "cid"})()

    async def start(self):
        return self._challenge

    async def submit(self, **kwargs):
        return self._outcome


class _ConcurrencyProbeProvider:
    """Отслеживает, был ли start() вызван ВТОРОЙ раз ДО того, как первый
    вызов успел выйти (см. self._inside) — если lock сломан, ДВА
    конкурентных check() позволят двум вызовам start() перекрыться."""

    def __init__(self, outcome, *, hold_seconds: float = 0.05):
        self._outcome = outcome
        self._hold_seconds = hold_seconds
        self.inside = False
        self.violation = False
        self.start_call_times: list[float] = []
        image_attr = "image_png"
        self._challenge = type("Challenge", (), {image_attr: b"PNG", "image_id": "cid"})()

    async def start(self):
        self.start_call_times.append(time.monotonic())
        if self.inside:
            self.violation = True
        self.inside = True
        await asyncio.sleep(self._hold_seconds)
        self.inside = False
        return self._challenge

    async def submit(self, **kwargs):
        return self._outcome


class _RaisingThenWorkingProvider:
    """Первый вызов start() бросает transport error (см. GİB error path в
    check_service.py) — второй (следующий check()) должен работать
    штатно, доказывая, что lock освобождается и на error-пути тоже."""

    def __init__(self, outcome):
        self._outcome = outcome
        self._first_call = True
        image_attr = "image_png"
        self._challenge = type("Challenge", (), {image_attr: b"PNG", "image_id": "cid"})()

    async def start(self):
        if self._first_call:
            self._first_call = False
            raise GibTransportError("boom")
        return self._challenge

    async def submit(self, **kwargs):
        return self._outcome


class _HangingProvider:
    """start() никогда не завершается сам по себе — используется, чтобы
    вызвать check() отменой/timeout'ом снаружи (см. тест cancellation)."""

    async def start(self):
        await asyncio.sleep(3600)

    async def submit(self, **kwargs):
        raise AssertionError("submit() must never be reached in the hanging-provider test")


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


# ---- 2/3: two concurrent check() calls do not overlap; second waits ----


async def test_two_concurrent_checks_do_not_overlap():
    probe = _ConcurrencyProbeProvider(_NO_DEBT_GIB, hold_seconds=0.05)
    service = _make_service(gib=probe)

    await asyncio.gather(service.check("AA001AA"), service.check("BB002BB"))

    assert probe.violation is False
    # Второй вызов start() начался ПОСЛЕ того, как первый успел завершить
    # свой hold_seconds (не сразу же, параллельно).
    assert len(probe.start_call_times) == 2
    assert probe.start_call_times[1] - probe.start_call_times[0] >= 0.04


# ---- 4: lock released after successful check ----


async def test_lock_released_after_successful_check():
    service = _make_service()

    result1 = await service.check("AA001AA")
    result2 = await service.check("BB002BB")  # would hang forever if lock stayed held

    assert result1.plate == "AA001AA"
    assert result2.plate == "BB002BB"


# ---- 5: lock released after provider exception ----


async def test_lock_released_after_provider_exception():
    flaky = _RaisingThenWorkingProvider(_NO_DEBT_GIB)
    service = _make_service(gib=flaky)

    first = await service.check("AA001AA")  # gib -> transport_error internally, check() itself doesn't raise
    second = await service.check("BB002BB")  # must not hang — proves lock was released

    assert first.plate == "AA001AA"
    assert second.plate == "BB002BB"


# ---- 6: lock released after cancellation ----


async def test_lock_released_after_cancellation():
    service = _make_service(gib=_HangingProvider())

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(service.check("AA001AA"), timeout=0.05)

    # Follow-up check on a service whose previous call was cancelled mid-flight
    # must not hang — async with releases the lock even on cancellation.
    service2 = _make_service()
    result = await asyncio.wait_for(service2.check("BB002BB"), timeout=2.0)
    assert result.plate == "BB002BB"


async def test_lock_released_after_cancellation_same_service_instance():
    """Более строгая версия — ТА ЖЕ service instance, у которой первый
    check() был отменён, должна принять следующий check() без hang."""
    hanging_then_instant = _HangingProvider()
    service = _make_service(gib=hanging_then_instant)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(service.check("AA001AA"), timeout=0.05)

    # Заменяем провайдера на нормальный ПОСЛЕ отмены — сам факт, что
    # check() снова получает управление (не виснет на lock), доказывает
    # release.
    service._gib_check_factory = lambda: (_FakeCloseable(), _InstantProvider(_NO_DEBT_GIB))
    result = await asyncio.wait_for(service.check("BB002BB"), timeout=2.0)
    assert result.plate == "BB002BB"


# ---- 7: providers inside ONE check still run concurrently ----


async def test_providers_within_one_check_still_run_concurrently():
    """asyncio.gather ВНУТРИ check() не менялся — все три провайдера
    ОДНОГО check() всё ещё идут параллельно, а не последовательно."""

    class _SlowProvider:
        def __init__(self, outcome, delay: float):
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

    # Параллельно — ~delay (плюс небольшой overhead), НЕ ~3×delay.
    assert elapsed < delay * 2.5


# ---- 8/9: different logical callers sharing the same service serialize ----


async def test_different_callers_sharing_service_serialize():
    """Manual check и manager refresh (или monitoring) в production делят
    ОДИН и тот же UnifiedTurkeyCheckService instance (см. reader/turkey_bot/
    main.py) — здесь проверяется ровно этот сценарий: ДВА логически
    разных "caller'а" (просто два await service.check(...) из разных
    asyncio-тасков) на одном instance не пересекаются."""
    probe = _ConcurrencyProbeProvider(_NO_DEBT_GIB, hold_seconds=0.05)
    service = _make_service(gib=probe)

    async def manual_check_caller():
        return await service.check("MANUAL01")

    async def manager_refresh_caller():
        return await service.check("REFRESH01")

    await asyncio.gather(manual_check_caller(), manager_refresh_caller())

    assert probe.violation is False


# ---- No deadlock: check() does not recursively call itself ----


async def test_check_does_not_reenter_lock_recursively():
    """Прямое подтверждение отсутствия deadlock-риска — check() вызывает
    ТОЛЬКО _check_gib/_check_avrasya/_check_kgm внутри уже удерживаемого
    lock, ни один из них не вызывает self.check() повторно (это было бы
    вечным deadlock'ом на не-reentrant asyncio.Lock)."""
    service = _make_service()
    result = await asyncio.wait_for(service.check("AA001AA"), timeout=2.0)
    assert result.plate == "AA001AA"
