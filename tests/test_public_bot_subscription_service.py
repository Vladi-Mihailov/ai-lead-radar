"""
Тесты SubscriptionService — application-слой Add Car flow @GEShtrafbot
(reader/public_bot/subscription_service.py). Repository — настоящие
(SQLite/tmp_path), FineProvider — фейковый (без сети). Тот же
FineCheckService, что использует и операторский FineJob/FineCommand —
никакой отдельной реализации проверки здесь не тестируется.
"""

import sys
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402
from telethon.errors import UsernameNotOccupiedError  # noqa: E402
from telethon.tl.types import User as TelethonUser  # noqa: E402

from reader.fines.check_service import FineCheckService  # noqa: E402
from reader.fines.detected_fine_repository import DetectedFineRepository  # noqa: E402
from reader.fines.models import ParsedFineRecord  # noqa: E402
from reader.fines.provider import FineProvider, FineProviderError  # noqa: E402
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.public_bot.owner_resolution import OwnerResolutionError  # noqa: E402
from reader.public_bot.subscription_repository import FineSubscriptionRepository  # noqa: E402
from reader.public_bot.subscription_service import (  # noqa: E402
    SubscriptionService,
    extend_client_bot_task_if_still_needed,
)
from reader.users.models import TelegramUserInfo  # noqa: E402
from reader.users.repository import UserRepository  # noqa: E402

_CHAT_ID = -100999
_OPERATOR_USER_ID = 111
_TRUSTED_ID = 5712994689
_TRUSTED_CHAT_ID = 5712994689


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


class _FakeTelegramClient:
    """Ровно то, что нужно от Telethon-клиента для owner_resolution.py —
    тот же приём, что и в tests/test_owner_resolution.py/test_fine_command.py."""

    def __init__(self, *, entities=None, errors=None):
        self._entities = {k.lower(): v for k, v in (entities or {}).items()}
        self._errors = {k.lower(): v for k, v in (errors or {}).items()}
        self.get_entity_calls: list[str] = []

    async def get_entity(self, entity):
        username = str(entity).lstrip("@").lower()
        self.get_entity_calls.append(username)
        if username in self._errors:
            raise self._errors[username]
        if username in self._entities:
            return self._entities[username]
        raise UsernameNotOccupiedError(request=None)


def _telethon_user(user_id: int, username: str) -> TelethonUser:
    return TelethonUser(
        id=user_id, is_self=False, contact=False, mutual_contact=False, deleted=False,
        bot=False, bot_chat_history=False, bot_nochats=False, verified=False, restricted=False,
        min=False, bot_inline_geo=False, support=False, scam=False, apply_min_photo=False,
        fake=False, bot_attach_menu=False, premium=False, attach_menu_enabled=False,
        bot_can_edit=False, close_friend=False, stories_hidden=False, stories_unavailable=False,
        access_hash=999,
        first_name="Real", last_name="Owner", username=username, phone=None, photo=None,
        status=None, bot_info_version=None, restriction_reason=None, bot_inline_placeholder=None,
        lang_code=None,
    )


def _record(
    car_number="M295YB196", fingerprint="fp-1", place=None, violation_description=None,
) -> ParsedFineRecord:
    return ParsedFineRecord(
        car_number=car_number,
        external_fine_id="AB123456",
        penalty_date=date(2026, 8, 6),
        due_date=date(2026, 8, 20),
        delivered_status="Не вручено",
        fingerprint=fingerprint,
        raw_data={"protocolNo": "AB123456"},
        place=place,
        violation_description=violation_description,
    )


class _Fixture:
    def __init__(self, tmp_path, records_by_car=None, provider_error=None, owner_resolver_client=None):
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
            owner_resolver_client=owner_resolver_client,
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


def test_list_my_cars_excludes_archived(fx):
    task = fx.task_repository.create(
        car_number="AA001AA", label=None, start_date=date(2026, 9, 1), end_date=date(2026, 10, 1),
        telegram_chat_id=10, created_by_user_id=10, monitoring_scope="client_bot",
    )
    sub = fx.subscription_repository.create(
        monitoring_task_id=task.id, car_number="AA001AA", telegram_user_id=10,
        telegram_chat_id=10, telegram_username="user10",
        start_date=date(2026, 9, 1), end_date=date(2026, 10, 1),
    )
    fx.subscription_repository.archive(sub.id)

    assert fx.service.list_my_cars(10) == []
    # Но строка сама по себе никуда не делась (soft delete).
    assert fx.subscription_repository.get(sub.id).status == "archived"


# ==== car-centric "📋 Мои авто" ON/OFF/Delete (см. design report про
# переработку UX) — SubscriptionService.turn_off_car/turn_on_car/
# delete_car, ownership через ту же get_actionable_subscription (владелец
# ИЛИ trusted-создатель), что и у 🔎/⛔. ====


def _make_client_car(fx, *, telegram_user_id=1, car_number="M295YB196", period_days=30, today=date(2026, 9, 3)):
    task = fx.task_repository.create(
        car_number=car_number, label=None, start_date=today, end_date=today + timedelta(days=period_days),
        telegram_chat_id=telegram_user_id, created_by_user_id=telegram_user_id, monitoring_scope="client_bot",
    )
    sub = fx.subscription_repository.create(
        monitoring_task_id=task.id, car_number=car_number, telegram_user_id=telegram_user_id,
        telegram_chat_id=telegram_user_id, telegram_username="client",
        start_date=today, end_date=today + timedelta(days=period_days),
    )
    return task, sub


