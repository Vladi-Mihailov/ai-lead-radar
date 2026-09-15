"""Telethon-клавиатуры Turkey-бота.

Два РАЗНЫХ вида клавиатур (см. design report про 📊 Статистика/🚗 Мои авто):
  - main_menu_keyboard() — персистентная reply-клавиатура (Button.text),
    та же механика, что и reader/public_bot/keyboards.py::
    main_menu_keyboard — GARAGE_LABEL видят ВСЕ, STATISTICS_LABEL только
    trusted-менеджеры (см. conversation.py::is_trusted, config
    settings.public_bot.trusted_operator_user_ids — та же настройка, что
    и у @ProtocolGEbot, не дублируется);
  - cancel_keyboard()/garage_keyboard() — inline-клавиатуры, прикреплённые
    к КОНКРЕТНОМУ сообщению.

Garage callback_data несёт car_id (см. design report: "Garage callbacks
must resolve the selected car server-side and verify ownership before
starting a check") — car_id сам по себе ПУБЛИЧЕН и НЕ является
доказательством владения: ConversationController.handle_garage_check()
ВСЕГДА заново проверяет владение через TurkeyUserCarsRepository.
get_owned_car(car_id, telegram_user_id=event.sender_id) перед тем, как
что-либо сделать — тот же принцип, что и у reader/public_bot/keyboards.py
(subscription_id тоже публичен, авторизация — только server-side).

CANCEL_CALLBACK_DATA — фиксированная константа без какого-либо
пользовательского/сессионного значения внутри — отменить можно только
СВОЙ собственный текущий диалог, а какой именно chat_id отменять,
определяется ИСКЛЮЧИТЕЛЬНО event.chat_id самого нажатия, никогда из тела
callback_data (тот же принцип "identity — только из события")."""

from telethon import Button

from reader.turkey_bot.models import TurkeyUserCar
from reader.turkey_bot.texts import (
    CANCEL_BUTTON_LABEL,
    GARAGE_CHECK_BUTTON_LABEL,
    GARAGE_LABEL,
    STATISTICS_LABEL,
)

CANCEL_CALLBACK_DATA = b"turkeycancel"

_GARAGE_CHECK_PREFIX = b"turkeygaragecheck:"


def cancel_keyboard() -> list[list[Button]]:
    return [[Button.inline(CANCEL_BUTTON_LABEL, CANCEL_CALLBACK_DATA)]]


def main_menu_keyboard(*, is_trusted: bool = False) -> list[list[Button]]:
    """is_trusted — управляет ТОЛЬКО STATISTICS_LABEL (см. design report:
    "A normal user must not see the button and must not be able to
    retrieve statistics by manually sending the text" — второе
    обеспечивает ConversationController, не эта функция; здесь — только
    видимость самой кнопки). GARAGE_LABEL виден всем всегда."""
    rows = [[Button.text(GARAGE_LABEL, resize=True)]]
    if is_trusted:
        rows.append([Button.text(STATISTICS_LABEL, resize=True)])
    return rows


def encode_garage_check_callback(car_id: int) -> bytes:
    return _GARAGE_CHECK_PREFIX + str(car_id).encode("ascii")


def decode_garage_check_callback(data: bytes | None) -> int | None:
    if not data or not data.startswith(_GARAGE_CHECK_PREFIX):
        return None
    try:
        return int(data[len(_GARAGE_CHECK_PREFIX):])
    except ValueError:
        return None


def garage_keyboard(cars: list[TurkeyUserCar]) -> list[list[Button]]:
    """[НОМЕР] [🔎 Проверить] на строку (см. design report про желаемый
    UI) — ОБЕ кнопки ведут на ОДИН и тот же callback (car_id -> начать
    проверку этого сохранённого номера, см. design report: "Clicking the
    plate itself may either be a no-op/info action or start the same
    check — choose the cleanest existing callback architecture") - два
    отдельных обработчика для визуально одинакового действия только
    добавили бы код без пользы."""
    return [
        [
            Button.inline(car.car_number, encode_garage_check_callback(car.id)),
            Button.inline(GARAGE_CHECK_BUTTON_LABEL, encode_garage_check_callback(car.id)),
        ]
        for car in cars
    ]
