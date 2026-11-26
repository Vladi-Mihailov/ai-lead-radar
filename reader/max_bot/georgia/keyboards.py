"""Клавиатуры MAX-бота Грузии. Подписи — те же константы, что у Telegram-бота
(reader/public_bot/texts.py), чтобы клиенты видели одинаковый интерфейс.

payload: "ge:<действие>[:<id>]" — id подписки публичен, права проверяются
по user_id отправителя (MaxGeorgiaService.get_owned).
"""

from __future__ import annotations

from reader.max_bot.keyboards import Keyboard, callback, link
from reader.max_bot.georgia.repository import MaxGeSubscription
from reader.public_bot import texts as ge_texts
from reader.public_bot.payable_amount_texts import REFRESH_BUTTON_LABEL

PREFIX = "ge:"

MENU = "ge:menu"
ADD = "ge:add"
CARS = "ge:cars"
CHECK = "ge:check"
HELP = "ge:help"
LINK = "ge:link"
LINK_NEW_CODE = "ge:linkcode"
LINK_IMPORT = "ge:import"
CANCEL = "ge:cancel"
STATS = "ge:stats"
SEARCH = "ge:search"

PAYABLE_LABEL = "💰 К оплате"
PAID_LABEL = "🟢 Оплаченные"
LINK_LABEL = "🔗 Telegram"
LINK_NEW_CODE_LABEL = "🔑 Получить код"
LINK_IMPORT_LABEL = "📥 Перенести авто из Telegram"
MENU_LABEL = "🏠 Главное меню"
PAYMENT_HELP_LABEL = "💳 Оплатить в рублях"
INSURANCE_LABEL = "🚗 ОСАГО Грузии"
MANAGER_STATS_LABEL = "📊 Статистика MAX"
MANAGER_SEARCH_LABEL = "🔎 Поиск авто"


def action(name: str, subscription_id: int) -> str:
    return f"{PREFIX}{name}:{subscription_id}"


def parse(payload: str | None) -> tuple[str, int | None] | None:
    """("cars", None) / ("chk", 12) / None — не payload этого бота."""
    if not payload or not payload.startswith(PREFIX):
        return None
    parts = payload[len(PREFIX):].split(":")
    if len(parts) == 1 and parts[0]:
        return parts[0], None
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0], int(parts[1])
    return None


def main_menu(*, is_manager: bool = False) -> Keyboard:
    rows = [
        [callback(ge_texts.ADD_CAR_LABEL, ADD), callback(ge_texts.MY_CARS_LABEL, CARS)],
        [callback(ge_texts.CHECK_NOW_LABEL, CHECK)],
        [callback(ge_texts.HELP_LABEL, HELP), callback(LINK_LABEL, LINK)],
    ]
    if is_manager:
        rows.append([callback(MANAGER_STATS_LABEL, STATS), callback(MANAGER_SEARCH_LABEL, SEARCH)])
    return rows


def back_to_menu() -> Keyboard:
    return [[callback(MENU_LABEL, MENU)]]


def cancel() -> Keyboard:
    return [[callback(ge_texts.CANCEL_BUTTON_LABEL, CANCEL)]]


def add_confirm(car_number: str) -> Keyboard:
    # Номер в payload не нужен: что добавлять, решает состояние диалога.
    return [[callback(ge_texts.ADD_CAR_CONFIRM_LABEL, "ge:addok"), callback(ge_texts.CANCEL_BUTTON_LABEL, CANCEL)]]


def cars_list(subscriptions: list[MaxGeSubscription], *, prefix: str = "car") -> Keyboard:
    rows = []
    for subscription in subscriptions[:25]:
        state = "🟢" if subscription.is_active else "⚪"
        label = f"{state} {subscription.car_number}"
        rows.append([callback(label, action(prefix, subscription.id))])
    rows.append([callback(MENU_LABEL, MENU)])
    return rows


def car_card(subscription: MaxGeSubscription, *, payable_enabled: bool) -> Keyboard:
    rows = [[callback(ge_texts.CHECK_NOW_LABEL, action("chk", subscription.id))]]
    if payable_enabled:
        rows.append([
            callback(PAYABLE_LABEL, action("pay", subscription.id)),
            callback(PAID_LABEL, action("paid", subscription.id)),
        ])
    toggle = (
        callback(ge_texts.TURN_OFF_BUTTON_LABEL, action("off", subscription.id))
        if subscription.is_active
        else callback(ge_texts.TURN_ON_BUTTON_LABEL, action("on", subscription.id))
    )
    rows.append([toggle])
    rows.append([callback(ge_texts.DELETE_CAR_BUTTON_LABEL, action("del", subscription.id))])
    rows.append([callback(ge_texts.BACK_BUTTON_LABEL, CARS)])
    return rows


def delete_confirm(subscription_id: int) -> Keyboard:
    return [[
        callback(ge_texts.DELETE_CAR_CONFIRM_BUTTON_LABEL, action("delok", subscription_id)),
        callback(ge_texts.CANCEL_BUTTON_LABEL, action("car", subscription_id)),
    ]]


def payment_cta(url: str | None) -> Keyboard | None:
    if not url:
        return None
    return [[link(PAYMENT_HELP_LABEL, url), link(INSURANCE_LABEL, url)]]


def amount_card(subscription_id: int, payment_url: str | None, *, include_payment: bool = True) -> Keyboard:
    rows = [[callback(REFRESH_BUTTON_LABEL, action("amt", subscription_id))]]
    if include_payment and payment_url:
        rows.extend(payment_cta(payment_url))
    return rows


def check_result(subscription_id: int, *, refresh: bool, payment_url: str | None, show_payment: bool) -> Keyboard:
    rows = []
    if refresh:
        rows.append([callback(REFRESH_BUTTON_LABEL, action("amt", subscription_id))])
    if show_payment and payment_url:
        rows.extend(payment_cta(payment_url))
    rows.append([callback(ge_texts.BACK_BUTTON_LABEL, action("car", subscription_id))])
    return rows


def link_menu(*, linked: bool) -> Keyboard:
    rows = [[callback(LINK_NEW_CODE_LABEL, LINK_NEW_CODE)]]
    if linked:
        rows.append([callback(LINK_IMPORT_LABEL, LINK_IMPORT)])
    rows.append([callback(MENU_LABEL, MENU)])
    return rows