def test_turn_off_car_stops_active_subscription(fx):
    _task, sub = _make_client_car(fx)

    result = fx.service.turn_off_car(sub.id, telegram_user_id=1)

    assert result is not None
    assert result.status == "stopped"
    assert fx.subscription_repository.get(sub.id).status == "stopped"


def test_turn_off_car_excludes_it_from_actionable_and_deliverable(fx):
    """Явное требование: OFF исключается из фонового мониторинга/доставки."""
    _task, sub = _make_client_car(fx)
    fx.service.turn_off_car(sub.id, telegram_user_id=1)

    assert fx.service.list_actionable_subscriptions(1, today=date(2026, 9, 3)) == []
    assert fx.subscription_repository.list_all_deliverable(today=date(2026, 9, 3)) == []


def test_turn_off_car_rejects_wrong_user(fx):
    _task, sub = _make_client_car(fx)

    result = fx.service.turn_off_car(sub.id, telegram_user_id=999)

    assert result is None
    assert fx.subscription_repository.get(sub.id).status == "active"


def test_turn_off_car_returns_none_for_already_stopped(fx):
    """turn_off_car — ИСКЛЮЧИТЕЛЬНО ON -> OFF; повторный вызов на уже
    stopped ничего не делает (защита от двойного нажатия/гонки)."""
    _task, sub = _make_client_car(fx)
    fx.service.turn_off_car(sub.id, telegram_user_id=1)

    result = fx.service.turn_off_car(sub.id, telegram_user_id=1)

    assert result is None


def test_turn_on_car_reactivates_within_remaining_period(fx):
    _task, sub = _make_client_car(fx, today=date(2026, 9, 3), period_days=30)
    fx.service.turn_off_car(sub.id, telegram_user_id=1)

    result = fx.service.turn_on_car(sub.id, telegram_user_id=1, today=date(2026, 9, 10))

    assert result is not None
    assert result.status == "active"
    # end_date ещё не прошёл — период НЕ меняется (не дарим лишние дни).
    assert result.start_date == date(2026, 9, 3)
    assert result.end_date == date(2026, 10, 3)


def test_turn_on_car_gives_fresh_period_when_old_one_elapsed(fx):
    """Явное требование задачи: "корректно обработай reactivation", если
    подписка простояла OFF дольше своего исходного периода — свежий
    период ТОЙ ЖЕ длины (30 дней), начиная с today, а не оживший в прошлом
    end_date (что было бы немедленно снова неактуально)."""
    _task, sub = _make_client_car(fx, today=date(2026, 1, 1), period_days=30)
    fx.service.turn_off_car(sub.id, telegram_user_id=1)

    result = fx.service.turn_on_car(sub.id, telegram_user_id=1, today=date(2026, 9, 3))

    assert result is not None
    assert result.status == "active"
    assert result.start_date == date(2026, 9, 3)
    assert result.end_date == date(2026, 10, 3)  # те же 30 дней, от today


def test_turn_on_car_does_not_create_duplicate_subscription(fx):
    _task, sub = _make_client_car(fx)
    fx.service.turn_off_car(sub.id, telegram_user_id=1)

    fx.service.turn_on_car(sub.id, telegram_user_id=1, today=date(2026, 9, 10))

    all_subs = fx.subscription_repository.list_by_user(1)
    assert [s.id for s in all_subs] == [sub.id]


def test_turn_on_car_returns_none_for_not_stopped(fx):
    """turn_on_car — ИСКЛЮЧИТЕЛЬНО OFF -> ON; активную подписку "включить"
    нельзя (нечего включать)."""
    _task, sub = _make_client_car(fx)

    result = fx.service.turn_on_car(sub.id, telegram_user_id=1, today=date(2026, 9, 10))

    assert result is None


def test_turn_on_car_rejects_wrong_user(fx):
    _task, sub = _make_client_car(fx)
    fx.service.turn_off_car(sub.id, telegram_user_id=1)

    result = fx.service.turn_on_car(sub.id, telegram_user_id=999, today=date(2026, 9, 10))

    assert result is None
    assert fx.subscription_repository.get(sub.id).status == "stopped"


def test_turn_on_car_reactivates_completed_task(fx):
    """Если ПОКА подписка была stopped, сама FineMonitoringTask успела
    стать completed (например, единственный подписчик перестал быть
    actionable) — turn_on_car должен вернуть её в active, иначе
    status='active' у подписки ничего бы не значил для фонового
    FineJob (см. design report: "корректно обработай reactivation")."""
    task, sub = _make_client_car(fx, today=date(2026, 1, 1), period_days=30)
    fx.service.turn_off_car(sub.id, telegram_user_id=1)
    fx.task_repository.set_status(task.id, "completed")

    result = fx.service.turn_on_car(sub.id, telegram_user_id=1, today=date(2026, 9, 3))

    assert result is not None
    updated_task = fx.task_repository.get(task.id)
    assert updated_task.status == "active"
    assert updated_task.end_date >= result.end_date


def test_delete_car_archives_active_subscription(fx):
    _task, sub = _make_client_car(fx)

    deleted = fx.service.delete_car(sub.id, telegram_user_id=1)

    assert deleted is True
    assert fx.subscription_repository.get(sub.id).status == "archived"
    assert fx.service.list_my_cars(1) == []


def test_delete_car_archives_stopped_subscription(fx):
    """Удаление — ОТДЕЛЬНОЕ от OFF действие, разрешено из любого статуса,
    не только 'active'."""
    _task, sub = _make_client_car(fx)
    fx.service.turn_off_car(sub.id, telegram_user_id=1)

    deleted = fx.service.delete_car(sub.id, telegram_user_id=1)

    assert deleted is True
    assert fx.subscription_repository.get(sub.id).status == "archived"


