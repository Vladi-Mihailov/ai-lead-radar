"""Тесты TurkeyDebtRefreshService (@ProtocolTRbot, задача "manual Turkey
debt refresh") — реальные TurkeyUserCarsRepository/TurkeyCheckRunRepository/
TurkeyStatisticsService (SQLite/:memory:), UnifiedTurkeyCheckService — с
лёгкими fake GİB/Avrasya/KGM providers (тот же приём, что и в
tests/test_turkey_bot_unified_check_service.py), НИКАКИХ реальных HTTP-
запросов/OCR/CAPTCHA-решателей.
"""

import asyncio
import inspect
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from reader.turkey_bot.avrasya.models import AvrasyaDebtItem, AvrasyaSubmitOutcome
from reader.turkey_bot.debt_refresh_service import (
    TurkeyDebtRefreshService,
    TurkeyRefreshAlreadyInProgressError,
)
from reader.turkey_bot.gib.models import GibFineRecord, GibSubmitOutcome
from reader.turkey_bot.gib.session import GibTransportError
from reader.turkey_bot.kgm.models import KgmSubmitOutcome
from reader.turkey_bot.known_users_repository import TurkeyBotKnownUsersRepository
from reader.turkey_bot.monitoring.subscription_repository import (
    TurkeyMonitoringSubscriptionRepository,
)
from reader.turkey_bot.statistics_service import TurkeyStatisticsService
from reader.turkey_bot.unified.check_service import UnifiedTurkeyCheckService
from reader.turkey_bot.unified.run_repository import TurkeyCheckRunRepository
from reader.turkey_bot.user_cars_repository import TurkeyUserCarsRepository

pytestmark = pytest.mark.asyncio

_TZ = ZoneInfo("UTC")


class _FakeCloseable:
    async def aclose(self) -> None:
        pass


class _FakeGibProvider:
    def __init__(self, *, start_exc=None, outcome=None):
        self._start_exc = start_exc
        self._outcome = outcome

    async def start(self):
        if self._start_exc:
            raise self._start_exc
        from reader.turkey_bot.gib.models import CaptchaChallenge
        return CaptchaChallenge(image_id="cid-1", image_png=b"GIB-PNG")

    async def submit(self, *, plate, image_id, captcha_code):
        return self._outcome


class _FakeAvrasyaProvider:
    def __init__(self, *, start_exc=None, outcome=None):
        self._start_exc = start_exc
        self._outcome = outcome

    async def start(self):
        if self._start_exc:
            raise self._start_exc
        from reader.turkey_bot.avrasya.models import AvrasyaCaptchaChallenge
        return AvrasyaCaptchaChallenge(image_png=b"AVRASYA-PNG")

    async def submit(self, *, plate, captcha_code):
        return self._outcome


class _FakeKgmProvider:
    def __init__(self, *, start_exc=None, outcome=None):
        self._start_exc = start_exc
        self._outcome = outcome

    async def start(self):
        if self._start_exc:
            raise self._start_exc
        from reader.turkey_bot.kgm.models import KgmCaptchaChallenge
        return KgmCaptchaChallenge(image_png=b"KGM-PNG")

    async def submit(self, *, plate, captcha_code):
        return self._outcome


class _FakeCaptchaResolver:
    async def resolve(self, *, provider, image_png):
        return "ABCDE"


def _gib_outcome(amount: Decimal) -> GibSubmitOutcome:
    if amount <= 0:
        return GibSubmitOutcome(kind="no_debt", messages=(), raw_data=None)
    return GibSubmitOutcome(
        kind="has_debt", messages=(), raw_data=None,
        fines=(GibFineRecord(
            protocol_no="P1", plate="X", amount=amount, description="d",
            violation_date=None, authority=None, late_fee=None, discount=None,
        ),),
    )


def _avrasya_outcome(amount: Decimal) -> AvrasyaSubmitOutcome:
    if amount <= 0:
        return AvrasyaSubmitOutcome(kind="no_debt", status_code=404, messages=(), raw_data=None)
    return AvrasyaSubmitOutcome(
        kind="has_debt", status_code=200, messages=(), raw_data=None,
        debt_items=(AvrasyaDebtItem(
            principal_amount=amount, total_amount=amount, service_file_type="toll",
        ),),
    )


