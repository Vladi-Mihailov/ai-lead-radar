"""
Тесты reader/turkey_bot/user_cars_repository.py::TurkeyUserCarsRepository —
"гараж" (см. design report: НЕ мониторинг, только чтобы не вводить номер
заново).
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.turkey_bot.user_cars_repository import (
    TurkeyUserCarsRepository,  # noqa: E402
)


def _repo() -> TurkeyUserCarsRepository:
    return TurkeyUserCarsRepository(":memory:")


def test_record_successful_check_creates_a_car():
    repo = _repo()

    repo.record_successful_check(telegram_user_id=1, car_number="34ABC123")

    cars = repo.list_cars(1)
    assert len(cars) == 1
    assert cars[0].car_number == "34ABC123"
    assert cars[0].telegram_user_id == 1


def test_duplicate_plate_does_not_duplicate_car():
    repo = _repo()

    repo.record_successful_check(telegram_user_id=1, car_number="34ABC123")
    repo.record_successful_check(telegram_user_id=1, car_number="34ABC123")

    cars = repo.list_cars(1)
    assert len(cars) == 1


def test_duplicate_check_updates_last_checked_at_not_created_at():
    repo = _repo()
    repo.record_successful_check(telegram_user_id=1, car_number="34ABC123")
    first = repo.list_cars(1)[0]

    repo.record_successful_check(telegram_user_id=1, car_number="34ABC123")
    second = repo.list_cars(1)[0]

    assert second.created_at == first.created_at
    assert second.last_checked_at >= first.last_checked_at


def test_same_plate_different_users_are_independent():
    repo = _repo()

    repo.record_successful_check(telegram_user_id=1, car_number="34ABC123")
    repo.record_successful_check(telegram_user_id=2, car_number="34ABC123")

    assert len(repo.list_cars(1)) == 1
    assert len(repo.list_cars(2)) == 1
    assert repo.list_cars(1)[0].telegram_user_id == 1
    assert repo.list_cars(2)[0].telegram_user_id == 2


def test_users_see_only_their_own_cars():
    repo = _repo()
    repo.record_successful_check(telegram_user_id=1, car_number="34ABC123")
    repo.record_successful_check(telegram_user_id=1, car_number="06XYZ999")
    repo.record_successful_check(telegram_user_id=2, car_number="AA111111")

    user1_plates = {c.car_number for c in repo.list_cars(1)}
    user2_plates = {c.car_number for c in repo.list_cars(2)}

    assert user1_plates == {"34ABC123", "06XYZ999"}
    assert user2_plates == {"AA111111"}


def test_list_cars_for_unknown_user_is_empty():
    repo = _repo()

    assert repo.list_cars(999) == []


def test_get_owned_car_returns_car_for_correct_owner():
    repo = _repo()
    repo.record_successful_check(telegram_user_id=1, car_number="34ABC123")
    car_id = repo.list_cars(1)[0].id

    car = repo.get_owned_car(car_id, telegram_user_id=1)

    assert car is not None
    assert car.car_number == "34ABC123"


def test_get_owned_car_returns_none_for_wrong_owner():
    """Явное требование задачи: "A normal user must not be able to
    inspect another user's garage by forging callback data"."""
    repo = _repo()
    repo.record_successful_check(telegram_user_id=1, car_number="34ABC123")
    car_id = repo.list_cars(1)[0].id

    car = repo.get_owned_car(car_id, telegram_user_id=2)

    assert car is None


def test_get_owned_car_returns_none_for_nonexistent_id():
    repo = _repo()

    assert repo.get_owned_car(999999, telegram_user_id=1) is None