def test_delete_car_rejects_wrong_user(fx):
    _task, sub = _make_client_car(fx)

    deleted = fx.service.delete_car(sub.id, telegram_user_id=999)

    assert deleted is False
    assert fx.subscription_repository.get(sub.id).status == "active"


def test_delete_car_does_not_remove_historical_detected_fines(fx):
    task, sub = _make_client_car(fx)
    fine = fx.detected_fine_repository.create(
        monitoring_task_id=task.id, car_number=sub.car_number,
        external_fine_id="AB1", fingerprint="fp-1",
        penalty_date=date(2026, 9, 1), due_date=date(2026, 9, 20),
        delivered_status="Не вручено", raw_data="{}",
    )

    fx.service.delete_car(sub.id, telegram_user_id=1)

    assert fx.detected_fine_repository.get_by_fingerprint(task.id, "fp-1") is not None
    assert fine.id is not None


async def test_readd_car_after_delete_creates_new_subscription(fx):
    """Явное требование: "повторно добавить этот автомобиль в будущем
    можно" — ➕ Добавить авто на тот же номер после удаления создаёт
    НОВУЮ подписку (archived не считается "уже есть активная")."""
    _task, sub = _make_client_car(fx, telegram_user_id=1, car_number="M295YB196")
    fx.service.delete_car(sub.id, telegram_user_id=1)

    outcome = await fx.service.add_car(
        telegram_user_id=1, telegram_chat_id=1, username="alice",
        first_name=None, last_name=None, car_number="M295YB196",
        period_days=30, today=date(2026, 9, 10),
    )

    assert outcome.subscription.id != sub.id
    assert outcome.subscription.status == "active"
    active_cars = [s for s in fx.service.list_my_cars(1) if s.status == "active"]
    assert [s.car_number for s in active_cars] == ["M295YB196"]


# ==== trusted-operator delegated flow (см. design report) ====


async def test_add_delegated_car_resolves_immediately_via_local_user_repository(fx):
    fx.user_repository.upsert(
        TelegramUserInfo(user_id=777, username="real_owner", first_name="Real", last_name="Owner")
    )

    outcome = await fx.service.add_delegated_car(
        created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
        owner_username="real_owner", car_number="M295YB196", period_days=90,
        today=date(2026, 9, 3),
    )

    assert outcome.pending_claim is False
    assert outcome.claim_link is None
    assert outcome.subscription.status == "active"
    assert outcome.subscription.telegram_user_id == 777
    assert outcome.subscription.telegram_chat_id == 777
    assert outcome.subscription.created_by_telegram_user_id == _TRUSTED_ID
    assert outcome.subscription.owner_username_hint == "real_owner"
    assert outcome.task.monitoring_scope == "client_bot"

    # UserRepository синхронизирован — оператор увидит владельца в fine list/check.
    owners = fx.user_repository.find_by_car_number("M295YB196")
    assert [o.user_id for o in owners] == [777]


async def test_add_delegated_car_resolves_via_live_telegram_lookup(tmp_path):
    client = _FakeTelegramClient(entities={"newcomer": _telethon_user(888, "newcomer")})
    fixture = _Fixture(tmp_path, owner_resolver_client=client)
    try:
        outcome = await fixture.service.add_delegated_car(
            created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
            owner_username="newcomer", car_number="M295YB196", period_days=30,
            today=date(2026, 9, 3),
        )

        assert outcome.pending_claim is False
        assert outcome.subscription.telegram_user_id == 888
        assert client.get_entity_calls == ["newcomer"]
    finally:
        fixture.close()


async def test_add_delegated_car_creates_pending_claim_when_owner_cannot_be_resolved(fx):
    outcome = await fx.service.add_delegated_car(
        created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
        owner_username="unknown_person", car_number="M295YB196", period_days=30,
        today=date(2026, 9, 3),
    )

    assert outcome.pending_claim is True
    assert outcome.subscription.status == "pending_claim"
    assert outcome.subscription.telegram_user_id is None
    assert outcome.subscription.telegram_chat_id is None
    assert outcome.subscription.owner_username_hint == "unknown_person"
    assert outcome.claim_link is not None
    assert outcome.claim_link.startswith("https://t.me/ProtocolGEbot?start=claim_")

    # Мониторинг УЖЕ идёт — задача создана/проверена, несмотря на то, что
    # владелец ещё не резолвлен (см. design report).
    assert outcome.task.status == "active"
    assert outcome.check_ok is True


async def test_add_delegated_car_monitoring_starts_even_when_owner_unresolved(fx):
    """Явная регрессия на "monitoring task запускается сразу, claim
    владельца не блокирует мониторинг"."""
    outcome = await fx.service.add_delegated_car(
        created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
        owner_username="unknown_person", car_number="M295YB196", period_days=30,
        today=date(2026, 9, 3),
    )

    [task] = fx.task_repository.list_active()
    assert task.id == outcome.task.id
    assert task.monitoring_scope == "client_bot"


async def test_add_delegated_car_reuses_operator_task_without_changing_scope(fx):
    operator_task = fx.task_repository.create(
        car_number="M295YB196", label=None,
        start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
        telegram_chat_id=_CHAT_ID, created_by_user_id=_OPERATOR_USER_ID,
    )

    outcome = await fx.service.add_delegated_car(
        created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
        owner_username="unknown_person", car_number="M295YB196", period_days=30,
        today=date(2026, 9, 3),
    )

    assert outcome.task.id == operator_task.id
    assert outcome.task.monitoring_scope == "operator"  # НЕ изменился


