"""
Тесты FineCommand — операторский интерфейс (fine add/list/stop/check/status).
Repository — настоящие (SQLite/tmp_path), FineProvider/NotificationService —
фейковые (без сети/Telegram). Ничего не переопределяет логику FineJob —
использует те же FineCheckService/FineNotificationCoordinator.
"""

import sys
from datetime import date, timedelta
from datetime import time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from reader.commands.base import CommandContext, CommandError  # noqa: E402
from reader.commands.fine import FineCommand  # noqa: E402
from reader.fines.check_service import FineCheckService  # noqa: E402
from reader.fines.detected_fine_repository import DetectedFineRepository  # noqa: E402
from reader.fines.models import ParsedFineRecord  # noqa: E402
from reader.fines.notification_coordinator import FineNotificationCoordinator  # noqa: E402
from reader.fines.provider import FineProvider, FineProviderError  # noqa: E402
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.jobs.fine_job import FineJob  # noqa: E402
from reader.jobs.scheduler import Scheduler  # noqa: E402
from reader.notifications.base import NotificationResult, NotificationService  # noqa: E402

_CHAT_ID = -100999
_USER_ID = 111
_TBILISI = ZoneInfo("Asia/Tbilisi")
_RUN_TIMES = [dt_time(9, 0), dt_time(15, 0), dt_time(21, 0)]


class _FakeProvider(FineProvider):
    def __init__(self, records_by_car=None, error: Exception | None = None):
        self._records_by_car = records_by_car or {}
        self._error = error
        self.requested_plates: list[str] = []

    async def search_by_plate(self, plate: str) -> list[ParsedFineRecord]:
        self.requested_plates.append(plate)
        if self._error is not None:
            raise self._error
        return self._records_by_car.get(plate, [])


class _FakeNotificationService(NotificationService):
    def __init__(self):
        self.notify_calls: list[list] = []

    async def notify(self, events) -> NotificationResult:
        self.notify_calls.append(events)
        return NotificationResult(
            delivered_event_ids=[e.detected_fine_id for e in events], failed_event_ids=[]
        )


def _record(
    car_number="B957MA09",
    external_fine_id="AB123456",
    fingerprint="fp-1",
    penalty_date=date(2026, 8, 6),
    due_date=date(2026, 8, 20),
    delivered_status="Не вручено",
) -> ParsedFineRecord:
    return ParsedFineRecord(
        car_number=car_number,
        external_fine_id=external_fine_id,
        penalty_date=penalty_date,
        due_date=due_date,
        delivered_status=delivered_status,
        fingerprint=fingerprint,
        raw_data={"protocolNo": external_fine_id},
    )


def _ctx(args: list[str], *, chat_id=_CHAT_ID, user_id=_USER_ID) -> CommandContext:
    return CommandContext(
        chat_id=chat_id, user_id=user_id, args=args, raw_text="fine " + " ".join(args), event=None
    )


class _Fixture:
    """Полный набор реальных зависимостей (кроме FineProvider/NotificationService)
    — тот же граф объектов, что собирает reader/main.py."""

    def __init__(self, tmp_path, records_by_car=None, provider_error=None):
        db_path = tmp_path / "users.db"
        self.task_repository = FineMonitoringTaskRepository(db_path)
        self.detected_fine_repository = DetectedFineRepository(db_path)
        self.provider = _FakeProvider(records_by_car, error=provider_error)
        self.check_service = FineCheckService(
            self.provider, self.task_repository, self.detected_fine_repository
        )
        self.notification_service = _FakeNotificationService()
        self.coordinator = FineNotificationCoordinator(
            self.detected_fine_repository, self.task_repository, self.notification_service
        )
        self.fine_job = FineJob(
            task_repository=self.task_repository,
            check_service=self.check_service,
            notification_coordinator=self.coordinator,
            run_times=_RUN_TIMES,
            tz=_TBILISI,
        )
        self.scheduler = Scheduler([self.fine_job])
        self.command = FineCommand(
            task_repository=self.task_repository,
            check_service=self.check_service,
            notification_coordinator=self.coordinator,
            scheduler=self.scheduler,
            fine_job=self.fine_job,
            run_times=_RUN_TIMES,
            tz=_TBILISI,
        )

    def close(self):
        self.task_repository.close()
        self.detected_fine_repository.close()


@pytest.fixture
def fx(tmp_path):
    fixture = _Fixture(tmp_path)
    yield fixture
    fixture.close()


