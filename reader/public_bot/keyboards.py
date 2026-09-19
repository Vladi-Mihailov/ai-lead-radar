"""Telethon-клавиатуры/callback_data @GEShtrafbot.

Security-инвариант (см. design report и reader/public_bot/conversation.py):
callback_data периода несёт ТОЛЬКО выбранное значение (b"period:30" и
т.п.) — никакого telegram_user_id/task_id. Владение и идентичность
проверяются ИСКЛЮЧИТЕЛЬНО через event.sender_id + conversation_state (см.
ConversationController.handle_period_choice) — callback_data сам по себе
не является доказательством ничего, кроме "какая кнопка была нажата"
(пользователь может переслать сообщение с кнопкой в свой собственный чат с
ботом или его callback_data теоретически может быть воспроизведён — но это
не даёт доступа ни к чьей чужой записи, потому что никакая чужая запись в
payload не упоминается).

🔎 Проверить сейчас и car-centric "📋 Мои авто" (ON/OFF/Delete, см. design
report про переработку UX) устроены немного иначе: их callback_data
ДЕЙСТВИТЕЛЬНО несёт subscription_id (иначе "выбрать одно из НЕСКОЛЬКИХ
авто" невозможно закодировать без идентификатора вообще), а my-car-* ещё и
page (чтобы "⬅️ Назад"/обновлённая карточка возвращали на ту же страницу
пагинации) — но тот же инвариант сохраняется на СЛЕДУЮЩЕМ уровне:
subscription_id сам по себе публичен и НЕ является доказательством
владения — ConversationController.handle_check_now_choice/
handle_my_car_open/handle_my_car_turn_on/handle_my_car_turn_off/
handle_my_car_delete_* вызывают SubscriptionService.
get_actionable_subscription()/check_now()/turn_on_car()/turn_off_car()/
delete_car(), которые ВСЕГДА заново проверяют владение по РЕАЛЬНОМУ
event.sender_id перед тем, как что-либо показать/сделать — то есть
подделанный/чужой subscription_id просто не пройдёт эту проверку,
независимо от того, насколько "правильно" он выглядит в callback_data.
page в my-car-* callback'ах — ТОЛЬКО для навигации (куда вернуться), не
идентификатор ресурса и не доказательство чего-либо.
"""

from telethon import Button

from reader.public_bot.conversation import PERIOD_CHOICES
from reader.public_bot.texts import (
    ADD_CAR_LABEL,
    BACK_BUTTON_LABEL,
    CANCEL_BUTTON_LABEL,
    CHECK_NOW_LABEL,
    DELETE_CAR_BUTTON_LABEL,
    DELETE_CAR_CONFIRM_BUTTON_LABEL,
    MY_CARS_LABEL,
    STATISTICS_LABEL,
    STOP_LABEL,
    TURKEY_BOT_LINK_LABEL,
    TURKEY_BOT_URL,
    TURN_OFF_BUTTON_LABEL,
    TURN_ON_BUTTON_LABEL,
)

_PERIOD_PREFIX = b"period:"
_CHECK_NOW_PREFIX = b"checknow:"
STOP_NO = b"stopno"
_ADD_CLIENT_YES = b"addclient:yes"
_ADD_CLIENT_NO = b"addclient:no"
# Trusted-operator task-level admin (см. design report: пересмотр
# архитектуры) — task_id вместо subscription_id, отдельные префиксы, чтобы
# НИКОГДА не перепутать с subscription-based callback'ами выше (см.
# _decode_id — startswith конкретного префикса, коллизий по префиксу нет).
_TRUSTED_STOP_PICK_PREFIX = b"tstoppick:"
_TRUSTED_STOP_YES_PREFIX = b"tstopyes:"
# 📋 Мои авто пагинация (trusted task-level, см. design report) — page
# (0-indexed), НЕ task_id — сам по себе не даёт никаких привилегий:
# is_trusted() перепроверяется на каждом callback заново, а page вне
# диапазона просто кламп(ится) до ближайшей валидной страницы (см.
# ConversationController._format_trusted_tasks_page_reply).
_TRUSTED_TASKS_PAGE_PREFIX = b"ttaskspage:"