async def test_add_delegated_car_propagates_owner_resolution_error(tmp_path):
    client = _FakeTelegramClient(errors={"flaky": RuntimeError("network down")})
    fixture = _Fixture(tmp_path, owner_resolver_client=client)
    try:
        with pytest.raises(OwnerResolutionError):
            await fixture.service.add_delegated_car(
                created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
                owner_username="flaky", car_number="M295YB196", period_days=30,
                today=date(2026, 9, 3),
            )

        # Ничего не создано — ни задачи, ни подписки.
        assert fixture.task_repository.list_active() == []
    finally:
        fixture.close()


# ---- trusted-operator delegated flow БЕЗ клиента ("Отмена" на "Добавить
# Telegram клиента?", см. design report) ----


async def test_add_delegated_car_without_client_creates_no_subscription(fx):
    """Явное требование задачи: "без фиктивного owner/subscription" —
    НИ ОДНОЙ строки fine_monitoring_subscriptions для этого flow, ни
    активной, ни pending_claim."""
    outcome = await fx.service.add_delegated_car_without_client(
        created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
        car_number="M295YB196", period_days=90, today=date(2026, 9, 3),
    )

    assert outcome.task.monitoring_scope == "client_bot"
    assert outcome.task.status == "active"
    assert fx.subscription_repository.list_managed_by_creator(_TRUSTED_ID) == []
    assert fx.subscription_repository.list_by_user(_TRUSTED_ID) == []


async def test_add_delegated_car_without_client_starts_monitoring_immediately(fx):
    """Мониторинг стартует сразу — тот же принцип, что и у delegated-с-
    клиентом (см. test_add_delegated_car_monitoring_starts_even_when_owner_
    unresolved) — отсутствие клиента не блокирует мониторинг."""
    outcome = await fx.service.add_delegated_car_without_client(
        created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
        car_number="M295YB196", period_days=30, today=date(2026, 9, 3),
    )

    [task] = fx.task_repository.list_active()
    assert task.id == outcome.task.id
    assert task.monitoring_scope == "client_bot"
    assert outcome.check_ok is True


async def test_add_delegated_car_without_client_does_not_sync_fictitious_owner_to_user_repository(fx):
    """Явное требование задачи: "Не создавать фиктивный owner
    telegram_user_id" — UserRepository не должен получить ни одной записи
    из этого flow."""
    await fx.service.add_delegated_car_without_client(
        created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
        car_number="M295YB196", period_days=30, today=date(2026, 9, 3),
    )

    assert fx.user_repository.find_by_car_number("M295YB196") == []


async def test_add_delegated_car_without_client_repeat_extends_same_task(fx):
    """Повторное "Добавить авто без клиента" тем же оператором на ту же
    машину — продлевает ТУ ЖЕ задачу (обычный _create_or_extend_task,
    extend_period_if_shorter), а не создаёт вторую задачу или какую-либо
    подписку."""
    first = await fx.service.add_delegated_car_without_client(
        created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
        car_number="M295YB196", period_days=30, today=date(2026, 9, 3),
    )

    second = await fx.service.add_delegated_car_without_client(
        created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
        car_number="M295YB196", period_days=90, today=date(2026, 9, 3),
    )

    assert second.task.id == first.task.id
    assert second.task.end_date == date(2026, 9, 3) + timedelta(days=90)
    assert fx.subscription_repository.list_managed_by_creator(_TRUSTED_ID) == []


async def test_add_delegated_car_without_client_reuses_operator_task_without_changing_scope(fx):
    operator_task = fx.task_repository.create(
        car_number="M295YB196", label=None,
        start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
        telegram_chat_id=_CHAT_ID, created_by_user_id=_OPERATOR_USER_ID,
    )

    outcome = await fx.service.add_delegated_car_without_client(
        created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
        car_number="M295YB196", period_days=30, today=date(2026, 9, 3),
    )

    assert outcome.task.id == operator_task.id
    assert outcome.task.monitoring_scope == "operator"  # НЕ изменился


async def test_add_delegated_car_without_client_then_with_client_attaches_to_same_task(fx):
    """Явное требование задачи: "если позже понадобится привязать клиента —
    архитектура не должна этому препятствовать" — обычный delegated-с-
    клиентом flow на ту же машину переиспользует ТУ ЖЕ задачу и создаёт
    для неё первую (и единственную) подписку — никакой миграции/связывания
    не требуется, т.к. до этого не было подписки вовсе."""
    fx.user_repository.upsert(
        TelegramUserInfo(user_id=777, username="real_owner", first_name="Real", last_name="Owner")
    )
    without_client = await fx.service.add_delegated_car_without_client(
        created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
        car_number="M295YB196", period_days=30, today=date(2026, 9, 3),
    )

    with_client = await fx.service.add_delegated_car(
        created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
        owner_username="real_owner", car_number="M295YB196", period_days=30,
        today=date(2026, 9, 3),
    )

    assert with_client.task.id == without_client.task.id
    [subscription] = fx.subscription_repository.list_by_user(777)
    assert subscription.monitoring_task_id == without_client.task.id


async def test_claim_binds_owner_and_syncs_user_repository(fx):
    outcome = await fx.service.add_delegated_car(
        created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
        owner_username="unknown_person", car_number="M295YB196", period_days=30,
        today=date(2026, 9, 3),
    )
    token = outcome.claim_link.rsplit("claim_", 1)[1]

    claim_outcome = fx.service.claim(
        token, telegram_user_id=777, telegram_chat_id=777, telegram_username="unknown_person",
        first_name="Real", last_name="Owner",
    )

    assert claim_outcome is not None
    assert claim_outcome.subscription.status == "active"
    assert claim_outcome.subscription.telegram_user_id == 777

    stored = fx.user_repository.get(777)
    assert stored is not None
    assert stored.username == "unknown_person"
    owners = fx.user_repository.find_by_car_number("M295YB196")
    assert [o.user_id for o in owners] == [777]