# ---- fine add ----


async def test_fine_add_with_explicit_dates(fx):
    result = await fx.command.handle(_ctx(["add", "b957ma09", "03.08.2026", "13.08.2026"]))

    assert "✅ Мониторинг штрафов добавлен" in result.text
    assert "Автомобиль: B957MA09" in result.text
    assert "Период: 03.08.2026–13.08.2026" in result.text
    assert "09:00, 15:00 и 21:00" in result.text

    [task] = fx.task_repository.list_active()
    assert task.car_number == "B957MA09"
    assert task.start_date == date(2026, 8, 3)
    assert task.end_date == date(2026, 8, 13)
    assert task.telegram_chat_id == _CHAT_ID
    assert task.created_by_user_id == _USER_ID


async def test_fine_add_without_dates_defaults_to_today_plus_30_days(fx):
    result = await fx.command.handle(_ctx(["add", "AA001AA"]))

    assert "✅ Мониторинг штрафов добавлен" in result.text

    [task] = fx.task_repository.list_active()
    assert (task.end_date - task.start_date) == timedelta(days=30)


async def test_fine_add_rejects_invalid_car_number(fx):
    with pytest.raises(CommandError) as exc_info:
        await fx.command.handle(_ctx(["add", "AA-001-AA"]))

    assert "❌" in exc_info.value.message
    assert fx.task_repository.list_active() == []


async def test_fine_add_rejects_wrong_argument_count(fx):
    with pytest.raises(CommandError) as exc_info:
        await fx.command.handle(_ctx(["add", "AA001AA", "03.08.2026"]))

    assert "Неверный формат команды" in exc_info.value.message


async def test_fine_add_rejects_invalid_date(fx):
    with pytest.raises(CommandError) as exc_info:
        await fx.command.handle(_ctx(["add", "AA001AA", "2026-08-03", "13.08.2026"]))

    assert "формат даты" in exc_info.value.message


async def test_fine_add_rejects_end_before_start(fx):
    with pytest.raises(CommandError) as exc_info:
        await fx.command.handle(_ctx(["add", "AA001AA", "13.08.2026", "03.08.2026"]))

    assert "END_DATE" in exc_info.value.message


async def test_fine_add_rejects_overlapping_active_task(fx):
    await fx.command.handle(_ctx(["add", "AA001AA", "01.08.2026", "31.08.2026"]))

    with pytest.raises(CommandError) as exc_info:
        await fx.command.handle(_ctx(["add", "AA001AA", "15.08.2026", "20.09.2026"]))

    assert "уже есть активная задача" in exc_info.value.message
    assert len(fx.task_repository.list_active()) == 1


# ---- fine list ----


async def test_fine_list_with_tasks(fx):
    await fx.command.handle(_ctx(["add", "AA001AA", "01.08.2026", "31.08.2026"]))
    await fx.command.handle(_ctx(["add", "BB002BB", "01.08.2026", "31.08.2026"]))

    result = await fx.command.handle(_ctx(["list"]))

    assert "AA001AA" in result.text
    assert "BB002BB" in result.text
    assert "01.08.2026–31.08.2026" in result.text


async def test_fine_list_with_no_tasks(fx):
    result = await fx.command.handle(_ctx(["list"]))

    assert result.text == "Активных задач мониторинга нет."


# ---- fine stop ----


async def test_fine_stop_active_task(fx):
    await fx.command.handle(_ctx(["add", "AA001AA", "01.08.2026", "31.08.2026"]))
    task_id = fx.task_repository.list_active()[0].id

    result = await fx.command.handle(_ctx(["stop", str(task_id)]))

    assert "✅" in result.text
    assert "остановлен" in result.text
    assert fx.task_repository.get(task_id).status == "stopped"
    assert fx.task_repository.list_active() == []


async def test_fine_stop_unknown_task_returns_command_error(fx):
    with pytest.raises(CommandError) as exc_info:
        await fx.command.handle(_ctx(["stop", "999999"]))

    assert "не найдена" in exc_info.value.message


async def test_fine_stop_already_inactive_task_returns_command_error(fx):
    await fx.command.handle(_ctx(["add", "AA001AA", "01.08.2026", "31.08.2026"]))
    task_id = fx.task_repository.list_active()[0].id
    fx.task_repository.set_status(task_id, "stopped")

    with pytest.raises(CommandError) as exc_info:
        await fx.command.handle(_ctx(["stop", str(task_id)]))

    assert "не активна" in exc_info.value.message


