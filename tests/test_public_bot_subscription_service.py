"""
Тесты SubscriptionService — application-слой Add Car flow @GEShtrafbot
(reader/public_bot/subscription_service.py). Repository — настоящие
(SQLite/tmp_path), FineProvider — фейковый (без сети). Тот же
FineCheckService, что использует и операторский FineJob/FineCommand —
никакой отдельной реализации проверки здесь не тестируется.
"""

import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from reader.fines.check_service import FineCheckService  # noqa: E402
from reader.fines.detected_fine_repository import DetectedFineRepository  # noqa: E402
from reader.fines.models import ParsedFineRecord  # noqa: E402
from reader.fines.provider import FineProvider, FineProviderError  # noqa: E402
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.public_bot.subscription_repository import FineSubscriptionRepository  # noqa: E402
from reader.public_bot.subscription_service import SubscriptionService  # noqa: E402
from reader.users.models import TelegramUserInfo  # noqa: E402
from reader.users.repository import UserRepository  # noqa: E402

_CHAT_ID = -100999
_OPERATOR_USER_ID = 111


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


def _record(car_number="M295YB196", fingerprint="fp-1") -> ParsedFineRecord:
    return ParsedFineRecord(
        car_number=car_number,
        external_fine_id="AB123456",
        penalty_date=date(2026, 8, 6),
        due_date=date(2026, 8, 20),
        delivered_status="Не вручено",
        fingerprint=fingerprint,
        raw_data={"protocolNo": "AB123456"},
    )


class _Fixture:
    def __init__(self, tmp_path, records_by_car=None, provider_error=None):
        db_path = tmp_path / "users.db"
        self.task_repository = FineMonitoringTaskRepository(db_path)
        self.detected_fine_repository = DetectedFineRepository(db_path)
        self.subscription_repository = FineSubscriptionRepository(db_path)
        self.user_repository = UserRepository(db_path)
        self.provider = _FakeProvider(records_by_car, error=provider_error)
        self.check_service = FineCheckService(
            self.provider, self.task_repository, self.detected_fine_repository,
        )
        self.service = SubscriptionService(
            self.task_repository, self.subscription_repository,
            self.user_repository, self.check_service,
        )

    def close(self):
        self.task_repository.close()
        self.detected_fine_repository.close()
        self.subscription_repository.close()
        self.user_repository.close()


@pytest.fixture
def fx(tmp_path):
    fixture = _Fixture(tmp_path)
    yield fixture
    fixture.close()


# ---- новая задача ----


async def test_add_car_creates_new_client_bot_task_when_none_exists(fx):
    outcome = await fx.service.add_car(
        telegram_user_id=42, telegram_chat_id=42, username="client_one",
        first_name="Anna", last_name=None, car_number="M295YB196",
        period_days=30, today=date(2026, 9, 3),
    )

    assert outcome.task.monitoring_scope == "client_bot"
    assert outcome.task.start_date == date(2026, 9, 3)
    assert outcome.task.end_date == date(2026, 10, 3)
    assert outcome.subscription.telegram_user_id == 42
    assert outcome.subscription.car_number == "M295YB196"
    assert outcome.subscription.source == "geshtrafbot"
    assert outcome.check_ok is True
    assert outcome.new_fines_count == 0

    [task] = fx.task_repository.list_active()
    assert task.id == outcome.task.id


# ---- переиспользование существующей client_bot задачи ----


async def test_add_car_reuses_and_extends_existing_client_bot_task(fx):
    first = await fx.service.add_car(
        telegram_user_id=1, telegram_chat_id=1, username="alice",
        first_name=None, last_name=None, car_number="M295YB196",
        period_days=30, today=date(2026, 9, 3),
    )

    # Второй, независимый клиент подписывается на ту же машину с БОЛЬШИМ
    # периодом — задача должна быть переиспользована (не создана заново) и
    # продлена, а не пересоздана/сброшена.
    second = await fx.service.add_car(
        telegram_user_id=2, telegram_chat_id=2, username="bob",
        first_name=None, last_name=None, car_number="M295YB196",
        period_days=365, today=date(2026, 9, 3),
    )

    assert second.task.id == first.task.id
    assert second.task.monitoring_scope == "client_bot"
    assert second.task.end_date == date(2027, 9, 3)

    [task] = fx.task_repository.list_active()
    assert task.id == first.task.id
    assert task.end_date == date(2027, 9, 3)


async def test_add_car_does_not_shorten_existing_task_period(fx):
    first = await fx.service.add_car(
        telegram_user_id=1, telegram_chat_id=1, username="alice",
        first_name=None, last_name=None, car_number="M295YB196",
        period_days=365, today=date(2026, 9, 3),
    )

    # Второй клиент выбирает МЕНЬШИЙ период — задача не должна сократиться.
    second = await fx.service.add_car(
        telegram_user_id=2, telegram_chat_id=2, username="bob",
        first_name=None, last_name=None, car_number="M295YB196",
        period_days=30, today=date(2026, 9, 3),
    )

    assert second.task.end_date == first.task.end_date == date(2027, 9, 3)
    # Подписка "bob" при этом сохраняет ЕГО СОБСТВЕННЫЙ (более короткий) период.
    assert second.subscription.end_date == date(2026, 10, 3)


# ---- существующая операторская задача ----