# car-centric "📋 Мои авто" (см. design report про переработку UX) — для
# обычного (не-trusted) пользователя, subscription-based, ОТДЕЛЬНЫЕ
# префиксы от trusted task-level (_TRUSTED_*) и от старого subscription
# stop-picker'а (удалён вместе с ⛔ для обычных пользователей, см. design
# report п.8) — коллизий по префиксу нет (см. _decode_id/_decode_id_page).
_MY_CARS_PAGE_PREFIX = b"mycarspage:"
_MY_CAR_OPEN_PREFIX = b"mycaropen:"
_MY_CAR_TURN_ON_PREFIX = b"mycaron:"
_MY_CAR_TURN_OFF_PREFIX = b"mycaroff:"
_MY_CAR_DELETE_PREFIX = b"mycardel:"
_MY_CAR_DELETE_YES_PREFIX = b"mycardelyes:"
_MY_CAR_DELETE_NO_PREFIX = b"mycardelno:"


def main_menu_keyboard(*, is_trusted: bool = False) -> list[list[Button]]:
    """is_trusted — управляет ОБЕИМИ кнопками, доступными только
    trusted_operator_user_ids (см. reader/public_bot/handlers.py —
    единственное место, откуда сюда приходит True):

    - "⛔ Остановить мониторинг" — для обычного пользователя эта кнопка
      стала избыточной (car-centric ON/OFF в "📋 Мои авто" её заменяет
      целиком, см. design report п.8), но trusted-оператору всё ещё нужна:
      она ведёт к ЕГО ОТДЕЛЬНОМУ, task-level admin flow (см.
      ConversationController._build_trusted_stop_picker_reply/
      handle_trusted_stop_pick), который умеет останавливать ЛЮБУЮ задачу
      мониторинга (включая операторские, без единой client-подписки) — то,
      что car-centric UX (subscription-based) заведомо не покрывает;
    - "📊 Статистика" — как и раньше, обычный клиент её никогда не видит.

    НЕ содержит переход в Turkey-бот (см. design report "связать Georgian
    bot и Turkey bot взаимными кнопками перехода") — Telethon/Telegram
    физически не позволяет смешивать reply-кнопки (Button.text, эта
    клавиатура) с inline URL-кнопкой в ОДНОЙ разметке (client.
    build_reply_markup поднимает ValueError('You cannot mix inline with
    normal buttons') при попытке) — переход отправляется ОТДЕЛЬНЫМ
    сообщением сразу после текста главного меню, см.
    turkey_bot_link_keyboard() ниже и reader/public_bot/handlers.py::
    _send_reply."""
    rows = [
        [Button.text(ADD_CAR_LABEL, resize=True), Button.text(MY_CARS_LABEL, resize=True)],
        [Button.text(CHECK_NOW_LABEL, resize=True)],
    ]
    if is_trusted:
        rows[1].append(Button.text(STOP_LABEL, resize=True))
        rows.append([Button.text(STATISTICS_LABEL, resize=True)])
    return rows


def turkey_bot_link_keyboard() -> list[list[Button]]:
    """Inline URL-кнопка перехода в Turkey-бот (см. design report "связать
    Georgian bot и Turkey bot взаимными кнопками перехода") — ОТДЕЛЬНАЯ
    разметка (не часть main_menu_keyboard(), см. её докстрок про то, почему
    их нельзя смешать в одном сообщении). Button.url (не callback) —
    открывает t.me/ProtocolTRbot напрямую, ничего не кодирует и не
    проверяет на стороне бота."""
    return [[Button.url(TURKEY_BOT_LINK_LABEL, TURKEY_BOT_URL)]]


def add_client_decision_keyboard() -> list[list[Button]]:
    """"👤 Добавить Telegram клиента?" — trusted-оператор, сразу после
    ввода номера авто (см. design report: username клиента больше НЕ
    обязателен для постановки на мониторинг)."""
    return [[
        Button.inline("OK", _ADD_CLIENT_YES),
        Button.inline("Отмена", _ADD_CLIENT_NO),
    ]]


