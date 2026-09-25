"""Тесты DebtRefreshService (@ProtocolGEbot, задача "OPTIONAL DEBT
REFRESH") — реальные FineMonitoringTaskRepository/DetectedFineRepository
(SQLite/tmp_path), FineProvider — лёгкий фейк (без сети). Проверяем:
- какие задачи попадают под refresh (только известная сумма > 0);
- что refresh НИКОГДА не трогает уведомления (структурно — у сервиса нет
  ссылки ни на NotificationService, ни на координатор);
- ERROR не стирает предыдущую известную сумму (см. check_service.py —
  check_task() сам не трогает detected_fines при ошибке, сервис только
  честно не вызывает record_successful_check в этом случае);
- защиту от параллельного запуска (см. задачу п.11).
"""

import asyncio
import inspect
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from reader.fines.check_service import FineCheckService
from reader.fines.detected_fine_repository import DetectedFineRepository
from reader.fines.models import ParsedFineRecord
from reader.fines.provider import FineProvider, FineProviderError
from reader.fines.task_repository import FineMonitoringTaskRepository
from reader.public_bot.debt_refresh_service import (
    DebtRefreshService,
    RefreshAlreadyInProgressError,
)
from reader.public_bot.subscription_repository import (
    FineSubscriptionRepository,
)

_CHAT_ID = -100999
_USER_ID = 111


class _FakeProvider(FineProvider):
    """records_by_car/error_cars МУТИРУЮТСЯ вызывающим тестом между
    вызовами check_task() (например, "сначала есть штраф на 40 ₾, потом
    добавился ещё один на 60 ₾" или "сначала ok, потом error") — тот же
    смысл, что и у _FakeProvider в tests/test_public_bot_conversation.py,
    только с добавленной возможностью симулировать FineProviderError по
    конкретному car_number."""

    def __init__(self):
        self.records_by_car: dict[str, list[ParsedFineRecord]] = {}
        self.error_cars: set[str] = set()
        self.requested_plates: list[str] = []

    async def search_by_plate(self, plate: str) -> list[ParsedFineRecord]:
        self.requested_plates.append(plate)
        if plate in self.error_cars:
            raise FineProviderError("simulated transport error")
        return self.records_by_car.get(plate, [])


def _record(*, car_number: str, fingerprint: str, amount: float) -> ParsedFineRecord:
    return ParsedFineRecord(
        car_number=car_number, external_fine_id=fingerprint,
        penalty_date=date(2026, 8, 6), due_date=date(2026, 8, 20),
        delivered_status="Не вручено", fingerprint=fingerprint,
        raw_data={"protocolNo": fingerprint}, amount=amount,
    )


class _Fixture:
    def __init__(self, tmp_path):
        self.db_path = tmp_path / "users.db"
        self.task_repository = FineMonitoringTaskRepository(self.db_path)
        self.detected_fine_repository = DetectedFineRepository(self.db_path)
        self.subscription_repository = FineSubscriptionRepository(self.db_path)
        self.provider = _FakeProvider()
        self.check_service = FineCheckService(
            self.provider, self.task_repository, self.detected_fine_repository,
        )
        self.debt_refresh = DebtRefreshService(
            self.task_repository, self.detected_fine_repository, self.check_service,
        )

    def make_task(self, car_number: str):
        return self.task_repository.create(
            car_number=car_number, label=None,
            start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )

    def add_subscription(self, task, *, telegram_user_id: int) -> None:
        self.subscription_repository.create(
            monitoring_task_id=task.id, car_number=task.car_number,
            telegram_user_id=telegram_user_id, telegram_chat_id=telegram_user_id,
            telegram_username=None, start_date=task.start_date, end_date=task.end_date,
        )

    async def seed_debt(self, task, *, amount: float, fingerprint: str = "fp-seed") -> None:
        """Устанавливает начальное известное состояние ЧЕРЕЗ реальный
        check_task() (а не INSERT напрямую в detected_fines) — та же
        последовательность событий, что и в production: задача сначала
        проверяется штатным пайплайном, и только потом менеджер жмёт
        "🔄 Обновить задолженности"."""
        self.provider.records_by_car[task.car_number] = [
            _record(car_number=task.car_number, fingerprint=fingerprint, amount=amount)
        ]
        await self.check_service.check_task(task)

    def close(self):
        self.task_repository.close()
        self.detected_fine_repository.close()
        self.subscription_repository.close()


@pytest.fixture
def fx(tmp_path):
    fixture = _Fixture(tmp_path)
    yield fixture
    fixture.close()


# ---- какие задачи попадают под refresh (задача п.1-2) ----


