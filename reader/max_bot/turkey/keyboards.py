"""Клавиатуры MAX-бота Турции. Подписи — те же константы, что у Telegram-бота
(reader/turkey_bot/texts.py).

payload: "tr:<действие>[:<id>]" — id машины публичен, права проверяются по
user_id отправителя (MaxTurkeyRepository.get_owned_car). Payload Грузии
("ge:...") этим ботом не распознаётся.
"""

from __future__ import annotations

from reader.max_bot.keyboards import Keyboard, callback, link
from reader.max_bot.turkey.repository import MaxTrCar
from reader.turkey_bot import texts as tr_texts

PREFIX = "tr:"

MENU = "tr:menu"
ADD = "tr:add"
CARS = "tr:cars"
CHECK = "tr:check"
HELP = "tr:help"
LINK = "tr:link"
LINK_NEW_CODE = "tr:linkcode"
LINK_IMPORT = "tr:import"
CANCEL = "tr:cancel"
STATS = "tr:stats"
SEARCH = "tr:search"

MENU_LABEL = "🏠 Главное меню"
LINK_LABEL = "🔗 Telegram"
LINK_NEW_CODE_LABEL = "🔑 Получить код"
LINK_IMPORT_LABEL = "📥 Перенести авто из Telegram"
MANAGER_STATS_LABEL = "📊 Статистика MAX"
MANAGER_SEARCH_LABEL = "🔎 Поиск авто"
PAYMENT_HELP_LABEL = "💳 Оплатить в рублях"
INSURANCE_LABEL = "🚗 ОСАГО Турции"


def action(name: str, car_id: int) -> str:
    return f"{PREFIX}{name}:{car_id}"


def parse(payload: str | None) -> tuple[str, int | None] | None:
    """("cars", None) / ("chk", 12) / None — не payload этого бота."""
    if not payload or not payload.startswith(PREFIX):
        return None
    parts = payload[len(PREFIX):].split(":")
    if len(parts) == 1 and parts[0]:
        return parts[0], None
    if len(parts) == 2 and parts[0] and parts[1].isdigit():
        return parts[0], int(parts[1])
    return None


def main_menu(*, is_manager: bool = False) -> Keyboard:
    rows = [
        [callback(tr_texts.ADD_CAR_LABEL, ADD), callback(tr_texts.MY_CARS_LABEL, CARS)],
        [callback(tr_texts.CHECK_NOW_LABEL, CHECK)],
        [callback(tr_texts.HELP_LABEL, HELP), callback(LINK_LABEL, LINK)],
    ]
    if is_manager:
        rows.append([callback(MANAGER_STATS_LABEL, STATS), callback(MANAGER_SEARCH_LABEL, SEARCH)])
    return rows


def back_to_menu() -> Keyboard:
    return [[callback(MENU_LABEL, MENU)]]


def cancel() -> Keyboard:
    return [[callback(tr_texts.CANCEL_BUTTON_LABEL, CANCEL)]]


def cars_list(cars: list[MaxTrCar], labels: dict[int, str], *, prefix: str = "car") -> Keyboard:
    rows = [[callback(labels[car.id], action(prefix, car.id))] for car in cars[:25]]
    rows.append([callback(MENU_LABEL, MENU)])
    return rows


def car_card(car: MaxTrCar, *, monitoring_active: bool) -> Keyboard:
    toggle = (
        callback(tr_texts.DISABLE_MONITORING_LABEL, action("off", car.id))
        if monitoring_active
        else callback(tr_texts.ENABLE_MONITORING_LABEL, action("on", car.id))
    )
    return [
        [callback(tr_texts.CHECK_NOW_LABEL, action("chk", car.id))],
        [toggle],
        [callback(tr_texts.HISTORY_LABEL, action("hist", car.id))],
        [callback(tr_texts.DELETE_CAR_LABEL, action("del", car.id))],
        [callback(tr_texts.BACK_LABEL, CARS)],
    ]


def delete_confirm(car_id: int) -> Keyboard:
    return [[
        callback(tr_texts.DELETE_CAR_CONFIRM_BUTTON_LABEL, action("delok", car_id)),
        callback(tr_texts.DELETE_CANCEL_LABEL, action("car", car_id)),
    ]]


def back_to_car(car_id: int) -> Keyboard:
    return [[callback(tr_texts.BACK_LABEL, action("car", car_id))]]


def check_result(car_id: int, *, payment_url: str | None, show_payment: bool) -> Keyboard:
    rows: Keyboard = []
    if show_payment and payment_url:
        rows.append([link(PAYMENT_HELP_LABEL, payment_url), link(INSURANCE_LABEL, payment_url)])
    rows.extend(back_to_car(car_id))
    return rows


def link_menu(*, can_import: bool) -> Keyboard:
    rows = [[callback(LINK_NEW_CODE_LABEL, LINK_NEW_CODE)]]
    if can_import:
        rows.append([callback(LINK_IMPORT_LABEL, LINK_IMPORT)])
    rows.append([callback(MENU_LABEL, MENU)])
    return rows