def decode_add_client_decision_callback(data: bytes | None) -> bool | None:
    """True — оператор нажал OK (хочет указать клиента), False — Отмена
    (мониторинг без клиента). None — этот callback_data не про этот шаг
    вовсе (вызывающий код должен пробовать следующий decode_*, а не
    трактовать None как "Отмена")."""
    if data == _ADD_CLIENT_YES:
        return True
    if data == _ADD_CLIENT_NO:
        return False
    return None


def period_choice_keyboard() -> list[list[Button]]:
    buttons = [
        Button.inline(f"{days} дней", encode_period_callback(days)) for days in PERIOD_CHOICES
    ]
    # 2x2 — ровно как в макете задачи ([30][90] / [180][365]).
    return [buttons[0:2], buttons[2:4]]


def encode_period_callback(days: int) -> bytes:
    return _PERIOD_PREFIX + str(days).encode("ascii")


def decode_period_callback(data: bytes | None) -> int | None:
    """None для чего угодно, кроме РОВНО одного значения из PERIOD_CHOICES —
    намеренно не парсит произвольные числа из чужого/подделанного
    callback_data (allowlist, а не "любое целое число")."""
    return _decode_id(data, _PERIOD_PREFIX, allowlist=PERIOD_CHOICES)


def _decode_id(data: bytes | None, prefix: bytes, *, allowlist: tuple[int, ...] | None = None) -> int | None:
    if not data or not data.startswith(prefix):
        return None
    try:
        value = int(data[len(prefix):])
    except ValueError:
        return None
    if allowlist is not None and value not in allowlist:
        return None
    return value


def decode_check_now_callback(data: bytes | None) -> int | None:
    return _decode_id(data, _CHECK_NOW_PREFIX)


def options_keyboard(options: list[tuple[int, str]], *, prefix: bytes) -> list[list[Button]]:
    """Один subscription_id на кнопку — см. модуль docstring про то, почему
    это безопасно (owner-check происходит при нажатии, не здесь)."""
    return [
        [Button.inline(f"🚗 {label}", prefix + str(subscription_id).encode("ascii"))]
        for subscription_id, label in options
    ]


def check_now_options_keyboard(options: list[tuple[int, str]]) -> list[list[Button]]:
    return options_keyboard(options, prefix=_CHECK_NOW_PREFIX)


def _encode_id_page(prefix: bytes, id_: int, page: int) -> bytes:
    return prefix + str(id_).encode("ascii") + b":" + str(page).encode("ascii")


def _decode_id_page(data: bytes | None, prefix: bytes) -> tuple[int, int] | None:
    """(id, page) — используется всеми car-centric "📋 Мои авто"
    callback'ами (см. модуль docstring): id — ресурс (subscription_id),
    page — куда вернуться после действия/отмены. Ровно два числовых
    сегмента, разделённых ':' — что угодно ещё (лишние ':', нечисловые
    сегменты) трактуется как невалидный/чужой callback, а не как ошибка."""
    if not data or not data.startswith(prefix):
        return None
    parts = data[len(prefix):].split(b":")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def encode_my_cars_page_callback(page: int) -> bytes:
    return _MY_CARS_PAGE_PREFIX + str(page).encode("ascii")


def decode_my_cars_page_callback(data: bytes | None) -> int | None:
    return _decode_id(data, _MY_CARS_PAGE_PREFIX)


def encode_my_car_open_callback(subscription_id: int, page: int) -> bytes:
    return _encode_id_page(_MY_CAR_OPEN_PREFIX, subscription_id, page)


def decode_my_car_open_callback(data: bytes | None) -> tuple[int, int] | None:
    return _decode_id_page(data, _MY_CAR_OPEN_PREFIX)


def encode_my_car_turn_on_callback(subscription_id: int, page: int) -> bytes:
    return _encode_id_page(_MY_CAR_TURN_ON_PREFIX, subscription_id, page)


def decode_my_car_turn_on_callback(data: bytes | None) -> tuple[int, int] | None:
    return _decode_id_page(data, _MY_CAR_TURN_ON_PREFIX)