async def test_zero_debt_car_is_not_included_or_checked(fx):
    fx.make_task("AA001AA")
    # Ни одной detected_fines строки для этой задачи вообще — "неизвестное
    # состояние", НЕ "0 ₾" — п.2 задачи: "не проверять машины без
    # persisted debt state".
    assert fx.debt_refresh.list_debt_rows() == []

    outcome = await fx.debt_refresh.refresh()

    assert outcome.checked == 0
    assert fx.provider.requested_plates == []


async def test_only_tasks_with_known_positive_debt_are_checked(fx):
    debt_task = fx.make_task("BB002BB")
    fx.make_task("CC003CC")  # no_fines_task — никогда не проверялась вовсе
    await fx.seed_debt(debt_task, amount=40)
    # no_fines_task никогда не проверялась вовсе.
    fx.provider.requested_plates.clear()  # seed_debt() сам дёрнул provider — не в счёт

    rows = fx.debt_refresh.list_debt_rows()
    assert [row.task_id for row in rows] == [debt_task.id]

    outcome = await fx.debt_refresh.refresh()

    assert outcome.checked == 1
    assert fx.provider.requested_plates == ["BB002BB"]
    assert "CC003CC" not in fx.provider.requested_plates


async def test_shared_task_is_checked_exactly_once_regardless_of_subscriber_count(fx):
    """См. задачу п.4 — один task с несколькими subscribers не должен
    породить несколько одинаковых police.ge-запросов. DebtRefreshService
    вообще не смотрит на подписки (список берётся из detected_fines,
    сгруппированных по monitoring_task_id) — дедупликация получается по
    построению, но тест явно это фиксирует на реалистичных данных (2
    разных telegram_user_id подписаны на одну и ту же задачу)."""
    task = fx.make_task("DD004DD")
    fx.add_subscription(task, telegram_user_id=1001)
    fx.add_subscription(task, telegram_user_id=1002)
    await fx.seed_debt(task, amount=100)
    fx.provider.requested_plates.clear()  # seed_debt() сам дёрнул provider — не в счёт

    outcome = await fx.debt_refresh.refresh()

    assert outcome.checked == 1
    assert fx.provider.requested_plates == ["DD004DD"]


# ---- результаты успешной проверки (задача п.5) ----


async def test_successful_check_with_new_fine_counts_as_increased(fx):
    task = fx.make_task("EE005EE")
    await fx.seed_debt(task, amount=40, fingerprint="fp-1")
    # На этот раз police.ge отдаёт И старый, И новый штраф — сумма растёт.
    fx.provider.records_by_car[task.car_number] = [
        _record(car_number=task.car_number, fingerprint="fp-1", amount=40),
        _record(car_number=task.car_number, fingerprint="fp-2", amount=60),
    ]

    outcome = await fx.debt_refresh.refresh()

    assert outcome.checked == 1
    assert outcome.increased == 1
    assert outcome.unchanged == 0
    assert outcome.failed == 0
    assert fx.detected_fine_repository.sum_amount_for_task(task.id) == 100
    refreshed = fx.task_repository.get(task.id)
    assert refreshed.last_successful_checked_at is not None


async def test_successful_check_with_no_new_fines_counts_as_unchanged(fx):
    task = fx.make_task("FF006FF")
    await fx.seed_debt(task, amount=40, fingerprint="fp-1")
    # police.ge снова отдаёт РОВНО тот же штраф — сумма не меняется. Это
    # НЕ значит "оплачено" (police.ge не отслеживает оплату, см.
    # reader/fines/payment_status.py) — только что новых штрафов не нашли.
    fx.provider.records_by_car[task.car_number] = [
        _record(car_number=task.car_number, fingerprint="fp-1", amount=40),
    ]

    outcome = await fx.debt_refresh.refresh()

    assert outcome.increased == 0
    assert outcome.unchanged == 1
    assert fx.detected_fine_repository.sum_amount_for_task(task.id) == 40


# ---- get_debt_rows_for_display: сортировка/recency (задача п.2) ----


async def test_display_rows_sorted_by_amount_desc_then_more_recent_first(fx):
    small = fx.make_task("GG010GG")
    big_older = fx.make_task("HH011HH")
    big_newer = fx.make_task("II012II")
    await fx.seed_debt(small, amount=10)
    await fx.seed_debt(big_older, amount=100)
    await fx.seed_debt(big_newer, amount=100)

    now = datetime.now(timezone.utc)
    fx.task_repository._conn.execute(
        "UPDATE fine_monitoring_tasks SET last_successful_checked_at = ? WHERE id = ?",
        ((now - timedelta(days=5)).isoformat(), big_older.id),
    )
    fx.task_repository._conn.execute(
        "UPDATE fine_monitoring_tasks SET last_successful_checked_at = ? WHERE id = ?",
        (now.isoformat(), big_newer.id),
    )
    fx.task_repository._conn.commit()

    rows = fx.debt_refresh.get_debt_rows_for_display()

    assert [row.task_id for row in rows] == [big_newer.id, big_older.id, small.id]


