"""Тексты/форматирование сообщений @GEShtrafbot — отдельно от
reader/public_bot/conversation.py (шаги диалога) и
reader/public_bot/keyboards.py (Telethon-кнопки), чтобы формулировки не
были размазаны по нескольким файлам. Никакого Telegram/БД здесь нет —
чистые функции над уже готовыми значениями.
"""

from datetime import date, datetime

from reader.fines.models import FineMonitoringTask, NewFineEvent
from reader.public_bot.delivery_texts import format_check_now_fines_message
from reader.public_bot.models import FineMonitoringSubscription
from reader.public_bot.statistics_service import BotStatistics

MAIN_MENU_TEXT = "🚗 Штрафы Грузии 🇬🇪"

ADD_CAR_LABEL = "➕ Добавить авто"
MY_CARS_LABEL = "📋 Мои авто"
CHECK_NOW_LABEL = "🔎 Проверить сейчас"
# ⛔ Остановить мониторинг — БОЛЬШЕ НЕ показывается в manager main menu
# (см. задачу "manager/trusted Search" п.1/п.16: заменена на SEARCH_LABEL
# ниже) — константа и вся её underlying-логика (_handle_menu_label/
# _build_trusted_stop_picker_reply/handle_trusted_stop_pick и т.д.)
# сознательно НЕ удалены (задача явно требует "underlying functionality
# не удалять") — per-car ON/OFF в manager "📋 Мои авто" [STATUS] кнопке
# теперь единственный штатный путь остановки мониторинга конкретной
# машины.
STOP_LABEL = "⛔ Остановить мониторинг"
# Manager/trusted Search (см. задачу) — заменяет STOP_LABEL в manager main
# menu (см. reader/public_bot/keyboards.py::main_menu_keyboard).
SEARCH_LABEL = "🔎 Поиск"
# Trusted-operator-only (см. design report про "📊 Статистика") — кнопка
# добавляется в главное меню ТОЛЬКО для trusted_operator_user_ids (см.
# reader/public_bot/keyboards.py::main_menu_keyboard(include_statistics=...)
# и reader/public_bot/handlers.py) — обычный клиент её никогда не видит.
STATISTICS_LABEL = "📊 Статистика"

# Переход в Turkey-бот (см. design report "унификация UI") —
# TURKEY_BOT_LINK_LABEL теперь ОБЫЧНАЯ reply-кнопка в главном меню (ROW 2,
# см. reader/public_bot/keyboards.py::main_menu_keyboard), видна ВСЕМ
# пользователям одинаково (не trusted-gated, в отличие от STATISTICS_LABEL
# выше). Нажатие распознаётся как текст (см.
# reader/public_bot/conversation.py::_handle_menu_label) и отвечает
# ОТДЕЛЬНЫМ сообщением (TURKEY_BOT_LINK_TEXT) с inline URL-кнопкой (см.
# turkey_bot_link_keyboard) — Telegram reply-кнопки физически не могут
# сами быть URL-кнопками. @ProtocolTRbot — реальный, уже подключённый
# username Turkey-бота (см. reader/turkey_bot/main.py).
TURKEY_BOT_LINK_LABEL = "🇹🇷 Штрафы Турции"
TURKEY_BOT_LINK_TEXT = "🇹🇷 Проверка штрафов и платных дорог Турции"
TURKEY_BOT_URL = "https://t.me/ProtocolTRbot"

CAR_NUMBER_PROMPT = "🚗 Введите госномер автомобиля\n\nНапример: M295YB196"
# Self-service (обычный клиент) больше НЕ имеет своего username-prompt (см.
# задачу "Georgia должен работать с Telegram identity так же, как Turkey")
# — telegram_user_id достаточен как identity, username — только auto-
# captured metadata, если Telegram его отдал. OWNER_USERNAME_PROMPT ниже
# относится ИСКЛЮЧИТЕЛЬНО к delegated-флоу (оператор указывает клиента) и
# НЕ затронут этой задачей.
#
# Trusted-operator delegated flow (см. design report) — ВСЕГДА запрашивается
# после номера авто у пользователей из trusted_operator_user_ids (self-
# service username-prompt выше больше не существует, см. комментарий про
# CAR_NUMBER_PROMPT). Может быть указан и собственный username trusted-
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