# ---- fine check ----


async def test_fine_check_successful(tmp_path):
    fx = _Fixture(tmp_path, records_by_car={"AA001AA": [_record(car_number="AA001AA")]})
    try:
        await fx.command.handle(_ctx(["add", "AA001AA", "01.08.2026", "31.08.2026"]))
        task_id = fx.task_repository.list_active()[0].id

        result = await fx.command.handle(_ctx(["check", str(task_id)]))

        assert "✅ Проверка завершена" in result.text
        assert "Автомобиль: AA001AA" in result.text
        assert "Найдено штрафов: 1" in result.text
        assert "Новых: 1" in result.text
        assert "мс" in result.text
    finally:
        fx.close()


async def test_fine_check_unknown_task_returns_command_error(fx):
    with pytest.raises(CommandError) as exc_info:
        await fx.command.handle(_ctx(["check", "999999"]))

    assert "не найдена" in exc_info.value.message


async def test_fine_check_with_provider_error_returns_clean_message(tmp_path):
    fx = _Fixture(tmp_path, provider_error=FineProviderError("police.ge недоступен"))
    try:
        await fx.command.handle(_ctx(["add", "AA001AA", "01.08.2026", "31.08.2026"]))
        task_id = fx.task_repository.list_active()[0].id

        with pytest.raises(CommandError) as exc_info:
            await fx.command.handle(_ctx(["check", str(task_id)]))

        assert "police.ge недоступен" in exc_info.value.message
        # Никакого трейсбека оператору — только чистое сообщение.
        assert "Traceback" not in exc_info.value.message
    finally:
        fx.close()


async def test_fine_check_uses_shared_pending_notification_mechanism(tmp_path):
    fx = _Fixture(tmp_path, records_by_car={"AA001AA": [_record(car_number="AA001AA", fingerprint="fp-1")]})
    try:
        await fx.command.handle(_ctx(["add", "AA001AA", "01.08.2026", "31.08.2026"]))
        task_id = fx.task_repository.list_active()[0].id

        await fx.command.handle(_ctx(["check", str(task_id)]))

        # То же самое, что делает FineJob после check_task(): flush_pending()
        # доставил штраф через тот же NotificationService и отметил
        # notification_sent_at, а не оставил его висеть.
        assert len(fx.notification_service.notify_calls) == 1
        fine = fx.detected_fine_repository.get_by_fingerprint(task_id, "fp-1")
        assert fine.notification_sent_at is not None
    finally:
        fx.close()


async def test_fine_check_does_not_duplicate_already_notified_fine_on_repeat(tmp_path):
    fx = _Fixture(tmp_path, records_by_car={"AA001AA": [_record(car_number="AA001AA", fingerprint="fp-1")]})
    try:
        await fx.command.handle(_ctx(["add", "AA001AA", "01.08.2026", "31.08.2026"]))
        task_id = fx.task_repository.list_active()[0].id

        await fx.command.handle(_ctx(["check", str(task_id)]))
        result = await fx.command.handle(_ctx(["check", str(task_id)]))

        assert "Новых: 0" in result.text
        # flush_pending() второй раз не находит ничего для отправки.
        assert len(fx.notification_service.notify_calls) == 1
    finally:
        fx.close()


# ---- fine status ----


async def test_fine_status(fx):
    await fx.command.handle(_ctx(["add", "AA001AA", "01.08.2026", "31.08.2026"]))

    result = await fx.command.handle(_ctx(["status"]))

    assert "Мониторинг: включён" in result.text
    assert "Активных задач: 1" in result.text
    assert "09:00, 15:00 и 21:00" in result.text
    assert "Asia/Tbilisi" in result.text
    assert "Ошибок: 0" in result.text
    assert "Последняя ошибка: Нет" in result.text
    assert "ещё не запускался" in result.text


async def test_fine_status_reflects_scheduler_running_state(fx):
    fx.scheduler.is_running = True

    result = await fx.command.handle(_ctx(["status"]))

    assert "Scheduler: работает" in result.text


# ---- общие ошибки формата ----


async def test_unknown_subcommand_returns_command_error(fx):
    with pytest.raises(CommandError):
        await fx.command.handle(_ctx(["frobnicate"]))


async def test_empty_args_returns_command_error(fx):
    with pytest.raises(CommandError):
        await fx.command.handle(_ctx([]))
