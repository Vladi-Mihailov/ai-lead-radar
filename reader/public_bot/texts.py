"""Тексты/форматирование сообщений @GEShtrafbot — отдельно от
reader/public_bot/conversation.py (шаги диалога) и
reader/public_bot/keyboards.py (Telethon-кнопки), чтобы формулировки не
были размазаны по нескольким файлам. Никакого Telegram/БД здесь нет —
чистые функции над уже готовыми значениями.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime

from reader.fines.models import FineMonitoringTask, NewFineEvent
from reader.fines.payment_status import (
    DISPLAY_GONE,
    PAYMENT_NEEDS_REVIEW,
    PAYMENT_PAID,
    PAYMENT_UNPAID,
    PRESENCE_MISSING,
    SOURCE_PAYMENT_PAGE,
    SOURCE_RECEIPT,
    DebtBreakdown,
    FineStatus,
    PaymentPageVerdict,
)
from reader.plate_display import format_plate_for_telegram
from reader.public_bot.debt_refresh_service import DebtSummary, RefreshOutcome
from reader.public_bot.delivery_texts import format_check_now_fines_message
from reader.public_bot.full_check_service import FullCheckOutcome
from reader.public_bot.models import FineMonitoringSubscription
from reader.public_bot.statistics_service import BotStatistics
from reader.telegram_markdown import escape_telethon_markdown, safe_telegram_username

MAIN_MENU_TEXT = "🚗 Штрафы Грузии 🇬🇪"
# Короткое объяснение под заголовком — ТОЛЬКО на настоящем "/start", НЕ на
# остальных 13+ "вернуться в главное меню" местах (отмена/ошибка/
# восстановление — там повторять приветствие было бы навязчиво), поэтому
# отдельная константа, а не правка самого MAIN_MENU_TEXT. "штрафы, которые
# уже есть" — НЕ "вся история штрафов"/"все штрафы за всё время" (см.
# задачу "Improve the /start UX..."): police.ge отдаёт только то, что
# сейчас числится неоплаченным, не архив уже оплаченных/закрытых штрафов.
# Вторая строка ("Просто отправьте...") — см. ту же задачу п.2: прямой
# ввод номера с home-состояния теперь РЕАЛЬНО работает (см.
# ConversationController.handle_text()::looks_like_car_number() ветку),
# подсказка не опережает функциональность.
START_WELCOME_EXPLANATION_TEXT = (
    "🚗 Добавьте номер авто — я проверю неоплаченные штрафы, которые уже есть, "
    "и сообщу о новых, как только они появятся.\n\n"
    "Просто отправьте номер авто сообщением."
)
# ЕДИНСТВЕННЫЙ источник истины для полного текста "/start" (см. задачу
# "Fix the Georgia public bot /start greeting" — production-баг: ДВА
# независимых места реагируют на буквальный текст "/start":
#   1) ConversationController.start() — обычный путь (нет открытого
#      диалога с оператором);
#   2) reader/protocol_support/telethon_gateway.py::handle_private_support()
#      — пользователь печатает "/start", чтобы выйти из диалога с
#      "👨‍💼 Оператор" (см. reader/public_bot/handlers.py::_on_message,
#      main_menu_text= параметр, проверяется РАНЬШE controller.handle_text()
#      и возвращает True, обрывая обычный путь вообще).
# Раньше (2) использовал голый MAIN_MENU_TEXT напрямую — ровно тот самый
# "отдельный текстовый constant/unused function" баг, которого эта задача
# явно требует избежать: подсказка добавлялась только в (1). Оба места
# теперь берут ЭТУ константу, а не собирают f-строку заново каждое у
# себя — дальнейшее расхождение этих двух путей технически невозможно.
START_GREETING_TEXT = f"{MAIN_MENU_TEXT}\n\n{START_WELCOME_EXPLANATION_TEXT}"

ADD_CAR_LABEL = "➕ Добавить авто"
MY_CARS_LABEL = "📋 Мои авто"
CHECK_NOW_LABEL = "🔎 Проверить штрафы"
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

# "ℹ️ Справка" — reply-кнопка последней строки главного меню (см.
# reader/public_bot/keyboards.py::main_menu_keyboard), видна ВСЕМ
# пользователям; ответ — HELP_TEXT вместе с той же персистентной
# клавиатурой (отдельная кнопка "Назад" не нужна — меню остаётся внизу).
HELP_LABEL = "ℹ️ Справка"
HELP_TEXT = (
    "ℹ️ Справка\n\n"
    "В боте можно:\n\n"
    "• ➕ Добавить авто — сохранить автомобиль и получать уведомления о новых штрафах.\n"
    "• 📋 Мои авто — посмотреть сохранённые автомобили, включить или выключить уведомления.\n"
    "• 🔎 Проверить штрафы — показать все неоплаченные штрафы по автомобилю.\n"
    "• 🇹🇷 Штрафы Турции — штрафы и платные дороги Турции в нашем боте @ProtocolTRbot.\n"
    "• 🛡 ОСАГО Грузия — оформить страховку для поездки по Грузии в группе @tplgee."
)

# "🛡 ОСАГО Грузия" — тот же приём, что и у TURKEY_BOT_LINK_LABEL выше:
# reply-кнопка распознаётся как текст, ответ — ОТДЕЛЬНОЕ сообщение с inline
# URL-кнопкой в @OSAGO24GEbot. Вся логика оформления (цены, документы,
# заказ, оплата, полис) — только там; здесь лишь переход. start=georgia_bot
# — deep-link payload, который @OSAGO24GEbot сохраняет как источник заказа
# (acquisition_source), чтобы заказы из этого бота можно было отличить.
OSAGO_LABEL = "🛡 ОСАГО Грузия"
OSAGO_LINK_TEXT = (
    "🛡 ОСАГО Грузия\n\n"
    "Оформление страховки происходит в нашем страховом боте.\n\n"
    "Нажмите кнопку ниже:"
)
OSAGO_LINK_BUTTON_LABEL = "🚗 Оформить ОСАГО Грузия"
OSAGO_BOT_URL = "https://t.me/OSAGO24GEbot?start=georgia_bot"

CAR_NUMBER_PROMPT = "🚗 Введите госномер автомобиля\n\nНапример: M295YB196"
CAR_NUMBER_TOO_LONG_TEXT = "❌ Номер автомобиля слишком длинный"
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

# Бессрочный мониторинг (см. задачу "убрать выбор срока мониторинга") —
# вместо выбора срока Add Car заканчивается одним подтверждением (см.
# format_add_car_confirm_prompt), после него мониторинг включается без
# конечной даты (см. reader/fines/models.py::UNLIMITED_END_DATE).
ADD_CAR_CONFIRM_LABEL = "✅ Добавить"
ADD_CAR_DONE_LINE = "✅ Автомобиль добавлен. Проверка штрафов включена"
# Статус вместо даты окончания. Экраны менеджера говорят о мониторинге,
# клиентские — об уведомлениях (см. задачу "remove the word мониторинг
# from user-facing UI"; NOTIFICATIONS_*_LINE — карточка автомобиля клиента).
MONITORING_ACTIVE_LINE = "🟢 Мониторинг активен"
MONITORING_OFF_LINE = "⚪ Мониторинг выключен"
NOTIFICATIONS_ON_LINE = "🟢 Уведомления включены — сообщим, если в дальнейшем появятся новые штрафы."
NOTIFICATIONS_OFF_LINE = "⚪ Уведомления выключены — включите, чтобы мы сообщали о новых штрафах."


def format_add_car_confirm_prompt(car_number: str, *, owner_username: str | None = None) -> str:
    """Последний шаг Add Car перед созданием (self-service и trusted) —
    номер (и владелец, если trusted указал клиента) ещё раз перед глазами,
    чтобы опечатка в номере не включала мониторинг молча."""
    lines = [f"🚗 {format_plate_for_telegram(car_number)}"]
    if owner_username:
        lines.append(f"👤 Владелец: @{owner_username}")
    lines += ["", "Добавить автомобиль и включить проверку штрафов?"]
    return "\n".join(lines)

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


def format_check_now_result(outcome, *, statuses: dict[str, str] | None = None) -> str:
    """outcome: reader.public_bot.subscription_service.CheckNowOutcome.
    Без технической детали ошибки в тексте клиенту (см. design: та же
    осторожность, что и в format_add_car_summary/
    format_delegated_add_car_summary).

    Manual "🔎 Проверить сейчас" — read/display семантика ТЕКУЩЕГО
    состояния машины (см. задачу про UX manual check), а не delta с
    прошлой проверки: "новых штрафов нет" здесь принципиально не
    показывается — outcome.fines это ВСЕ штрафы из ответа police.ge на
    этой проверке, новые и уже известные (см. CheckResult.current_fines).

    provider_unavailable (см. задачу "back off when police.ge is
    unavailable", production incident 2026-10-21) — ОТДЕЛЬНОЕ, более
    понятное сообщение вместо общего "попробуйте позже", когда circuit
    breaker уже признал police.ge недоступным (не единичный сбой, а
    известное временное состояние источника) — быстрый ответ БЕЗ
    ожидания повторного request_timeout."""
    if not outcome.check_ok:
        if outcome.provider_unavailable:
            return "⚠️ Сервис police.ge временно недоступен.\nПопробуйте проверить штрафы позже."
        return (
            f"⚠️ Проверить штрафы для {format_plate_for_telegram(outcome.car_number)} "
            "сейчас не удалось. Попробуйте позже."
        )
    if not outcome.fines:
        return format_no_fines_found(outcome.car_number)
    if statuses is None:
        return format_check_now_fines_message(car_number=outcome.car_number, fines=outcome.fines)
    # statuses — {protocolNo: display_status} (см. FineStatus.display_status):
    # у каждого штрафа строка статуса, внизу — что к оплате, а что уже
    # оплачено или требует проверки (оплаченное в "к оплате" не входит).
    labels = {
        fine.external_fine_id: fine_status_label(statuses.get(fine.external_fine_id, PAYMENT_UNPAID))
        for fine in outcome.fines
        if fine.external_fine_id
    }
    blocks = format_check_now_fines_message(car_number=outcome.car_number, fines=outcome.fines, status_labels=labels)
    return f"{blocks}\n\n{format_check_now_status_summary(outcome.fines, statuses)}"


# ==== Статусы оплаты (см. reader/fines/payment_status.py) ====

FINE_STATUS_LABELS = {
    PAYMENT_UNPAID: "🔴 Не оплачен",
    PAYMENT_PAID: "🟢 Оплачен",
    PAYMENT_NEEDS_REVIEW: "🟡 Оплата требует проверки",
    DISPLAY_GONE: "⚪ Больше не отображается в базе полиции",
}


def fine_status_label(display_status: str) -> str:
    return FINE_STATUS_LABELS.get(display_status, FINE_STATUS_LABELS[PAYMENT_UNPAID])


def format_check_now_status_summary(fines: list[NewFineEvent], statuses: dict[str, str]) -> str:
    """Итог под результатом "🔎 Проверить сейчас": из суммы к оплате
    исключены только подтверждённо оплаченные (🟢) — 🟡 (оплата не прошла
    по неизвестной причине) остаётся долгом и показывается отдельной
    строкой "из них"; ⚪ (может попасть сюда только из кэша при
    недоступности police.ge) в сумму не входит."""
    to_pay = paid = review = 0.0
    paid_count = review_count = 0
    for fine in fines:
        status = statuses.get(fine.external_fine_id or "", PAYMENT_UNPAID)
        amount = fine.amount or 0.0
        if status == PAYMENT_PAID:
            paid += amount
            paid_count += 1
        elif status == DISPLAY_GONE:
            continue
        else:
            to_pay += amount
            if status == PAYMENT_NEEDS_REVIEW:
                review += amount
                review_count += 1
    lines = [f"💰 К оплате: {_format_search_amount(to_pay)}"]
    if review_count:
        lines.append(f"🟡 Из них оплата требует проверки: {_format_search_amount(review)}")
    if paid_count:
        lines.append(f"🟢 Оплачено, ждёт обновления базы полиции: {_format_search_amount(paid)}")
    return "\n".join(lines)


def format_no_fines_found(car_number: str) -> str:
    """Canonical "штрафов не найдено" (см. задачу "ВРЕМЕННЫЙ manager
    workflow для Georgia fines bot" п.4: "не придумывать второй формат,
    если стандартный уже есть") — извлечено из format_check_now_result()
    выше, чтобы manual-bootstrap "✅ Штрафов нет" отправлял клиенту ТОТ ЖЕ
    текст, без второй копии f-строки.

    Вторая строка (см. задачу "remove the word мониторинг from user-facing
    UI") — НЕ "штрафов нет вообще"/"история чистая": police.ge отдаёт
    ТОЛЬКО текущие неоплаченные штрафы (см. provider.py), не полный архив
    уже оплаченных — формулировка "на данный момент неоплаченных" явно это
    отражает, не обещая того, что реально не проверялось."""
    return (
        f"✅ {format_plate_for_telegram(car_number)}: штрафов не найдено\n\n"
        "На данный момент неоплаченных штрафов нет."
    )


def format_stop_success(car_number: str) -> str:
    return f"✅ Мониторинг для {format_plate_for_telegram(car_number)} остановлен."

_DATE_FORMAT = "%d.%m.%Y"


def _fmt_date(value: date) -> str:
    return value.strftime(_DATE_FORMAT)


def format_add_car_summary(
    *,
    car_number: str,
    check_ok: bool,
    new_fines_count: int,
    provider_unavailable: bool = False,
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
    свой Telegram-логин, это не новая для него информация.

    provider_unavailable (см. задачу "back off when police.ge is
    unavailable") — подписка/задача УЖЕ создана независимо от исхода
    проверки (см. SubscriptionService.add_car — check_task() вызывается
    ПОСЛЕ создания, никогда не блокирует его), здесь только разные
    формулировки ПОЧЕМУ проверка не удалась: источник временно недоступен
    (circuit breaker) — отдельное, более понятное сообщение; любая другая
    причина — общее "проверить штрафы сейчас не удалось".

    Срока нет (см. задачу "убрать выбор срока мониторинга") — первая
    строка всегда ADD_CAR_DONE_LINE, мониторинг включён бессрочно."""
    formatted_car_number = format_plate_for_telegram(car_number)

    if not check_ok:
        if provider_unavailable:
            return "\n".join([
                ADD_CAR_DONE_LINE,
                "⚠️ Первичная проверка штрафов временно недоступна",
                "(сервис police.ge не отвечает) и будет выполнена позже.",
                "",
                f"🚗 {formatted_car_number}",
            ])
        return "\n".join([
            ADD_CAR_DONE_LINE,
            "⚠️ Но проверить штрафы сейчас не удалось",
            "",
            f"🚗 {formatted_car_number}",
        ])

    return "\n".join([
        ADD_CAR_DONE_LINE,
        "",
        f"🚗 {formatted_car_number}",
        _format_add_car_check_line(new_fines_count),
    ])


def _format_add_car_check_line(new_fines_count: int) -> str:
    if new_fines_count:
        return f"🔎 Штрафы проверены: найдено новых — {new_fines_count}"
    return "🔎 Штрафы проверены: новых штрафов нет"


def format_delegated_add_car_summary(
    *,
    car_number: str,
    owner_username: str,
    check_ok: bool,
    new_fines_count: int,
    pending_claim: bool,
    claim_link: str | None,
) -> str:
    """Итог trusted-operator delegated Add Car flow — показывается
    ТОЛЬКО trusted-оператору (владельцу это же событие ничего не
    показывает, пока он не claimed, см. design report). Срока нет — та же
    первая строка, что и у self-service (см. format_add_car_summary)."""
    formatted_car_number = format_plate_for_telegram(car_number)

    if not check_ok:
        lines = [
            ADD_CAR_DONE_LINE,
            "⚠️ Но проверить штрафы сейчас не удалось",
            "",
            f"🚗 {formatted_car_number}",
            f"👤 Владелец: @{owner_username}",
        ]
    else:
        lines = [
            ADD_CAR_DONE_LINE,
            "",
            f"🚗 {formatted_car_number}",
            f"👤 Владелец: @{owner_username}",
            _format_add_car_check_line(new_fines_count),
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
    check_ok: bool,
    new_fines_count: int,
) -> str:
    """Итог trusted-operator delegated Add Car БЕЗ клиента ("Отмена" на
    ADD_CLIENT_DECISION_PROMPT) — как format_delegated_add_car_summary, но
    без строки "👤 Владелец: ..." — клиента нет и, возможно, не будет
    никогда (см. design report: username клиента не обязателен)."""
    formatted_car_number = format_plate_for_telegram(car_number)

    if not check_ok:
        return "\n".join([
            ADD_CAR_DONE_LINE,
            "⚠️ Но проверить штрафы сейчас не удалось",
            "",
            f"🚗 {formatted_car_number}",
        ])

    return "\n".join([
        ADD_CAR_DONE_LINE,
        "",
        f"🚗 {formatted_car_number}",
        _format_add_car_check_line(new_fines_count),
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

    ON — подписка активна прямо сейчас (см. FineMonitoringSubscription.
    is_effectively_active). Всё остальное — OFF: 'stopped' (пользователь
    сам выключил) и подписки, чей срок истёк до перехода на бессрочный
    мониторинг (см. задачу "убрать выбор срока мониторинга" — отдельного
    состояния "ИСТЁК" больше нет, срок не показывается). OFF включается
    обратно одной кнопкой (см. SubscriptionService.turn_on_car) — только
    явным действием пользователя."""
    if subscription.is_effectively_active(today=today):
        return "🟢", "ON"
    return "⚪", "OFF"


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
# Короткая подсказка под списком (см. задачу "small UX text change in
# Мои авто") — объясняет, что делает нажатие на НОМЕР автомобиля в
# списке, без привязки к текущему ON/OFF конкретной машины: страница
# может показывать несколько машин сразу в разных состояниях уведомлений
# (см. _format_my_cars_page_reply/format_car_button_label — каждая
# кнопка уже показывает своё состояние отдельно), поэтому формулировка
# "включить/отключить" осознанно не зависит от state, вместо двух разных
# ON/OFF-вариантов текста. НЕ "вся история штрафов"/"все штрафы за всё
# время" (см. задачу) — police.ge отдаёт только текущие неоплаченные
# штрафы, "ранее выписанные" здесь означает "уже существующие сейчас, в
# том числе старые по дате выписки", не архив оплаченных.
MY_CARS_HINT_TEXT = (
    "Нажмите на номер авто, чтобы проверить неоплаченные штрафы "
    "или включить/отключить уведомления о новых штрафах."
)


def format_car_details(subscription: FineMonitoringSubscription, today: date) -> str:
    """См. задачу "remove the word мониторинг from user-facing UI" — опрос
    показал, что пользователи путали "Добавить/Снять с мониторинга" с
    проверкой штрафов за прошлый период, хотя реально это два РАЗНЫХ
    действия (см. SubscriptionService.check_now() vs turn_on_car/
    turn_off_car): явно разделяем их здесь текстом при КАЖДОМ открытии
    карточки машины, а не только один раз — та же карточка используется
    и для объяснения, и для статуса (НЕ отдельный экран, см. задачу: "do
    not create a separate new screen just for this").

    НЕ "старые штрафы"/"история штрафов" (см. задачу) — police.ge отдаёт
    ТОЛЬКО текущие неоплаченные штрафы (см. provider.py), поэтому
    "которые уже есть сейчас" точно описывает реальный охват проверки, не
    обещая полного архива уже оплаченных.

    Срока нет (см. задачу "убрать выбор срока мониторинга") — вместо даты
    окончания одна строка статуса уведомлений (NOTIFICATIONS_ON_LINE/
    NOTIFICATIONS_OFF_LINE)."""
    _, state = car_monitoring_state(subscription, today)
    lines = [
        f"🚗 {format_plate_for_telegram(subscription.car_number)}",
        "",
        "🔎 Проверить штрафы — покажем все неоплаченные штрафы, которые сейчас числятся "
        "по автомобилю, в том числе выписанные ранее.",
        "",
        NOTIFICATIONS_ON_LINE if state == "ON" else NOTIFICATIONS_OFF_LINE,
    ]
    return "\n".join(lines)


TURN_OFF_BUTTON_LABEL = "🔕 Отключить уведомления"
TURN_ON_BUTTON_LABEL = "🔔 Включить уведомления"
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
    """См. задачу "remove the word мониторинг from user-facing UI" — НЕ
    подразумевает, что уже найденные штрафы удалены/недоступны (та же
    осторожность, что и в format_delete_confirm_prompt), машина остаётся в
    «Мои авто»."""
    return (
        f"🔕 Уведомления для {format_plate_for_telegram(car_number)} отключены.\n"
        "Автомобиль сохранён в «Мои авто»."
    )


def format_turn_on_success(car_number: str) -> str:
    return (
        f"✅ Уведомления для {format_plate_for_telegram(car_number)} включены.\n"
        "Мы сообщим, если появятся новые штрафы для этого автомобиля."
    )


def format_delete_confirm_prompt(car_number: str) -> str:
    return (
        f"⚠️ Удалить {format_plate_for_telegram(car_number)} из списка?\n"
        "После удаления автомобиль исчезнет из «Мои авто»."
    )


def format_trusted_task_detail(task: FineMonitoringTask) -> str:
    """Карточка одной машины (см. design report про "убрать текстовый
    перечень из списка, показывать в карточке по нажатию на номер") —
    открывается по нажатию на кнопку-номер в списке "📋 Мои авто" (см.
    keyboards.py::trusted_tasks_page_keyboard/encode_trusted_task_open_
    callback). ON/OFF + последняя проверка — та же информация, что
    раньше была видна прямо в списке, теперь ТОЛЬКО здесь. Срока нет (см.
    задачу "убрать выбор срока мониторинга") — вместо периода статус."""
    lines = [f"🚗 {format_plate_for_telegram(task.car_number)}" + (f" ({task.label})" if task.label else "")]
    lines.append(MONITORING_ACTIVE_LINE if task.status == "active" else MONITORING_OFF_LINE)
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


# Разовая проверка trusted-менеджера без сохранения (см. задачу "GE-2: UI
# менеджера") — номер не обязан быть на мониторинге; результат не пишется в
# БД и уведомлений не вызывает, поэтому в тексте результата это сказано
# явно (ONE_OFF_CHECK_NOT_SAVED_LINE).
TRUSTED_ONE_OFF_CHECK_LABEL = "🔎 Разовая проверка"
TRUSTED_ONE_OFF_MONITOR_LABEL = "➕ На мониторинг"
TRUSTED_ONE_OFF_ADD_LABEL = "➕ Добавить на мониторинг"
TRUSTED_ONE_OFF_ANOTHER_LABEL = "🔎 Проверить другой номер"
TRUSTED_RESUME_MONITORING_LABEL = "▶️ Продолжить мониторинг"
ONE_OFF_CHECK_NOT_SAVED_LINE = "ℹ️ Разовая проверка: результат не сохранён, мониторинг не включён."
ONE_OFF_CHECK_IN_PROGRESS_TEXT = "⏳ Проверка уже выполняется, дождитесь результата."


def format_trusted_plate_choice_prompt(car_number: str) -> str:
    """Номер, присланный trusted-менеджером с главного экрана (см. задачу
    "GE-2", решение 3) — выбор между разовой проверкой и постановкой на
    мониторинг. У обычного пользователя тот же ввод по-прежнему сразу
    запускает Add Car (см. ConversationController.handle_text)."""
    return (
        f"🚗 {format_plate_for_telegram(car_number)}\n\n"
        "Проверить штрафы разово или поставить номер на мониторинг?"
    )


def format_one_off_check_result(result, *, inactive_task: FineMonitoringTask | None = None) -> str:
    """result: reader.fines.models.OneOffCheckResult (см. FineCheckService.
    check_plate_readonly). ВСЕ штрафы из ответа police.ge/REDNET тем же
    блоком, что и у "🔎 Проверить сейчас" (delivery_texts.
    format_check_now_fines_message), плюс количество и сумма (format_search_
    money_line — "неизвестно", если хотя бы у одного штрафа нет суммы:
    сумма никогда не занижается). Ошибка — явное "❌ Проверка не
    выполнена": отката к сохранённому состоянию нет, его у разовой проверки
    и не бывает. Коммерческих CTA нет — это экран менеджера.

    inactive_task — историческая (completed/stopped) задача номера, если
    она есть: хвост MONITORING_STILL_OFF_LINE подтверждает, что разовая
    проверка мониторинг НЕ включила."""
    plate = format_plate_for_telegram(result.car_number)
    if not result.shows_as_ok:
        if result.provider_unavailable:
            parts = [
                "❌ Проверка не выполнена: сервис police.ge сейчас недоступен.\n"
                f"Попробуйте проверить {plate} позже."
            ]
        else:
            parts = [f"❌ Проверка не выполнена: не удалось получить штрафы для {plate}.\nПопробуйте позже."]
    elif not result.fines:
        parts = [format_no_fines_found(result.car_number), ONE_OFF_CHECK_NOT_SAVED_LINE]
    else:
        parts = [
            f"🔎 {plate}: найдено штрафов — {len(result.fines)}\n"
            + format_search_money_line(check_ok=True, fines=result.fines),
            format_check_now_fines_message(car_number=result.car_number, fines=result.fines),
            ONE_OFF_CHECK_NOT_SAVED_LINE,
        ]
    if inactive_task is not None:
        parts.append(MONITORING_STILL_OFF_LINE)
    return "\n\n".join(parts)


# Хвост результата разовой проверки номера, у которого есть только
# историческая (completed/stopped) задача (см. format_one_off_check_result)
# — подтверждает, что разовая проверка мониторинг НЕ включила. Без дат (см.
# задачу "убрать выбор срока мониторинга": срок нигде не показывается).
MONITORING_STILL_OFF_LINE = "⚪ Мониторинг по-прежнему выключен."


def format_trusted_task_off_detail(task: FineMonitoringTask) -> str:
    """Экран "▶️ Продолжить мониторинг" (см. design report) — показывается
    после нажатия ⚪ OFF в списке, ИЛИ сразу после ON -> OFF toggle."""
    return f"🚗 {format_plate_for_telegram(task.car_number)}\n{MONITORING_OFF_LINE}"


def format_trusted_task_turn_off_success(car_number: str) -> str:
    return f"⏸ Мониторинг {format_plate_for_telegram(car_number)} выключен."


def format_trusted_task_resume_success(car_number: str) -> str:
    """Подтверждение "▶️ Продолжить мониторинг" — мониторинг включён
    бессрочно (см. задачу "убрать выбор срока мониторинга": выбора 15/30/90
    дней и строки "До: {дата}" больше нет)."""
    return f"✅ Мониторинг {format_plate_for_telegram(car_number)} включён"


def format_trusted_stop_confirm_prompt(car_number: str, subscriber_count: int) -> str:
    """Текст подтверждения ⛔ для trusted-оператора — честно предупреждает,
    если у задачи есть ещё actionable (active/pending_claim) client-
    подписки (см. design report): формулировка корректно отражает
    единственное/множественное число получателей."""
    formatted_car_number = format_plate_for_telegram(car_number)
    if subscriber_count == 0:
        return _TRUSTED_STOP_CONFIRM_PROMPT_NO_CLIENTS.format(car_number=formatted_car_number)
    template = (
        _TRUSTED_STOP_CONFIRM_PROMPT_ONE_CLIENT if subscriber_count == 1
        else _TRUSTED_STOP_CONFIRM_PROMPT_MANY_CLIENTS
    )
    return template.format(car_number=formatted_car_number)


def trusted_stop_confirm_button_label(subscriber_count: int) -> str:
    return (
        _TRUSTED_STOP_CONFIRM_BUTTON_NO_CLIENTS if subscriber_count == 0
        else _TRUSTED_STOP_CONFIRM_BUTTON_WITH_CLIENTS
    )


def format_statistics_header(stats: BotStatistics) -> str:
    """Часть "📊 Статистика", НЕ относящаяся к штрафам (см.
    format_debt_summary_block ниже) — вынесена отдельно, чтобы
    ConversationController мог переиспользовать ТОЛЬКО debt-блок в
    результате "🔄 Проверить авто со штрафами" (см. задачу "manager
    Statistics / refresh для обоих ботов" п.7 — там НЕ нужны 👥
    Пользователи/🚗 Подписки, только сам факт проверки + актуальный
    debt-блок)."""
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


def format_debt_summary_block(debt: DebtSummary) -> str:
    """"🚨 Штрафы по последней проверке" (см. задачу п.3/п.7) —
    debt.total_amount означает "что показала последняя УСПЕШНАЯ проверка"
    (FineMonitoringTask.last_successful_total_amount), а не сумма истории
    detected_fines — может стать 0 (машина тогда пропадает из списка) или
    уменьшиться. Переиспользуется И обычным 📊 Статистика, И результатом
    "🔄 Проверить авто со штрафами" (см. ConversationController.
    _build_debt_section) — один и тот же блок в обоих местах.
    debt.car_count == 0 — блок всё равно показывается (с нулями), а не
    скрывается: кнопка проверки остаётся доступной даже когда сейчас
    известных штрафов нет (см. handle_debt_refresh_pick — п.10 задачи)."""
    return "\n".join([
        "🚨 Штрафы по последней проверке",
        f"Автомобилей: {debt.car_count}",
        f"Общая сумма: {_format_search_amount(debt.total_amount)}",
        "",
        "ℹ️ Показана сумма последней успешной проверки — police.ge не подтверждает оплату явно.",
    ])


def format_statistics(stats: BotStatistics, *, debt: DebtSummary) -> str:
    """Trusted-operator-only "📊 Статистика" (см. design report) — все
    значения уже посчитаны BotStatisticsService.get_statistics()/
    DebtRefreshService.get_debt_summary(), здесь только форматирование."""
    return f"{format_statistics_header(stats)}\n\n{format_debt_summary_block(debt)}"


DEBT_REFRESH_BUTTON_LABEL = "🔄 Проверить авто со штрафами"
DEBT_REFRESH_NONE_TEXT = "✅ Автомобилей с известными штрафами нет."
DEBT_REFRESH_IN_PROGRESS_TEXT = "⏳ Проверка уже выполняется, подождите."
DEBT_REFRESH_CANCELLED_TEXT = "Отменено."
DEBT_REFRESH_CONFIRM_BUTTON_LABEL = "✅ Подтвердить"


def format_debt_refresh_prompt(car_count: int) -> str:
    return f"🔄 Будет проверено автомобилей: {car_count}"


# Сколько номеров перечислять в "Не удалось проверить: ..." — при массовом
# сбое police.ge/REDNET список всех машин вместе с многострочной страницей
# debt-списка (см. ConversationController._DEBT_LIST_PAGE_CHAR_BUDGET)
# перерос бы одно сообщение; счётчик "⚠️ Не удалось проверить: N" остаётся
# точным.
_DEBT_REFRESH_FAILED_PLATES_SHOWN = 20


def format_debt_refresh_summary(outcome: RefreshOutcome) -> str:
    """См. задачу п.7 "RESULT" — БЕЗ "Найдены новые штрафы"/"Без новых
    штрафов"-аналитики: только факт проверки, актуальный список — заново
    построенный "🚨 Штрафы по последней проверке" блок (см.
    ConversationController._build_debt_section), склеиваемый вызывающим
    кодом ПОСЛЕ этого текста, а не здесь. Непроверенные номера —
    не больше _DEBT_REFRESH_FAILED_PLATES_SHOWN, остальные — "и ещё N"."""
    lines = [
        "🔄 Проверка завершена",
        "",
        f"Проверено: {outcome.checked}",
        f"⚠️ Не удалось проверить: {outcome.failed}",
    ]
    if outcome.failed_car_numbers:
        shown = outcome.failed_car_numbers[:_DEBT_REFRESH_FAILED_PLATES_SHOWN]
        hidden_count = len(outcome.failed_car_numbers) - len(shown)
        lines.append("")
        lines.append(
            "Не удалось проверить: "
            + ", ".join(format_plate_for_telegram(p) for p in shown)
            + (f" и ещё {hidden_count}" if hidden_count else "")
        )
    return "\n".join(lines)


DEBT_OWNER_NOT_LINKED_LINE = "👤 Пользователь не привязан"
_DEBT_OWNER_NAME_MISSING_TEXT = "Имя не указано"
_DEBT_OWNER_USERNAME_MISSING_TEXT = "Логин не указан"
_DEBT_OWNER_NOTIFICATIONS_OFF_MARK = " (уведомления выключены)"


@dataclass(frozen=True)
class DebtOwner:
    """Один привязанный к машине пользователь в debt-списке 📊 Статистика
    (см. задачу "дополнить статистику штрафов информацией о
    пользователях") — уже выбранные для показа значения (см.
    ConversationController._debt_owners_by_car): name — имя из Telegram
    first_name + last_name (см. debt_owner_name), username — Telegram
    username как он есть (без "@"), НИКОГДА не собранный из имени. None —
    значение неизвестно (format_debt_row покажет "Имя не указано"/"Логин
    не указан"). notifications_off — у пользователя нет действующей
    подписки на машину (уведомления выключены или срок истёк): такой
    пользователь показывается, только если действующих нет, и помечается
    явно — чтобы менеджер не принял его за получающего уведомления."""

    name: str | None
    username: str | None
    notifications_off: bool = False


def debt_owner_name(first_name: str | None, last_name: str | None) -> str | None:
    """Имя для DebtOwner — Telegram first_name + last_name с тем же
    отсевом placeholder-мусора ("."/"-"/"_"), что и у Search (см.
    _sanitize_name_part); None — показывать нечего."""
    return _full_name(first_name, last_name)


def _format_debt_owner_lines(owner: DebtOwner) -> list[str]:
    """Имя — произвольный пользовательский текст, поэтому обезвреживается
    для Telethon markdown; username показывается, только если это
    настоящий Telegram username (см. reader/telegram_markdown.py)."""
    username = safe_telegram_username(owner.username)
    name_text = escape_telethon_markdown(owner.name) if owner.name else _DEBT_OWNER_NAME_MISSING_TEXT
    username_text = f"@{username}" if username else _DEBT_OWNER_USERNAME_MISSING_TEXT
    mark = _DEBT_OWNER_NOTIFICATIONS_OFF_MARK if owner.notifications_off else ""
    return [f"👤 Пользователь: {name_text}{mark}", f"🔗 Telegram: {username_text}"]


def format_debt_row(
    *, car_number: str, owners: list[DebtOwner], total_amount: float, breakdown: DebtBreakdown | None = None,
) -> str:
    """Блок одной машины debt-списка (см. задачу "дополнить статистику
    штрафов информацией о пользователях"):
        🚗 Госномер: <номер>
        💰 Задолженность: <сумма> ₾
        👤 Пользователь: <имя>
        🔗 Telegram: @<username>
    БЕЗ даты/checked_at/"сегодня"/"вчера" (см. задачу "manager Statistics
    / refresh для обоих ботов" п.1). owners — ВСЕ пользователи, привязанные
    к физическому car_number (см. задачу "Georgia debt deduplication by
    physical car_number"): пара 👤/🔗 на каждого, сумма — РОВНО один раз
    (не дублируется на каждого владельца и не складывается между ними).
    Пустой owners (машина без клиента, см. add_delegated_car_without_client)
    -> "👤 Пользователь не привязан".

    breakdown (статусы оплаты ведутся) — "💰 Задолженность" это всё, что
    не подтверждено оплаченным (🔴 и 🟡), 🟡 — строкой "из них", 🟢 —
    отдельной строкой; total_amount тогда не используется."""
    if breakdown is None:
        lines = [
            f"🚗 Госномер: {format_plate_for_telegram(car_number)}",
            f"💰 Задолженность: {_format_search_amount(total_amount)}",
        ]
    else:
        lines = [
            f"🚗 Госномер: {format_plate_for_telegram(car_number)}",
            f"💰 Задолженность: {_format_search_amount(breakdown.unpaid_amount + breakdown.needs_review_amount)}",
        ]
        if breakdown.needs_review_count:
            lines.append(f"🟡 Из них требует проверки: {_format_search_amount(breakdown.needs_review_amount)}")
        if breakdown.paid_count:
            lines.append(f"🟢 Оплачено: {_format_search_amount(breakdown.paid_amount)}")
    if not owners:
        lines.append(DEBT_OWNER_NOT_LINKED_LINE)
    for owner in owners:
        lines.extend(_format_debt_owner_lines(owner))
    return "\n".join(lines)


def format_debt_list_section(row_lines: list[str], *, page: int, total_pages: int) -> str:
    """[] row_lines -> "" (вызывающий код тогда не добавляет секцию вовсе,
    см. ConversationController._build_debt_section) — итоговый блок ВСЕГДА
    либо полностью присутствует, либо полностью отсутствует, никогда не
    показывается "пустым". БЕЗ отдельного заголовка списка (см. задачу
    п.7 — пример показывает строки СРАЗУ после аггрегата, без "Автомобили
    со штрафами:"). Пагинация (см. задачу п.7: "если список большой —
    сохранить существующую pagination") — тот же footer, что и у Search
    (format_search_pagination_footer), показывается ТОЛЬКО когда страниц
    больше одной. Блоки машин (см. format_debt_row) многострочные —
    разделяются пустой строкой."""
    if not row_lines:
        return ""
    parts = ["\n\n".join(row_lines)]
    if total_pages > 1:
        parts.append("")
        parts.append(format_search_pagination_footer(page=page, total_pages=total_pages))
    return "\n".join(parts)


def format_debt_status_summary_block(
    *, car_count: int, totals: DebtBreakdown, source_updated_on: date | None, recheck_line: str | None,
) -> str:
    """Заголовок "🚨 Штрафы по последней проверке", когда ведутся статусы
    оплаты (см. задачу: "отдельно показывать подтверждённую задолженность,
    оплаченные штрафы и случаи с неопределённым статусом")."""
    lines = [
        "🚨 Штрафы по последней проверке",
        f"Автомобилей: {car_count}",
        f"🔴 Подтверждённая задолженность: {_format_search_amount(totals.unpaid_amount)}"
        f" ({totals.unpaid_count} шт.)",
        f"🟡 Неопределённый статус оплаты: {_format_search_amount(totals.needs_review_amount)}"
        f" ({totals.needs_review_count} шт.)",
        f"🟢 Оплачено, ещё в базе полиции: {_format_search_amount(totals.paid_amount)}"
        f" ({totals.paid_count} шт.)",
    ]
    if source_updated_on is not None:
        lines.append(f"🗓 База police.ge обновлена: {_fmt_date(source_updated_on)}")
    if recheck_line:
        lines.append(recheck_line)
    lines.extend([
        "",
        "ℹ️ 🟢 — оплата подтверждена, police.ge ещё показывает штраф. 🟡 — оплата не прошла, долг не снят.",
    ])
    return "\n".join(lines)


_RECHECK_STOP_REASONS = {
    "blocked": "police.ge ответил отказом (403/429)",
    "unavailable": "police.ge недоступен",
    "errors": "ошибки подряд",
    "cutoff": "закончилось окно проверки",
    "busy": "шла другая массовая проверка",
    "interrupted": "бот перезапускался",
}


def format_daily_recheck_line(run) -> str | None:
    """Последний вечерний проход (см. reader/public_bot/daily_recheck.py) —
    одной строкой для статистики; None — проходов ещё не было."""
    if run is None:
        return None
    day = run.run_date.strftime("%d.%m")
    reason = _RECHECK_STOP_REASONS.get(run.stop_reason or "", run.stop_reason or "")
    progress = f"проверено {run.checked} из {run.total}"
    if run.status == "running":
        return f"🌙 Вечерняя перепроверка {day}: идёт — {progress}"
    if run.status == "completed":
        if run.total == 0:
            return f"🌙 Вечерняя перепроверка {day}: проверять было нечего"
        return f"🌙 Вечерняя перепроверка {day}: завершена — {progress}, ошибок {run.failed}"
    if run.status == "postponed":
        return f"🌙 Вечерняя перепроверка {day}: перенесена ({reason}) — {progress}"
    return f"🌙 Вечерняя перепроверка {day}: остановлена ({reason}) — {progress}, ошибок {run.failed}"


FINE_STATUS_BUTTON_LABEL = "🧾 Статусы оплаты"
FINE_STATUS_PLATE_PROMPT = "🧾 Статусы оплаты\n\nВведите номер автомобиля."
FINE_STATUS_PLATE_INVALID_TEXT = "❌ Не удалось распознать номер.\n\n" + FINE_STATUS_PLATE_PROMPT
FINE_STATUS_NOT_FOUND_TEXT = "Штраф не найден — возможно, список уже обновился."
FINE_STATUS_CHANGED_TEXT = "⚠️ Статус штрафа уже изменился — проверьте его ещё раз."
FINE_STATUS_SAVED_TEXT = "✅ Сохранено."
FINE_STATUS_CANCELLED_TEXT = "Отменено."
FINE_STATUS_ACTION_PAID_LABEL = "🟢 Оплачен — есть квитанция"
FINE_STATUS_ACTION_FAILED_LABEL = "🟡 Оплата не прошла на сайте МВД"
FINE_STATUS_ACTION_TEXT_LABEL = "📝 Текст со страницы оплаты"
FINE_STATUS_ACTION_UNPAID_LABEL = "🔴 Снять отметку"
FINE_STATUS_CONFIRM_LABEL = "✅ Подтвердить"
FINE_STATUS_CANCEL_LABEL = "❌ Отмена"
FINE_STATUS_BACK_LABEL = "⬅️ К списку"
FINE_STATUS_PAYMENT_NOTE_PROMPT = (
    "🟢 Подтверждение оплаты\n\n"
    "Отправьте одним сообщением номер квитанции или операции, дату и способ оплаты "
    "(например: «TBC Pay 123456789, 18.11.2026»).\n"
    "Без основания отметку «Оплачен» поставить нельзя."
)
FINE_STATUS_PAYMENT_NOTE_EMPTY_TEXT = "❌ Основание не может быть пустым.\n\n" + FINE_STATUS_PAYMENT_NOTE_PROMPT
FINE_STATUS_PAGE_TEXT_PROMPT = (
    "📝 Текст со страницы оплаты\n\n"
    "Вставьте одним сообщением текст, который показала страница оплаты или police.ge после попытки оплаты. "
    "Банковские данные не присылайте."
)
FINE_STATUS_PAGE_TEXT_EMPTY_TEXT = "❌ Текст не может быть пустым.\n\n" + FINE_STATUS_PAGE_TEXT_PROMPT

_PAYMENT_SOURCE_TEXT = {
    SOURCE_RECEIPT: "квитанция",
    SOURCE_PAYMENT_PAGE: "страница оплаты",
}


def _fine_status_amount(status: FineStatus) -> str:
    return _format_search_amount(status.amount) if status.amount is not None else "сумма неизвестна"


def format_fine_status_car(*, car_number: str, statuses: list[FineStatus], source_updated_on: date | None) -> str:
    """Экран менеджера "🧾 Статусы оплаты" для одной машины."""
    lines = [f"🧾 Статусы оплаты: {format_plate_for_telegram(car_number)}"]
    if source_updated_on is not None:
        lines.append(f"🗓 База police.ge обновлена: {_fmt_date(source_updated_on)}")
    lines.append("")
    if not statuses:
        lines.append("Штрафов в базе полиции по этому номеру не найдено (по данным последних проверок).")
        return "\n".join(lines)
    for index, status in enumerate(statuses, start=1):
        line = f"{index}. №{status.protocol_no} — {_fine_status_amount(status)} — {fine_status_label(status.display_status)}"
        if status.presence == PRESENCE_MISSING:
            line += " · не найден в последней проверке"
        lines.append(line)
    if any(status.display_status != DISPLAY_GONE for status in statuses):
        lines.extend(["", "Нажмите на штраф, чтобы изменить статус оплаты."])
    return "\n".join(lines)


def format_fine_status_detail(status: FineStatus, *, notice: str | None = None) -> str:
    lines = []
    if notice:
        lines.extend([notice, ""])
    lines.extend([
        f"🧾 Штраф №{status.protocol_no}",
        f"🚗 {format_plate_for_telegram(status.car_number)}",
        f"💰 Сумма: {_fine_status_amount(status)}",
        f"Статус: {fine_status_label(status.display_status)}",
    ])
    if status.presence == PRESENCE_MISSING:
        lines.append("⏳ Не найден в последней проверке police.ge — ждём подтверждения следующей проверкой.")
    if status.payment_status != PAYMENT_UNPAID and status.payment_note:
        source = _PAYMENT_SOURCE_TEXT.get(status.payment_source or "", "менеджер")
        lines.append(f"Основание ({source}): {escape_telethon_markdown(status.payment_note)}")
    return "\n".join(lines)


def format_fine_status_confirm(status: FineStatus, *, target_label: str, explanation: str | None = None) -> str:
    lines = [
        f"Изменить статус штрафа №{status.protocol_no} ({format_plate_for_telegram(status.car_number)})?",
        "",
        f"Сейчас: {fine_status_label(status.display_status)}",
        f"Станет: {target_label}",
    ]
    if explanation:
        lines.extend(["", explanation])
    return "\n".join(lines)


def payment_page_verdict_explanation(verdict: PaymentPageVerdict) -> str:
    if verdict is PaymentPageVerdict.ALREADY_PAID:
        return "Официальный сервис явно сообщает, что штраф уже оплачен."
    if verdict is PaymentPageVerdict.TECHNICAL_ERROR:
        return "Это техническая ошибка сервиса: статус и задолженность не меняются, сообщение сохранится в журнале."
    if verdict is PaymentPageVerdict.PAYMENT_FAILED:
        return (
            "«ჯარიმის გადახდა ვერ მოხერხდა!» — общий отказ: так выглядит и уже оплаченный штраф, "
            "и сбой, и отказ по другой причине. Это не доказательство оплаты."
        )
    return "Текст не распознан как подтверждение оплаты — причина отказа неизвестна."


# ==== manager/trusted Search (см. задачу "manager/trusted Search") —
# ТОЛЬКО manager/trusted-operator, self-service вообще не затронут ====

# Компактный prompt (см. задачу "add name search to trusted bot search"
# п.1: "не добавлять дополнительные пояснения") — заменяет прежний
# развёрнутый вариант целиком.
SEARCH_ENTRY_TEXT = (
    "🔎 Поиск\n\n"
    "Введите имя, @логин или номер авто\n"
    "Например: Иван, @Mihailov_vm, AA123BB"
)
SEARCH_BACK_LABEL = "↩️ Назад"
SEARCH_NEW_LABEL = "🔎 Новый поиск"
SEARCH_MENU_LABEL = "↩️ В меню"


def format_search_not_found(query: str) -> str:
    return f"🔎 Ничего не найдено\n\nПо запросу:\n{query}"


# Telegram first_name/last_name иногда содержит placeholder-мусор вместо
# реального имени (наблюдалось вживую: "." — см. задачу "manager
# Statistics / refresh для обоих ботов" п.2, пример "T851CO790 — .
# (@Roma12312)" -> должно стать "@Roma12312"). Такие значения — ТОЛЬКО
# пунктуация/whitespace, без единой буквы/цифры — не являются человеческим
# именем и не должны показываться как имя (но username при этом НЕ
# теряется, см. _sanitize_name_part/format_search_owner_display).
_MEANINGLESS_NAME_RE = re.compile(r"^[\s.\-_]*$")


def _sanitize_name_part(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped or _MEANINGLESS_NAME_RE.fullmatch(stripped):
        return None
    return stripped


def _full_name(first_name: str | None, last_name: str | None) -> str | None:
    parts = [part for part in (_sanitize_name_part(first_name), _sanitize_name_part(last_name)) if part]
    return " ".join(parts) if parts else None


def format_search_owner_display(
    *, first_name: str | None, last_name: str | None, username: str | None,
) -> str:
    """Owner-строка Search-результата (см. задачу "add name search to
    trusted bot search" п.7):
      имя + username -> "Имя Фамилия (@username)";
      только имя     -> "Имя Фамилия" (или просто "Имя", если фамилии нет);
      только username -> "@username";
      ничего         -> "—".
    НИКОГДА "None"/"@None"/пустых скобок — та же гарантия, что и у
    format_owner_username_button (manager car list), но с добавленным
    именем и другим набором случаев, поэтому отдельная функция, а не
    переиспользование той (задача явно требует Georgia/Turkey
    fines/unified-логику и manager car list НЕ менять)."""
    name = _full_name(first_name, last_name)
    if name and username:
        return f"{name} (@{username})"
    if name:
        return name
    if username:
        return f"@{username}"
    return "—"


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


def format_search_results(*, blocks: list[str]) -> str:
    """Каждый block уже полностью самодостаточен — владелец (см.
    format_search_owner_display) И машина вместе (см.
    ConversationController._format_search_block) — единый формат
    независимо от того, что искали (@username/username/имя/номер, см.
    задачу "add name search to trusted bot search" п.5/п.6/п.7): один и
    тот же человек может встретиться по имени ИЛИ username, а имя может
    соответствовать НЕСКОЛЬКИМ разным telegram_user_id одновременно —
    единого "внешнего" owner/car для заголовка может не быть вовсе,
    поэтому больше нет отдельной "👤 {query}"/"🚗 {query}" шапки (см. более
    раннюю реализацию) — каждая строка результата сама называет своего
    владельца И свою машину. len(blocks) решает единственное/
    множественное число заголовка ("Результат"/"Результаты")."""
    title = "🔎 Результат поиска" if len(blocks) == 1 else "🔎 Результаты поиска"
    return "\n".join([title, "", "\n\n".join(blocks)])


def format_search_pagination_footer(*, page: int, total_pages: int) -> str:
    return f"Страница {page + 1} из {total_pages}"


# ==== "📸 Проверить протокол" (см. задачу) — Georgia-only, one-shot lookup
# против videos.police.ge (НЕ police.ge/protocol, см. задачу п.5/п.10: "не
# смешивать с текущим FineCheckService"/"не влиять на 📊 Статистика/🔎
# Проверить сейчас/📋 Мои авто") — доступна ВСЕМ пользователям бота, не
# trusted-gated (см. reader/public_bot/keyboards.py::main_menu_keyboard).
# Номер техпаспорта/протокола/ID-TAX нарушителя — sensitive (см. задачу
# п.4) — ни один текст здесь не подставляет их обратно пользователю
# (результат — ТОЛЬКО фиксированные сообщения ниже, никогда echo введённых
# значений). ====

PROTOCOL_CHECK_LABEL = "📸 Проверить протокол"

PROTOCOL_CHECK_INTRO_TEXT = (
    "📸 Проверить протокол\n\n"
    "Для проверки понадобятся данные одного из вариантов:\n\n"
    "🚗 По автомобилю\n"
    "• номер автомобиля\n"
    "• номер техпаспорта\n\n"
    "📄 По протоколу\n"
    "• номер протокола\n"
    "• ID или TAX номер нарушителя\n\n"
    "Выберите способ проверки:"
)

PROTOCOL_CHECK_VEHICLE_METHOD_LABEL = "🚗 По автомобилю"
PROTOCOL_CHECK_PROTOCOL_METHOD_LABEL = "📄 По протоколу"
# Отдельная константа от BACK_BUTTON_LABEL ("⬅️ Назад")/SEARCH_BACK_LABEL
# ("↩️ Назад") выше — задача явно specif'ит именно "◀️ Назад" для этого
# flow (та же стрелка, что и у пагинации, см. keyboards.py — но здесь это
# НЕ пагинация, отдельный смысл: "на один шаг назад").
PROTOCOL_CHECK_BACK_LABEL = "◀️ Назад"

PROTOCOL_CHECK_VEHICLE_CAR_NUMBER_PROMPT = "🚗 Введите номер автомобиля:"
PROTOCOL_CHECK_VEHICLE_DOCUMENT_PROMPT = "📄 Введите номер техпаспорта:"
PROTOCOL_CHECK_PROTOCOL_NUMBER_PROMPT = "📄 Введите номер протокола:"
PROTOCOL_CHECK_PERSONAL_NUMBER_PROMPT = "👤 Введите ID или TAX номер нарушителя:"

# NOT_FOUND — ТОЛЬКО когда provider подтвердил точный marker сайта (см.
# reader/public_bot/protocol_check_provider.py) — никогда для
# timeout/HTTP-ошибки/session-ошибки/неопознанного ответа (см. задачу п.7).
PROTOCOL_CHECK_NOT_FOUND_TEXT = "✅ Протоколы по указанным данным не найдены."
# ERROR — timeout/HTTP-ошибка/session-ошибка после повторной попытки (см.
# reader/public_bot/protocol_check_provider.py) — НИКОГДА не показывается
# как "не найдено" (см. задачу п.7/п.12).
PROTOCOL_CHECK_ERROR_TEXT = "⚠️ Не удалось выполнить проверку. Попробуйте позже."
# UNKNOWN/UNPARSED_SUCCESS — технически успешный ответ, но positive-
# структура ещё не распознаётся (см. задачу п.8: "не придумывай CSS
# selectors для FOUND") — raw HTML пользователю никогда не показывается.
PROTOCOL_CHECK_UNKNOWN_TEXT = "⚠️ Ответ получен, но формат результата пока не удалось распознать."
# FOUND — parser сегодня НИКОГДА не возвращает этот статус (см.
# protocol_check_provider.py::_parse_result, задача п.8) — константа и
# ветка обработки существуют для расширяемости, текст будет пересмотрен
# вместе с реальным parser'ом, когда появится подтверждённый positive-пример.
PROTOCOL_CHECK_FOUND_TEXT = "✅ Найдены протоколы по указанным данным."
# provider вообще не сконфигурирован в этой сборке (см.
# ConversationController.__init__ — protocol_check_provider=None по
# умолчанию, тот же приём, что и у debt_refresh_service) — практически
# недостижимо в production (reader/public_bot/main.py всегда его
# конструирует), защитный fallback на случай неполной сборки/теста.
PROTOCOL_CHECK_UNAVAILABLE_TEXT = "⚠️ Функция временно недоступна."


# ==== "fine check-all" (см. задачу "add silent Georgia full database check
# command") — скрытая fine-admin-only maintenance-команда, НИГДЕ не
# отображается (не в keyboards.py, не в главном меню) — эти константы сами
# по себе НЕ делают команду видимой, только задают её точный текст ввода/
# ответов. ====

FULL_CHECK_COMMAND = "fine check-all"
FULL_CHECK_CONFIRM_COMMAND = "fine check-all confirm"

FULL_CHECK_ALREADY_IN_PROGRESS_TEXT = "⚠️ Полная проверка уже выполняется."


def format_full_check_preview(candidate_count: int, *, interval_seconds: float) -> str:
    """Экран ПЕРЕД запуском (см. задачу п.8) — НЕ начинает проверки сам по
    себе; candidate_count — свежий, только что пересчитанный (см.
    FullCheckService.list_candidate_task_ids(), вызывается заново
    непосредственно перед confirm, см. задачу: "повторно пересчитать
    candidate set")."""
    interval_text = f"{interval_seconds:g}"
    return (
        "🔄 Полная проверка автомобилей\n\n"
        f"Будет проверено: {candidate_count}\n"
        f"Интервал: {interval_text} сек.\n\n"
        "Для запуска:\n"
        f"{FULL_CHECK_CONFIRM_COMMAND}"
    )


def _format_duration(seconds: float) -> str:
    total_seconds = round(seconds)
    minutes, secs = divmod(total_seconds, 60)
    if minutes:
        return f"{minutes} мин {secs} сек"
    return f"{secs} сек"


# ---- "ВРЕМЕННЫЙ manager workflow для Georgia fines bot" (см. задачу) ----
# Manual bootstrap нового физического номера менеджером, пока production
# VPS не имеет сетевого доступа к police.ge — тексты/кнопки ниже видны
# ТОЛЬКО fine_admin менеджерам (см. ConversationController._is_fine_admin),
# никогда обычным пользователям.

MANUAL_BOOTSTRAP_NO_FINES_LABEL = "✅ Штрафов нет"
MANUAL_BOOTSTRAP_ADD_FINE_LABEL = "➕ Добавить штраф"
MANUAL_BOOTSTRAP_CONFIRM_LABEL = "✅ Подтвердить"
MANUAL_BOOTSTRAP_CANCEL_ARROW_LABEL = "⬅️ Отмена"
MANUAL_BOOTSTRAP_SAVE_FINE_LABEL = "✅ Сохранить штраф"
MANUAL_BOOTSTRAP_EDIT_LABEL = "✏️ Исправить"
MANUAL_BOOTSTRAP_CANCEL_X_LABEL = "❌ Отмена"
MANUAL_BOOTSTRAP_ADD_ANOTHER_LABEL = "➕ Добавить ещё штраф"
MANUAL_BOOTSTRAP_FINISH_LABEL = "✅ Завершить проверку"
MANUAL_BOOTSTRAP_FINISH_YES_LABEL = "✅ Да, завершить"
MANUAL_BOOTSTRAP_BACK_LABEL = "⬅️ Назад"
MANUAL_BOOTSTRAP_PENDING_QUEUE_LABEL = "⏳ Ожидают проверки"
MANUAL_BOOTSTRAP_OPEN_LABEL = "Открыть"
MANUAL_BOOTSTRAP_SKIP_COMMAND = "/skip"

MANUAL_BOOTSTRAP_ALREADY_COMPLETED_TEXT = "ℹ️ Проверка этого автомобиля уже завершена."
MANUAL_BOOTSTRAP_NO_PLATES_TASKS_TEXT = (
    "⚠️ Для этого номера не найдено активных задач мониторинга."
)
MANUAL_BOOTSTRAP_NO_PENDING_TEXT = "✅ Нет автомобилей, ожидающих первичной проверки."


def format_manual_bootstrap_nofine_confirm_prompt(car_number: str) -> str:
    return (
        f"🚗 {format_plate_for_telegram(car_number)}\n\n"
        "Подтвердить, что вы вручную проверили автомобиль на police.ge "
        "и неоплаченных штрафов нет?"
    )


def format_manual_bootstrap_nofine_done_manager_text(car_number: str) -> str:
    return (
        "✅ Проверка завершена\n"
        f"🚗 {format_plate_for_telegram(car_number)}\n"
        "Штрафов не найдено."
    )


MANUAL_BOOTSTRAP_PROTOCOL_PROMPT_TEXT = "Введите номер протокола/квитанции штрафа:"
MANUAL_BOOTSTRAP_AMOUNT_PROMPT_TEXT = (
    "Введите сумму штрафа (как указано на странице police.ge, только число, без валюты):"
)
MANUAL_BOOTSTRAP_DATE_PROMPT_TEXT = "Введите дату нарушения (в формате ДД.ММ.ГГГГ):"
MANUAL_BOOTSTRAP_PLACE_PROMPT_TEXT = (
    f"Введите место нарушения, если указано (или {MANUAL_BOOTSTRAP_SKIP_COMMAND}, чтобы пропустить):"
)
MANUAL_BOOTSTRAP_ARTICLE_PROMPT_TEXT = (
    f"Введите статью/описание нарушения, если указано "
    f"(или {MANUAL_BOOTSTRAP_SKIP_COMMAND}, чтобы пропустить):"
)
MANUAL_BOOTSTRAP_INVALID_AMOUNT_TEXT = "⚠️ Не удалось распознать сумму. Введите число, например: 50"
MANUAL_BOOTSTRAP_INVALID_DATE_TEXT = (
    "⚠️ Не удалось распознать дату. Введите в формате ДД.ММ.ГГГГ, например: 05.10.2026"
)


def format_manual_bootstrap_review(
    *, car_number: str, protocol_no: str, violation_date: date, amount: float,
    place: str | None, article: str | None,
) -> str:
    lines = [
        f"🚗 {format_plate_for_telegram(car_number)}",
        "",
        f"Протокол: {protocol_no}",
        f"Дата нарушения: {_fmt_date(violation_date)}",
        f"Сумма: {amount} GEL",
        f"Статья: {article or '—'}",
    ]
    if place:
        lines.append(f"Место: {place}")
    return "\n".join(lines)


def format_manual_bootstrap_duplicate_text(car_number: str, *, protocol_no: str) -> str:
    return (
        "ℹ️ Этот штраф уже есть в базе.\n"
        f"🚗 {format_plate_for_telegram(car_number)}\n"
        f"Протокол: {protocol_no}"
    )


MANUAL_BOOTSTRAP_FINE_SAVED_TEXT = "✅ Штраф сохранён."


def format_manual_bootstrap_finish_confirm_prompt(car_number: str) -> str:
    return (
        f"🚗 {format_plate_for_telegram(car_number)}\n\n"
        "Вы подтверждаете, что все найденные штрафы для этого автомобиля внесены?"
    )


def format_manual_bootstrap_finish_done_text(car_number: str, *, fines_added: int) -> str:
    return (
        "✅ Первичная проверка завершена\n"
        f"🚗 {format_plate_for_telegram(car_number)}\n"
        f"Добавлено штрафов: {fines_added}"
    )


def format_manual_bootstrap_pending_queue(plates: list[str]) -> str:
    if not plates:
        return MANUAL_BOOTSTRAP_NO_PENDING_TEXT
    lines = [MANUAL_BOOTSTRAP_PENDING_QUEUE_LABEL, ""]
    lines += [f"🚗 {format_plate_for_telegram(plate)}" for plate in plates]
    return "\n".join(lines)


def format_full_check_summary(outcome: FullCheckOutcome) -> str:
    """Итог оператору ПОСЛЕ завершения скана (см. задачу п.10) — НЕ
    перечисляет номера машин (см. задачу: "не перечислять все номера в
    Telegram") — только агрегированные счётчики."""
    return (
        "✅ Полная проверка завершена\n\n"
        f"Всего: {outcome.total}\n"
        f"Проверено успешно: {outcome.checked_ok}\n"
        f"С задолженностью: {outcome.with_debt}\n"
        f"Без задолженности: {outcome.without_debt}\n"
        f"Не удалось проверить: {outcome.failed}\n"
        f"Время выполнения: {_format_duration(outcome.duration_seconds)}"
    )
