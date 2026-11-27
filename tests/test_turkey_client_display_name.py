"""👤 Данные клиента: имя без Telegram-логина в Turkey-боте (только менеджеру).

Имя — не user_id и не доступ: оно ничего не открывает клиенту и не создаёт
уведомлений. Настоящие репозитории поверх SQLite (tmp_path).
"""

import sqlite3
from datetime import timedelta
from decimal import Decimal

import pytest

from reader.turkey_bot import texts
from reader.turkey_bot.keyboards import client_link_screen_keyboard, decode_client_link_callback
from reader.turkey_bot.unified.models import ProviderStatus
from reader.turkey_bot.user_cars_repository import TurkeyUserCarsRepository
from reader.turkey_bot.validation import normalize_client_name
from tests.test_turkey_client_linking import _CLIENT, _MANAGER, _PLATE, _T0, _Fixture, _result


@pytest.fixture
def fx(tmp_path):
    fixture = _Fixture(tmp_path)
    yield fixture
    fixture.close()


@pytest.mark.parametrize("raw, expected", [
    ("Иван Иванов", "Иван Иванов"),
    ("Giorgi123", "Giorgi123"),
    ("გიორგი", "გიორგი"),
    ("Ahmet 007", "Ahmet 007"),
    ("Клиент 12345", "Клиент 12345"),
    ("Александр-2", "Александр-2"),
    ("  Ivan​ \t\n Petrov‮ ", "Ivan Petrov"),  # невидимые/управляющие — удалены, пробелы схлопнуты
    ("O’Neil (Jr.)", "O’Neil (Jr.)"),
])
def test_names_of_any_script_are_kept_as_typed(raw, expected):
    assert normalize_client_name(raw) == (expected, None)


@pytest.mark.parametrize("raw, error", [
    ("", "empty"), ("   ", "empty"), ("​‍", "empty"),
    ("x" * 65, "too_long"),
    ("😀", "bad_chars"), ("<b>Ivan</b>", "bad_chars"), ("**Ivan**", "bad_chars"), ("---", "bad_chars"),
])
def test_bad_names_are_refused(raw, error):
    assert normalize_client_name(raw) == (None, error)


def test_64_characters_is_the_limit():
    assert normalize_client_name("я" * 64) == ("я" * 64, None)


async def _set_name(fx, car, raw):
    prompt = fx.action("name", car)
    assert prompt.show_cancel_button
    return await fx.controller.handle_text(raw, chat_id=_MANAGER, telegram_user_id=_MANAGER)


async def test_card_with_a_name_and_without_telegram(fx):
    car, _ = await fx.manager_car()
    saved = await _set_name(fx, car, "Иван Иванов")
    assert saved.text.splitlines()[:5] == [
        texts.CLIENT_NAME_SAVED_TEXT, "",
        f"🚗 Автомобиль: {texts.format_plate_for_telegram(_PLATE)}",
        "👤 Клиент: Иван Иванов",
        "🔗 Telegram: не привязан",
    ]
    assert saved.client_link_screen[4] is True  # есть имя -> кнопка «🗑 Удалить имя»
    detail = fx.controller.handle_manager_car_open(car.id, 0, telegram_user_id=_MANAGER)
    assert detail.text.splitlines()[-2:] == ["👤 Клиент: Иван Иванов", "🔗 Telegram: не привязан"]


async def test_name_alone_grants_nothing_and_sends_nothing(fx):
    car, _ = await fx.manager_car()
    saved = await _set_name(fx, car, "client_one")  # совпадает с username клиента — всё равно только текст
    assert saved.outgoing_messages == ()
    assert fx.garage.get_owned_car_by_number(_CLIENT, _PLATE) is None
    assert fx.links.active_for_anchor(_MANAGER, _PLATE) is None and fx.links.pending_for_anchor(_MANAGER, _PLATE) is None
    fx.check.results.append(_result(ProviderStatus.HAS_DEBT, Decimal(800), at=_T0 + timedelta(hours=1)))
    await fx.monitoring.run_scheduled_batch("21:00")
    assert [chat for chat, _ in fx.notifier.sent] == [_MANAGER]
    assert fx.check.calls == [_PLATE]