def test_claim_returns_none_for_unknown_token(fx):
    assert fx.service.claim(
        "bogus", telegram_user_id=1, telegram_chat_id=1, telegram_username="x",
        first_name=None, last_name=None,
    ) is None


async def test_list_managed_cars_returns_only_delegated_by_this_trusted_operator(fx):
    await fx.service.add_delegated_car(
        created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
        owner_username="owner_one", car_number="AA001AA", period_days=30, today=date(2026, 9, 3),
    )
    other_trusted_id = 410811386
    await fx.service.add_delegated_car(
        created_by_telegram_user_id=other_trusted_id, created_by_telegram_chat_id=other_trusted_id,
        owner_username="owner_two", car_number="BB002BB", period_days=30, today=date(2026, 9, 3),
    )
    await fx.service.add_car(
        telegram_user_id=_TRUSTED_ID, telegram_chat_id=_TRUSTED_CHAT_ID, username="trusted_self",
        first_name=None, last_name=None, car_number="CC003CC", period_days=30, today=date(2026, 9, 3),
    )

    managed = fx.service.list_managed_cars(_TRUSTED_ID)

    assert [s.car_number for s in managed] == ["AA001AA"]


async def test_stop_subscription_allows_creator_and_rejects_stranger(fx):
    fx.user_repository.upsert(
        TelegramUserInfo(user_id=777, username="real_owner", first_name=None, last_name=None)
    )
    outcome = await fx.service.add_delegated_car(
        created_by_telegram_user_id=_TRUSTED_ID, created_by_telegram_chat_id=_TRUSTED_CHAT_ID,
        owner_username="real_owner", car_number="M295YB196", period_days=30, today=date(2026, 9, 3),
    )
    assert outcome.pending_claim is False  # резолвлено сразу — подписка active

    assert fx.service.stop_subscription(outcome.subscription.id, telegram_user_id=999999) is False
    assert fx.service.stop_subscription(outcome.subscription.id, telegram_user_id=_TRUSTED_ID) is True


# ==== 🔎 Проверить сейчас / ⛔ Остановить мониторинг (см. design report Stage 4) ====


async def test_list_actionable_subscriptions_combines_own_and_managed_without_duplicates(fx):
    fx.user_repository.upsert(
        TelegramUserInfo(user_id=777, username="real_owner", first_name=None, last_name=None)
    )
    own = await fx.service.add_car(
        telegram_user_id=42, telegram_chat_id=42, username="client",
        first_name=None, last_name=None, car_number="AA001AA", period_days=30, today=date(2026, 9, 3),
    )
    delegated = await fx.service.add_delegated_car(
        created_by_telegram_user_id=42, created_by_telegram_chat_id=42,
        owner_username="real_owner", car_number="BB002BB", period_days=30, today=date(2026, 9, 3),
    )

    actionable = fx.service.list_actionable_subscriptions(42, today=date(2026, 9, 3))

    assert {s.id for s in actionable} == {own.subscription.id, delegated.subscription.id}


def test_list_actionable_subscriptions_excludes_stopped_and_expired(fx):
    task = fx.task_repository.create(
        car_number="AA001AA", label=None, start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
        telegram_chat_id=1, created_by_user_id=1, monitoring_scope="client_bot",
    )
    expired = fx.subscription_repository.create(
        monitoring_task_id=task.id, car_number="AA001AA", telegram_user_id=1,
        telegram_chat_id=1, telegram_username="alice",
        start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
    )
    active = fx.subscription_repository.create(
        monitoring_task_id=task.id, car_number="AA001AA", telegram_user_id=2,
        telegram_chat_id=2, telegram_username="bob",
        start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
    )
    fx.subscription_repository.stop_by_owner_or_creator(expired.id, telegram_user_id=1)  # не влияет — уже expired по дате

    actionable_for_expired_owner = fx.service.list_actionable_subscriptions(1, today=date(2026, 9, 3))
    actionable_for_active_owner = fx.service.list_actionable_subscriptions(2, today=date(2026, 9, 3))

    assert actionable_for_expired_owner == []
    assert [s.id for s in actionable_for_active_owner] == [active.id]


async def test_get_actionable_subscription_rejects_stranger(fx):
    outcome = await fx.service.add_car(
        telegram_user_id=42, telegram_chat_id=42, username="client",
        first_name=None, last_name=None, car_number="AA001AA", period_days=30, today=date(2026, 9, 3),
    )

    assert fx.service.get_actionable_subscription(outcome.subscription.id, telegram_user_id=42) is not None
    assert fx.service.get_actionable_subscription(outcome.subscription.id, telegram_user_id=999) is None
    assert fx.service.get_actionable_subscription(999999, telegram_user_id=42) is None


async def test_check_now_uses_existing_check_service_and_dedup(fx):
    """Явное требование задачи: 🔎 Проверить сейчас не должен создавать
    отдельную систему штрафов/обходить дедуп."""
    outcome = await fx.service.add_car(
        telegram_user_id=42, telegram_chat_id=42, username="client",
        first_name=None, last_name=None, car_number="AA001AA", period_days=30, today=date(2026, 9, 3),
    )
    assert fx.provider.requested_plates == ["AA001AA"]  # уже проверено внутри add_car

    result = await fx.service.check_now(outcome.subscription.id, telegram_user_id=42)

    assert result is not None
    assert result.car_number == "AA001AA"
    assert result.check_ok is True
    assert result.fines == []
    assert result.is_owner is True
    # Тот же provider/FineCheckService — второй запрос, не отдельная система.
    assert fx.provider.requested_plates == ["AA001AA", "AA001AA"]


