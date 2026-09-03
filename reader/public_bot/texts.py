"""Тексты/форматирование сообщений @GEShtrafbot — отдельно от
reader/public_bot/conversation.py (шаги диалога) и
reader/public_bot/keyboards.py (Telethon-кнопки), чтобы формулировки не
были размазаны по нескольким файлам. Никакого Telegram/БД здесь нет —
чистые функции над уже готовыми значениями.
"""

from datetime import date

from reader.public_bot.models import FineMonitoringSubscription

MAIN_MENU_TEXT = "🚗 Штрафы Грузии 🇬🇪"

ADD_CAR_LABEL = "➕ Добавить авто"
MY_CARS_LABEL = "📋 Мои авто"
CHECK_NOW_LABEL = "🔎 Проверить сейчас"
STOP_LABEL = "⛔ Остановить мониторинг"

CAR_NUMBER_PROMPT = "🚗 Введите госномер автомобиля\n\nНапример: M295YB196"
USERNAME_PROMPT = "👤 Введите ваш Telegram-логин\n\nНапример: @VeronaWarm"
PERIOD_PROMPT = "📅 Выберите срок мониторинга"

# "Проверить сейчас"/"Остановить мониторинг" реализуются в следующем этапе
# (см. Stage 2 report) — сейчас это единственная реакция на нажатие,
# без выбора конкретного авто и без callback вообще.
COMING_SOON_TEXT = "🚧 Функция скоро будет доступна"

STALE_DIALOG_TEXT = "⚠️ Диалог устарел, начните заново."
NO_CARS_TEXT = "У вас пока нет добавленных автомобилей."

_DATE_FORMAT = "%d.%m.%Y"


def _fmt_date(value: date) -> str:
    return value.strftime(_DATE_FORMAT)


def format_add_car_summary(
    *,
    car_number: str,
    username: str,
    start_date: date,
    end_date: date,
    check_ok: bool,
    new_fines_count: int,
) -> str:
    """Итог Add Car flow, показываемый клиенту — не путать с операторским
    reader/commands/fine.py::_format_add_summary (другой текст/аудитория,
    но тот же принцип: короткая факт-строка, без дублирования детального
    уведомления о самом штрафе — его здесь на этом этапе ещё нет вовсе,
    см. Stage 2 report)."""
    period = f"{_fmt_date(start_date)} — {_fmt_date(end_date)}"

    if not check_ok:
        return "\n".join([
            "⚠️ Автомобиль добавлен на мониторинг,",
            "но проверить штрафы сейчас не удалось",
            "",
            f"🚗 {car_number}",
            f"👤 @{username}",
            f"📅 Мониторинг: {period}",
        ])

    check_line = (
        f"🔎 Штрафы проверены: найдено новых — {new_fines_count}"
        if new_fines_count
        else "🔎 Штрафы проверены: новых штрафов нет"
    )
    return "\n".join([
        "✅ Автомобиль добавлен на мониторинг",
        "",
        f"🚗 {car_number}",
        f"👤 @{username}",
        f"📅 Мониторинг: {period}",
        check_line,
    ])


def _status_label(subscription: FineMonitoringSubscription, today: date) -> str:
    """Вычисляется на лету из status+end_date, а НЕ только из status — та
    же логика, что и FineMonitoringSubscription.is_effectively_active:
    подписка с прошедшим end_date показывается как "Истёк", даже если
    expire_elapsed() ещё ни разу не прошёлся по этой строке (см. Stage 1
    design про lifecycle подписки)."""
    if subscription.status == "stopped":
        return "⛔ Остановлен"
    if subscription.end_date < today:
        return "⏱ Истёк"
    return "✅ Активен"


def format_my_cars(subscriptions: list[FineMonitoringSubscription], today: date) -> str:
    if not subscriptions:
        return NO_CARS_TEXT

    blocks = [
        "\n".join([
            f"🚗 {sub.car_number}",
            f"📅 до {_fmt_date(sub.end_date)}",
            _status_label(sub, today),
        ])
        for sub in subscriptions
    ]
    return "\n\n".join(blocks)
