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
    CHECK_TOLLS_AVRASYA_LABEL,
    CHECK_TOLLS_KGM_LABEL,
    CHECK_TOLLS_LABEL,
    GARAGE_LABEL,
    GEORGIAN_BOT_LINK_LABEL,
    GEORGIAN_BOT_URL,
    HELP_AVRASYA_LABEL,
    HELP_BACK_LABEL,
    HELP_GIB_LABEL,
    HELP_LABEL,
    HELP_PAYMENT_LABEL,
    HELP_TERMS_LABEL,
    STATISTICS_LABEL,
)

CANCEL_CALLBACK_DATA = b"turkeycancel"
# Фиксированная константа (тот же принцип, что и CANCEL_CALLBACK_DATA выше) —
# "⬅️ Назад" из Help-меню в обычное главное меню: без какого-либо
# пользовательского/сессионного значения внутри.
HELP_BACK_TO_MAIN_CALLBACK_DATA = b"turkeyhelpmainmenu"

_GARAGE_CHECK_PREFIX = b"turkeygaragecheck:"
_HELP_CALLBACK_PREFIX = b"turkeyhelp:"
# См. design report "Реализация KGM provider" п.10 — выбор конкретного
# провайдера ПОСЛЕ нажатия CHECK_TOLLS_LABEL, ДО ввода номера (см.
# toll_provider_keyboard/decode_toll_provider_callback ниже).
_TOLL_PROVIDER_CALLBACK_PREFIX = b"turkeytollprovider:"

# "menu" — служебное значение для "⬅️ Назад" ИЗ раздела Help ОБРАТНО в
# Help-меню (см. help_section_keyboard) — не путать с
# HELP_BACK_TO_MAIN_CALLBACK_DATA (Help-меню -> обычное главное меню).
_HELP_SECTIONS = frozenset({"menu", "terms", "gib", "avrasya", "payment"})

# Провайдеры, допустимые в garage callback_data (см. design report Stage
# 1: "design the internal architecture so additional toll-road providers
# can be added later" — decode_garage_check_callback уже готов принять
# третий/четвёртый ключ здесь без изменения формата данных) — "kgm"
# добавлен по этому же заделу (см. design report "Реализация KGM
# provider").
_KNOWN_PROVIDERS = frozenset({"gib", "avrasya", "kgm"})
# Допустимые значения в _TOLL_PROVIDER_CALLBACK_PREFIX — ТОЛЬКО два
# провайдера платных дорог (см. toll_provider_keyboard ниже) — GIB сюда
# не входит (штрафы — отдельная кнопка CHECK_FINES_LABEL, без подменю).
_TOLL_PROVIDERS = frozenset({"avrasya", "kgm"})


def cancel_keyboard() -> list[list[Button]]:
    return [[Button.inline(CANCEL_BUTTON_LABEL, CANCEL_CALLBACK_DATA)]]


def main_menu_keyboard(*, is_trusted: bool = False) -> list[list[Button]]:
    """is_trusted — управляет ТОЛЬКО STATISTICS_LABEL (см. design report:
    "A normal user must not see the button and must not be able to
    retrieve statistics by manually sending the text" — второе
    обеспечивает ConversationController, не эта функция; здесь — только
    видимость самой кнопки). CHECK_FINES_LABEL/CHECK_TOLLS_LABEL/
    GARAGE_LABEL видны всем всегда (см. design report Stage 2B).

    НЕ содержит переход в Georgian-бот — см. georgian_bot_link_keyboard()
    ниже и её докстрок про то, почему это должно быть отдельное сообщение,
    а не часть этой разметки."""
    rows = [
        [Button.text(CHECK_FINES_LABEL, resize=True)],
        [Button.text(CHECK_TOLLS_LABEL, resize=True)],
        [Button.text(GARAGE_LABEL, resize=True)],
    ]
    if is_trusted:
        rows.append([Button.text(STATISTICS_LABEL, resize=True)])
    # ℹ️ Справка видна ВСЕМ (см. design report: "Do not change permissions
    # or visibility rules for existing buttons" — только добавляется,
    # порядок последним совпадает с порядком в самой задаче).
    rows.append([Button.text(HELP_LABEL, resize=True)])
    return rows


def georgian_bot_link_keyboard() -> list[list[Button]]:
    """Inline URL-кнопка перехода в Georgian-бот (см. design report
    "связать Georgian bot и Turkey bot взаимными кнопками перехода") —
    ОТДЕЛЬНАЯ разметка, не часть main_menu_keyboard() (Telethon/Telegram
    не позволяет смешивать reply-кнопки, Button.text, с inline URL-кнопкой
    в ОДНОЙ разметке — client.build_reply_markup поднимает ValueError(
    'You cannot mix inline with normal buttons') — переход отправляется
    отдельным сообщением сразу после текста главного меню, см.
    reader/turkey_bot/handlers.py::_send_reply). Button.url (не callback) —
    открывает t.me/ProtocolGEbot напрямую, ничего не кодирует и не
    проверяет на стороне бота."""
    return [[Button.url(GEORGIAN_BOT_LINK_LABEL, GEORGIAN_BOT_URL)]]