async def test_check_now_returns_none_for_unauthorized_user(fx):
    outcome = await fx.service.add_car(
        telegram_user_id=42, telegram_chat_id=42, username="client",
        first_name=None, last_name=None, car_number="AA001AA", period_days=30, today=date(2026, 9, 3),
    )

    result = await fx.service.check_now(outcome.subscription.id, telegram_user_id=999)

    assert result is None


async def test_check_now_reports_new_fines_found(tmp_path):
    fixture = _Fixture(tmp_path)
    try:
        outcome = await fixture.service.add_car(
            telegram_user_id=42, telegram_chat_id=42, username="client",
            first_name=None, last_name=None, car_number="AA001AA", period_days=30, today=date(2026, 9, 3),
        )
        # Провайдер теперь "находит" штраф для следующей проверки.
        fixture.provider._records_by_car["AA001AA"] = [_record(car_number="AA001AA", fingerprint="fp-new")]

        result = await fixture.service.check_now(outcome.subscription.id, telegram_user_id=42)

        assert len(result.fines) == 1
    finally:
        fixture.close()


async def test_check_now_shows_existing_fine_not_only_new(tmp_path):
    """Явное требование задачи про UX manual check: 🔎 Проверить сейчас
    должен показывать ВСЕ штрафы, которые police.ge вернул сейчас, а не
    только новые с прошлой проверки — второй вызов check_now() с тем же
    штрафом (уже существующим в detected_fines) должен по-прежнему
    вернуть его в outcome.fines, не пустой список."""
    fixture = _Fixture(tmp_path)
    try:
        fixture.provider._records_by_car["AA001AA"] = [_record(car_number="AA001AA", fingerprint="fp-existing")]
        outcome = await fixture.service.add_car(
            telegram_user_id=42, telegram_chat_id=42, username="client",
            first_name=None, last_name=None, car_number="AA001AA", period_days=30, today=date(2026, 9, 3),
        )

        result = await fixture.service.check_now(outcome.subscription.id, telegram_user_id=42)

        assert len(result.fines) == 1
        assert result.fines[0].external_fine_id == "AB123456"
    finally:
        fixture.close()


async def test_check_now_by_trusted_creator_is_not_owner(tmp_path):
    """Явное требование задачи: CTA-кнопки — только реальному владельцу,
    не trusted-оператору, управляющему чужой delegated-подпиской (см.
    design report "CTA — только owner"). is_owner=False здесь — тот же
    subscription_id, но telegram_user_id совпадает с created_by, не с
    owner'ом."""
    fixture = _Fixture(tmp_path)
    try:
        outcome = await fixture.service.add_delegated_car(
            created_by_telegram_user_id=555, created_by_telegram_chat_id=555,
            car_number="AA001AA", owner_username="unknown_person",
            period_days=30, today=date(2026, 9, 3),
        )

        result = await fixture.service.check_now(
            outcome.subscription.id, telegram_user_id=555,  # создатель, не владелец
        )

        assert result is not None
        assert result.is_owner is False
    finally:
        fixture.close()


# ==== trusted-operator task-level admin (см. design report: пересмотр
# архитектуры — fine_monitoring_tasks = source of truth, subscription НЕ
# требуется). is_trusted() — ответственность ConversationController, эти
# методы сами авторизацию не проверяют (см. их докстроки). ====


async def test_list_all_active_tasks_returns_tasks_without_any_subscription(fx):
    task = fx.task_repository.create(
        car_number="E911EE95", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 12, 31),
        telegram_chat_id=_CHAT_ID, created_by_user_id=_OPERATOR_USER_ID,
    )
    fx.task_repository.create(
        car_number="COMPLETED1", label=None, start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
        telegram_chat_id=_CHAT_ID, created_by_user_id=_OPERATOR_USER_ID,
    ).id
    completed = fx.task_repository.get_active_by_car_number("COMPLETED1")[0]
    fx.task_repository.set_status(completed.id, "completed")

    tasks = fx.service.list_all_active_tasks()

    assert [t.id for t in tasks] == [task.id]


def test_get_active_task_for_trusted_admin_returns_none_for_missing_task(fx):
    assert fx.service.get_active_task_for_trusted_admin(999999) is None


def test_get_active_task_for_trusted_admin_returns_none_for_inactive_task(fx):
    task = fx.task_repository.create(
        car_number="E911EE95", label=None, start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
        telegram_chat_id=_CHAT_ID, created_by_user_id=_OPERATOR_USER_ID,
    )
    fx.task_repository.set_status(task.id, "stopped")

    assert fx.service.get_active_task_for_trusted_admin(task.id) is None


def test_get_active_task_for_trusted_admin_returns_active_task(fx):
    task = fx.task_repository.create(
        car_number="E911EE95", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 12, 31),
        telegram_chat_id=_CHAT_ID, created_by_user_id=_OPERATOR_USER_ID,
    )

    found = fx.service.get_active_task_for_trusted_admin(task.id)

    assert found is not None
    assert found.id == task.id


async def test_check_now_task_works_without_any_subscription(fx):
    task = fx.task_repository.create(
        car_number="E911EE95", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 12, 31),
        telegram_chat_id=_CHAT_ID, created_by_user_id=_OPERATOR_USER_ID,
    )

    result = await fx.service.check_now_task(task.id)

    assert result is not None
    assert result.car_number == "E911EE95"
    assert result.check_ok is True
    assert result.is_owner is False  # task-level admin — всегда trusted-оператор
    assert fx.subscription_repository.list_by_user(_OPERATOR_USER_ID) == []