async def test_add_car_reuses_operator_task_without_changing_scope(fx):
    """Существующая операторская задача (monitoring_scope='operator', см.
    Stage 1) не должна становиться client_bot — клиент просто подписывается
    поверх неё, monitoring_scope остаётся операторским."""
    operator_task = fx.task_repository.create(
        car_number="M295YB196", label=None,
        start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
        telegram_chat_id=_CHAT_ID, created_by_user_id=_OPERATOR_USER_ID,
    )
    assert operator_task.monitoring_scope == "operator"

    outcome = await fx.service.add_car(
        telegram_user_id=99, telegram_chat_id=99, username="driver",
        first_name=None, last_name=None, car_number="M295YB196",
        period_days=90, today=date(2026, 9, 3),
    )

    assert outcome.task.id == operator_task.id
    assert outcome.task.monitoring_scope == "operator"  # НЕ изменился
    assert outcome.task.end_date == date(2026, 12, 2)  # продлён


# ---- повторное добавление тем же клиентом ----


async def test_repeated_add_car_by_same_user_updates_period_without_duplicate(fx):
    first = await fx.service.add_car(
        telegram_user_id=42, telegram_chat_id=42, username="client",
        first_name=None, last_name=None, car_number="M295YB196",
        period_days=30, today=date(2026, 9, 3),
    )

    second = await fx.service.add_car(
        telegram_user_id=42, telegram_chat_id=42, username="client",
        first_name=None, last_name=None, car_number="M295YB196",
        period_days=90, today=date(2026, 9, 10),
    )

    assert second.subscription.id == first.subscription.id  # не дубль
    assert second.subscription.start_date == date(2026, 9, 10)
    assert second.subscription.end_date == date(2026, 12, 9)

    subscriptions = fx.subscription_repository.list_by_user(42)
    assert len(subscriptions) == 1


# ---- UserRepository ----


async def test_add_car_syncs_user_repository_without_removing_other_cars(fx):
    fx.user_repository.upsert(
        TelegramUserInfo(user_id=42, username="old", first_name=None, last_name=None)
    )
    fx.user_repository.add_car_numbers(42, ["AA001AA"])

    await fx.service.add_car(
        telegram_user_id=42, telegram_chat_id=42, username="client_one",
        first_name="Anna", last_name="K.", car_number="M295YB196",
        period_days=30, today=date(2026, 9, 3),
    )

    stored_user = fx.user_repository.get(42)
    assert stored_user.username == "client_one"
    assert stored_user.first_name == "Anna"

    car_numbers = fx.user_repository.get_car_numbers(42)
    assert set(car_numbers) == {"AA001AA", "M295YB196"}  # старый номер не удалён

    owners = fx.user_repository.find_by_car_number("M295YB196")
    assert [o.user_id for o in owners] == [42]


# ---- immediate check ----


async def test_add_car_immediate_check_reports_no_new_fines(fx):
    outcome = await fx.service.add_car(
        telegram_user_id=42, telegram_chat_id=42, username="client",
        first_name=None, last_name=None, car_number="M295YB196",
        period_days=30, today=date(2026, 9, 3),
    )

    assert outcome.check_ok is True
    assert outcome.new_fines_count == 0


async def test_add_car_immediate_check_reports_new_fines(tmp_path):
    fixture = _Fixture(tmp_path, records_by_car={"M295YB196": [_record()]})
    try:
        outcome = await fixture.service.add_car(
            telegram_user_id=42, telegram_chat_id=42, username="client",
            first_name=None, last_name=None, car_number="M295YB196",
            period_days=30, today=date(2026, 9, 3),
        )

        assert outcome.check_ok is True
        assert outcome.new_fines_count == 1
    finally:
        fixture.close()


async def test_add_car_immediate_check_failure_keeps_task_and_subscription(tmp_path):
    fixture = _Fixture(tmp_path, provider_error=FineProviderError("police.ge недоступен"))
    try:
        outcome = await fixture.service.add_car(
            telegram_user_id=42, telegram_chat_id=42, username="client",
            first_name=None, last_name=None, car_number="M295YB196",
            period_days=30, today=date(2026, 9, 3),
        )

        assert outcome.check_ok is False
        assert outcome.new_fines_count == 0

        # Подписка/задача остаются созданными, несмотря на ошибку проверки.
        [task] = fixture.task_repository.list_active()
        assert task.id == outcome.task.id
        assert fixture.subscription_repository.get(outcome.subscription.id) is not None
    finally:
        fixture.close()


def test_list_my_cars_returns_only_this_users_subscriptions(fx):
    task_a = fx.task_repository.create(
        car_number="AA001AA", label=None, start_date=date(2026, 9, 1), end_date=date(2026, 10, 1),
        telegram_chat_id=10, created_by_user_id=10, monitoring_scope="client_bot",
    )
    task_b = fx.task_repository.create(
        car_number="BB002BB", label=None, start_date=date(2026, 9, 1), end_date=date(2026, 10, 1),
        telegram_chat_id=20, created_by_user_id=20, monitoring_scope="client_bot",
    )
    fx.subscription_repository.create(
        monitoring_task_id=task_a.id, car_number="AA001AA", telegram_user_id=10,
        telegram_chat_id=10, telegram_username="user10",
        start_date=date(2026, 9, 1), end_date=date(2026, 10, 1),
    )
    fx.subscription_repository.create(
        monitoring_task_id=task_b.id, car_number="BB002BB", telegram_user_id=20,
        telegram_chat_id=20, telegram_username="user20",
        start_date=date(2026, 9, 1), end_date=date(2026, 10, 1),
    )

    result = fx.service.list_my_cars(10)

    assert [s.car_number for s in result] == ["AA001AA"]