async def test_display_rows_fall_back_to_last_seen_at_when_never_refreshed(fx):
    """last_successful_checked_at ещё NULL (задача никогда не проходила
    через DebtRefreshService.refresh(), только через обычный check_task()
    при seed_debt) — checked_at берётся из TaskFineTotal.last_seen_at, а
    не остаётся None/не падает."""
    task = fx.make_task("JJ013JJ")
    await fx.seed_debt(task, amount=10)
    assert fx.task_repository.get(task.id).last_successful_checked_at is None

    rows = fx.debt_refresh.get_debt_rows_for_display()

    assert len(rows) == 1
    assert rows[0].checked_at is not None


# ---- ERROR не должен стирать известное состояние (задача п.6) ----


async def test_error_preserves_previous_amount_and_stays_in_next_debt_list(fx):
    task = fx.make_task("GG007GG")
    await fx.seed_debt(task, amount=250, fingerprint="fp-1")
    fx.provider.error_cars.add(task.car_number)

    outcome = await fx.debt_refresh.refresh()

    assert outcome.failed == 1
    assert outcome.failed_car_numbers == ("GG007GG",)
    assert outcome.increased == 0
    assert outcome.unchanged == 0
    # Сумма НЕ обнулена и НЕ изменена — check_task() при ошибке вообще не
    # трогает detected_fines (см. reader/fines/check_service.py).
    assert fx.detected_fine_repository.sum_amount_for_task(task.id) == 250
    # Машина остаётся в списке "известных штрафов" следующего открытия
    # Статистики/следующего refresh — НЕ исчезает из-за ошибки.
    rows = fx.debt_refresh.list_debt_rows()
    assert [row.task_id for row in rows] == [task.id]
    assert rows[0].total_amount == 250


async def test_error_does_not_set_or_clear_last_successful_checked_at(fx):
    task = fx.make_task("HH008HH")
    await fx.seed_debt(task, amount=10, fingerprint="fp-1")
    assert fx.task_repository.get(task.id).last_successful_checked_at is None

    fx.provider.error_cars.add(task.car_number)
    await fx.debt_refresh.refresh()

    # ERROR никогда не выставляет last_successful_checked_at — поле
    # остаётся ровно тем же, что и было (здесь — всё ещё None, потому что
    # ни одна проверка этой задачи никогда не была успешной через
    # DebtRefreshService в этом тесте — seed_debt тоже НЕ вызывает
    # record_successful_check, это делает ТОЛЬКО refresh()).
    assert fx.task_repository.get(task.id).last_successful_checked_at is None


# ---- отсутствие уведомлений — АРХИТЕКТУРНОЕ свойство (задача п.8) ----


def test_debt_refresh_service_has_no_notification_dependency():
    """DebtRefreshService физически не может разослать уведомление
    "просто потому что менеджер нажал refresh" (см. задачу п.8) — у него
    нет параметра конструктора ни для NotificationService, ни для
    FineNotificationCoordinator, а refresh() никогда не вызывает
    flush_pending() (grep reader/public_bot/debt_refresh_service.py
    подтверждает отсутствие самого слова "notif" в файле, кроме
    докстроков, объясняющих ИМЕННО это свойство)."""
    params = list(inspect.signature(DebtRefreshService.__init__).parameters)
    assert params == ["self", "task_repository", "detected_fine_repository", "check_service"]

    # Модуль может УПОМИНАТЬ уведомления в докстроках (объясняя ИМЕННО их
    # отсутствие), но не должен ИМПОРТИРОВАТЬ ни один класс, через который
    # их вообще можно было бы отправить.
    import reader.public_bot.debt_refresh_service as module
    assert not hasattr(module, "NotificationService")
    assert not hasattr(module, "FineNotificationCoordinator")


# ---- защита от повторного/параллельного запуска (задача п.11) ----


async def test_concurrent_refresh_is_rejected(fx):
    task = fx.make_task("II009II")
    await fx.seed_debt(task, amount=5)

    started = asyncio.Event()
    release = asyncio.Event()

    class _SlowProvider(FineProvider):
        async def search_by_plate(self, plate: str):
            started.set()
            await release.wait()
            return []

    fx.check_service._provider = _SlowProvider()

    first = asyncio.ensure_future(fx.debt_refresh.refresh())
    await started.wait()

    with pytest.raises(RefreshAlreadyInProgressError):
        await fx.debt_refresh.refresh()

    assert fx.debt_refresh.is_in_progress() is True
    release.set()
    await first
    assert fx.debt_refresh.is_in_progress() is False
