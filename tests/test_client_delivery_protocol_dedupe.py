"""ClientDeliveryService: одна квитанция — одно "🚨 Обнаружен новый штраф"
на получателя подписки, даже если квитанция лежит под несколькими задачами
номера; квитанции, которых police.ge больше не показывает (⚪) или не
показал на последней полной проверке, клиенту как "новые" не уходят.
Repository — настоящие (SQLite, tmp_path); номера синтетические."""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from reader.fines.detected_fine_repository import DetectedFineRepository
from reader.fines.models import UNLIMITED_END_DATE, ParsedFineRecord
from reader.fines.payment_status import FineStatusRepository, FineStatusTracker
from reader.fines.task_repository import FineMonitoringTaskRepository
from reader.public_bot.delivery_repository import ClientFineDeliveryRepository
from reader.public_bot.delivery_service import ClientDeliveryService
from reader.public_bot.subscription_repository import FineSubscriptionRepository

_CAR = "AA111BB"
_OWNER = 7001
_TBILISI = ZoneInfo("Asia/Tbilisi")
_T0 = datetime(2026, 11, 18, 19, 0)


class _Sender:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, *, buttons=None):
        self.sent.append((chat_id, text))


class _Fixture:
    def __init__(self, tmp_path, *, with_statuses=True):
        db_path = tmp_path / "users.db"
        self.tasks = FineMonitoringTaskRepository(db_path)
        self.fines = DetectedFineRepository(db_path)
        self.subscriptions = FineSubscriptionRepository(db_path)
        self.deliveries = ClientFineDeliveryRepository(db_path)
        self.status_repository = FineStatusRepository(db_path)
        self.tracker = FineStatusTracker(self.status_repository, disappearance_min_gap=timedelta(hours=12))
        self.sender = _Sender()
        self.service = ClientDeliveryService(
            self.fines, self.subscriptions, self.deliveries, self.sender, tz=_TBILISI,
            status_lookup=self.tracker if with_statuses else None,
        )

    def task(self):
        return self.tasks.create(
            car_number=_CAR, label=None, start_date=date(2026, 11, 1), end_date=date(2026, 12, 31),
            telegram_chat_id=-1, created_by_user_id=1,
        )

    def fine(self, task, protocol_no, fingerprint=None):
        return self.fines.create(
            monitoring_task_id=task.id, car_number=_CAR, external_fine_id=protocol_no,
            fingerprint=fingerprint or f"fp-{task.id}-{protocol_no}", penalty_date=None, due_date=None,
            delivered_status=None, raw_data="{}", amount=50,
        )

    def subscribe(self, task, user_id=_OWNER):
        return self.subscriptions.create(
            monitoring_task_id=task.id, car_number=_CAR, telegram_user_id=user_id, telegram_chat_id=user_id,
            telegram_username=None, start_date=date(2026, 11, 1), end_date=UNLIMITED_END_DATE,
        )

    def close(self):
        for repository in (self.status_repository, self.deliveries, self.subscriptions, self.fines, self.tasks):
            repository.close()


@pytest.fixture()
def fx(tmp_path):
    fixture = _Fixture(tmp_path)
    yield fixture
    fixture.close()


def _record(protocol_no):
    return ParsedFineRecord(
        car_number=_CAR, external_fine_id=protocol_no, penalty_date=None, due_date=None,
        delivered_status="Не вручено", fingerprint=f"fp-{protocol_no}", raw_data={}, amount=50,
    )


async def test_same_receipt_under_two_tasks_is_delivered_once(fx):
    first, second = fx.task(), fx.task()
    fx.fine(first, "GE1")
    fx.fine(second, "GE1")
    fx.subscribe(first)
    fx.subscribe(second, user_id=_OWNER + 1)

    await fx.service.run_once()
    await fx.service.run_once()  # повторный тик ничего не добавляет

    assert sorted(chat for chat, _ in fx.sender.sent) == [_OWNER, _OWNER + 1]


async def test_receipt_already_delivered_through_another_row_is_not_resent(fx):
    first = fx.task()
    subscription = fx.subscribe(first)
    later_row = fx.fine(first, "GE1", fingerprint="fp-late")
    # историческая доставка шла по другой строке той же квитанции
    fx.deliveries.record_attempt(later_row.id, subscription.id, "owner")
    fx.deliveries.mark_delivered(later_row.id, subscription.id, "owner")
    second = fx.task()
    fx.fines.create(
        monitoring_task_id=second.id, car_number=_CAR, external_fine_id="GE1", fingerprint="fp-other",
        penalty_date=None, due_date=None, delivered_status=None, raw_data="{}", amount=50,
    )

    await fx.service.run_once()

    assert fx.sender.sent == []


async def test_distinct_receipts_are_all_delivered(fx):
    task = fx.task()
    fx.fine(task, "GE1")
    fx.fine(task, "GE2")
    fx.subscribe(task)

    await fx.service.run_once()

    assert len(fx.sender.sent) == 2


async def test_gone_and_missing_receipts_are_not_announced_as_new(fx):
    task = fx.task()
    for protocol_no in ("GE1", "GE2", "GE3"):
        fx.fine(task, protocol_no)
    fx.tracker.record_full_check(_CAR, [_record("GE1"), _record("GE2"), _record("GE3")], complete=True, at=_T0)
    fx.tracker.record_full_check(_CAR, [_record("GE3")], complete=True, at=_T0 + timedelta(hours=1))  # GE1/GE2 missing
    fx.tracker.record_full_check(_CAR, [_record("GE2"), _record("GE3")], complete=True, at=_T0 + timedelta(hours=14))
    # итог: GE1 ⚪ (два раза не найден с интервалом), GE2 найден снова, GE3 в базе
    fx.subscribe(task)

    await fx.service.run_once()

    texts_sent = [text for _, text in fx.sender.sent]
    assert len(texts_sent) == 2
    assert not any("GE1" in text for text in texts_sent)


async def test_without_status_lookup_dedupe_still_works(tmp_path):
    fixture = _Fixture(tmp_path, with_statuses=False)
    first, second = fixture.task(), fixture.task()
    fixture.fine(first, "GE1")
    fixture.fine(second, "GE1")
    fixture.subscribe(first)

    await fixture.service.run_once()

    assert len(fixture.sender.sent) == 1
    fixture.close()


async def test_legacy_rows_without_receipt_number_are_each_delivered(fx):
    task = fx.task()
    for fingerprint in ("fp-a", "fp-b"):
        fx.fines.create(
            monitoring_task_id=task.id, car_number=_CAR, external_fine_id=None, fingerprint=fingerprint,
            penalty_date=None, due_date=None, delivered_status=None, raw_data="{}", amount=10,
        )
    fx.subscribe(task)

    await fx.service.run_once()

    assert len(fx.sender.sent) == 2
