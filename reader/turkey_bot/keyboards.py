"""Telethon-клавиатуры Turkey-бота (см. design report "Перестроить UX
Turkey test bot").

Два РАЗНЫХ вида клавиатур:
  - main_menu_keyboard() — персистентная reply-клавиатура (Button.text);
  - остальные — inline-клавиатуры, прикреплённые к КОНКРЕТНОМУ сообщению.

GİB/Avrasya/KGM — ВНУТРЕННИЕ providers (см.
reader/turkey_bot/unified/check_service.py) — пользователь их больше
не выбирает явно нигде в UI (см. design report решение п.4: старый
provider-by-provider UX убран из основного conversation flow).

car_id в callback_data сам по себе НЕ является доказательством владения —
ConversationController ВСЕГДА заново проверяет владение через
TurkeyUserCarsRepository.get_owned_car(car_id, telegram_user_id=
event.sender_id), тот же принцип, что и раньше."""

from telethon import Button

from reader.turkey_bot.models import TurkeyUserCar
from reader.turkey_bot.texts import (
    ADD_CAR_LABEL,
    BACK_LABEL,
    CANCEL_BUTTON_LABEL,
    CHECK_NOW_LABEL,
    DELETE_CANCEL_LABEL,
    DELETE_CAR_CONFIRM_BUTTON_LABEL,
    DELETE_CAR_LABEL,
    DISABLE_MONITORING_LABEL,
    ENABLE_MONITORING_LABEL,
    GEORGIAN_BOT_LINK_LABEL,
    GEORGIAN_BOT_URL,
    HELP_AVRASYA_LABEL,
    HELP_BACK_LABEL,
    HELP_GIB_LABEL,
    HELP_LABEL,
    HELP_PAYMENT_LABEL,
    HELP_TERMS_LABEL,
    HISTORY_LABEL,
    MY_CARS_LABEL,
    STATISTICS_LABEL,
    STOP_MONITORING_LABEL,
    format_car_button_label,
    format_manager_car_status_label,
)

CANCEL_CALLBACK_DATA = b"turkeycancel"
HELP_BACK_TO_MAIN_CALLBACK_DATA = b"turkeyhelpmainmenu"
# "⬅️ Назад" из карточки автомобиля -> обновлённый список "🚗 Мои
# автомобили" (см. задачу, п.8: "Не отправлять пользователя в главное
# меню") — фиксированный callback без car_id/page (Turkey не пагинирует
# список, см. design report решение — в отличие от Georgian bot).
BACK_TO_MY_CARS_CALLBACK_DATA = b"turkeymycarsback"

_CAR_OPEN_PREFIX = b"turkeycaropen:"
_CAR_OPEN_BY_PLATE_PREFIX = b"turkeycaropenplate:"
_CAR_ACTION_PREFIX = b"turkeycaraction:"
_HELP_CALLBACK_PREFIX = b"turkeyhelp:"

_HELP_SECTIONS = frozenset({"menu", "terms", "gib", "avrasya", "payment"})
# "delete" заменён двухшаговым подтверждением (см. задачу, п.7: "Если
# Georgian bot использует confirmation перед удалением — повторить этот
# pattern", reader/public_bot/conversation.py::handle_my_car_delete_prompt/
# _confirm/_cancel) — delete_prompt показывает подтверждение, ничего не
# удаляя; delete_confirm удаляет; delete_cancel возвращает к карточке.
_CAR_ACTIONS = frozenset({
    "check", "monitor_on", "monitor_off", "history",
    "delete_prompt", "delete_confirm", "delete_cancel",
})

# manager/trusted-operator "🚗 Мои автомобили" (см. задачу "Реализуем
# manager/trusted 'Мои автомобили' для Turkey bot", референс — Georgian
# bot keyboards.py::trusted_tasks_page_keyboard) — ОТДЕЛЬНЫЙ набор
# callback'ов от self-service (_CAR_OPEN_PREFIX/_CAR_ACTION_PREFIX выше
# остаются НЕТРОНУТЫМИ) — car_id здесь адресует КОНКРЕТНУЮ строку
# turkey_bot_user_cars (её PK), а не car_number: один и тот же номер у
# нескольких пользователей — разные car_id, callback однозначен (см.
# задачу п.6). is_trusted() ВСЕГДА перепроверяется server-side в
# ConversationController.handle_manager_cars_page/
# handle_manager_car_open/handle_manager_car_toggle на КАЖДЫЙ вызов — эти
# callback'и сами по себе НЕ являются доказательством авторизации (тот же
# принцип, что и везде в проекте).
_MANAGER_CARS_PAGE_PREFIX = b"turkeymgrcarspage:"
_MANAGER_CAR_OPEN_PREFIX = b"turkeymgrcaropen:"
_MANAGER_CAR_TOGGLE_PREFIX = b"turkeymgrcartoggle:"


