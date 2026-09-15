"""Telethon-клавиатуры Turkey-бота.

Два РАЗНЫХ вида клавиатур (см. design report про 📊 Статистика/🚗 Мои авто):
  - main_menu_keyboard() — персистентная reply-клавиатура (Button.text),
    та же механика, что и reader/public_bot/keyboards.py::
    main_menu_keyboard — GARAGE_LABEL/CHECK_FINES_LABEL/CHECK_TOLLS_LABEL
    видят ВСЕ, STATISTICS_LABEL только trusted-менеджеры (см.
    conversation.py::is_trusted, config settings.public_bot.
    trusted_operator_user_ids — та же настройка, что и у @ProtocolGEbot,
    не дублируется);
  - cancel_keyboard()/garage_keyboard() — inline-клавиатуры, прикреплённые
    к КОНКРЕТНОМУ сообщению.

Garage callback_data несёт (provider, car_id) (см. design report Stage
2B: "Garage callbacks must resolve the selected car server-side and
verify ownership before starting a check") — car_id сам по себе ПУБЛИЧЕН
и НЕ является доказательством владения: ConversationController.
handle_garage_check() ВСЕГДА заново проверяет владение через
TurkeyUserCarsRepository.get_owned_car(car_id, telegram_user_id=
event.sender_id) перед тем, как что-либо сделать — тот же принцип, что и
у reader/public_bot/keyboards.py (subscription_id тоже публичен,
авторизация — только server-side). provider ('gib'/'avrasya') сам по себе
тоже ничего не авторизует — это только "какую из двух проверок начать",
владение проверяется одинаково для обоих (см. design report: "trusted
status... does not grant managers access to another user's Turkey garage
or checks" — распространяется и на выбор провайдера).

CANCEL_CALLBACK_DATA — фиксированная константа без какого-либо
пользовательского/сессионного значения внутри — отменить можно только
СВОЙ собственный текущий диалог, а какой именно chat_id отменять,
определяется ИСКЛЮЧИТЕЛЬНО event.chat_id самого нажатия, никогда из тела
callback_data (тот же принцип "identity — только из события")."""

from telethon import Button

from reader.turkey_bot.models import TurkeyUserCar
from reader.turkey_bot.texts import (
    CANCEL_BUTTON_LABEL,
    CHECK_FINES_LABEL,
    CHECK_TOLLS_LABEL,
    GARAGE_LABEL,
    STATISTICS_LABEL,
)

CANCEL_CALLBACK_DATA = b"turkeycancel"

_GARAGE_CHECK_PREFIX = b"turkeygaragecheck:"

# Провайдеры, допустимые в garage callback_data (см. design report Stage
# 1: "design the internal architecture so additional toll-road providers
# can be added later" — decode_garage_check_callback уже готов принять
# третий/четвёртый ключ здесь без изменения формата данных).
_KNOWN_PROVIDERS = frozenset({"gib", "avrasya"})


def cancel_keyboard() -> list[list[Button]]:
    return [[Button.inline(CANCEL_BUTTON_LABEL, CANCEL_CALLBACK_DATA)]]


def main_menu_keyboard(*, is_trusted: bool = False) -> list[list[Button]]:
    """is_trusted — управляет ТОЛЬКО STATISTICS_LABEL (см. design report:
    "A normal user must not see the button and must not be able to
    retrieve statistics by manually sending the text" — второе
    обеспечивает ConversationController, не эта функция; здесь — только
    видимость самой кнопки). CHECK_FINES_LABEL/CHECK_TOLLS_LABEL/
    GARAGE_LABEL видны всем всегда (см. design report Stage 2B)."""
    rows = [
        [Button.text(CHECK_FINES_LABEL, resize=True)],
        [Button.text(CHECK_TOLLS_LABEL, resize=True)],
        [Button.text(GARAGE_LABEL, resize=True)],
    ]
    if is_trusted:
        rows.append([Button.text(STATISTICS_LABEL, resize=True)])
    return rows


def encode_garage_check_callback(provider: str, car_id: int) -> bytes:
    return _GARAGE_CHECK_PREFIX + f"{provider}:{car_id}".encode("ascii")


def decode_garage_check_callback(data: bytes | None) -> tuple[str, int] | None:
    """None — данные не относятся к garage-callback'ам вовсе (см. модуль
    docstring: тот же generic "неизвестная кнопка" ответ, что и для
    чужого/несуществующего car_id, см. handlers.py). provider здесь ТОЛЬКО
    выбирает ветку (GIB/Avrasya) — само по себе НЕ авторизует ничего (см.
    модуль docstring)."""
    if not data or not data.startswith(_GARAGE_CHECK_PREFIX):
        return None
    remainder = data[len(_GARAGE_CHECK_PREFIX):].decode("ascii", errors="strict")
    provider, _, car_id_text = remainder.partition(":")
    if not car_id_text or provider not in _KNOWN_PROVIDERS:
        return None
    try:
        return provider, int(car_id_text)
    except ValueError:
        return None


def garage_keyboard(cars: list[TurkeyUserCar]) -> list[list[Button]]:
    """[НОМЕР] [🚔 Проверить штрафы] [🛣 Проверить платные дороги] на
    строку (см. design report Stage 2B: "Saved cars must show both
    actions"). Клик по самому номеру запускает ТУ ЖЕ проверку, что и
    явная кнопка "🚔 Проверить штрафы" (см. design report Stage 1:
    "Clicking the plate itself may either be a no-op/info action or start
    the same check — choose the cleanest existing callback architecture")
    — сохраняет прежнее поведение одиночной кнопки-номера, не вводя
    отдельный no-op обработчик."""
    rows = []
    for car in cars:
        gib_callback = encode_garage_check_callback("gib", car.id)
        rows.append([
            Button.inline(car.car_number, gib_callback),
            Button.inline(CHECK_FINES_LABEL, gib_callback),
            Button.inline(CHECK_TOLLS_LABEL, encode_garage_check_callback("avrasya", car.id)),
        ])
    return rows