def encode_my_car_turn_off_callback(subscription_id: int, page: int) -> bytes:
    return _encode_id_page(_MY_CAR_TURN_OFF_PREFIX, subscription_id, page)


def decode_my_car_turn_off_callback(data: bytes | None) -> tuple[int, int] | None:
    return _decode_id_page(data, _MY_CAR_TURN_OFF_PREFIX)


def encode_my_car_delete_callback(subscription_id: int, page: int) -> bytes:
    return _encode_id_page(_MY_CAR_DELETE_PREFIX, subscription_id, page)


def decode_my_car_delete_callback(data: bytes | None) -> tuple[int, int] | None:
    return _decode_id_page(data, _MY_CAR_DELETE_PREFIX)


def encode_my_car_delete_confirm_callback(subscription_id: int, page: int) -> bytes:
    return _encode_id_page(_MY_CAR_DELETE_YES_PREFIX, subscription_id, page)


def decode_my_car_delete_confirm_callback(data: bytes | None) -> tuple[int, int] | None:
    return _decode_id_page(data, _MY_CAR_DELETE_YES_PREFIX)


def encode_my_car_delete_cancel_callback(subscription_id: int, page: int) -> bytes:
    return _encode_id_page(_MY_CAR_DELETE_NO_PREFIX, subscription_id, page)


def decode_my_car_delete_cancel_callback(data: bytes | None) -> tuple[int, int] | None:
    return _decode_id_page(data, _MY_CAR_DELETE_NO_PREFIX)


def my_cars_page_keyboard(
    cars: list[tuple[int, str]], *, page: int, total_pages: int,
) -> list[list[Button]]:
    """Автомобиль — сама кнопка (см. design report про car-centric "📋 Мои
    авто"): cars — (subscription_id, label), label уже содержит эмодзи/
    номер/ON-OFF (см. texts.format_car_button_label). Пагинация (◀️/
    индикатор/▶️) — та же схема, что и у trusted_tasks_page_keyboard, но
    отдельными callback'ами (_MY_CARS_PAGE_PREFIX), не task-level; строка
    пагинации не добавляется вовсе, если все автомобили помещаются на одну
    страницу (нечего листать)."""
    rows = [
        [Button.inline(label, encode_my_car_open_callback(subscription_id, page))]
        for subscription_id, label in cars
    ]
    if total_pages > 1:
        back_page = max(page - 1, 0)
        next_page = min(page + 1, total_pages - 1)
        rows.append([
            Button.inline("◀️ Назад", encode_my_cars_page_callback(back_page)),
            Button.inline(f"{page + 1} / {total_pages}", encode_my_cars_page_callback(page)),
            Button.inline("Вперёд ▶️", encode_my_cars_page_callback(next_page)),
        ])
    return rows


def car_detail_keyboard(
    subscription_id: int, *, monitoring_state: str, page: int,
) -> list[list[Button]]:
    """monitoring_state — "ON"/"OFF"/"ИСТЁК" (см.
    texts.car_monitoring_state) — определяет, какую кнопку-переключатель
    показать: ON -> "⏸ Выключить", OFF -> "▶️ Включить"; для "ИСТЁК" —
    БЕЗ переключателя вовсе (см. design report: реактивация истёкшей
    подписки — не простой toggle, требует нового периода через "➕
    Добавить авто", а не одну кнопку здесь). 🔎 Проверить сейчас
    переиспользует ТОТ ЖЕ callback/handler, что и обычный picker (см.
    _CHECK_NOW_PREFIX/decode_check_now_callback/handle_check_now_choice) —
    работает независимо от ON/OFF (см. design report: "Check Now должен
    работать даже если OFF")."""
    rows = [[Button.inline(CHECK_NOW_LABEL, _CHECK_NOW_PREFIX + str(subscription_id).encode("ascii"))]]
    if monitoring_state == "ON":
        rows.append([
            Button.inline(TURN_OFF_BUTTON_LABEL, encode_my_car_turn_off_callback(subscription_id, page)),
        ])
    elif monitoring_state == "OFF":
        rows.append([
            Button.inline(TURN_ON_BUTTON_LABEL, encode_my_car_turn_on_callback(subscription_id, page)),
        ])
    rows.append([
        Button.inline(DELETE_CAR_BUTTON_LABEL, encode_my_car_delete_callback(subscription_id, page)),
    ])
    rows.append([Button.inline(BACK_BUTTON_LABEL, encode_my_cars_page_callback(page))])
    return rows