def cancel_keyboard() -> list[list[Button]]:
    return [[Button.inline(CANCEL_BUTTON_LABEL, CANCEL_CALLBACK_DATA)]]


def main_menu_keyboard(*, is_trusted: bool = False) -> list[list[Button]]:
    """Макет обычного пользователя (НЕ менялся, см. задачу "Расположение
    кнопок manager ReplyKeyboard"):
      ROW 1: ADD_CAR_LABEL | MY_CARS_LABEL
      ROW 2: CHECK_NOW_LABEL | GEORGIAN_BOT_LINK_LABEL
      ROW 3: HELP_LABEL

    Макет manager (is_trusted=True):
      ROW 1: ADD_CAR_LABEL | MY_CARS_LABEL
      ROW 2: CHECK_NOW_LABEL | STOP_MONITORING_LABEL
      ROW 3: STATISTICS_LABEL | GEORGIAN_BOT_LINK_LABEL
      ROW 4: HELP_LABEL

    "🇬🇪 Штрафы Грузии" — обычная reply-кнопка (см. georgian_bot_link_keyboard
    докстрок ниже про то, почему она не может сама быть URL-кнопкой)."""
    rows = [[Button.text(ADD_CAR_LABEL, resize=True), Button.text(MY_CARS_LABEL, resize=True)]]
    if is_trusted:
        rows.append([Button.text(CHECK_NOW_LABEL, resize=True), Button.text(STOP_MONITORING_LABEL, resize=True)])
        rows.append([Button.text(STATISTICS_LABEL, resize=True), Button.text(GEORGIAN_BOT_LINK_LABEL, resize=True)])
    else:
        rows.append([Button.text(CHECK_NOW_LABEL, resize=True), Button.text(GEORGIAN_BOT_LINK_LABEL, resize=True)])
    rows.append([Button.text(HELP_LABEL, resize=True)])
    return rows


def georgian_bot_link_keyboard() -> list[list[Button]]:
    """Inline URL-кнопка перехода в Georgian-бот — Telethon/Telegram не
    позволяет смешивать reply-кнопки с inline URL-кнопкой в ОДНОЙ
    разметке, поэтому нажатие "🇬🇪 Штрафы Грузии" отвечает ОТДЕЛЬНЫМ
    сообщением с этой разметкой."""
    return [[Button.url(GEORGIAN_BOT_LINK_LABEL, GEORGIAN_BOT_URL)]]


def encode_car_open_callback(car_id: int) -> bytes:
    return _CAR_OPEN_PREFIX + str(car_id).encode("ascii")


def decode_car_open_callback(data: bytes | None) -> int | None:
    if not data or not data.startswith(_CAR_OPEN_PREFIX):
        return None
    try:
        return int(data[len(_CAR_OPEN_PREFIX):])
    except ValueError:
        return None


def encode_car_open_by_plate_callback(plate: str) -> bytes:
    """Используется кнопкой "🔎 Подробнее" в уведомлениях мониторинга (см.
    reader/turkey_bot/monitoring/notification_texts.py) — car_id там
    неизвестен заранее, плейт сам по себе не секрет, владение всё равно
    перепроверяется server-side (по telegram_user_id события + plate)."""
    return _CAR_OPEN_BY_PLATE_PREFIX + plate.encode("ascii")


def decode_car_open_by_plate_callback(data: bytes | None) -> str | None:
    if not data or not data.startswith(_CAR_OPEN_BY_PLATE_PREFIX):
        return None
    return data[len(_CAR_OPEN_BY_PLATE_PREFIX):].decode("ascii", errors="strict")


def encode_car_action_callback(action: str, car_id: int) -> bytes:
    return _CAR_ACTION_PREFIX + f"{action}:{car_id}".encode("ascii")


def decode_car_action_callback(data: bytes | None) -> tuple[str, int] | None:
    if not data or not data.startswith(_CAR_ACTION_PREFIX):
        return None
    remainder = data[len(_CAR_ACTION_PREFIX):].decode("ascii", errors="strict")
    action, _, car_id_text = remainder.partition(":")
    if action not in _CAR_ACTIONS or not car_id_text:
        return None
    try:
        return action, int(car_id_text)
    except ValueError:
        return None


def encode_manager_cars_page_callback(page: int) -> bytes:
    return _MANAGER_CARS_PAGE_PREFIX + str(page).encode("ascii")


def decode_manager_cars_page_callback(data: bytes | None) -> int | None:
    if not data or not data.startswith(_MANAGER_CARS_PAGE_PREFIX):
        return None
    try:
        return int(data[len(_MANAGER_CARS_PAGE_PREFIX):])
    except ValueError:
        return None


