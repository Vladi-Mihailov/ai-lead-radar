"""
Тесты reader/turkey_bot/user_cars_repository.py::TurkeyUserCarsRepository —
"📋 Мои авто" (см. задачу "Перенос Unified Turkey функционала в
production" — автомобиль добавляется СРАЗУ после ввода номера, успешная
проверка больше не требуется; last_overall_status/last_total_amount —
additive-колонки, кэш последнего unified-check).
"""

import sys
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.turkey_bot.user_cars_repository import (
    TurkeyUserCarsRepository,
)


def _repo() -> TurkeyUserCarsRepository:
    return TurkeyUserCarsRepository(":memory:")


def test_add_car_creates_a_car():
    repo = _repo()

    car = repo.add_car(telegram_user_id=1, car_number="34ABC123")

    cars = repo.list_cars(1)
    assert len(cars) == 1
    assert cars[0].car_number == "34ABC123"
    assert cars[0].telegram_user_id == 1
    assert car.last_overall_status is None
    assert car.last_total_amount is None


def test_duplicate_plate_does_not_duplicate_car():
    repo = _repo()

    repo.add_car(telegram_user_id=1, car_number="34ABC123")
    repo.add_car(telegram_user_id=1, car_number="34ABC123")

    cars = repo.list_cars(1)
    assert len(cars) == 1


def test_duplicate_add_preserves_existing_check_cache():
    """Повторное "добавление" уже существующего, уже проверенного
    автомобиля не должно стирать last_overall_status/last_total_amount
    (см. reader/turkey_bot/user_cars_repository.py::add_car — ON CONFLICT
    DO NOTHING)."""
    repo = _repo()
    repo.add_car(telegram_user_id=1, car_number="34ABC123")
    repo.update_last_result(
        telegram_user_id=1, car_number="34ABC123",
        overall_status="has_debt", total_amount=Decimal(500),
    )

    repo.add_car(telegram_user_id=1, car_number="34ABC123")

    car = repo.list_cars(1)[0]
    assert car.last_overall_status == "has_debt"
    assert car.last_total_amount == Decimal(500)


def test_update_last_result_updates_cache_and_last_checked_at():
    repo = _repo()
    repo.add_car(telegram_user_id=1, car_number="34ABC123")
    first = repo.list_cars(1)[0]

    repo.update_last_result(
        telegram_user_id=1, car_number="34ABC123",
        overall_status="no_debt", total_amount=Decimal(0),
    )

    second = repo.list_cars(1)[0]
    assert second.created_at == first.created_at
    assert second.last_checked_at >= first.last_checked_at
    assert second.last_overall_status == "no_debt"
    assert second.last_total_amount == Decimal(0)


def test_same_plate_different_users_are_independent():
    repo = _repo()

    repo.add_car(telegram_user_id=1, car_number="34ABC123")
    repo.add_car(telegram_user_id=2, car_number="34ABC123")

    assert len(repo.list_cars(1)) == 1
    assert len(repo.list_cars(2)) == 1
    assert repo.list_cars(1)[0].telegram_user_id == 1
    assert repo.list_cars(2)[0].telegram_user_id == 2


def test_users_see_only_their_own_cars():
    repo = _repo()
    repo.add_car(telegram_user_id=1, car_number="34ABC123")
    repo.add_car(telegram_user_id=1, car_number="06XYZ999")
    repo.add_car(telegram_user_id=2, car_number="AA111111")

    user1_plates = {c.car_number for c in repo.list_cars(1)}
    user2_plates = {c.car_number for c in repo.list_cars(2)}

    assert user1_plates == {"34ABC123", "06XYZ999"}
    assert user2_plates == {"AA111111"}


def test_list_cars_for_unknown_user_is_empty():
    repo = _repo()

    assert repo.list_cars(999) == []


def test_get_owned_car_returns_car_for_correct_owner():
    repo = _repo()
    repo.add_car(telegram_user_id=1, car_number="34ABC123")
    car_id = repo.list_cars(1)[0].id

    car = repo.get_owned_car(car_id, telegram_user_id=1)

    assert car is not None
    assert car.car_number == "34ABC123"


def test_get_owned_car_returns_none_for_wrong_owner():
    """Явное требование задачи: "A normal user must not be able to
    inspect another user's garage by forging callback data"."""
    repo = _repo()
    repo.add_car(telegram_user_id=1, car_number="34ABC123")
    car_id = repo.list_cars(1)[0].id

    car = repo.get_owned_car(car_id, telegram_user_id=2)

    assert car is None


def test_get_owned_car_returns_none_for_nonexistent_id():
    repo = _repo()

    assert repo.get_owned_car(999999, telegram_user_id=1) is None


def test_get_owned_car_by_number_returns_car_for_correct_owner():
    repo = _repo()
    repo.add_car(telegram_user_id=1, car_number="34ABC123")

    car = repo.get_owned_car_by_number(1, "34ABC123")

    assert car is not None
    assert car.telegram_user_id == 1


def test_get_owned_car_by_number_returns_none_for_wrong_owner():
    repo = _repo()
    repo.add_car(telegram_user_id=1, car_number="34ABC123")

    assert repo.get_owned_car_by_number(2, "34ABC123") is None


def test_delete_car_removes_only_that_owners_row():
    repo = _repo()
    repo.add_car(telegram_user_id=1, car_number="34ABC123")
    repo.add_car(telegram_user_id=2, car_number="34ABC123")
    car_id = repo.list_cars(1)[0].id

    deleted = repo.delete_car(car_id, telegram_user_id=1)

    assert deleted is True
    assert repo.list_cars(1) == []
    assert len(repo.list_cars(2)) == 1


def test_delete_car_returns_false_for_wrong_owner():
    repo = _repo()
    repo.add_car(telegram_user_id=1, car_number="34ABC123")
    car_id = repo.list_cars(1)[0].id

    deleted = repo.delete_car(car_id, telegram_user_id=2)

    assert deleted is False
    assert len(repo.list_cars(1)) == 1
