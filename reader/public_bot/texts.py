"""Тексты/форматирование сообщений @GEShtrafbot — отдельно от
reader/public_bot/conversation.py (шаги диалога) и
reader/public_bot/keyboards.py (Telethon-кнопки), чтобы формулировки не
были размазаны по нескольким файлам. Никакого Telegram/БД здесь нет —
чистые функции над уже готовыми значениями.
"""

from datetime import date

from reader.fines.models import FineMonitoringTask
from reader.public_bot.models import FineMonitoringSubscription

MAIN_MENU_TEXT = "🚗 Штрафы Грузии 🇬🇪"

ADD_CAR_LABEL = "➕ Добавить авто"
MY_CARS_LABEL = "📋 Мои авто"
CHECK_NOW_LABEL = "🔎 Проверить сейчас"
STOP_LABEL = "⛔ Остановить мониторинг"

CAR_NUMBER_PROMPT = "🚗 Введите госномер автомобиля\n\nНапример: M295YB196"
USERNAME_PROMPT = "👤 Введите ваш Telegram-логин\n\nНапример: @VeronaWarm"
# Trusted-operator delegated flow (см. design report) — ВСЕГДА запрашивается
# после номера авто у пользователей из trusted_operator_user_ids, вместо
# USERNAME_PROMPT выше. Может быть указан и собственный username trusted-
# оператора, если он ставит на мониторинг свой же автомобиль.
OWNER_USERNAME_PROMPT = "👤 Укажите Telegram владельца автомобиля\n\nНапример: @VeronaWarm"
# Trusted-operator flow — ПЕРЕД OWNER_USERNAME_PROMPT (см. design: username
# клиента больше не обязателен для постановки машины на мониторинг). "OK" →
# OWNER_USERNAME_PROMPT; "Отмена" → мониторинг без клиента (см.
# reader/public_bot/subscription_service.py::add_delegated_car_without_client).
ADD_CLIENT_DECISION_PROMPT = "👤 Добавить Telegram клиента?"
PERIOD_PROMPT = "📅 Выберите срок мониторинга"

STALE_DIALOG_TEXT = "⚠️ Диалог устарел, начните заново."
NO_CARS_TEXT = "У вас пока нет добавленных автомобилей."

# 🔎 Проверить сейчас / ⛔ Остановить мониторинг (см. design report Stage 4).
NO_ACTIONABLE_CARS_TEXT = "У вас нет автомобилей, с которыми можно выполнить это действие."
CHECK_NOW_PICK_PROMPT = "🔎 Выберите автомобиль для проверки:"
STOP_PICK_PROMPT = "⛔ Выберите автомобиль для остановки мониторинга:"
STOP_CONFIRM_PROMPT = "Остановить мониторинг для {car_number}?"
STOP_FAILED_TEXT = "⚠️ Не удалось остановить — попробуйте ещё раз через «⛔ Остановить мониторинг»."
CALLBACK_NOT_AUTHORIZED_TEXT = "Это действие недоступно — начните заново через меню."

# Trusted-operator task-level admin (см. design report: пересмотр
# архитектуры — fine_monitoring_tasks остаётся source of truth, subscription
# для этих трёх пунктов меню НЕ требуется вовсе).
NO_ACTIVE_TASKS_TEXT = "Активных задач мониторинга нет."
TRUSTED_TASKS_HEADER = "📋 Все активные автомобили под мониторингом:"
TRUSTED_CHECK_NOW_PICK_PROMPT = "🔎 Выберите автомобиль для проверки:"
TRUSTED_STOP_PICK_PROMPT = "⛔ Выберите автомобиль для остановки мониторинга:"
TRUSTED_STOP_FAILED_TEXT = "⚠️ Не удалось остановить — попробуйте ещё раз через «⛔ Остановить мониторинг»."
_TRUSTED_STOP_CONFIRM_PROMPT_NO_CLIENTS = "Остановить мониторинг для {car_number}?"
_TRUSTED_STOP_CONFIRM_PROMPT_ONE_CLIENT = (
    "⚠️ Автомобиль {car_number} также отслеживается клиентом.\n"
    "Остановка прекратит мониторинг автомобиля для всех."
)
_TRUSTED_STOP_CONFIRM_PROMPT_MANY_CLIENTS = (
    "⚠️ Автомобиль {car_number} также отслеживается клиентами.\n"
    "Остановка прекратит мониторинг автомобиля для всех."
)
_TRUSTED_STOP_CONFIRM_BUTTON_NO_CLIENTS = "⛔ Остановить"
_TRUSTED_STOP_CONFIRM_BUTTON_WITH_CLIENTS = "⛔ Остановить для всех"