async def test_check_now_task_returns_none_for_inactive_task(fx):
    task = fx.task_repository.create(
        car_number="E911EE95", label=None, start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
        telegram_chat_id=_CHAT_ID, created_by_user_id=_OPERATOR_USER_ID,
    )
    fx.task_repository.set_status(task.id, "completed")

    result = await fx.service.check_now_task(task.id)

    assert result is None


# ---- count_active_tasks / list_active_tasks_page (📋 Мои авто pagination,
# см. design report) ----


def test_count_active_tasks_counts_only_active(fx):
    active = fx.task_repository.create(
        car_number="AA001AA", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 12, 31),
        telegram_chat_id=_CHAT_ID, created_by_user_id=_OPERATOR_USER_ID,
    )
    completed = fx.task_repository.create(
        car_number="BB002BB", label=None, start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
        telegram_chat_id=_CHAT_ID, created_by_user_id=_OPERATOR_USER_ID,
    )
    fx.task_repository.set_status(completed.id, "completed")

    assert fx.service.count_active_tasks() == 1
    assert active.status == "active"


def test_list_active_tasks_page_computes_offset_from_page_and_size(fx):
    ids = []
    for i in range(25):
        task = fx.task_repository.create(
            car_number=f"CAR{i:04d}", label=None,
            start_date=date(2026, 8, 1), end_date=date(2026, 12, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_OPERATOR_USER_ID,
        )
        ids.append(task.id)

    page0 = fx.service.list_active_tasks_page(page=0, page_size=10)
    page1 = fx.service.list_active_tasks_page(page=1, page_size=10)
    page2 = fx.service.list_active_tasks_page(page=2, page_size=10)

    assert [t.id for t in page0] == ids[0:10]
    assert [t.id for t in page1] == ids[10:20]
    assert [t.id for t in page2] == ids[20:25]


# ---- check_now_task_by_car_number (trusted 🔎 Проверить сейчас по
# номеру, см. design report: "искать автомобиль в списке неудобно") ----


async def test_check_now_task_by_car_number_finds_active_task_without_subscription(fx):
    fx.task_repository.create(
        car_number="E911EE95", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 12, 31),
        telegram_chat_id=_CHAT_ID, created_by_user_id=_OPERATOR_USER_ID,
    )

    result = await fx.service.check_now_task_by_car_number("E911EE95")

    assert result is not None
    assert result.car_number == "E911EE95"
    assert result.check_ok is True
    assert fx.subscription_repository.list_by_user(_OPERATOR_USER_ID) == []


async def test_check_now_task_by_car_number_returns_none_for_unknown_plate(fx):
    result = await fx.service.check_now_task_by_car_number("E911EE95")

    assert result is None


async def test_check_now_task_by_car_number_returns_none_for_inactive_task(fx):
    task = fx.task_repository.create(
        car_number="E911EE95", label=None, start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
        telegram_chat_id=_CHAT_ID, created_by_user_id=_OPERATOR_USER_ID,
    )
    fx.task_repository.set_status(task.id, "completed")

    result = await fx.service.check_now_task_by_car_number("E911EE95")

    assert result is None


# ---- перевод грузинского place/violation_description на русский (см.
# задачу про перевод текста штрафов) — "manual check receives Russian" ----

_GEORGIAN_PLACE = "სამტრედია-გრიგოლეთი 26კმ"
_GEORGIAN_DESCRIPTION = "ასკ 125-ე მუხლის პირველის პრიმა ნაწილი"
_RUSSIAN_PLACE = "Самтредиа-Григолети 26км"
_RUSSIAN_DESCRIPTION = "Статья 125-1-1, часть первая прима"


async def test_check_now_task_by_car_number_shows_russian_translation(tmp_path):
    """Manual "🔎 Проверить сейчас" (task-level, trusted-оператор) должен
    показывать уже переведённый (кешированный) русский текст — тот же
    format_fine_block, что и у client/operator/trusted notification."""
    fixture = _Fixture(tmp_path)
    try:
        task = fixture.task_repository.create(
            car_number="E911EE95", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 12, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_OPERATOR_USER_ID,
        )
        # Строка уже была переведена ранее (например, фоновой проверкой) —
        # place_ru/violation_description_ru закешированы в detected_fines.
        fixture.detected_fine_repository.create(
            monitoring_task_id=task.id, car_number="E911EE95",
            external_fine_id="AB123456", fingerprint="fp-1",
            penalty_date=date(2026, 8, 6), due_date=date(2026, 8, 20),
            delivered_status="Не вручено", raw_data="{}",
            place=_GEORGIAN_PLACE, violation_description=_GEORGIAN_DESCRIPTION,
            place_ru=_RUSSIAN_PLACE, violation_description_ru=_RUSSIAN_DESCRIPTION,
        )
        fixture.provider._records_by_car["E911EE95"] = [
            _record(
                car_number="E911EE95", fingerprint="fp-1",
                place=_GEORGIAN_PLACE, violation_description=_GEORGIAN_DESCRIPTION,
            )
        ]

        result = await fixture.service.check_now_task_by_car_number("E911EE95")

        assert len(result.fines) == 1
        assert result.fines[0].place_ru == _RUSSIAN_PLACE
        assert result.fines[0].violation_description_ru == _RUSSIAN_DESCRIPTION
    finally:
        fixture.close()