def _encode_car_and_page(prefix: bytes, car_id: int, page: int) -> bytes:
    return prefix + f"{car_id}:{page}".encode("ascii")


def _decode_car_and_page(data: bytes | None, prefix: bytes) -> tuple[int, int] | None:
    if not data or not data.startswith(prefix):
        return None
    remainder = data[len(prefix):].decode("ascii", errors="strict")
    car_id_text, _, page_text = remainder.partition(":")
    if not car_id_text or not page_text:
        return None
    try:
        return int(car_id_text), int(page_text)
    except ValueError:
        return None


def encode_manager_car_open_callback(car_id: int, page: int) -> bytes:
    return _encode_car_and_page(_MANAGER_CAR_OPEN_PREFIX, car_id, page)


def decode_manager_car_open_callback(data: bytes | None) -> tuple[int, int] | None:
    return _decode_car_and_page(data, _MANAGER_CAR_OPEN_PREFIX)


def encode_manager_car_toggle_callback(car_id: int, page: int) -> bytes:
    return _encode_car_and_page(_MANAGER_CAR_TOGGLE_PREFIX, car_id, page)


def decode_manager_car_toggle_callback(data: bytes | None) -> tuple[int, int] | None:
    return _decode_car_and_page(data, _MANAGER_CAR_TOGGLE_PREFIX)


def manager_cars_page_keyboard(
    options: list[tuple[int, str, str, bool]], *, page: int, total_pages: int,
) -> list[list[Button]]:
    """Manager-facing "🚗 Мои автомобили" (см. задачу "унифицировать оба
    интерфейса Georgia/Turkey", референс — Georgian bot
    trusted_tasks_page_keyboard) — options: (car_id, car_number,
    owner_display, monitoring_active). РОВНО ТРИ кнопки на строку —
    [CAR_NUMBER] [@username/—] [STATUS]:

    ЛЕВАЯ — car_number, открывает read-only детали этой строки (см.
    encode_manager_car_open_callback/handle_manager_car_open).

    СРЕДНЯЯ — owner_display, уже готовая строка "@username"/"—" (см.
    ConversationController._format_manager_cars_page_reply/
    texts.format_owner_username_button — НИКОГДА "@None"/"None"/пустая
    кнопка) — чисто информационная, НЕ меняет monitoring/status:
    callback_data — тот же безопасный no-op, что и средний пагинационный
    индикатор ниже (encode_manager_cars_page_callback(page) —
    переоткрывает ту же страницу, не раскрывает дополнительных данных).

    ПРАВАЯ (🟢/⚪) переключает мониторинг НАПРЯМУЮ, без промежуточного
    экрана (см. encode_manager_car_toggle_callback/handle_manager_car_toggle
    — Turkey monitoring бессрочен, в отличие от Georgian bot с периодами,
    поэтому здесь нет аналога "выбора срока").

    car_id — PK строки turkey_bot_user_cars, однозначно адресует
    конкретного владельца даже при повторяющемся car_number (см. задачу
    п.6); owner_display на car/status callback_data не влияет."""
    rows = [
        [
            Button.inline(car_number, encode_manager_car_open_callback(car_id, page)),
            Button.inline(owner_display, encode_manager_cars_page_callback(page)),
            Button.inline(
                format_manager_car_status_label(monitoring_active=monitoring_active),
                encode_manager_car_toggle_callback(car_id, page),
            ),
        ]
        for car_id, car_number, owner_display, monitoring_active in options
    ]
    if total_pages > 1:
        back_page = max(page - 1, 0)
        next_page = min(page + 1, total_pages - 1)
        rows.append([
            Button.inline("◀️ Назад", encode_manager_cars_page_callback(back_page)),
            Button.inline(f"{page + 1} / {total_pages}", encode_manager_cars_page_callback(page)),
            Button.inline("Вперёд ▶️", encode_manager_cars_page_callback(next_page)),
        ])
    return rows


def manager_car_detail_keyboard(page: int) -> list[list[Button]]:
    """"⬅️ Назад" из read-only деталей строки -> та же страница
    manager-списка (см. handle_manager_car_open/_format_manager_cars_page_reply)."""
    return [[Button.inline(BACK_LABEL, encode_manager_cars_page_callback(page))]]


def my_cars_list_keyboard(
    cars: list[TurkeyUserCar], monitoring_active: dict[int, bool],
) -> list[list[Button]]:
    """"🚗 Мои автомобили" (см. задачу, п.1) — один автомобиль, одна
    inline-кнопка, формат "🟢 E911EE95 — ON" / "⚪ O687KE761 — OFF" по
    РЕАЛЬНОМУ состоянию turkey_monitoring_subscriptions (см.
    ConversationController._monitoring_map — НЕ кэшируется отдельно как
    UI-state). Нажатие открывает карточку (car_card_keyboard)."""
    return [
        [Button.inline(
            format_car_button_label(car, monitoring_active=monitoring_active.get(car.id, False)),
            encode_car_open_callback(car.id),
        )]
        for car in cars
    ]