def format_check_now_result(outcome) -> str:
    """outcome: reader.public_bot.subscription_service.CheckNowOutcome.
    Без технической детали ошибки в тексте клиенту (см. design: та же
    осторожность, что и в format_add_car_summary/
    format_delegated_add_car_summary)."""
    if not outcome.check_ok:
        return f"⚠️ Проверить штрафы для {outcome.car_number} сейчас не удалось. Попробуйте позже."
    if outcome.new_fines_count:
        return f"🔎 {outcome.car_number}: найдено новых штрафов — {outcome.new_fines_count}"
    return f"🔎 {outcome.car_number}: новых штрафов нет"


def format_stop_success(car_number: str) -> str:
    return f"✅ Мониторинг для {car_number} остановлен."

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


def format_delegated_add_car_summary(
    *,
    car_number: str,
    owner_username: str,
    start_date: date,
    end_date: date,
    check_ok: bool,
    new_fines_count: int,
    pending_claim: bool,
    claim_link: str | None,
) -> str:
    """Итог trusted-operator delegated Add Car flow — показывается
    ТОЛЬКО trusted-оператору (владельцу это же событие ничего не
    показывает, пока он не claimed, см. design report)."""
    period = f"{_fmt_date(start_date)} — {_fmt_date(end_date)}"

    if not check_ok:
        lines = [
            "⚠️ Автомобиль добавлен на мониторинг,",
            "но проверить штрафы сейчас не удалось",
            "",
            f"🚗 {car_number}",
            f"👤 Владелец: @{owner_username}",
            f"📅 Мониторинг: {period}",
        ]
    else:
        check_line = (
            f"🔎 Штрафы проверены: найдено новых — {new_fines_count}"
            if new_fines_count
            else "🔎 Штрафы проверены: новых штрафов нет"
        )
        lines = [
            "✅ Автомобиль добавлен на мониторинг",
            "",
            f"🚗 {car_number}",
            f"👤 Владелец: @{owner_username}",
            f"📅 Мониторинг: {period}",
            check_line,
        ]

    if pending_claim and claim_link:
        lines += [
            "",
            "⚠️ Не удалось однозначно связать этого пользователя с ботом — "
            "Telegram не позволяет боту первым написать тому, кто ни разу "
            "не открывал с ним диалог.",
            "Перешлите владельцу эту ссылку — как только он откроет её, "
            "бот сможет присылать штрафы лично ему:",
            claim_link,
        ]

    return "\n".join(lines)


def format_delegated_add_car_without_client_summary(
    *,
    car_number: str,
    start_date: date,
    end_date: date,
    check_ok: bool,
    new_fines_count: int,
) -> str:
    """Итог trusted-operator delegated Add Car БЕЗ клиента ("Отмена" на
    ADD_CLIENT_DECISION_PROMPT) — как format_delegated_add_car_summary, но
    без строки "👤 Владелец: ..." — клиента нет и, возможно, не будет
    никогда (см. design report: username клиента не обязателен)."""
    period = f"{_fmt_date(start_date)} — {_fmt_date(end_date)}"

    if not check_ok:
        return "\n".join([
            "⚠️ Автомобиль добавлен на мониторинг,",
            "но проверить штрафы сейчас не удалось",
            "",
            f"🚗 {car_number}",
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
        f"📅 Мониторинг: {period}",
        check_line,
    ])


CLAIM_SUCCESS_TEXT = (
    "✅ Готово! Теперь уведомления о штрафах по автомобилю {car_number} "
    "будут приходить вам лично через @{bot_username}."
)
CLAIM_INVALID_TEXT = "⚠️ Ссылка недействительна, уже использована или устарела."
OWNER_RESOLUTION_ERROR_TEXT = (
    "❌ Не удалось проверить Telegram-пользователя (техническая ошибка). "
    "Попробуйте ещё раз через «➕ Добавить авто»."
)


