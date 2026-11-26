"""Тексты MAX-бота Грузии, которых нет у Telegram-бота (справка без
Telegram-ссылок, оплаченные штрафы, привязка, экраны менеджера). Всё общее
(итог добавления, результат проверки, карточка суммы, уведомления) берётся
из reader/public_bot/texts.py и delivery_texts.py без изменений.

Разметка — как у общих текстов (Telethon md), в MAX её переводит в HTML
reader/max_bot/formatting.py.
"""

from __future__ import annotations

from reader.fines.payment_status import FineStatus
from reader.max_bot.georgia.repository import MaxGeSubscription
from reader.max_bot.linking import format_code
from reader.plate_display import format_plate_for_telegram
from reader.public_bot import texts as ge_texts
from reader.public_bot.payable_amount_texts import format_money

GREETING_TEXT = ge_texts.START_GREETING_TEXT
MAX_CARS_SHOWN = 25
MENU_TEXT = ge_texts.MAIN_MENU_TEXT
UNKNOWN_TEXT = "Не понял сообщение. Отправьте номер авто или выберите действие в меню."
STALE_BUTTON_TEXT = "Кнопка устарела — откройте меню заново."
NOT_YOURS_TEXT = ge_texts.CALLBACK_NOT_AUTHORIZED_TEXT
IN_PROGRESS_TEXT = "⏳ Проверка уже выполняется, дождитесь результата."
CHECKING_TEXT = "⏳ Проверяю штрафы…"
ADDING_TEXT = "⏳ Добавляю автомобиль и проверяю штрафы…"
REFRESHING_TEXT = "⏳ Уточняю сумму к оплате…"
ALREADY_ADDED_LINE = "ℹ️ Этот автомобиль уже есть в вашем списке."
DELETED_TEXT = "🗑 Автомобиль удалён из списка. Уведомлений по нему больше не будет."
PAYABLE_DISABLED_TEXT = "Уточнение суммы к оплате сейчас недоступно."
MANAGER_ONLY_TEXT = "Раздел доступен только менеджерам."

HELP_TEXT = (
    "ℹ️ Справка\n\n"
    "• ➕ Добавить авто — сохранить автомобиль и получать уведомления о новых штрафах.\n"
    "• 📋 Мои авто — сохранённые автомобили: проверка, сумма к оплате, оплаченные штрафы, "
    "включение и выключение уведомлений.\n"
    "• 🔎 Проверить штрафы — все неоплаченные штрафы по автомобилю.\n"
    "• 🔗 Telegram — привязать аккаунт Telegram и перенести автомобили.\n\n"
    "Уведомления в MAX и в Telegram настраиваются отдельно: выключение в одном "
    "мессенджере не выключает их в другом."
)


def format_car_card(subscription: MaxGeSubscription) -> str:
    lines = [
        f"🚗 {format_plate_for_telegram(subscription.car_number)}",
        "",
        "🔎 Проверить штрафы — покажем все неоплаченные штрафы, которые сейчас числятся "
        "по автомобилю, в том числе выписанные ранее.",
        "",
        ge_texts.NOTIFICATIONS_ON_LINE if subscription.is_active else ge_texts.NOTIFICATIONS_OFF_LINE,
    ]
    return "\n".join(lines)


def format_cars_list(subscriptions: list[MaxGeSubscription]) -> str:
    if not subscriptions:
        return ge_texts.NO_CARS_TEXT
    text = f"{ge_texts.MY_CARS_HEADER}\n\n{ge_texts.MY_CARS_HINT_TEXT}"
    if len(subscriptions) > MAX_CARS_SHOWN:
        text += f"\n\nПоказаны первые {MAX_CARS_SHOWN} из {len(subscriptions)}."
    return text


def format_turn_off(subscription: MaxGeSubscription) -> str:
    return ge_texts.format_turn_off_success(subscription.car_number) + "\n\n" + format_car_card(subscription)