async def test_check_now_task_by_car_number_uses_existing_dedup(tmp_path):
    fixture = _Fixture(tmp_path, records_by_car={"E911EE95": [_record(car_number="E911EE95")]})
    try:
        fixture.task_repository.create(
            car_number="E911EE95", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 12, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_OPERATOR_USER_ID,
        )

        first = await fixture.service.check_now_task_by_car_number("E911EE95")
        assert len(first.fines) == 1  # genuinely new — становится detected_fines

        second = await fixture.service.check_now_task_by_car_number("E911EE95")
        # Явное требование задачи про UX manual check: тот же (уже
        # известный) штраф всё равно показывается — 🔎 показывает ТЕКУЩЕЕ
        # состояние машины, а не только новые с прошлой проверки.
        assert len(second.fines) == 1
        assert second.fines[0].external_fine_id == first.fines[0].external_fine_id
    finally:
        fixture.close()


def test_count_active_or_pending_subscribers_for_task(fx):
    task = fx.task_repository.create(
        car_number="M398YK763", label=None, start_date=date(2026, 9, 1), end_date=date(2027, 9, 1),
        telegram_chat_id=_TRUSTED_ID, created_by_user_id=_TRUSTED_ID, monitoring_scope="client_bot",
    )
    assert fx.service.count_active_or_pending_subscribers_for_task(task.id) == 0

    fx.subscription_repository.create(
        monitoring_task_id=task.id, car_number="M398YK763",
        telegram_user_id=777, telegram_chat_id=777, telegram_username="client",
        start_date=date(2026, 9, 1), end_date=date(2027, 9, 1),
    )

    assert fx.service.count_active_or_pending_subscribers_for_task(task.id) == 1


def test_stop_task_for_trusted_admin_stops_task_and_returns_true(fx):
    task = fx.task_repository.create(
        car_number="E911EE95", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 12, 31),
        telegram_chat_id=_CHAT_ID, created_by_user_id=_OPERATOR_USER_ID,
    )

    stopped = fx.service.stop_task_for_trusted_admin(task.id)

    assert stopped is True
    assert fx.task_repository.get(task.id).status == "stopped"


def test_stop_task_for_trusted_admin_returns_false_for_already_inactive_task(fx):
    task = fx.task_repository.create(
        car_number="E911EE95", label=None, start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
        telegram_chat_id=_CHAT_ID, created_by_user_id=_OPERATOR_USER_ID,
    )
    fx.task_repository.set_status(task.id, "stopped")

    stopped = fx.service.stop_task_for_trusted_admin(task.id)

    assert stopped is False


def test_stop_task_for_trusted_admin_stops_related_subscriptions_too(fx):
    """Явное требование задачи: не оставлять клиенту ложное "мониторинг
    активен" после forced task-level Stop — реальный переход
    active/pending_claim -> stopped на уровне подписок, тот же статус,
    что и у обычного user-initiated Stop."""
    task = fx.task_repository.create(
        car_number="M398YK763", label=None, start_date=date(2026, 9, 1), end_date=date(2027, 9, 1),
        telegram_chat_id=_TRUSTED_ID, created_by_user_id=_TRUSTED_ID, monitoring_scope="client_bot",
    )
    subscription = fx.subscription_repository.create(
        monitoring_task_id=task.id, car_number="M398YK763",
        telegram_user_id=777, telegram_chat_id=777, telegram_username="client",
        start_date=date(2026, 9, 1), end_date=date(2027, 9, 1),
    )

    stopped = fx.service.stop_task_for_trusted_admin(task.id)

    assert stopped is True
    assert fx.subscription_repository.get(subscription.id).status == "stopped"


# ==== extend_client_bot_task_if_still_needed (см. design report Stage 4,
# раздел "Task lifecycle") ====


async def test_extend_hook_extends_task_when_subscription_still_needs_it(fx):
    task = fx.task_repository.create(
        car_number="AA001AA", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
        telegram_chat_id=1, created_by_user_id=1, monitoring_scope="client_bot",
    )
    fx.subscription_repository.create(
        monitoring_task_id=task.id, car_number="AA001AA", telegram_user_id=1,
        telegram_chat_id=1, telegram_username="alice",
        start_date=date(2026, 8, 1), end_date=date(2026, 12, 1),  # дольше, чем сама задача
    )

    updated = await extend_client_bot_task_if_still_needed(
        task, date(2026, 9, 1),
        task_repository=fx.task_repository, subscription_repository=fx.subscription_repository,
    )

    assert updated.end_date == date(2026, 12, 1)


async def test_extend_hook_returns_unchanged_when_no_subscribers_remain(fx):
    task = fx.task_repository.create(
        car_number="AA001AA", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
        telegram_chat_id=1, created_by_user_id=1, monitoring_scope="client_bot",
    )

    updated = await extend_client_bot_task_if_still_needed(
        task, date(2026, 9, 1),
        task_repository=fx.task_repository, subscription_repository=fx.subscription_repository,
    )

    assert updated.end_date == task.end_date
    assert updated.id == task.id


async def test_extend_hook_is_noop_for_operator_scope_task(fx):
    task = fx.task_repository.create(
        car_number="AA001AA", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
        telegram_chat_id=1, created_by_user_id=1,
    )
    assert task.monitoring_scope == "operator"
    fx.subscription_repository.create(
        monitoring_task_id=task.id, car_number="AA001AA", telegram_user_id=1,
        telegram_chat_id=1, telegram_username="alice",
        start_date=date(2026, 8, 1), end_date=date(2026, 12, 1),
    )

    updated = await extend_client_bot_task_if_still_needed(
        task, date(2026, 9, 1),
        task_repository=fx.task_repository, subscription_repository=fx.subscription_repository,
    )

    # "operator task semantics не менять" — хук не должен трогать
    # операторскую задачу, даже если формально нашёл более позднюю подписку.
    assert updated.end_date == task.end_date