def _kgm_outcome(amount: Decimal) -> KgmSubmitOutcome:
    if amount <= 0:
        return KgmSubmitOutcome(kind="no_debt", kgm_total=Decimal(0), yid_total=Decimal(0), grand_total=Decimal(0))
    return KgmSubmitOutcome(
        kind="has_debt", operators=(), kgm_total=amount, yid_total=Decimal(0), grand_total=amount,
    )


def _make_check_service(
    *, gib: Decimal = Decimal(0), avrasya: Decimal = Decimal(0), kgm: Decimal = Decimal(0),
    gib_error: bool = False, avrasya_error: bool = False, kgm_error: bool = False,
) -> UnifiedTurkeyCheckService:
    gib_provider = _FakeGibProvider(
        start_exc=GibTransportError("boom") if gib_error else None, outcome=_gib_outcome(gib),
    )
    avrasya_provider = _FakeAvrasyaProvider(
        start_exc=GibTransportError("boom") if avrasya_error else None, outcome=_avrasya_outcome(avrasya),
    )
    kgm_provider = _FakeKgmProvider(
        start_exc=GibTransportError("boom") if kgm_error else None, outcome=_kgm_outcome(kgm),
    )
    return UnifiedTurkeyCheckService(
        lambda: (_FakeCloseable(), gib_provider),
        lambda: (_FakeCloseable(), avrasya_provider),
        lambda: (_FakeCloseable(), kgm_provider),
        captcha_resolver=_FakeCaptchaResolver(),
    )


class _Fixture:
    def __init__(self, *, check_service: UnifiedTurkeyCheckService | None = None):
        self.garage = TurkeyUserCarsRepository(":memory:")
        self.runs = TurkeyCheckRunRepository(":memory:")
        self.subscriptions = TurkeyMonitoringSubscriptionRepository(":memory:")
        self.known_users = TurkeyBotKnownUsersRepository(":memory:")
        self.statistics = TurkeyStatisticsService(self.known_users, self.runs, self.subscriptions)
        self.check_service = check_service or _make_check_service()
        self.debt_refresh = TurkeyDebtRefreshService(
            self.garage, self.runs, self.check_service, self.statistics,
        )

    def add_car(self, *, telegram_user_id: int, car_number: str) -> None:
        self.garage.add_car(telegram_user_id=telegram_user_id, car_number=car_number)

    async def seed(self, *, telegram_user_id: int, plate: str, amount: Decimal, days_ago: int = 0) -> None:
        """Устанавливает начальное достоверное состояние ЧЕРЕЗ реальный
        UnifiedTurkeyCheckService.check() + save() + update_last_result() —
        РОВНО та же последовательность, что и ConversationController.
        _run_manual_check(), а не прямой INSERT."""
        seed_service = _make_check_service(gib=amount)
        result = await seed_service.check(plate)
        run_id = self.runs.save(
            result, telegram_user_id=telegram_user_id, telegram_chat_id=telegram_user_id, initiator="manual",
        )
        self.garage.update_last_result(
            telegram_user_id=telegram_user_id, car_number=plate,
            overall_status=result.overall_status.value, total_amount=result.total_amount,
        )
        if days_ago:
            finished_at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
            self.runs._conn.execute(
                "UPDATE turkey_check_runs SET finished_at = ? WHERE id = ?", (finished_at, run_id),
            )
            self.runs._conn.commit()


@pytest.fixture
def fx():
    return _Fixture()


# ---- селекция кандидатов (задача TESTS п.4-6) ----


async def test_only_debt_gt_zero_cars_are_checked(fx):
    fx.add_car(telegram_user_id=1, car_number="AA001AA")
    fx.add_car(telegram_user_id=2, car_number="BB002BB")
    fx.add_car(telegram_user_id=3, car_number="CC003CC")
    await fx.seed(telegram_user_id=1, plate="AA001AA", amount=Decimal(500))  # debt > 0 -> candidate
    await fx.seed(telegram_user_id=2, plate="BB002BB", amount=Decimal(0))  # known 0 -> not checked
    # CC003CC never checked -> unknown -> not checked.

    candidates = fx.debt_refresh.list_candidates()

    assert [row.car_number for row in candidates] == ["AA001AA"]


