"""Тесты DebtRefreshService (@ProtocolGEbot, задача "manager Statistics /
refresh для обоих ботов") — реальные FineMonitoringTaskRepository/
FineCheckService (SQLite/tmp_path, РЕАЛЬНЫЕ файлы — не :memory:, чтобы
честно проверить persistence через restart), FineProvider — лёгкий фейк
(без сети). Проверяем:
- "🚨 Штрафы по последней проверке" = что показала последняя УСПЕШНАЯ
  проверка (last_successful_total_amount), НЕ сумма истории detected_fines;
- refresh полностью ЗАМЕНЯЕТ, а не накапливает, предыдущее значение
  (800 -> 500, 450 -> 0 машина исчезает из списка);
- ERROR сохраняет предыдущее достоверное состояние;
- следующий refresh берёт candidates ИЗ ЭТОГО ОБНОВЛЁННОГО состояния, а
  не из первоначального historical detected_fines-набора;
- restart (закрытие и переоткрытие repository на том же файле БД) не
  теряет state;
- что refresh НИКОГДА не трогает уведомления (структурно — у сервиса нет
  ссылки ни на NotificationService, ни на координатор);
- защиту от параллельного запуска.
"""

import asyncio
import inspect
import sys
from datetime import date
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

_CHAT_ID = -100999
_USER_ID = 111


class _FakeProvider(FineProvider):
    """records_by_car/error_cars МУТИРУЮТСЯ вызывающим тестом между
    вызовами check_task() (например, "сначала 800, потом 500, потом 0")."""

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


def _set_amount(provider: _FakeProvider, *, car_number: str, amount: float, fingerprint: str) -> None:
    """Заменяет весь набор штрафов police.ge для car_number РОВНО одним
    (fingerprint, amount) — эмулирует "последняя проверка показывает
    именно эту сумму", независимо от того, что было раньше."""
    if amount <= 0:
        provider.records_by_car[car_number] = []
    else:
        provider.records_by_car[car_number] = [
            _record(car_number=car_number, fingerprint=fingerprint, amount=amount)
        ]


class _Fixture:
    def __init__(self, db_path):
        self.db_path = db_path
        self.task_repository = FineMonitoringTaskRepository(self.db_path)
        self.detected_fine_repository = DetectedFineRepository(self.db_path)
        self.provider = _FakeProvider()
        self.check_service = FineCheckService(
            self.provider, self.task_repository, self.detected_fine_repository,
        )
        self.debt_refresh = DebtRefreshService(self.task_repository, self.check_service)

    def make_task(self, car_number: str):
        return self.task_repository.create(
            car_number=car_number, label=None,
            start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )

    async def seed(self, task, *, amount: float, fingerprint: str = "fp-seed") -> None:
        """Устанавливает начальное состояние ЧЕРЕЗ реальный check_task()
        (единственный authoritative writer last_successful_total_amount),
        а не прямым INSERT/UPDATE — та же последовательность событий, что
        и в production."""
        _set_amount(self.provider, car_number=task.car_number, amount=amount, fingerprint=fingerprint)
        await self.check_service.check_task(task)

    def close(self):
        self.task_repository.close()
        self.detected_fine_repository.close()

    def reopen(self) -> "_Fixture":
        """Симулирует restart процесса (см. задачу п.6/п.9) — закрывает
        текущие соединения и открывает НОВЫЕ repository/service/
        DebtRefreshService поверх ТОГО ЖЕ файла БД. Возвращает НОВУЮ
        _Fixture — старая больше не должна использоваться."""
        self.close()
        return _Fixture(self.db_path)


@pytest.fixture
def fx(tmp_path):
    fixture = _Fixture(tmp_path / "users.db")
    yield fixture
    fixture.close()


# ---- какие задачи попадают под refresh ----


async def test_zero_debt_car_is_not_included_or_checked(fx):
    fx.make_task("AA001AA")
    # Ни одной успешной проверки вообще — "неизвестное состояние", НЕ "0".
    assert fx.debt_refresh.list_debt_rows() == []

    outcome = await fx.debt_refresh.refresh()

    assert outcome.checked == 0
    assert fx.provider.requested_plates == []


async def test_only_tasks_with_known_positive_debt_are_checked(fx):
    debt_task = fx.make_task("BB002BB")
    fx.make_task("CC003CC")  # никогда не проверялась вовсе
    await fx.seed(debt_task, amount=40)
    fx.provider.requested_plates.clear()  # seed() сам дёрнул provider — не в счёт

    rows = fx.debt_refresh.list_debt_rows()
    assert [row.task_id for row in rows] == [debt_task.id]

    outcome = await fx.debt_refresh.refresh()

    assert outcome.checked == 1
    assert fx.provider.requested_plates == ["BB002BB"]
    assert "CC003CC" not in fx.provider.requested_plates