def toll_provider_keyboard() -> list[list[Button]]:
    """Показывается ПОСЛЕ нажатия CHECK_TOLLS_LABEL (см. design report
    "Реализация KGM provider" п.10: "🚇 Avrasya Tüneli" / "🛣 Все дороги и
    мосты (KGM)") — armит конкретного провайдера ТОЛЬКО после этого
    выбора (см. conversation.py::handle_toll_provider_callback), Avrasya
    не удалена и не изменена, просто больше не выбирается неявно одним
    нажатием CHECK_TOLLS_LABEL."""
    return [
        [Button.inline(CHECK_TOLLS_AVRASYA_LABEL, encode_toll_provider_callback("avrasya"))],
        [Button.inline(CHECK_TOLLS_KGM_LABEL, encode_toll_provider_callback("kgm"))],
    ]


def encode_toll_provider_callback(provider: str) -> bytes:
    return _TOLL_PROVIDER_CALLBACK_PREFIX + provider.encode("ascii")


def decode_toll_provider_callback(data: bytes | None) -> str | None:
    """None — данные не относятся к этому callback'у вовсе, или значение
    не входит в _TOLL_PROVIDERS (тот же generic "неизвестная кнопка"
    fallback, см. handlers.py, что и у decode_garage_check_callback/
    decode_help_callback)."""
    if not data or not data.startswith(_TOLL_PROVIDER_CALLBACK_PREFIX):
        return None
    provider = data[len(_TOLL_PROVIDER_CALLBACK_PREFIX):].decode("ascii", errors="strict")
    if provider not in _TOLL_PROVIDERS:
        return None
    return provider


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


def encode_help_callback(section: str) -> bytes:
    return _HELP_CALLBACK_PREFIX + section.encode("ascii")


def decode_help_callback(data: bytes | None) -> str | None:
    """None — данные не относятся к Help-callback'ам вовсе (тот же generic
    "неизвестная кнопка" ответ для неизвестного section, см. модуль
    docstring про garage). Единственные допустимые значения —
    _HELP_SECTIONS (см. выше)."""
    if not data or not data.startswith(_HELP_CALLBACK_PREFIX):
        return None
    section = data[len(_HELP_CALLBACK_PREFIX):].decode("ascii", errors="strict")
    if section not in _HELP_SECTIONS:
        return None
    return section


def help_menu_keyboard() -> list[list[Button]]:
    """Верхний уровень ℹ️ Справка (см. design report) — 4 раздела + "⬅️
    Назад" в обычное главное меню (см. HELP_BACK_TO_MAIN_CALLBACK_DATA)."""
    return [
        [Button.inline(HELP_TERMS_LABEL, encode_help_callback("terms"))],
        [Button.inline(HELP_GIB_LABEL, encode_help_callback("gib"))],
        [Button.inline(HELP_AVRASYA_LABEL, encode_help_callback("avrasya"))],
        [Button.inline(HELP_PAYMENT_LABEL, encode_help_callback("payment"))],
        [Button.inline(HELP_BACK_LABEL, HELP_BACK_TO_MAIN_CALLBACK_DATA)],
    ]


def help_section_keyboard() -> list[list[Button]]:
    """Один из 4 разделов Help — только "⬅️ Назад" В Help-меню (не в
    главное меню, см. design report: "⬅️ Назад returns to the normal main
    menu" относится ТОЛЬКО к самому Help-меню, разделы возвращают в
    Help-меню, откуда уже есть свой "⬅️ Назад" в главное меню)."""
    return [[Button.inline(HELP_BACK_LABEL, encode_help_callback("menu"))]]


def garage_keyboard(cars: list[TurkeyUserCar]) -> list[list[Button]]:
    """[НОМЕР] [🚔 Проверить штрафы] [🚇 Avrasya Tüneli] [🛣 KGM] на
    строку (см. design report Stage 2B: "Saved cars must show both
    actions", теперь — все три; см. design report "Реализация KGM
    provider" п.10 — KGM добавлен как ТРЕТЬЯ кнопка, Avrasya не удалена).
    Клик по самому номеру запускает ТУ ЖЕ проверку, что и явная кнопка
    "🚔 Проверить штрафы" (см. design report Stage 1: "Clicking the plate
    itself may either be a no-op/info action or start the same check —
    choose the cleanest existing callback architecture") — сохраняет
    прежнее поведение одиночной кнопки-номера, не вводя отдельный no-op
    обработчик."""
    rows = []
    for car in cars:
        gib_callback = encode_garage_check_callback("gib", car.id)
        rows.append([
            Button.inline(car.car_number, gib_callback),
            Button.inline(CHECK_FINES_LABEL, gib_callback),
            Button.inline(CHECK_TOLLS_AVRASYA_LABEL, encode_garage_check_callback("avrasya", car.id)),
            Button.inline(CHECK_TOLLS_KGM_LABEL, encode_garage_check_callback("kgm", car.id)),
        ])
    return rows