async def test_confirm_flow_checks_exactly_the_candidates(fx):
    fx.add_car(telegram_user_id=1, car_number="AA001AA")
    fx.add_car(telegram_user_id=2, car_number="BB002BB")
    await fx.seed(telegram_user_id=1, plate="AA001AA", amount=Decimal(500))
    await fx.seed(telegram_user_id=2, plate="BB002BB", amount=Decimal(0))

    checked_plates: list[str] = []
    original_check = fx.check_service.check

    async def _spy_check(plate, **kwargs):
        checked_plates.append(plate)
        return await original_check(plate, **kwargs)

    fx.check_service.check = _spy_check

    outcome = await fx.debt_refresh.refresh()

    assert checked_plates == ["AA001AA"]
    assert outcome.checked == 1


# ---- СЕМАНТИКА: погашена/осталась/ERROR сохраняет прежнее (задача ----


async def test_reliable_zero_makes_car_disappear_from_debt_list(fx):
    fx.add_car(telegram_user_id=1, car_number="AA001AA")
    await fx.seed(telegram_user_id=1, plate="AA001AA", amount=Decimal(2740))
    fx.debt_refresh._check_service = _make_check_service(gib=Decimal(0))

    outcome = await fx.debt_refresh.refresh()

    assert outcome.paid == 1
    assert outcome.remains == 0
    assert outcome.incomplete == 0
    assert fx.debt_refresh.list_candidates() == []


async def test_reliable_lower_amount_updates_stored_total(fx):
    fx.add_car(telegram_user_id=1, car_number="AA001AA")
    await fx.seed(telegram_user_id=1, plate="AA001AA", amount=Decimal(2740))
    fx.debt_refresh._check_service = _make_check_service(gib=Decimal(1800))

    outcome = await fx.debt_refresh.refresh()

    assert outcome.remains == 1
    rows = fx.debt_refresh.list_candidates()
    assert len(rows) == 1
    assert rows[0].total_amount == Decimal(1800)


async def test_reliable_higher_amount_updates_stored_total(fx):
    fx.add_car(telegram_user_id=1, car_number="AA001AA")
    await fx.seed(telegram_user_id=1, plate="AA001AA", amount=Decimal(1000))
    fx.debt_refresh._check_service = _make_check_service(gib=Decimal(3000))

    outcome = await fx.debt_refresh.refresh()

    assert outcome.remains == 1
    rows = fx.debt_refresh.list_candidates()
    assert rows[0].total_amount == Decimal(3000)


async def test_error_after_previous_debt_preserves_previous_reliable_debt(fx):
    fx.add_car(telegram_user_id=1, car_number="AA001AA")
    await fx.seed(telegram_user_id=1, plate="AA001AA", amount=Decimal(2740))
    fx.debt_refresh._check_service = _make_check_service(gib_error=True, avrasya_error=True, kgm_error=True)

    outcome = await fx.debt_refresh.refresh()

    assert outcome.incomplete == 1
    assert outcome.paid == 0
    assert outcome.remains == 0
    rows = fx.debt_refresh.list_candidates()
    assert len(rows) == 1
    assert rows[0].total_amount == Decimal(2740)  # предыдущее достоверное состояние НЕ стёрто


async def test_partial_result_is_stored_and_marked_incomplete_in_summary(fx):
    """См. задачу "PARTIAL" — GİB упал (ERROR), Avrasya/KGM успешны с
    долгом -> overall PARTIAL, total_amount > 0 (см. derive_overall_status/
    total_amount_for) — становится НОВЫМ reliable состоянием (см.
    get_latest_reliable_for_owner — PARTIAL не исключается), но summary
    честно относит его к "не удалось проверить полностью", а не к
    "задолженность осталась"."""
    fx.add_car(telegram_user_id=1, car_number="AA001AA")
    await fx.seed(telegram_user_id=1, plate="AA001AA", amount=Decimal(500))
    fx.debt_refresh._check_service = _make_check_service(gib_error=True, kgm=Decimal(900))

    outcome = await fx.debt_refresh.refresh()

    assert outcome.incomplete == 1
    assert outcome.remains == 0
    assert outcome.paid == 0
    rows = fx.debt_refresh.list_candidates()
    assert len(rows) == 1
    assert rows[0].is_partial is True
    assert rows[0].total_amount == Decimal(900)


# ---- Avrasya/KGM double-counting (задача TESTS п.11-12) ----