def car_delete_confirm_keyboard(subscription_id: int, *, page: int) -> list[list[Button]]:
    return [[
        Button.inline(DELETE_CAR_CONFIRM_BUTTON_LABEL, encode_my_car_delete_confirm_callback(subscription_id, page)),
        Button.inline(CANCEL_BUTTON_LABEL, encode_my_car_delete_cancel_callback(subscription_id, page)),
    ]]


def decode_trusted_stop_pick_callback(data: bytes | None) -> int | None:
    return _decode_id(data, _TRUSTED_STOP_PICK_PREFIX)


def decode_trusted_stop_confirm_callback(data: bytes | None) -> int | None:
    return _decode_id(data, _TRUSTED_STOP_YES_PREFIX)


def trusted_stop_options_keyboard(options: list[tuple[int, str]]) -> list[list[Button]]:
    return options_keyboard(options, prefix=_TRUSTED_STOP_PICK_PREFIX)


def encode_trusted_tasks_page_callback(page: int) -> bytes:
    return _TRUSTED_TASKS_PAGE_PREFIX + str(page).encode("ascii")


def decode_trusted_tasks_page_callback(data: bytes | None) -> int | None:
    return _decode_id(data, _TRUSTED_TASKS_PAGE_PREFIX)


def trusted_tasks_page_keyboard(*, page: int, total_pages: int) -> list[list[Button]]:
    """[◀️ Назад] [N / M] [Вперёд ▶️] — один ряд (см. design report). Back
    на первой странице и Next на последней — no-op (кламп к той же
    странице, см. design report: "первая страница — Back disabled/no-op";
    "последняя — Next disabled/no-op"), а не отсутствующая кнопка: нажатие
    просто заново показывает ту же страницу. Средняя кнопка-индикатор —
    тоже no-op (кодирует текущую page)."""
    back_page = max(page - 1, 0)
    next_page = min(page + 1, total_pages - 1)
    return [[
        Button.inline("◀️ Назад", encode_trusted_tasks_page_callback(back_page)),
        Button.inline(f"{page + 1} / {total_pages}", encode_trusted_tasks_page_callback(page)),
        Button.inline("Вперёд ▶️", encode_trusted_tasks_page_callback(next_page)),
    ]]


def trusted_stop_confirm_keyboard(task_id: int, *, label: str) -> list[list[Button]]:
    """label — "⛔ Остановить" или "⛔ Остановить для всех" в зависимости
    от того, есть ли у задачи ещё actionable client-подписки (см.
    reader/public_bot/texts.py::trusted_stop_confirm_button_label) —
    вычисляется вызывающим кодом (ConversationController), не здесь."""
    return [[
        Button.inline(label, _TRUSTED_STOP_YES_PREFIX + str(task_id).encode("ascii")),
        Button.inline("Отмена", STOP_NO),
    ]]


_PAYMENT_HELP_BUTTON_LABEL = "💳 Оплатить в рублях"
_INSURANCE_BUTTON_LABEL = "🚗 ОСАГО Грузии"


def owner_fine_cta_buttons(contact_username: str) -> list[list[Button]]:
    """Коммерческий CTA под owner-уведомлением о новом штрафе — ТОЛЬКО
    owner, trusted_operator и операторский чат этих кнопок не получают. Обе
    кнопки — один ряд (утверждённый макет), обе ведут на одну и ту же
    destination.

    contact_username — БЕЗ ведущего "@" (см. settings.public_bot.
    payment_help_contact_username) — destination задаётся конфигом, не
    hardcoded здесь, только шаблон ссылки t.me/<username>."""
    url = f"https://t.me/{contact_username}"
    return [[
        Button.url(_PAYMENT_HELP_BUTTON_LABEL, url),
        Button.url(_INSURANCE_BUTTON_LABEL, url),
    ]]