def format_turn_on(subscription: MaxGeSubscription) -> str:
    return ge_texts.format_turn_on_success(subscription.car_number) + "\n\n" + format_car_card(subscription)


def format_paid_fines(car_number: str, statuses: list[FineStatus]) -> str:
    plate = format_plate_for_telegram(car_number)
    if not statuses:
        return f"🟢 {plate}: оплаченных штрафов в нашей базе нет."
    lines = [f"🟢 Оплаченные штрафы — {plate}", ""]
    for status in statuses:
        amount = format_money(status.amount) if status.amount is not None else "сумма неизвестна"
        lines.append(f"• № {status.protocol_no} — {amount}")
    return "\n".join(lines)


def format_link_menu(*, linked: bool, telegram_bot: str) -> str:
    state = "✅ Аккаунт Telegram привязан." if linked else "Аккаунт Telegram не привязан."
    return (
        f"🔗 Привязка Telegram\n\n{state}\n\n"
        f"Привязка нужна, чтобы перенести автомобили из @{telegram_bot}. "
        "Подписки и уведомления в Telegram при этом не меняются."
    )


def format_link_code(code: str, *, telegram_bot: str) -> str:
    return (
        "🔑 Код привязки\n\n"
        f"Отправьте боту @{telegram_bot} в Telegram сообщение:\n"
        f"`/max_link {format_code(code)}`\n\n"
        "Код действует 15 минут и подходит только один раз. Никому его не пересылайте."
    )


LINK_REQUIRED_TEXT = "Сначала привяжите аккаунт Telegram: «🔗 Telegram» → «🔑 Получить код»."


def format_import_result(imported: list[str], skipped: int) -> str:
    if not imported:
        return "В привязанном Telegram-аккаунте нет автомобилей для переноса."
    lines = ["📥 Перенесено в MAX:", ""]
    lines += [f"• {format_plate_for_telegram(car)}" for car in imported]
    lines += ["", "Уведомления придут только о новых штрафах — уже известные повторно не отправляются."]
    if skipped:
        lines.append(f"Уже были в списке: {skipped}.")
    return "\n".join(lines)


def format_manager_stats(
    *, users: int, subscriptions: dict[str, int], fine_deliveries: dict[str, int], amount_deliveries: dict[str, int],
) -> str:
    return "\n".join([
        "📊 MAX — штрафы Грузии",
        "",
        f"👤 Пользователей: {users}",
        f"🚗 Автомобилей: уведомления вкл. {subscriptions.get('active', 0)}, "
        f"выкл. {subscriptions.get('stopped', 0)}, удалено {subscriptions.get('archived', 0)}",
        f"⚠️ Новые штрафы: доставлено {fine_deliveries['delivered']}, ожидают {fine_deliveries['pending']}, "
        f"исключено {fine_deliveries['excluded']}",
        f"📈 Увеличения суммы: доставлено {amount_deliveries['delivered']}, ожидают {amount_deliveries['pending']}, "
        f"исключено {amount_deliveries['excluded']}",
        "",
        "Полная статистика и список задолженности — в менеджерском интерфейсе Telegram-бота.",
    ])


SEARCH_PROMPT = "🔎 Введите госномер для поиска."


def format_manager_search(
    car_number: str, *, task_statuses: list[str], telegram_subscribers: int, max_subscribers: int,
    unpaid_count: int, unpaid_amount: float, paid_count: int,
) -> str:
    plate = format_plate_for_telegram(car_number)
    if not task_statuses and not max_subscribers:
        return f"🔎 {plate}: номер не найден в мониторинге."
    monitoring = "🟢 активен" if "active" in task_statuses else "⚪ не активен"
    return "\n".join([
        f"🔎 {plate}",
        "",
        f"Мониторинг: {monitoring}",
        f"Подписчиков: Telegram {telegram_subscribers}, MAX {max_subscribers}",
        f"🔴 Неоплачено: {unpaid_count} — {format_money(unpaid_amount)}",
        f"🟢 Оплачено: {paid_count}",
    ])