# 🔎 Проверить сейчас (см. design report Stage 4) — единственный
# оставшийся потребитель picker'а по subscription_id; "⛔ Остановить
# мониторинг" для обычного пользователя заменена car-centric ON/OFF (см.
# "📋 Мои авто" ниже) — STOP_PICK_PROMPT/STOP_CONFIRM_PROMPT/
# STOP_FAILED_TEXT удалены вместе с handle_stop_pick/handle_stop_confirm
# (см. design report про переработку UX, п.8 "старая кнопка Stop").
NO_ACTIONABLE_CARS_TEXT = "У вас нет автомобилей, с которыми можно выполнить это действие."
CHECK_NOW_PICK_PROMPT = "🔎 Выберите автомобиль для проверки:"
CALLBACK_NOT_AUTHORIZED_TEXT = "Это действие недоступно — начните заново через меню."

# Trusted-operator task-level admin (см. design report: пересмотр
# архитектуры — fine_monitoring_tasks остаётся source of truth, subscription
# для этих трёх пунктов меню НЕ требуется вовсе).
NO_ACTIVE_TASKS_TEXT = "Активных задач мониторинга нет."
# 📋 Мои авто — пагинирован (см. design report: hard cap "первые 50 из N"
# убран, доступны ВСЕ задачи, включая OFF, по 10 на страницу, см. design
# report про per-car ON/OFF toggle — заголовок больше не говорит
# "активные", т.к. список теперь показывает и остановленные/завершённые
# задачи тоже, просто со статусом ⚪ OFF).
TRUSTED_TASKS_HEADER = "📋 Мои авто"