def _status_label(subscription: FineMonitoringSubscription, today: date) -> str:
    """Вычисляется на лету из status+end_date, а НЕ только из status — та
    же логика, что и FineMonitoringSubscription.is_effectively_active:
    подписка с прошедшим end_date показывается как "Истёк", даже если
    expire_elapsed() ещё ни разу не прошёлся по этой строке (см. Stage 1
    design про lifecycle подписки)."""
    if subscription.status == "pending_claim":
        return "⏳ Ждём подтверждения владельцем"
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


MANAGED_CARS_HEADER = "📋 Добавлено вами для других пользователей:"
NO_MANAGED_CARS_TEXT = "Вы пока не добавляли автомобили для других пользователей."


def format_managed_cars(subscriptions: list[FineMonitoringSubscription], today: date) -> str:
    """"Мои авто" для trusted-оператора показывает delegated-подписки
    ОТДЕЛЬНЫМ разделом (см. design report) — эта функция форматирует
    только этот раздел; собственные подписки оператора — тот же
    format_my_cars(), что и у обычного пользователя."""
    if not subscriptions:
        return NO_MANAGED_CARS_TEXT

    blocks = []
    for sub in subscriptions:
        owner_display = f"@{sub.telegram_username}" if sub.telegram_username else f"@{sub.owner_username_hint}"
        blocks.append(
            "\n".join([
                f"🚗 {sub.car_number}",
                f"👤 Владелец: {owner_display}",
                f"📅 до {_fmt_date(sub.end_date)}",
                _status_label(sub, today),
            ])
        )
    return "\n\n".join(blocks)


def _format_trusted_task_line(task: FineMonitoringTask) -> str:
    lines = [f"🚗 {task.car_number}" + (f" ({task.label})" if task.label else "")]
    lines.append(f"📅 {_fmt_date(task.start_date)} — {_fmt_date(task.end_date)}")
    if task.last_checked_at is not None:
        lines.append(f"🔎 Последняя проверка: {_fmt_date(task.last_checked_at.date())}")
    else:
        lines.append("🔎 Ещё не проверялась")
    return "\n".join(lines)


def format_trusted_tasks_list(tasks: list[FineMonitoringTask], *, limit: int) -> str:
    """"📋 Мои авто" для trusted-оператора — ВСЕ активные
    fine_monitoring_tasks (см. design report), не только те, у которых
    есть fine_monitoring_subscription. limit — защита от превышения лимита
    длины Telegram-сообщения на production-масштабе (250+ активных задач,
    см. design report про известное упрощение) — НЕ бизнес-правило."""
    if not tasks:
        return NO_ACTIVE_TASKS_TEXT

    shown = tasks[:limit]
    blocks = [_format_trusted_task_line(task) for task in shown]
    text = TRUSTED_TASKS_HEADER + "\n\n" + "\n\n".join(blocks)
    if len(tasks) > limit:
        text += f"\n\n… показаны первые {limit} из {len(tasks)}."
    return text


def format_trusted_stop_confirm_prompt(car_number: str, subscriber_count: int) -> str:
    """Текст подтверждения ⛔ для trusted-оператора — честно предупреждает,
    если у задачи есть ещё actionable (active/pending_claim) client-
    подписки (см. design report): формулировка корректно отражает
    единственное/множественное число получателей."""
    if subscriber_count == 0:
        return _TRUSTED_STOP_CONFIRM_PROMPT_NO_CLIENTS.format(car_number=car_number)
    template = (
        _TRUSTED_STOP_CONFIRM_PROMPT_ONE_CLIENT if subscriber_count == 1
        else _TRUSTED_STOP_CONFIRM_PROMPT_MANY_CLIENTS
    )
    return template.format(car_number=car_number)


def trusted_stop_confirm_button_label(subscriber_count: int) -> str:
    return (
        _TRUSTED_STOP_CONFIRM_BUTTON_NO_CLIENTS if subscriber_count == 0
        else _TRUSTED_STOP_CONFIRM_BUTTON_WITH_CLIENTS
    )