async def test_shared_task_is_checked_exactly_once_regardless_of_subscriber_count(fx):
    """DebtRefreshService выбирает по task_id (см.
    list_tasks_with_known_debt(), группировка по fine_monitoring_tasks.id),
    поэтому количество подписчиков задачи не может породить дублирующиеся
    police.ge-запросы — дедупликация получается по построению."""
    task = fx.make_task("DD004DD")
    await fx.seed(task, amount=100)
    fx.provider.requested_plates.clear()

    outcome = await fx.debt_refresh.refresh()

    assert outcome.checked == 1
    assert fx.provider.requested_plates == ["DD004DD"]


# ---- refresh ПОЛНОСТЬЮ заменяет, а не накапливает (задача п.3/п.5) ----


async def test_refresh_replaces_amount_lower(fx):
    task = fx.make_task("EE005EE")
    await fx.seed(task, amount=800, fingerprint="fp-1")
    _set_amount(fx.provider, car_number="EE005EE", amount=500, fingerprint="fp-1")

    outcome = await fx.debt_refresh.refresh()

    assert outcome.checked == 1
    assert outcome.failed == 0
    rows = fx.debt_refresh.list_debt_rows()
    assert len(rows) == 1
    assert rows[0].total_amount == 500
    refreshed = fx.task_repository.get(task.id)
    assert refreshed.last_successful_total_amount == 500


async def test_refresh_replaces_amount_higher(fx):
    task = fx.make_task("FF006FF")
    await fx.seed(task, amount=100, fingerprint="fp-1")
    fx.provider.records_by_car[task.car_number] = [
        _record(car_number="FF006FF", fingerprint="fp-1", amount=100),
        _record(car_number="FF006FF", fingerprint="fp-2", amount=200),
    ]

    await fx.debt_refresh.refresh()

    rows = fx.debt_refresh.list_debt_rows()
    assert rows[0].total_amount == 300


async def test_refresh_to_zero_makes_car_disappear_from_debt_list(fx):
    """См. задачу п.5 (пример B: 450 -> 0) — машина ПОЛНОСТЬЮ исчезает из
    "🚨 Штрафы по последней проверке", в отличие от старой Georgia-модели
    (где сумма истории detected_fines никогда не убывала)."""
    task = fx.make_task("GG007GG")
    await fx.seed(task, amount=450, fingerprint="fp-1")
    _set_amount(fx.provider, car_number="GG007GG", amount=0, fingerprint="fp-1")

    outcome = await fx.debt_refresh.refresh()

    assert outcome.checked == 1
    assert outcome.failed == 0
    assert fx.debt_refresh.list_debt_rows() == []
    refreshed = fx.task_repository.get(task.id)
    assert refreshed.last_successful_total_amount == 0


# ---- ERROR сохраняет предыдущее достоверное состояние ----


async def test_error_preserves_previous_amount_and_stays_in_next_debt_list(fx):
    task = fx.make_task("HH008HH")
    await fx.seed(task, amount=250, fingerprint="fp-1")
    fx.provider.error_cars.add(task.car_number)

    outcome = await fx.debt_refresh.refresh()

    assert outcome.failed == 1
    assert outcome.failed_car_numbers == ("HH008HH",)
    rows = fx.debt_refresh.list_debt_rows()
    assert [row.task_id for row in rows] == [task.id]
    assert rows[0].total_amount == 250

    refreshed = fx.task_repository.get(task.id)
    assert refreshed.last_successful_total_amount == 250


# ---- следующий refresh candidates берёт из НОВОГО состояния (задача п.5/п.9) ----


