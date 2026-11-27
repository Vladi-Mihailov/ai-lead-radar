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

import re

from telethon import Button

from reader.turkey_bot import texts
from reader.turkey_bot.models import TurkeyUserCar
from reader.protocol_support.texts import OPERATOR_LABEL
from reader.turkey_bot.texts import (
    ADD_CAR_LABEL,
    BACK_LABEL,
    CANCEL_BUTTON_LABEL,
    CHECK_NOW_LABEL,
    DEBT_REFRESH_BUTTON_LABEL,
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
    OSAGO_TR_LABEL,
    SEARCH_BACK_LABEL,
    SEARCH_LABEL,
    SEARCH_MENU_LABEL,
    SEARCH_NEW_LABEL,
    STATISTICS_LABEL,
    debt_refresh_confirm_button_label,
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


def main_menu_keyboard(*, is_trusted: bool = False, is_admin: bool = False) -> list[list[Button]]:
    """Макет обычного пользователя:
      ROW 1: ADD_CAR_LABEL | MY_CARS_LABEL
      ROW 2: CHECK_NOW_LABEL | OSAGO_TR_LABEL
      ROW 3: HELP_LABEL | GEORGIAN_BOT_LINK_LABEL

    Макет manager (is_trusted=True):
      ROW 1: ADD_CAR_LABEL | MY_CARS_LABEL
      ROW 2: CHECK_NOW_LABEL | SEARCH_LABEL
      ROW 3: STATISTICS_LABEL | OSAGO_TR_LABEL
      ROW 4: HELP_LABEL | GEORGIAN_BOT_LINK_LABEL

    OSAGO_TR_LABEL/GEORGIAN_BOT_LINK_LABEL swapped (см. задачу "swap OSAGO
    and cross-country fines button positions") -- чисто порядок кнопок, ни
    один handler/URL/текст не менялся (см. conversation.py::handle_text/
    handle_osago_tr_link/handle_georgian_bot_link, оба нетронуты).

    SEARCH_LABEL (см. задачу "manager/trusted Search") заменяет здесь
    бывшую "⛔ Остановить мониторинг" (STOP_MONITORING_LABEL, см.
    reader/turkey_bot/texts.py — константа и весь underlying flow
    сознательно НЕ удалены) — per-car ON/OFF в manager "🚗 Мои
    автомобили" [STATUS] кнопке остаётся штатным способом остановить
    мониторинг КОНКРЕТНОЙ машины; полный "остановить всё" по-прежнему
    доступен через handle_stop_monitoring(), просто без кнопки в меню.

    "🇬🇪 Штрафы Грузии" — обычная reply-кнопка (см. georgian_bot_link_keyboard
    докстрок ниже про то, почему она не может сама быть URL-кнопкой)."""
    rows = [[Button.text(ADD_CAR_LABEL, resize=True), Button.text(MY_CARS_LABEL, resize=True)]]
    if is_trusted:
        # Меню менеджера НЕ меняется: «👨‍💼 Оператор» — только у пользователей.
        rows.append([Button.text(CHECK_NOW_LABEL, resize=True), Button.text(SEARCH_LABEL, resize=True)])
        rows.append([Button.text(STATISTICS_LABEL, resize=True), Button.text(OSAGO_TR_LABEL, resize=True)])
        rows.append([Button.text(HELP_LABEL, resize=True), Button.text(GEORGIAN_BOT_LINK_LABEL, resize=True)])
        if is_admin:
            rows.append([Button.text(texts.ORPHAN_LINKS_LABEL, resize=True)])
        return rows
    # Пользователь: «👨‍💼 Оператор» — на месте «ℹ️ Справка» (сразу под
    # «🔎 Проверить сейчас»), «ℹ️ Справка» — отдельной последней строкой.
    rows.append([Button.text(CHECK_NOW_LABEL, resize=True), Button.text(OSAGO_TR_LABEL, resize=True)])
    rows.append([Button.text(OPERATOR_LABEL, resize=True), Button.text(GEORGIAN_BOT_LINK_LABEL, resize=True)])
    rows.append([Button.text(HELP_LABEL, resize=True)])
    if is_admin:
        # Администратор (не менеджер) — «🔗 Привязки без менеджера».
        rows.append([Button.text(texts.ORPHAN_LINKS_LABEL, resize=True)])
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


def manager_car_detail_keyboard(page: int, *, car_id: int | None = None) -> list[list[Button]]:
    """"⬅️ Назад" из read-only деталей строки -> та же страница
    manager-списка (см. handle_manager_car_open/_format_manager_cars_page_reply).
    car_id — кнопка «👤 Клиент» (привязка клиента, см. client_link_screen_keyboard)."""
    rows = []
    if car_id is not None:
        rows.append([Button.inline(texts.CLIENT_DATA_LABEL, encode_client_link_callback("screen", car_id, page))])
    rows.append([Button.inline(BACK_LABEL, encode_manager_cars_page_callback(page))])
    return rows


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


def add_car_confirmation_keyboard(car_id: int, *, offer_client_link: bool = False) -> list[list[Button]]:
    """После добавления автомобиля (см. design report п.2, пример
    "[🔎 Проверить сейчас] [🏠 Главное меню]") — "Главное меню" отдельной
    inline-кнопкой не нужен: персистентная reply-клавиатура (см.
    main_menu_keyboard) остаётся видна пользователю и без повторной
    отправки. offer_client_link — менеджеру ещё «👤 Привязать клиента»."""
    rows = [[Button.inline(CHECK_NOW_LABEL, encode_car_action_callback("check", car_id))]]
    if offer_client_link:
        rows.append([Button.inline(texts.CLIENT_LINK_BUTTON_LABEL, encode_client_link_callback("screen", car_id, 0))])
    return rows


# ---- 👤 Привязка клиента (см. reader/turkey_bot/client_links.py) — car_id/
# link_id/user_id в callback_data публичны: is_trusted() и владение
# приглашением перепроверяются в ConversationController на КАЖДОМ нажатии ----

_CLIENT_LINK_PREFIXES = {
    "screen": b"trlscr:", "username": b"trlusr:", "pick": b"trlpick:", "pick_user": b"trlpk:",
    "invite": b"trlinv:", "cancel": b"trlcan:", "replace": b"trlrep:", "unlink": b"trlun:",
    "unlink_yes": b"trluny:", "name": b"trlnm:", "name_clear": b"trlnc:",
}
_CLIENT_LINK_CONFIRM_PREFIX = b"trlok:"
_CLIENT_LINK_DECLINE_PREFIX = b"trlno:"


def _decode_ints(data: bytes | None, prefix: bytes, count: int) -> tuple[int, ...] | None:
    if not data or not data.startswith(prefix):
        return None
    parts = data[len(prefix):].decode("ascii", errors="replace").split(":")
    if len(parts) != count or not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def encode_client_link_callback(action: str, car_id: int, page: int, extra: int | None = None) -> bytes:
    """action: screen/username/invite/cancel/replace/unlink/unlink_yes —
    car_id:page; pick — car_id:page:pick_page; pick_user — car_id:page:user_id."""
    values = [car_id, page] + ([extra] if extra is not None else [])
    return _CLIENT_LINK_PREFIXES[action] + ":".join(str(value) for value in values).encode("ascii")


def decode_client_link_callback(data: bytes | None) -> tuple[str, int, int, int | None] | None:
    for action, prefix in _CLIENT_LINK_PREFIXES.items():
        count = 3 if action in ("pick", "pick_user") else 2
        values = _decode_ints(data, prefix, count)
        if values is not None:
            return action, values[0], values[1], values[2] if count == 3 else None
    return None


def encode_client_link_answer_callback(link_id: int, *, confirm: bool) -> bytes:
    prefix = _CLIENT_LINK_CONFIRM_PREFIX if confirm else _CLIENT_LINK_DECLINE_PREFIX
    return prefix + str(link_id).encode("ascii")


def decode_client_link_answer_callback(data: bytes | None) -> tuple[int, bool] | None:
    for prefix, confirm in ((_CLIENT_LINK_CONFIRM_PREFIX, True), (_CLIENT_LINK_DECLINE_PREFIX, False)):
        values = _decode_ints(data, prefix, 1)
        if values is not None:
            return values[0], confirm
    return None


def client_link_confirm_keyboard(link_id: int) -> list[list[Button]]:
    return [[
        Button.inline(texts.CLIENT_LINK_CONFIRM_LABEL, encode_client_link_answer_callback(link_id, confirm=True)),
        Button.inline(texts.CLIENT_LINK_DECLINE_LABEL, encode_client_link_answer_callback(link_id, confirm=False)),
    ]]


def client_link_screen_keyboard(
    car_id: int, page: int, *, state: str, choosing: bool, has_name: bool = False, show_name: bool = True,
) -> list[list[Button]]:
    """state: none/expired/pending_user/pending_invite/active. choosing —
    показаны способы привязки (нет клиента, приглашение ждёт/истекло, замена).
    show_name — кнопки имени клиента (✏️, а при имени ещё 🗑)."""
    rows = []
    if show_name:
        name_row = [Button.inline(texts.CLIENT_NAME_EDIT_LABEL, encode_client_link_callback("name", car_id, page))]
        if has_name:
            name_row.append(Button.inline(
                texts.CLIENT_NAME_CLEAR_LABEL, encode_client_link_callback("name_clear", car_id, page),
            ))
        rows.append(name_row)
    if choosing:
        invite_label = (
            texts.CLIENT_LINK_NEW_INVITE_LABEL if state in ("pending_user", "pending_invite", "expired")
            else texts.CLIENT_LINK_INVITE_LABEL
        )
        rows += [
            [Button.inline(texts.CLIENT_LINK_USERNAME_LABEL, encode_client_link_callback("username", car_id, page))],
            [Button.inline(texts.CLIENT_LINK_PICK_LABEL, encode_client_link_callback("pick", car_id, page, 0))],
            [Button.inline(invite_label, encode_client_link_callback("invite", car_id, page))],
        ]
        if state in ("pending_user", "pending_invite"):
            rows.append([Button.inline(
                texts.CLIENT_LINK_CANCEL_INVITE_LABEL, encode_client_link_callback("cancel", car_id, page),
            )])
    if state == "active" and not choosing:
        rows += [
            [Button.inline(texts.CLIENT_LINK_REPLACE_LABEL, encode_client_link_callback("replace", car_id, page))],
            [Button.inline(texts.CLIENT_LINK_UNLINK_LABEL, encode_client_link_callback("unlink", car_id, page))],
        ]
    rows.append([Button.inline(BACK_LABEL, encode_manager_car_open_callback(car_id, page))])
    return rows


def client_link_pick_keyboard(
    car_id: int, page: int, options: list[tuple[int, str]], *, pick_page: int, total_pages: int,
) -> list[list[Button]]:
    rows = [
        [Button.inline(label, encode_client_link_callback("pick_user", car_id, page, user_id))]
        for user_id, label in options
    ]
    if total_pages > 1:
        rows.append([
            Button.inline("◀️", encode_client_link_callback("pick", car_id, page, max(0, pick_page - 1))),
            Button.inline(f"{pick_page + 1}/{total_pages}", encode_client_link_callback("pick", car_id, page, pick_page)),
            Button.inline("▶️", encode_client_link_callback("pick", car_id, page, min(total_pages - 1, pick_page + 1))),
        ])
    rows.append([Button.inline(BACK_LABEL, encode_client_link_callback("screen", car_id, page))])
    return rows


def client_link_unlink_confirm_keyboard(car_id: int, page: int) -> list[list[Button]]:
    return [[
        Button.inline(texts.CLIENT_LINK_UNLINK_YES_LABEL, encode_client_link_callback("unlink_yes", car_id, page)),
        Button.inline(DELETE_CANCEL_LABEL, encode_client_link_callback("screen", car_id, page)),
    ]]


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


# ==== manager/trusted Search (см. задачу "manager/trusted Search") —
# ТОЛЬКО trusted-facing клавиатуры, self-service не затронут. Тот же
# приём, что и reader/public_bot/keyboards.py (Georgian bot) — query
# (username/plate) кодируется В ОТКРЫТУЮ в callback_data, публичен, НЕ
# доказательство авторизации (is_trusted() перепроверяется на каждом
# handle_search_* заново). normalize_plate() никогда не отдаёт ":" в
# результате — split(":") безопасен. ====

_SEARCH_BACK_CALLBACK = b"turkeysearchback"
_SEARCH_NEW_CALLBACK = b"turkeysearchnew"
_SEARCH_PAGE_PREFIX = b"turkeysearchpage:"
_SEARCH_QUERY_TYPES = ("username", "car", "person")


def encode_search_back_callback() -> bytes:
    return _SEARCH_BACK_CALLBACK


def decode_search_back_callback(data: bytes | None) -> bool:
    return data == _SEARCH_BACK_CALLBACK


def encode_search_new_callback() -> bytes:
    return _SEARCH_NEW_CALLBACK


def decode_search_new_callback(data: bytes | None) -> bool:
    return data == _SEARCH_NEW_CALLBACK


def encode_search_page_callback(query_type: str, query: str, page: int) -> bytes:
    return _SEARCH_PAGE_PREFIX + f"{query_type}:{query}:{page}".encode()


def decode_search_page_callback(data: bytes | None) -> tuple[str, str, int] | None:
    """None для чего угодно, кроме РОВНО трёх сегментов с query_type из
    _SEARCH_QUERY_TYPES (allowlist) — не доверяет произвольному
    query_type/пустому query из чужого/подделанного callback_data."""
    if not data or not data.startswith(_SEARCH_PAGE_PREFIX):
        return None
    try:
        remainder = data[len(_SEARCH_PAGE_PREFIX):].decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    parts = remainder.split(":")
    if len(parts) != 3:
        return None
    query_type, query, page_text = parts
    if query_type not in _SEARCH_QUERY_TYPES or not query:
        return None
    try:
        page = int(page_text)
    except ValueError:
        return None
    return query_type, query, page


def search_entry_keyboard() -> list[list[Button]]:
    """Экран ввода запроса (см. texts.SEARCH_ENTRY_TEXT) — единственная
    кнопка "↩️ Назад", возвращает в главное меню."""
    return [[Button.inline(SEARCH_BACK_LABEL, encode_search_back_callback())]]


def search_result_keyboard(
    *, query_type: str, query: str, page: int, total_pages: int,
) -> list[list[Button]]:
    """Экран результата (найден он или нет) — пагинация (если
    total_pages > 1) + [🔎 Новый поиск][↩️ В меню]. "↩️ В меню"
    переиспользует ТОТ ЖЕ callback, что и "↩️ Назад" на экране ввода —
    оба ведут в главное меню (handle_search_back)."""
    rows = []
    if total_pages > 1:
        back_page = max(page - 1, 0)
        next_page = min(page + 1, total_pages - 1)
        rows.append([
            Button.inline("◀️ Назад", encode_search_page_callback(query_type, query, back_page)),
            Button.inline(f"{page + 1} / {total_pages}", encode_search_page_callback(query_type, query, page)),
            Button.inline("Вперёд ▶️", encode_search_page_callback(query_type, query, next_page)),
        ])
    rows.append([
        Button.inline(SEARCH_NEW_LABEL, encode_search_new_callback()),
        Button.inline(SEARCH_MENU_LABEL, encode_search_back_callback()),
    ])
    return rows


# ==== "🔄 Проверить авто с задолженностью" (см. задачу "manual Turkey
# debt refresh") — trusted-manager-only, is_trusted() перепроверяется
# server-side в ConversationController.handle_debt_refresh_pick/confirm/
# cancel на КАЖДЫЙ вызов (тот же принцип, что и у _SEARCH_*/_MANAGER_*
# выше) — фиксированные callback'и без car_id/page (refresh всегда над
# ТЕКУЩИМ полным debt-списком, см. TurkeyDebtRefreshService.
# list_candidates(), а не над каким-то конкретным выбором). ====

_DEBT_REFRESH_PICK_CALLBACK = b"turkeydebtrefreshpick"
_DEBT_REFRESH_YES_CALLBACK = b"turkeydebtrefreshyes"
_DEBT_REFRESH_NO_CALLBACK = b"turkeydebtrefreshno"


def decode_debt_refresh_pick_callback(data: bytes | None) -> bool:
    return data == _DEBT_REFRESH_PICK_CALLBACK


def decode_debt_refresh_confirm_callback(data: bytes | None) -> bool:
    return data == _DEBT_REFRESH_YES_CALLBACK


def decode_debt_refresh_cancel_callback(data: bytes | None) -> bool:
    return data == _DEBT_REFRESH_NO_CALLBACK


def debt_refresh_button_keyboard() -> list[list[Button]]:
    return [[Button.inline(DEBT_REFRESH_BUTTON_LABEL, _DEBT_REFRESH_PICK_CALLBACK)]]


def debt_refresh_confirm_keyboard(car_count: int) -> list[list[Button]]:
    return [[
        Button.inline(debt_refresh_confirm_button_label(car_count), _DEBT_REFRESH_YES_CALLBACK),
        Button.inline("↩️ Отмена", _DEBT_REFRESH_NO_CALLBACK),
    ]]



# ---- 🔗 Привязки без менеджера (администратор) — id в callback_data публичны:
# права администратора и одноразовый токен подтверждения проверяются в
# ConversationController.handle_orphan_action на КАЖДОМ нажатии ----

_ORPHAN_PREFIXES = {
    "list": (b"trolst:", 1), "disable": (b"trodis:", 1), "disable_yes": (b"trodisy:", 2),
    "transfer": (b"trotr:", 1), "transfer_to": (b"trotrm:", 2), "transfer_yes": (b"trotry:", 3),
}
_ORPHAN_TOKEN_RE = re.compile(r"[0-9a-f]{8}")


def encode_orphan_callback(action: str, first: int, extra: int | None = None, token: str | None = None) -> bytes:
    prefix, _count = _ORPHAN_PREFIXES[action]
    parts = [str(first)] + ([str(extra)] if extra is not None else []) + ([token] if token is not None else [])
    return prefix + ":".join(parts).encode("ascii")


def decode_orphan_callback(data: bytes | None) -> tuple[str, int, int | None, str | None] | None:
    """(action, link_id или page, manager_id, token)."""
    if not data:
        return None
    for action, (prefix, count) in _ORPHAN_PREFIXES.items():
        if not data.startswith(prefix):
            continue
        parts = data[len(prefix):].decode("ascii", errors="replace").split(":")
        if len(parts) != count:
            return None
        has_token = action in ("disable_yes", "transfer_yes")
        numbers, token = (parts[:-1], parts[-1]) if has_token else (parts, None)
        if not all(part.isdigit() for part in numbers) or (has_token and not _ORPHAN_TOKEN_RE.fullmatch(token)):
            return None
        first = int(numbers[0])
        extra = int(numbers[1]) if len(numbers) > 1 else None
        return action, first, extra, token
    return None


def orphan_list_keyboard(page: int, total_pages: int, options: list[tuple[int, str]]) -> list[list[Button]]:
    rows = [
        [
            Button.inline(f"{texts.ORPHAN_DISABLE_LABEL} {plate}", encode_orphan_callback("disable", link_id)),
            Button.inline(f"{texts.ORPHAN_TRANSFER_LABEL} {plate}", encode_orphan_callback("transfer", link_id)),
        ]
        for link_id, plate in options
    ]
    if total_pages > 1:
        rows.append([
            Button.inline("◀️", encode_orphan_callback("list", max(0, page - 1))),
            Button.inline(f"{page + 1}/{total_pages}", encode_orphan_callback("list", page)),
            Button.inline("▶️", encode_orphan_callback("list", min(total_pages - 1, page + 1))),
        ])
    return rows


def orphan_confirm_keyboard(link_id: int, action: str, extra: int | None, token: str) -> list[list[Button]]:
    label = texts.ORPHAN_DISABLE_YES_LABEL if action == "disable_yes" else texts.ORPHAN_TRANSFER_YES_LABEL
    return [[
        Button.inline(label, encode_orphan_callback(action, link_id, extra, token)),
        Button.inline(DELETE_CANCEL_LABEL, encode_orphan_callback("list", 0)),
    ]]


def orphan_picker_keyboard(link_id: int, options: list[tuple[int, str]]) -> list[list[Button]]:
    rows = [
        [Button.inline(label, encode_orphan_callback("transfer_to", link_id, manager_id))]
        for manager_id, label in options
    ]
    rows.append([Button.inline(BACK_LABEL, encode_orphan_callback("list", 0))])
    return rows
