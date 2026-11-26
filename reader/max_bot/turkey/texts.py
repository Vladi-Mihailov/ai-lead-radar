"""Тексты MAX-бота Турции, которых нет у Telegram-бота (приветствие и
справка для MAX, привязка, экраны менеджера). Результат проверки, карточка,
история и уведомления об изменениях — общие reader/turkey_bot/texts.py и
reader/turkey_bot/monitoring/notification_texts.py без изменений.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from reader.max_bot.linking import format_code
from reader.max_bot.turkey.repository import MaxTrCar
from reader.max_bot.turkey.service import PlateOverview
from reader.plate_display import format_plate_for_telegram
from reader.turkey_bot import texts as tr_texts
from reader.turkey_bot.models import TurkeyUserCar
from reader.turkey_bot.unified.models import ProviderStatus

MENU_TEXT = tr_texts.MAIN_MENU_TEXT
GREETING_TEXT = (
    f"{tr_texts.MAIN_MENU_TEXT}\n\n"
    "Проверка по гос. номеру одним действием: штрафы GİB, платные дороги Avrasya Tüneli и KGM.\n\n"
    "➕ Добавьте автомобиль — его можно сразу проверить и включить мониторинг: "
    "раз в день бот сообщит о новой, изменившейся или погашенной задолженности.\n\n"
    "Можно просто отправить номер сообщением."
)
HELP_TEXT = (
    "ℹ️ Справка\n\n"
    "• ➕ Добавить авто — отправьте гос. номер (например, A123AA123).\n"
    "• 📋 Мои авто — автомобили: 🟢 мониторинг включён, ⚪ выключен.\n"
    "• 🔎 Проверить сейчас — мгновенная проверка GİB, Avrasya Tüneli и KGM.\n"
    "• ▶️/⏹ Мониторинг — проверка раз в день (вечером по времени Турции) с уведомлением, "
    "если задолженность появилась, изменилась или погашена.\n"
    "• 📜 История — прошлые проверки номера.\n"
    "• 🔗 Telegram — привязать аккаунт Telegram и перенести автомобили.\n\n"
    "🚔 GİB — дорожные штрафы. 🚇 Avrasya Tüneli и 🚧 KGM — платные дороги, мосты и тоннели.\n\n"
    "Уведомления в MAX и в Telegram настраиваются отдельно."
)
UNKNOWN_TEXT = "Не понял сообщение. Отправьте гос. номер или выберите действие в меню."
STALE_BUTTON_TEXT = "Кнопка устарела — откройте меню заново."
NOT_YOURS_TEXT = tr_texts.CAR_NOT_FOUND_TEXT
CHECKING_TEXT = "⏳ Проверяю GİB, Avrasya Tüneli и KGM — это может занять пару минут…"
IN_PROGRESS_TEXT = "⏳ Этот номер уже проверяется, дождитесь результата."
MANAGER_ONLY_TEXT = "Раздел доступен только менеджерам."
LINK_UNAVAILABLE_TEXT = "Привязка Telegram пока недоступна."
LINK_REQUIRED_TEXT = "Сначала привяжите аккаунт Telegram: «🔗 Telegram» → «🔑 Получить код»."
SEARCH_PROMPT = "🔎 Введите гос. номер для поиска."
CHECK_PICK_PROMPT = "🔎 Выберите автомобиль для проверки:"


def as_user_car(car: MaxTrCar) -> TurkeyUserCar:
    """MAX-машина в виде модели Telegram-бота — чтобы общие тексты карточки
    и кнопок списка выглядели одинаково в обоих мессенджерах."""
    return TurkeyUserCar(
        id=car.id, telegram_user_id=0, car_number=car.plate, created_at=car.created_at,
        last_checked_at=car.last_checked_at or car.created_at,
        last_overall_status=car.last_overall_status, last_total_amount=car.last_total_amount,
    )


def format_car_card(car: MaxTrCar, *, monitoring_active: bool) -> str:
    return tr_texts.format_car_card_text(as_user_car(car), monitoring_active=monitoring_active)


def format_car_button(car: MaxTrCar, *, monitoring_active: bool) -> str:
    return tr_texts.format_car_button_label(as_user_car(car), monitoring_active=monitoring_active)


def format_car_added(car: MaxTrCar, *, created: bool, monitoring_active: bool) -> str:
    template = tr_texts.CAR_ADDED_TEMPLATE if created else tr_texts.CAR_ALREADY_EXISTS_TEMPLATE
    return f"{template.format(plate=car.plate)}\n\n{format_car_card(car, monitoring_active=monitoring_active)}"


def format_link_menu(*, linked: bool, telegram_bot: str) -> str:
    state = "✅ Аккаунт Telegram привязан." if linked else "Аккаунт Telegram не привязан."
    return (
        f"🔗 Привязка Telegram\n\n{state}\n\n"
        f"Привязка нужна, чтобы перенести автомобили из @{telegram_bot}. "
        "Автомобили и мониторинг в Telegram при этом не меняются."
    )


def format_link_code(code: str, *, telegram_bot: str) -> str:
    return (
        "🔑 Код привязки\n\n"
        f"Отправьте боту @{telegram_bot} в Telegram сообщение:\n"
        f"`/max_link {format_code(code)}`\n\n"
        "Код действует 15 минут и подходит только один раз. Никому его не пересылайте."
    )


def format_import_result(imported: list[str], skipped: int) -> str:
    if not imported:
        text = "В привязанном Telegram-аккаунте нет автомобилей для переноса."
    else:
        lines = ["📥 Перенесено в MAX:", ""]
        lines += [f"• {format_plate_for_telegram(plate)}" for plate in imported]
        lines += ["", "Мониторинг включён там, где он был включён в Telegram; уже известная "
                      "задолженность повторно не отправляется."]
        text = "\n".join(lines)
    if skipped:
        text += f"\nУже были в списке: {skipped}."
    return text


def format_manager_stats(*, users: int, cars: int, monitoring: dict[str, int]) -> str:
    return "\n".join([
        "📊 MAX — штрафы Турции",
        "",
        f"👤 Пользователей: {users}",
        f"🚗 Автомобилей: {cars}",
        f"🔔 Мониторинг: включён {monitoring.get('active', 0)}, выключен {monitoring.get('inactive', 0)}",
        "",
        "Полная статистика и список задолженности — в менеджерском интерфейсе Telegram-бота.",
    ])


_PROVIDER_NAMES = {"gib": "🚔 GİB", "avrasya": "🚇 Avrasya", "kgm": "🚧 KGM"}
_DISPLAY_TZ = ZoneInfo("Europe/Istanbul")


def _format_provider_state(provider: str, latest) -> str:
    name = _PROVIDER_NAMES[provider]
    if latest is None:
        return f"{name}: успешных проверок нет"
    result = latest.result
    when = _format_moment(latest.finished_at)
    if result.status == ProviderStatus.HAS_DEBT:
        return f"{name}: задолженность {tr_texts._format_try_amount(result.total_amount)} ({when})"
    return f"{name}: задолженности нет ({when})"


def _format_moment(moment: datetime) -> str:
    return moment.astimezone(_DISPLAY_TZ).strftime("%d.%m %H:%M")


def format_manager_search(overview: PlateOverview, *, telegram_subscribers: int) -> str:
    plate = format_plate_for_telegram(overview.plate)
    lines = [
        f"🔎 {plate}",
        "",
        f"MAX: в списках {overview.max_cars}, мониторинг {overview.max_monitoring}",
        f"Telegram: мониторинг {telegram_subscribers}",
        "",
    ]
    lines += [_format_provider_state(provider, overview.latest[provider]) for provider in ("gib", "avrasya", "kgm")]
    return "\n".join(lines)