async def test_next_refresh_candidates_use_new_persisted_state(tmp_path):
    """Регрессионный тест по примеру задачи (п.5/п.9):
    initial: A=800, B=450, C=350
    refresh #1: A=500, B=0, C=600
    candidates refresh #2 ДОЛЖНЫ быть A, C — НЕ B."""
    fx = _Fixture(tmp_path / "users.db")
    try:
        task_a = fx.make_task("A1111AA")
        task_b = fx.make_task("B2222BB")
        task_c = fx.make_task("C3333CC")
        await fx.seed(task_a, amount=800, fingerprint="fp-a")
        await fx.seed(task_b, amount=450, fingerprint="fp-b")
        await fx.seed(task_c, amount=350, fingerprint="fp-c")

        _set_amount(fx.provider, car_number="A1111AA", amount=500, fingerprint="fp-a")
        _set_amount(fx.provider, car_number="B2222BB", amount=0, fingerprint="fp-b")
        _set_amount(fx.provider, car_number="C3333CC", amount=600, fingerprint="fp-c")

        outcome1 = await fx.debt_refresh.refresh()
        assert outcome1.checked == 3

        rows_after_1 = fx.debt_refresh.list_debt_rows()
        assert {row.car_number for row in rows_after_1} == {"A1111AA", "C3333CC"}

        fx.provider.requested_plates.clear()
        outcome2 = await fx.debt_refresh.refresh()

        assert outcome2.checked == 2
        assert set(fx.provider.requested_plates) == {"A1111AA", "C3333CC"}
        assert "B2222BB" not in fx.provider.requested_plates
    finally:
        fx.close()


async def test_next_refresh_candidates_use_new_state_after_restart(tmp_path):
    """Тот же сценарий, что и выше, но МЕЖДУ refresh #1 и refresh #2
    repository/service/DebtRefreshService закрываются и переоткрываются
    заново (см. задачу п.6/п.9: "restart не теряет state... проверить
    тестом на persisted DB, а не только mocks/in-memory")."""
    fx = _Fixture(tmp_path / "users.db")
    task_a = fx.make_task("A4444AA")
    task_b = fx.make_task("B5555BB")
    await fx.seed(task_a, amount=800, fingerprint="fp-a")
    await fx.seed(task_b, amount=450, fingerprint="fp-b")
    _set_amount(fx.provider, car_number="A4444AA", amount=500, fingerprint="fp-a")
    _set_amount(fx.provider, car_number="B5555BB", amount=0, fingerprint="fp-b")
    await fx.debt_refresh.refresh()

    # --- "restart" ---
    fx = fx.reopen()
    try:
        rows = fx.debt_refresh.list_debt_rows()
        assert [row.car_number for row in rows] == ["A4444AA"]

        _set_amount(fx.provider, car_number="A4444AA", amount=700, fingerprint="fp-a")
        outcome = await fx.debt_refresh.refresh()

        assert outcome.checked == 1
        assert fx.provider.requested_plates == ["A4444AA"]
    finally:
        fx.close()


# ---- Statistics/candidates никогда не берут сумму из detected_fines-истории (задача п.3) ----


async def test_debt_amount_is_not_cumulative_history_sum(fx):
    """detected_fines остаётся append-only history/dedup (см. задачу
    п.3/п.13) — три РАЗНЫХ штрафа за три проверки суммарно дали бы 800+
    500+600=1900 в старой модели; в новой — только последнее значение."""
    task = fx.make_task("II009II")
    await fx.seed(task, amount=800, fingerprint="fp-1")
    _set_amount(fx.provider, car_number="II009II", amount=500, fingerprint="fp-2")
    await fx.debt_refresh.refresh()
    _set_amount(fx.provider, car_number="II009II", amount=600, fingerprint="fp-3")
    await fx.debt_refresh.refresh()

    rows = fx.debt_refresh.list_debt_rows()
    assert rows[0].total_amount == 600  # НЕ 800+500+600

    # detected_fines history осталась НЕТРОНУТОЙ (append-only/dedup) — все
    # три штрафа по-прежнему там, это отдельная история, а не источник
    # отображаемого состояния.
    fine_count = fx.detected_fine_repository._conn.execute(
        "SELECT COUNT(*) FROM detected_fines WHERE monitoring_task_id = ?", (task.id,),
    ).fetchone()[0]
    assert fine_count == 3


# ---- отсутствие уведомлений — АРХИТЕКТУРНОЕ свойство ----


def test_debt_refresh_service_has_no_notification_dependency():
    """DebtRefreshService физически не может разослать уведомление
    "просто потому что менеджер нажал refresh" — у него нет параметра
    конструктора ни для NotificationService, ни для
    FineNotificationCoordinator."""
    params = list(inspect.signature(DebtRefreshService.__init__).parameters)
    assert params == ["self", "task_repository", "check_service"]

    import reader.public_bot.debt_refresh_service as module
    assert not hasattr(module, "NotificationService")
    assert not hasattr(module, "FineNotificationCoordinator")


# ---- защита от повторного/параллельного запуска ----


async def test_concurrent_refresh_is_rejected(fx):
    task = fx.make_task("JJ010JJ")
    await fx.seed(task, amount=5)

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