async def test_authoritative_total_reused_avrasya_not_double_counted(fx):
    """GİB 80 + Avrasya 2580 + KGM 2660 -> authoritative total 2740 (см.
    задачу "Пример" — Avrasya уже включена в KGM, total_amount_for()
    исключает Avrasya из суммы), НЕ 5320 от наивного сложения всех трёх."""
    fx.add_car(telegram_user_id=1, car_number="AA001AA")
    await fx.seed(telegram_user_id=1, plate="AA001AA", amount=Decimal(1))
    fx.debt_refresh._check_service = _make_check_service(
        gib=Decimal(80), avrasya=Decimal(2580), kgm=Decimal(2660),
    )

    await fx.debt_refresh.refresh()

    rows = fx.debt_refresh.list_candidates()
    assert rows[0].total_amount == Decimal(2740)


# ---- Duplicate plates / multiple owners (задача TESTS п.13) ----


async def test_duplicate_plate_multiple_owners_one_check_two_saved_runs(fx):
    """Один и тот же plate у ДВУХ разных владельцев — ОДИН provider-check
    (см. модуль docstring про переиспользование), но ОТДЕЛЬНАЯ
    turkey_check_runs-строка/классификация для КАЖДОГО owner."""
    fx.add_car(telegram_user_id=1, car_number="M295YB196")
    fx.add_car(telegram_user_id=2, car_number="M295YB196")
    await fx.seed(telegram_user_id=1, plate="M295YB196", amount=Decimal(500))
    await fx.seed(telegram_user_id=2, plate="M295YB196", amount=Decimal(700))

    checked_plates: list[str] = []
    original_check = fx.check_service.check

    async def _spy_check(plate, **kwargs):
        checked_plates.append(plate)
        return await original_check(plate, **kwargs)

    fx.check_service.check = _spy_check

    outcome = await fx.debt_refresh.refresh()

    assert checked_plates == ["M295YB196"]  # ОДИН внешний запрос на уникальный plate
    assert outcome.checked == 2  # но ДВА owner/car кандидата классифицированы
    reliable_1 = fx.runs.get_latest_reliable_for_owner(plate="M295YB196", telegram_user_id=1)
    reliable_2 = fx.runs.get_latest_reliable_for_owner(plate="M295YB196", telegram_user_id=2)
    assert reliable_1 is not None and reliable_2 is not None
    # Оба владельца получили ОДИНАКОВЫЙ (общий) новый результат.
    assert reliable_1[1] == reliable_2[1]


# ---- Нет уведомлений владельцам (задача TESTS п.14) ----


def test_debt_refresh_service_has_no_notification_dependency():
    params = list(inspect.signature(TurkeyDebtRefreshService.__init__).parameters)
    assert params == ["self", "garage_repository", "run_repository", "check_service", "statistics_service"]

    import reader.turkey_bot.debt_refresh_service as module
    assert not hasattr(module, "TurkeyMonitoringService")
    assert not hasattr(module, "_TelethonNotifier")


# ---- Concurrency (задача TESTS п.15) ----


async def test_concurrent_refresh_is_rejected(fx):
    fx.add_car(telegram_user_id=1, car_number="AA001AA")
    await fx.seed(telegram_user_id=1, plate="AA001AA", amount=Decimal(5))

    started = asyncio.Event()
    release = asyncio.Event()

    class _SlowGibProvider(_FakeGibProvider):
        async def start(self):
            started.set()
            await release.wait()
            return await super().start()

    slow_service = _make_check_service(gib=Decimal(0))
    slow_service._gib_check_factory = lambda: (_FakeCloseable(), _SlowGibProvider(outcome=_gib_outcome(Decimal(0))))
    fx.debt_refresh._check_service = slow_service

    first = asyncio.ensure_future(fx.debt_refresh.refresh())
    await started.wait()

    with pytest.raises(TurkeyRefreshAlreadyInProgressError):
        await fx.debt_refresh.refresh()

    assert fx.debt_refresh.is_in_progress() is True
    release.set()
    await first
    assert fx.debt_refresh.is_in_progress() is False


# ---- Итоговая сумма было/стало (доп. деталь задачи RESULT) ----


async def test_outcome_reports_total_before_and_after(fx):
    fx.add_car(telegram_user_id=1, car_number="AA001AA")
    fx.add_car(telegram_user_id=2, car_number="BB002BB")
    await fx.seed(telegram_user_id=1, plate="AA001AA", amount=Decimal(500))
    await fx.seed(telegram_user_id=2, plate="BB002BB", amount=Decimal(300))
    fx.debt_refresh._check_service = _make_check_service(gib=Decimal(0))  # оба погашены

    outcome = await fx.debt_refresh.refresh()

    assert outcome.total_before == Decimal(800)
    assert outcome.total_after == Decimal(0)