async def test_later_telegram_link_keeps_the_name_and_history(fx):
    car, _ = await fx.manager_car()
    await _set_name(fx, car, "Ahmet 007")
    runs_before = fx.runs.count_total()
    await fx.link_client(car)
    detail = fx.controller.handle_manager_car_open(car.id, 0, telegram_user_id=_MANAGER)
    assert detail.text.splitlines()[-2:] == [
        "👤 Клиент: Ahmet 007", "🔗 Telegram: @client_one · привязан 20.11.2026",
    ]
    assert fx.runs.count_total() == runs_before
    # Отвязка Telegram имя не трогает.
    fx.action("unlink_yes", car)
    assert fx.garage.get_client_display_name(car.id) == "Ahmet 007"


async def test_manager_can_change_and_remove_the_name(fx):
    car, _ = await fx.manager_car()
    await _set_name(fx, car, "Клиент 12345")
    await _set_name(fx, car, "Александр-2")
    assert fx.garage.get_client_display_name(car.id) == "Александр-2"
    cleared = fx.action("name_clear", car)
    assert cleared.text.startswith(texts.CLIENT_NAME_CLEARED_TEXT) and "👤 Клиент: не указан" in cleared.text
    assert fx.garage.get_client_display_name(car.id) is None
    assert cleared.client_link_screen[4] is False


async def test_invalid_name_keeps_asking_and_saves_nothing(fx):
    car, _ = await fx.manager_car()
    reply = await _set_name(fx, car, "😀😀")
    assert reply.text == texts.CLIENT_NAME_ERROR_TEXTS["bad_chars"] and reply.show_cancel_button
    assert fx.garage.get_client_display_name(car.id) is None
    retry = await fx.controller.handle_text("Giorgi123", chat_id=_MANAGER, telegram_user_id=_MANAGER)
    assert "👤 Клиент: Giorgi123" in retry.text


async def test_name_is_escaped_for_telegram_markdown(fx):
    car, _ = await fx.manager_car()
    fx.garage.set_client_display_name(car.id, "Ivan__Petrov")
    screen = fx.action("screen", car)
    assert "👤 Клиент: Ivan__Petrov" not in screen.text  # разделитель курсива разбит
    assert "Ivan" in screen.text and "Petrov" in screen.text


async def test_name_actions_are_manager_only(fx):
    car, _ = await fx.manager_car()
    assert fx.action("name", car, user=_CLIENT) is None
    assert fx.action("name_clear", car, user=_CLIENT) is None
    # Клиент в состоянии ввода имени оказаться не может; даже если бы — trusted перепроверяется.
    fx.states.set(_CLIENT, telegram_user_id=_CLIENT, step="awaiting_client_name", payload={"car_id": car.id})
    reply = await fx.controller.handle_text("Хакер", chat_id=_CLIENT, telegram_user_id=_CLIENT)
    assert reply.text == texts.UNKNOWN_BUTTON_TEXT
    assert fx.garage.get_client_display_name(car.id) is None


def test_name_buttons_and_callbacks():
    rows = client_link_screen_keyboard(5, 0, state="none", choosing=True, has_name=True)
    labels = [button.text for row in rows for button in row]
    assert texts.CLIENT_NAME_EDIT_LABEL in labels and texts.CLIENT_NAME_CLEAR_LABEL in labels
    data = {button.text: button.data for row in rows for button in row}
    assert decode_client_link_callback(data[texts.CLIENT_NAME_EDIT_LABEL]) == ("name", 5, 0, None)
    assert decode_client_link_callback(data[texts.CLIENT_NAME_CLEAR_LABEL]) == ("name_clear", 5, 0, None)
    rows = client_link_screen_keyboard(5, 0, state="none", choosing=True, has_name=False)
    assert texts.CLIENT_NAME_CLEAR_LABEL not in [button.text for row in rows for button in row]


def test_column_migration_is_additive_on_an_existing_database(tmp_path):
    db = tmp_path / "users.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE turkey_bot_user_cars (id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_user_id INTEGER NOT NULL, "
        "car_number TEXT NOT NULL, created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "last_checked_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE (telegram_user_id, car_number))"
    )
    conn.execute("INSERT INTO turkey_bot_user_cars (telegram_user_id, car_number) VALUES (1, 'OLD1')")
    conn.commit()
    conn.close()
    repo = TurkeyUserCarsRepository(db)
    try:
        car = repo.get_owned_car_by_number(1, "OLD1")
        assert car is not None and repo.get_client_display_name(car.id) is None
        assert repo.set_client_display_name(car.id, "Иван") and repo.get_client_display_name(car.id) == "Иван"
    finally:
        repo.close()
    TurkeyUserCarsRepository(db).close()  # повторное открытие не падает