TRUSTED_TASK_PERIOD_PROMPT = "📅 На какой срок продолжить мониторинг?"
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
    format_delegated_add_car_summary).

    Manual "🔎 Проверить сейчас" — read/display семантика ТЕКУЩЕГО
    состояния машины (см. задачу про UX manual check), а не delta с
    прошлой проверки: "новых штрафов нет" здесь принципиально не
    показывается — outcome.fines это ВСЕ штрафы из ответа police.ge на
    этой проверке, новые и уже известные (см. CheckResult.current_fines)."""
    if not outcome.check_ok:
        return f"⚠️ Проверить штрафы для {outcome.car_number} сейчас не удалось. Попробуйте позже."
    if not outcome.fines:
        return f"🔎 {outcome.car_number}: штрафов не найдено"
    return format_check_now_fines_message(car_number=outcome.car_number, fines=outcome.fines)


def format_stop_success(car_number: str) -> str:
    return f"✅ Мониторинг для {car_number} остановлен."

_DATE_FORMAT = "%d.%m.%Y"


def _fmt_date(value: date) -> str:
    return value.strftime(_DATE_FORMAT)


def format_add_car_summary(
    *,
    car_number: str,
    start_date: date,
    end_date: date,
    check_ok: bool,
    new_fines_count: int,
) -> str:
    """Итог Add Car flow, показываемый клиенту — не путать с операторским
    reader/commands/fine.py::_format_add_summary (другой текст/аудитория,
    но тот же принцип: короткая факт-строка, без дублирования детального
    уведомления о самом штрафе — его здесь на этом этапе ещё нет вовсе,
    см. Stage 2 report).

    Без строки "👤 @username" (см. задачу "Georgia должен работать с
    Telegram identity так же, как Turkey") — self-service username больше
    не обязателен и часто отсутствует (None), показывать "@None" или
    вообще опускать строку по условию было бы непоследовательно; вместо
    этого строка убрана целиком для ВСЕХ self-service подтверждений,
    независимо от того, есть ли у клиента username — клиент и так знает
    свой Telegram-логин, это не новая для него информация."""
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


def car_monitoring_state(subscription: FineMonitoringSubscription, today: date) -> tuple[str, str]:
    """(emoji, state) для car-centric "📋 Мои авто" (см. design report про
    ON/OFF UX). status='pending_claim' сюда никогда не попадает — список
    строится ИСКЛЮЧИТЕЛЬНО из SubscriptionService.list_my_cars(), где
    telegram_user_id обязателен (pending_claim строки его не имеют, см.
    FineSubscriptionRepository.list_by_user).

    'stopped' — ВСЕГДА OFF, независимо от end_date: пользователь сам
    выключил мониторинг, это осознанное состояние, которое явно можно
    включить обратно одной кнопкой (см. SubscriptionService.turn_on_car).

    "ИСТЁК" — ОТДЕЛЬНОЕ, третье состояние (не ON и не OFF) для status=
    'expired' ИЛИ ещё 'active', но с уже прошедшим end_date (см.
    FineMonitoringSubscription.is_effectively_active) — время истекло
    само по себе, а не по решению пользователя, и "включить" его нельзя
    одной кнопкой (реактивация требует НОВОГО периода, см. design report:
    "не отображать expired как OFF автоматически")."""
    if subscription.status == "stopped":
        return "⚪", "OFF"
    if subscription.status == "active" and subscription.end_date >= today:
        return "🟢", "ON"
    return "⏱", "ИСТЁК"


def format_car_button_label(subscription: FineMonitoringSubscription, today: date) -> str:
    emoji, state = car_monitoring_state(subscription, today)
    return f"{emoji} {subscription.car_number} — {state}"


def format_owner_username_button(username: str | None) -> str:
    """Средняя из трёх кнопок manager/trusted-operator строки "📋 Мои
    авто" — [CAR_NUMBER] [@username] [STATUS] (см. задачу "унифицировать
    оба интерфейса", тот же формат, что и у Turkey bot). "@username", если
    владелец известен (auto-captured, см. bot_known_users), иначе "—"
    (единственное место, где решается ЭТОТ формат — задача явно требует
    "никогда @None/None/пустая кнопка", а Telegram inline-кнопка не может
    быть пустой строкой)."""
    return f"@{username}" if username else "—"


MY_CARS_HEADER = "🚗 Мои автомобили"


def format_car_details(subscription: FineMonitoringSubscription, today: date) -> str:
    emoji, state = car_monitoring_state(subscription, today)
    date_line = (
        f"📅 Истёк {_fmt_date(subscription.end_date)}" if state == "ИСТЁК"
        else f"📅 До {_fmt_date(subscription.end_date)}"
    )
    return "\n".join([
        f"🚗 {subscription.car_number}",
        f"Мониторинг: {emoji} {state}",
        date_line,
    ])


TURN_OFF_BUTTON_LABEL = "⏸ Выключить мониторинг"
TURN_ON_BUTTON_LABEL = "▶️ Включить мониторинг"
DELETE_CAR_BUTTON_LABEL = "🗑 Удалить автомобиль"
BACK_BUTTON_LABEL = "⬅️ Назад"
DELETE_CAR_CONFIRM_BUTTON_LABEL = "🗑 Да, удалить"
CANCEL_BUTTON_LABEL = "Отмена"

# Безопасный отказ для ON/OFF/Delete (см. design report: "все callback
# actions должны повторно проверять authorization server-side") — общий
# для "не найдено"/"не принадлежит"/"уже не в том состоянии"/гоночный
# конфликт — намеренно НЕ различает эти случаи в тексте пользователю (та
# же осторожность, что и у CALLBACK_NOT_AUTHORIZED_TEXT/STOP_FAILED_TEXT),
# только предлагает начать заново через актуальный список.
CAR_ACTION_FAILED_TEXT = (
    "⚠️ Не удалось выполнить действие — откройте автомобиль заново через «📋 Мои авто»."
)


def format_turn_off_success(car_number: str) -> str:
    return f"⏸ Мониторинг {car_number} выключен.\nАвтомобиль сохранён в «Мои авто»."


def format_turn_on_success(car_number: str) -> str:
    return f"▶️ Мониторинг {car_number} включён."


def format_delete_confirm_prompt(car_number: str) -> str:
    return (
        f"⚠️ Удалить {car_number} из списка?\n"
        "После удаления автомобиль исчезнет из «Мои авто»."
    )


def format_trusted_task_detail(task: FineMonitoringTask) -> str:
    """Карточка одной машины (см. design report про "убрать текстовый
    перечень из списка, показывать в карточке по нажатию на номер") —
    открывается по нажатию на кнопку-номер в списке "📋 Мои авто" (см.
    keyboards.py::trusted_tasks_page_keyboard/encode_trusted_task_open_
    callback). ON/OFF + период + последняя проверка — та же информация,
    что раньше была видна прямо в списке, теперь ТОЛЬКО здесь."""
    emoji, state = task_monitoring_state(task)
    lines = [f"🚗 {task.car_number}" + (f" ({task.label})" if task.label else "")]
    lines.append(f"Мониторинг: {emoji} {state}")
    lines.append(f"📅 {_fmt_date(task.start_date)} — {_fmt_date(task.end_date)}")
    if task.last_checked_at is not None:
        lines.append(f"🔎 Последняя проверка: {_fmt_date(task.last_checked_at.date())}")
    else:
        lines.append("🔎 Ещё не проверялась")
    return "\n".join(lines)


def format_trusted_tasks_page(tasks: list[FineMonitoringTask], *, page: int, total_pages: int) -> str:
    """"📋 Мои авто" для trusted-оператора — ОДНА страница (0-indexed page)
    из ВСЕХ fine_monitoring_tasks (см. design report: hard cap "первые 50
    из N" убран — все задачи доступны через пагинацию, 10 на страницу).
    Показ "Страница N из M" — 1-indexed для человека.

    БЕЗ текстового перечня машин (см. design report про переработку этого
    экрана в чистую inline keyboard — Telegram не позволяет разместить
    inline-кнопку справа от строки обычного текста, поэтому номер/ON-OFF
    каждой машины — ТОЛЬКО кнопки, см. keyboards.py::
    trusted_tasks_page_keyboard, а не текст здесь): период/последняя
    проверка конкретной машины теперь смотрят в её карточке (см.
    format_trusted_task_detail), а не в этом общем списке. tasks
    используется ТОЛЬКО для "список пуст" — держит тот же сигнатуру
    вызова, что и раньше, чтобы не трогать вызывающий код лишний раз."""
    if not tasks:
        return NO_ACTIVE_TASKS_TEXT

    return f"{TRUSTED_TASKS_HEADER}\nСтраница {page + 1} из {total_pages}"


def format_trusted_check_now_not_found(car_number: str) -> str:
    """Trusted-operator task-level 🔎 Проверить сейчас ПО НОМЕРУ (см.
    design report) — номер не найден среди активных fine_monitoring_tasks.
    Явное требование: ничего не добавляется автоматически, только эта
    ошибка."""
    return f"❌ Автомобиль {car_number} не найден в активном мониторинге."


def task_monitoring_state(task: FineMonitoringTask) -> tuple[str, str]:
    """(emoji, state) для manager-facing "📋 Мои авто" ON/OFF (см. design
    report про per-car monitoring toggle) — task-level, В ОТЛИЧИЕ от
    car_monitoring_state() выше (subscription-based, три состояния
    ON/OFF/ИСТЁК): здесь только ДВА состояния, ON/OFF, ровно как в задаче
    ("🟢 ON / ⚪ OFF", без отдельного "ИСТЁК") — status='active' САМ ПО
    СЕБЕ и есть ON (просроченный, но ещё не подхваченный FineJob'ом active
    — переходное состояние на секунды/минуты до следующего прогона
    FineJob, не отдельный UI-статус, см. reader/jobs/fine_job.py)."""
    if task.status == "active":
        return "🟢", "ON"
    return "⚪", "OFF"


def format_trusted_task_off_detail(task: FineMonitoringTask) -> str:
    """Экран "▶️ Продолжить мониторинг" (см. design report) — показывается
    после нажатия ⚪ OFF в списке, ИЛИ сразу после ON -> OFF toggle."""
    return f"🚗 {task.car_number}\nМониторинг: ⚪ OFF"


def format_trusted_task_turn_off_success(car_number: str) -> str:
    return f"⏸ Мониторинг {car_number} выключен."


def format_trusted_task_resume_success(car_number: str, days: int, end_date: date) -> str:
    """Подтверждение после выбора 15/30/90 дней (см. design report,
    ТОЧНЫЙ формат из задачи: "✅ Мониторинг {car} включён на {N} дней\\nДо:
    {дата}")."""
    return f"✅ Мониторинг {car_number} включён на {days} дней\nДо: {_fmt_date(end_date)}"


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


def format_statistics(stats: BotStatistics) -> str:
    """Trusted-operator-only "📊 Статистика" (см. design report) — все
    значения уже посчитаны BotStatisticsService.get_statistics(), здесь
    только форматирование."""
    return "\n".join([
        "📊 Статистика бота",
        "",
        f"👥 Всего пользователей: {stats.total_users}",
        f"🆕 Новых сегодня: {stats.new_users_today}",
        f"🆕 Новых за 7 дней: {stats.new_users_7d}",
        f"🆕 Новых за 30 дней: {stats.new_users_30d}",
        "",
        f"🚗 Активных подписок: {stats.active_subscriptions}",
        f"⏸ Остановленных подписок: {stats.stopped_subscriptions}",
        "",
        f"🚨 Новых штрафов найдено сегодня: {stats.new_fines_today}",
    ])


# ==== manager/trusted Search (см. задачу "manager/trusted Search") —
# ТОЛЬКО manager/trusted-operator, self-service вообще не затронут ====

SEARCH_ENTRY_TEXT = (
    "🔎 Поиск\n\n"
    "Введите:\n"
    "• @username пользователя\n"
    "или\n"
    "• номер автомобиля\n\n"
    "Например:\n"
    "@Mihailov_vm\n"
    "AA123BB"
)
SEARCH_BACK_LABEL = "↩️ Назад"
SEARCH_NEW_LABEL = "🔎 Новый поиск"
SEARCH_MENU_LABEL = "↩️ В меню"


def format_search_not_found(query: str) -> str:
    return f"🔎 Ничего не найдено\n\nПо запросу:\n{query}"


def _format_search_amount(value: float) -> str:
    """"40 ₾"/"40.5 ₾" — та же схема округления/обрезки, что и
    reader/notifications/telegram_notification_service.py::_format_amount
    (там суффикс "GEL", здесь "₾" — задача явно диктует именно этот
    символ для Search-карточки)."""
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{text} ₾"


def format_search_money_line(*, check_ok: bool, fines: list[NewFineEvent]) -> str:
    """КРИТИЧНО (см. задачу): "0 ₾" — ТОЛЬКО если live-проверка реально
    подтвердила отсутствие штрафов (check_ok=True И fines пуст) — тот же
    CheckNowOutcome/CheckResult.current_fines, что и у "🔎 Проверить
    сейчас" (см. SubscriptionService.check_now_task_for_search), никакой
    отдельной "системы расчёта штрафов" здесь нет. "неизвестно" — если
    live-проверка не удалась (check_ok=False, см. FineProviderError) ИЛИ
    хотя бы у одного найденного штрафа amount не определён (police.ge не
    отдал распознаваемую сумму) — сумма НИКОГДА не занижается молчаливым
    пропуском такого штрафа (задача явно запрещает "ERROR/UNKNOWN
    превращать в 0 ₾")."""
    if not check_ok:
        return "💰 Штрафы: неизвестно"
    if not fines:
        return "💰 Штрафы: 0 ₾"
    if any(fine.amount is None for fine in fines):
        return "💰 Штрафы: неизвестно"
    total = sum(fine.amount for fine in fines)
    return f"💰 Штрафы: {_format_search_amount(total)}"


def format_search_monitoring_line(*, monitoring_active: bool) -> str:
    return f"Мониторинг: {'🟢' if monitoring_active else '⚪'}"


def format_search_checked_at_line(checked_at: datetime) -> str:
    return f"Последняя проверка: {checked_at.strftime('%d.%m.%Y %H:%M')}"


def format_search_results(
    *,
    query_type: str,
    query_display: str,
    blocks: list[str],
) -> str:
    """query_type — "username" (заголовок "👤 {query_display}", каждый
    block начинается со своей "🚗 CAR_NUMBER") или "car" (заголовок
    "🚗 {query_display}", каждый block начинается со своего
    "👤 @username/—") — см. задачу п.6/п.11: username-search группирует
    ПО ПОЛЬЗОВАТЕЛЮ (одна и та же машина не повторяется), car-search
    группирует ПО НОМЕРУ (одна и та же машина, разные владельцы). len(blocks)
    решает единственное/множественное число заголовка ("Результат"/
    "Результаты") — сами blocks уже полностью готовы (см.
    ConversationController._format_search_block), эта функция только
    собирает их в одно сообщение."""
    title = "🔎 Результат поиска" if len(blocks) == 1 else "🔎 Результаты поиска"
    header_icon = "👤" if query_type == "username" else "🚗"
    lines = [title, "", f"{header_icon} {query_display}", ""]
    lines.append("\n\n".join(blocks))
    return "\n".join(lines)


def format_search_pagination_footer(*, page: int, total_pages: int) -> str:
    return f"Страница {page + 1} из {total_pages}"