def check_now_picker_keyboard(cars: list[TurkeyUserCar]) -> list[list[Button]]:
    """"🔎 Проверить сейчас" из главного меню (см. design report п.4:
    "пользователь выбирает ТОЛЬКО автомобиль") — простой пикер по номеру,
    ОТДЕЛЬНЫЙ от "🚗 Мои автомобили" (там кнопка несёт ON/OFF-статус) —
    callback сразу запускает unified check (action="check"), минуя
    открытие карточки."""
    return [
        [Button.inline(f"🚗 {car.car_number}", encode_car_action_callback("check", car.id))]
        for car in cars
    ]


def car_card_keyboard(car: TurkeyUserCar, *, monitoring_active: bool) -> list[list[Button]]:
    """Карточка одного автомобиля (см. задачу, п.3 — адаптация
    reader/public_bot/keyboards.py::car_detail_keyboard):
      [🔎 Проверить сейчас]
      [▶️ Включить мониторинг] / [⏹ Отключить мониторинг]
      [📜 История]                (доп. кнопка Turkey test bot, см. п.12
                                    предыдущего этапа — задача её явно не
                                    убирает и не упоминает)
      [🗑 Удалить автомобиль]      (-> подтверждение, см.
                                    car_delete_confirm_keyboard)
      [⬅️ Назад]                  (-> обновлённый список "🚗 Мои
                                    автомобили", см. BACK_TO_MY_CARS_
                                    CALLBACK_DATA, НЕ главное меню)"""
    monitoring_label = DISABLE_MONITORING_LABEL if monitoring_active else ENABLE_MONITORING_LABEL
    monitoring_action = "monitor_off" if monitoring_active else "monitor_on"
    return [
        [Button.inline(CHECK_NOW_LABEL, encode_car_action_callback("check", car.id))],
        [Button.inline(monitoring_label, encode_car_action_callback(monitoring_action, car.id))],
        [Button.inline(HISTORY_LABEL, encode_car_action_callback("history", car.id))],
        [Button.inline(DELETE_CAR_LABEL, encode_car_action_callback("delete_prompt", car.id))],
        [Button.inline(BACK_LABEL, BACK_TO_MY_CARS_CALLBACK_DATA)],
    ]


def car_delete_confirm_keyboard(car_id: int) -> list[list[Button]]:
    """Подтверждение удаления (см. задачу, п.7 — адаптация
    reader/public_bot/keyboards.py::car_delete_confirm_keyboard) — тот же
    Georgian pattern, два варианта в одной строке."""
    return [[
        Button.inline(DELETE_CAR_CONFIRM_BUTTON_LABEL, encode_car_action_callback("delete_confirm", car_id)),
        Button.inline(DELETE_CANCEL_LABEL, encode_car_action_callback("delete_cancel", car_id)),
    ]]


def add_car_confirmation_keyboard(car_id: int) -> list[list[Button]]:
    """После добавления автомобиля (см. design report п.2, пример
    "[🔎 Проверить сейчас] [🏠 Главное меню]") — "Главное меню" отдельной
    inline-кнопкой не нужен: персистентная reply-клавиатура (см.
    main_menu_keyboard) остаётся видна пользователю и без повторной
    отправки."""
    return [[Button.inline(CHECK_NOW_LABEL, encode_car_action_callback("check", car_id))]]


def encode_help_callback(section: str) -> bytes:
    return _HELP_CALLBACK_PREFIX + section.encode("ascii")


def decode_help_callback(data: bytes | None) -> str | None:
    if not data or not data.startswith(_HELP_CALLBACK_PREFIX):
        return None
    section = data[len(_HELP_CALLBACK_PREFIX):].decode("ascii", errors="strict")
    if section not in _HELP_SECTIONS:
        return None
    return section


def help_menu_keyboard() -> list[list[Button]]:
    return [
        [Button.inline(HELP_TERMS_LABEL, encode_help_callback("terms"))],
        [Button.inline(HELP_GIB_LABEL, encode_help_callback("gib"))],
        [Button.inline(HELP_AVRASYA_LABEL, encode_help_callback("avrasya"))],
        [Button.inline(HELP_PAYMENT_LABEL, encode_help_callback("payment"))],
        [Button.inline(HELP_BACK_LABEL, HELP_BACK_TO_MAIN_CALLBACK_DATA)],
    ]


def help_section_keyboard() -> list[list[Button]]:
    return [[Button.inline(HELP_BACK_LABEL, encode_help_callback("menu"))]]
