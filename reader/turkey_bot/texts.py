"""Пользовательские тексты Turkey-бота. Русский язык — та же аудитория,
что и у @ProtocolGEbot (см. design report Stage 1).

ВАЖНО (см. задачу): сюда НИКОГДА не попадает GibSubmitOutcome.raw_data как
есть — format_has_debt_messages() ниже строит текст ТОЛЬКО из уже
типизированных GibFineRecord (см. reader/turkey_bot/gib/fine_parser.py),
которые сами по себе никогда не содержат KK_HASH/KK_KIMLIK (платёжные
токены GIB — этих полей нет в самом dataclass, не только "не показаны").
raw_data/messages целиком уходят только в TurkeyCheckRepository
(server-side), никогда в текст, который увидит пользователь.

location_ru/violation_description_ru (см. reader/turkey_bot/gib/
translation.py) показываются вместо турецкого оригинала, КОГДА они
заполнены — `location_ru or location` (та же логика для violation_
description) — тихий fallback на турецкий текст, если перевод недоступен/
не выполнялся (не пустая строка, не ошибка, см. conversation.py::
_translate_fines)."""

import re
from decimal import Decimal
from zoneinfo import ZoneInfo

from reader.turkey_bot.avrasya.models import AvrasyaDebtItem
from reader.turkey_bot.debt_refresh_service import TurkeyRefreshOutcome
from reader.turkey_bot.gib.models import GibFineRecord
from reader.turkey_bot.kgm.models import KgmOperatorResult
from reader.turkey_bot.models import TurkeyUserCar
from reader.turkey_bot.statistics_service import TurkeyStatistics
from reader.turkey_bot.unified.models import (
    OverallStatus,
    ProviderCheckResult,
    ProviderStatus,
    UnifiedCheckResult,
)

# Тот же Europe/Istanbul, что и reader/turkey_bot/monitoring/
# scheduler_job.py::TURKEY_MONITORING_TZ — НЕ импортируется оттуда напрямую
# (texts.py — leaf-модуль, monitoring/ на него не завязан ни в одну
# сторону), значение продублировано намеренно как константа отображения.
_DISPLAY_TZ = ZoneInfo("Europe/Istanbul")

# Telegram режет сообщение на границе 4096 символов — см. format_has_debt_
# messages()/_split_into_telegram_messages() про то, как несколько штрафов
# безопасно распределяются по нескольким сообщениям, не разрывая ни один
# штраф пополам (см. задачу: "preserve fine boundaries when splitting").
_TELEGRAM_MESSAGE_LIMIT = 4096

_KEYCAP_DIGITS = {
    "0": "0️⃣", "1": "1️⃣", "2": "2️⃣",
    "3": "3️⃣", "4": "4️⃣", "5": "5️⃣",
    "6": "6️⃣", "7": "7️⃣", "8": "8️⃣",
    "9": "9️⃣",
}

WELCOME_TEXT = (
    "🇹🇷 Проверка штрафов и задолженности по гос. номеру в Турции.\n\n"
    "Бот проверяет ОДНИМ действием сразу все источники — штрафы GİB, "
    "платные дороги Avrasya Tüneli и KGM.\n\n"
    "➕ Добавьте автомобиль, и бот предложит проверить его сразу и "
    "включить регулярный мониторинг (дважды в день)."
)

ASK_PLATE_FOR_NEW_CAR_TEXT = (
    "Отправьте гос. номер автомобиля, который нужно добавить (например, А123АА123)."
)

CAR_ADDED_TEMPLATE = "🚗 {plate} добавлен."
CAR_ALREADY_EXISTS_TEMPLATE = "🚗 {plate} уже есть в вашем списке."

EMPTY_MY_CARS_TEXT = (
    "📋 У вас пока нет добавленных автомобилей.\n\n"
    "Нажмите «➕ Добавить авто», чтобы добавить первый."
)
# "🚗 Мои автомобили" (см. design report "Адаптация car-centric UX
# Georgian bot") — та же строка, что и reader/public_bot/texts.py::
# MY_CARS_HEADER (см. car_monitoring_state/format_car_button_label ниже —
# тот же ON/OFF-паттерн, адаптированный под Turkey: без периодов/дат
# окончания, только active: bool, см. TurkeyMonitoringSubscription).
MY_CARS_HEADER = "🚗 Мои автомобили"

# manager/trusted-operator "🚗 Мои автомобили" (см. задачу "Реализуем
# manager/trusted 'Мои автомобили' для Turkey bot" — референс: Georgian
# bot TRUSTED_TASKS_HEADER/format_trusted_tasks_page) — сознательно
# ОТДЕЛЬНЫЙ текст от MY_CARS_HEADER выше, чтобы оператору было сразу
# видно, что он смотрит на ВСЕ машины, а не только свои.
MANAGER_CARS_HEADER = "🚗 Все автомобили"
MANAGER_CARS_EMPTY_TEXT = "Автомобилей пока нет ни у одного пользователя."


def format_manager_cars_page(*, page: int, total_pages: int) -> str:
    """Заголовок manager-списка — та же схема "Страница N из M" (1-indexed
    для человека), что и Georgian bot::format_trusted_tasks_page — список
    машин/владельцев ТОЛЬКО в inline-клавиатуре (см.
    keyboards.py::manager_cars_page_keyboard), не в этом тексте."""
    return f"{MANAGER_CARS_HEADER}\nСтраница {page + 1} из {total_pages}"

MONITORING_STOPPED_ALL_TEMPLATE = "⛔ Мониторинг остановлен для всех подписок ({count})."

HISTORY_EMPTY_TEMPLATE = "📜 История {plate}\n\nПроверок пока не было."

CAR_NOT_FOUND_TEXT = "Автомобиль не найден или принадлежит другому пользователю."

INVALID_PLATE_TEXT = (
    "Не похоже на гос. номер. Отправьте номер буквами (кириллица или "
    "латиница) и цифрами, например: А123АА123."
)

# Провайдер-агностичный текст (см. design report Stage 2B) — одинаково
# уместен и для GIB, и для Avrasya CAPTCHA: ни то, ни другое не упоминается
# по имени, поэтому переиспользуется для обоих провайдеров без дублирования.
ASK_CAPTCHA_TEXT = "Введите код с картинки (регистр — как на картинке)."

CAPTCHA_FETCH_FAILED_TEXT = (
    "Не удалось получить CAPTCHA с сайта GIB (сбой соединения). "
    "Попробуйте ещё раз через минуту."
)

AVRASYA_CAPTCHA_FETCH_FAILED_TEXT = (
    "Не удалось получить CAPTCHA с сайта Avrasya Tüneli (сбой соединения). "
    "Попробуйте ещё раз через минуту."
)

AVRASYA_RATE_LIMITED_TEXT = (
    "Сайт Avrasya Tüneli временно ограничивает частоту запросов. "
    "Попробуйте ещё раз через минуту."
)

KGM_CAPTCHA_FETCH_FAILED_TEXT = (
    "Не удалось получить CAPTCHA с сайта KGM (сбой соединения). "
    "Попробуйте ещё раз через минуту."
)

# Провайдер-агностичный (см. ASK_CAPTCHA_TEXT выше) — переиспользуется для
# обоих провайдеров.
CAPTCHA_REJECTED_RETRY_TEXT = "Код введён неверно. Вот новая CAPTCHA — попробуйте ещё раз."

# Провайдер-агностичный (не упоминает GIB по имени) — переиспользуется для
# обоих провайдеров, см. design report Stage 2B.
SESSION_EXPIRED_RETRY_TEXT = (
    "Предыдущая CAPTCHA-сессия больше не действительна (бот перезапускался "
    "или сессия истекла). Вот новая CAPTCHA для того же номера — введите код."
)

# Провайдер-агностичный — переиспользуется для обоих провайдеров.
SESSION_LOST_TEXT = (
    "Не удалось восстановить проверку. Отправьте гос. номер ещё раз, чтобы начать заново."
)

TRANSPORT_ERROR_TEXT = "Не удалось связаться с сайтом GIB. Попробуйте ещё раз через минуту."

AVRASYA_TRANSPORT_ERROR_TEXT = (
    "Не удалось связаться с сайтом Avrasya Tüneli. Попробуйте ещё раз через минуту."
)

KGM_TRANSPORT_ERROR_TEXT = "Не удалось связаться с сайтом KGM. Попробуйте ещё раз через минуту."

UNEXPECTED_ERROR_TEXT = (
    "GIB вернул ответ в формате, который бот пока не понимает. Мы уже знаем "
    "об этом — попробуйте позже."
)

# Используется и для реально "unexpected", и для (пока никогда не
# наблюдавшегося вживую) has_debt (см. design report Stage 2B: "do not
# guess its schema or expose raw JSON to the user") — сырой ответ уходит
# ТОЛЬКО в TurkeyTollCheckRepository (server-side), пользователь видит
# одинаковое безопасное сообщение для обоих случаев, пока реальная форма
# has_debt не будет захвачена вживую (см. avrasya/parser.py).
AVRASYA_UNEXPECTED_TEXT = (
    "Avrasya Tüneli вернул ответ, который бот пока не понимает. Мы уже "
    "знаем об этом — попробуйте позже."
)

# Тот же принцип, что и AVRASYA_UNEXPECTED_TEXT — покрывает и реально
# "unexpected" (см. reader/turkey_bot/kgm/parser.py про fail-closed
# правила), и (пока не подтверждённый вживую) has_debt без единой
# распарсенной записи.
KGM_UNEXPECTED_TEXT = (
    "KGM вернул ответ, который бот пока не понимает. Мы уже знаем об "
    "этом — попробуйте позже."
)

CANCEL_CONFIRM_TEXT = "Проверка отменена. Отправьте гос. номер, чтобы начать заново."
NOTHING_TO_CANCEL_TEXT = "Сейчас нет активной проверки для отмены."

CANCEL_BUTTON_LABEL = "❌ Отмена"

# Главное reply-меню (см. reader/turkey_bot/keyboards.py::main_menu_keyboard) -
# GARAGE_LABEL/CHECK_FINES_LABEL/CHECK_TOLLS_LABEL видны ВСЕМ,
# STATISTICS_LABEL — ТОЛЬКО trusted-менеджерам (см. design report: "reuse
# the same trusted manager IDs... already used by ProtocolGEbot" —
# settings.public_bot.trusted_operator_user_ids). CHECK_FINES_LABEL/
# CHECK_TOLLS_LABEL ТАКЖЕ переиспользуются как текст inline-кнопок в
# garage_keyboard() (см. design report Stage 2B: "Saved cars must show
# both actions" — те же две подписи, одна константа на каждую, не
# дублируются под другим именем).
# НОВОЕ главное меню (см. design report "Перестроить UX Turkey test bot",
# reader/turkey_bot/keyboards.py::main_menu_keyboard) — GİB/Avrasya/KGM
# больше не выбираются пользователем явно (см. UnifiedTurkeyCheckService),
# поэтому CHECK_FINES_LABEL/CHECK_TOLLS_LABEL ниже сохранены как константы
# (ссылки на них могут остаться в старых текстах справки), но БОЛЬШЕ НЕ
# используются в главном меню/клавиатурах.
ADD_CAR_LABEL = "➕ Добавить авто"
MY_CARS_LABEL = "📋 Мои авто"
CHECK_NOW_LABEL = "🔎 Проверить сейчас"
# ⛔ Остановить мониторинг — БОЛЬШЕ НЕ показывается в manager main menu
# (см. задачу "manager/trusted Search" п.1/п.16: заменена на SEARCH_LABEL
# ниже, см. reader/turkey_bot/keyboards.py::main_menu_keyboard) —
# константа и её underlying-логика (handle_stop_monitoring/
# TurkeyMonitoringSubscriptionRepository.deactivate_all) сознательно НЕ
# удалены (задача явно требует "underlying functionality не удалять").
STOP_MONITORING_LABEL = "⛔ Остановить мониторинг"
# Manager/trusted Search (см. задачу) — заменяет STOP_MONITORING_LABEL в
# manager main menu.
SEARCH_LABEL = "🔎 Поиск"
# Play/stop-иконки (см. задачу "Адаптация car-centric UX Georgian bot") —
# ОТЛИЧАЮТСЯ от буквальной формулировки Georgian bot (TURN_OFF_BUTTON_LABEL
# = "⏸ Выключить мониторинг", pause-иконка) — здесь используется точный
# текст, явно и многократно заданный в задаче для карточки Turkey-авто.
ENABLE_MONITORING_LABEL = "▶️ Включить мониторинг"
DISABLE_MONITORING_LABEL = "⏹ Отключить мониторинг"
HISTORY_LABEL = "📜 История"
DELETE_CAR_LABEL = "🗑 Удалить автомобиль"
BACK_LABEL = "⬅️ Назад"

# Двухшаговое подтверждение удаления (см. reader/public_bot/texts.py::
# DELETE_CAR_CONFIRM_BUTTON_LABEL/CANCEL_BUTTON_LABEL — тот же Georgian
# pattern, отдельная константа отмены, НЕ переиспользует CANCEL_BUTTON_LABEL
# выше — та привязана к другому flow (отмена ➕ Добавить авто) и к другому
# callback_data).
DELETE_CAR_CONFIRM_BUTTON_LABEL = "🗑 Да, удалить"
DELETE_CANCEL_LABEL = "Отмена"

# Общий, намеренно НЕИНФОРМАТИВНЫЙ отказ для ON/OFF/Delete confirm/cancel
# (см. UNKNOWN_BUTTON_TEXT ниже про тот же принцип для "не найдено/чужое")
# — используется, когда владение НА МОМЕНТ действия подтверждено, но само
# действие не может быть выполнено (гоночное состояние между двумя
# действиями одного пользователя).
CAR_ACTION_FAILED_TEXT = "⚠️ Не удалось выполнить действие — откройте автомобиль заново через «📋 Мои авто»."

GARAGE_LABEL = "🚗 Мои авто"
STATISTICS_LABEL = "📊 Статистика"
CHECK_FINES_LABEL = "🚔 Штрафы"
CHECK_TOLLS_LABEL = "🛣 Платные дороги"

# Подменю CHECK_TOLLS_LABEL (см. design report "Реализация KGM provider"
# п.10) — выбор конкретного провайдера платных дорог ДО ввода номера, а
# не сразу Avrasya (см. reader/turkey_bot/keyboards.py::
# toll_provider_keyboard). Avrasya НЕ удалена и НЕ изменена — только
# появился явный выбор рядом.
# Отображаемый текст — "🚇 Туннели" (не "Avrasya Tüneli", см. задачу:
# "изменить только отображаемый текст кнопки") — provider/callback
# по-прежнему буквально "avrasya" везде (см. keyboards.py::
# encode_toll_provider_callback/encode_garage_check_callback) — эта
# константа НЕ участвует в кодировании callback_data, только в тексте
# кнопки.
CHECK_TOLLS_AVRASYA_LABEL = "🚇 Туннели"
CHECK_TOLLS_KGM_LABEL = "🛣 Все дороги и мосты (KGM)"

# Укороченные подписи ТОЛЬКО для инлайн-кнопок гаража (см.
# reader/turkey_bot/keyboards.py::garage_keyboard, design report
# "унификация UI") — ОТДЕЛЬНЫЕ константы, НЕ переиспользуют
# CHECK_FINES_LABEL/CHECK_TOLLS_KGM_LABEL, потому что те показываются и в
# других местах (главное меню/подменю платных дорог), где текст менять не
# просили. callback_data/provider routing НЕ меняются вовсе (gib/avrasya/
# kgm остаются буквально теми же строками) — эти константы влияют
# ИСКЛЮЧИТЕЛЬНО на отображаемый текст кнопки.
GARAGE_CHECK_FINES_LABEL = "🚔 Штрафы"
GARAGE_CHECK_TOLLS_KGM_LABEL = "🛣 Дороги"
# ℹ️ Справка (см. design report) — видна ВСЕМ, как и CHECK_FINES_LABEL/
# CHECK_TOLLS_LABEL/GARAGE_LABEL (НЕ trusted-gated, в отличие от
# STATISTICS_LABEL).
HELP_LABEL = "ℹ️ Справка"

# Переход в Georgian-бот (см. design report "унификация UI") —
# GEORGIAN_BOT_LINK_LABEL теперь ОБЫЧНАЯ reply-кнопка в главном меню (ROW
# 2, см. reader/turkey_bot/keyboards.py::main_menu_keyboard), видна ВСЕМ
# пользователям одинаково (не trusted-gated). Нажатие распознаётся как
# текст (см. reader/turkey_bot/conversation.py::handle_text) и отвечает
# ОТДЕЛЬНЫМ сообщением (GEORGIAN_BOT_LINK_TEXT) с inline URL-кнопкой (см.
# georgian_bot_link_keyboard) — Telegram reply-кнопки физически не могут
# сами быть URL-кнопками. ProtocolGEbot — реальный username Georgian-бота,
# взят из reader/public_bot/main.py::_BOT_USERNAME (тот же приём
# отдельного hardcode на каждой стороне — оба бота полностью независимые
# процессы, не импортируют друг у друга).
GEORGIAN_BOT_LINK_LABEL = "🇬🇪 Штрафы Грузии"
GEORGIAN_BOT_LINK_TEXT = "🇬🇪 Проверка штрафов в Грузии"
GEORGIAN_BOT_URL = "https://t.me/ProtocolGEbot"

# One-off сообщение существующим пользователям после production
# deployment (см. задачу "Перенос Unified Turkey функционала в
# production" п.8, reader/turkey_bot/migration_notify.py::notify_all) —
# ТОЛЬКО production-бот когда-либо отправляет этот текст (test-бот у
# существующих пользователей не было, переносить нечего).
MIGRATION_NOTIFICATION_TEXT = (
    "🚗 Бот обновлён\n"
    "Ваши ранее проверенные автомобили добавлены в «Мои авто»"
)

# Подписи 4 разделов ℹ️ Справка + общая "⬅️ Назад" (см.
# reader/turkey_bot/keyboards.py::help_menu_keyboard/help_section_keyboard).
# HELP_GIB_LABEL/HELP_AVRASYA_LABEL — внутренние ключи секций ("gib"/
# "avrasya", см. conversation.py::handle_help_callback) НЕ переименованы
# (чисто техническая деталь маршрутизации, пользователю не видна), но сами
# подписи и содержимое обновлены под актуальный unified-UX (см. задачу
# "исправь справку в соответствии с актуальным функционалом" — раньше
# описывали provider-by-provider выбор через "🚔 Штрафы"/"🛣 Платные
# дороги" и видимую CAPTCHA, которых в текущем UI больше нет).
HELP_TERMS_LABEL = "📖 Термины"
HELP_GIB_LABEL = "❓ Как проверить авто"
HELP_AVRASYA_LABEL = "🔔 Мои авто и мониторинг"
HELP_PAYMENT_LABEL = "💳 Как оплатить"
HELP_BACK_LABEL = "⬅️ Назад"

# Показывается сразу после нажатия CHECK_FINES_LABEL/CHECK_TOLLS_LABEL в
# главном меню — armит соответствующего провайдера для СЛЕДУЮЩЕГО
# введённого номера (см. conversation.py::_STEP_AWAITING_PLATE). Голое
# ввод номера БЕЗ предварительного нажатия кнопки по-прежнему сразу
# начинает GIB-проверку (см. design report: "bare plate still defaults to
# GİB" — регрессия, не новое поведение).
ASK_PLATE_FOR_FINES_TEXT = (
    "Отправьте гос. номер автомобиля для проверки штрафов (например, А123АА123)."
)
ASK_PLATE_FOR_TOLLS_TEXT = (
    "Отправьте гос. номер автомобиля для проверки платных дорог Avrasya "
    "Tüneli (например, А123АА123)."
)

# Показывается после нажатия CHECK_TOLLS_LABEL — ДО выбора конкретного
# провайдера (см. keyboards.py::toll_provider_keyboard).
CHOOSE_TOLL_PROVIDER_TEXT = "🛣 Проверить платные дороги — выберите, что проверить:"

ASK_PLATE_FOR_KGM_TEXT = (
    "Отправьте гос. номер автомобиля для проверки всех платных дорог и "
    "мостов Турции через KGM (например, А123АА123)."
)

GARAGE_HEADER = "🚗 Мои автомобили"
EMPTY_GARAGE_TEXT = (
    "🚗 У вас пока нет автомобилей.\n\n"
    "Проверьте автомобиль по номеру — после успешной проверки он появится здесь."
)

HELP_MENU_TEXT = "ℹ️ Справка\n\nВыберите раздел:"

HELP_TERMS_TEXT = (
    "📖 Термины\n\n"
    "🚔 Штрафы GİB — дорожные штрафы, найденные в государственной "
    "системе Турции.\n\n"
    "🚇 Avrasya Tüneli — платный автомобильный тоннель.\n\n"
    "🚧 KGM — остальные платные дороги и мосты Турции (кроме Avrasya "
    "Tüneli, который выделен отдельно).\n\n"
    "Бот проверяет GİB, Avrasya Tüneli и KGM ОДНИМ действием — выбирать "
    "их по отдельности не нужно.\n\n"
    "🚗 Стоимость проездов — стоимость проезда по платной дороге или "
    "тоннелю, которую необходимо было оплатить.\n\n"
    "⚠️ Начисленные штрафы — дополнительная сумма, начисленная за "
    "несвоевременную оплату проезда.\n\n"
    "💰 Итого — общая сумма задолженности по всем источникам."
)

# См. задачу "исправь справку в соответствии с актуальным функционалом" —
# провайдер-by-провайдер выбор (отдельные кнопки "🚔 Штрафы"/"🛣 Платные
# дороги", видимая CAPTCHA) убран из UI (см. UnifiedTurkeyCheckService/
# CaptchaResolver — CAPTCHA решается сервером, пользователь её не видит,
# см. conversation.py docstring), текст справки обновлён под РЕАЛЬНЫЙ
# текущий flow: добавление номера -> ОДИН unified-результат сразу по всем
# трём источникам.
HELP_GIB_TEXT = (
    "❓ Как проверить авто\n\n"
    "1. Нажмите «➕ Добавить авто» и отправьте гос. номер (например, "
    "A123AA123) — либо просто отправьте номер без предварительного "
    "нажатия кнопки.\n"
    "2. Бот добавит автомобиль и предложит проверить его сразу.\n"
    "3. Бот одним действием проверит штрафы GİB, Avrasya Tüneli и KGM и "
    "покажет общий результат."
)

HELP_AVRASYA_TEXT = (
    "🔔 Мои авто и мониторинг\n\n"
    "В разделе «📋 Мои авто» каждый автомобиль — отдельная кнопка: "
    "🟢 — мониторинг включён, ⚪ — выключен.\n\n"
    "Откройте автомобиль, чтобы:\n"
    "🔎 Проверить сейчас — мгновенная проверка;\n"
    "▶️/⏹ Включить/Отключить мониторинг — автоматическая проверка "
    "дважды в день (13:00 и 21:00 по времени Турции) с уведомлением при "
    "появлении, исчезновении или изменении суммы задолженности;\n"
    "📜 История — прошлые проверки;\n"
    "🗑 Удалить автомобиль — с подтверждением."
)

# CTA ("💳 Оплатить в рублях"/"🚗 ОСАГО Турции") появляется после ЛЮБОГО
# unified-результата с has_debt — по GİB, Avrasya Tüneli ИЛИ KGM (см.
# reader/turkey_bot/conversation.py::_debt_cta_buttons/
# _run_manual_check — НЕ только Avrasya, как было раньше при
# provider-специфичном UX). URL кнопок НЕ повторяется здесь текстом
# (settings.public_bot.payment_help_contact_username остаётся единственным
# источником) — этот текст только объясняет, что кнопки делают.
HELP_PAYMENT_TEXT = (
    "💳 Как оплатить\n\n"
    "Бот сам не принимает оплату.\n\n"
    "Если при проверке будет найдена задолженность (штрафы GİB, Avrasya "
    "Tüneli или KGM), под результатом появятся кнопки:\n\n"
    "💳 Оплатить в рублях\n"
    "🚗 ОСАГО Турции\n\n"
    "Они откроют чат с оператором, который поможет с оплатой/оформлением."
)

# Общий, намеренно НЕИНФОРМАТИВНЫЙ текст — и для реально неизвестного
# callback_data, и для garage-callback'а с чужим/несуществующим car_id (см.
# design report: одинаковый ответ в обоих случаях — НЕ раскрывает,
# существует ли машина вообще, просто принадлежит не этому пользователю,
# см. задачу: "A normal user must not be able to inspect another user's
# garage by forging callback data").
UNKNOWN_BUTTON_TEXT = "Неизвестная или устаревшая кнопка"


def no_debt_text(plate: str) -> str:
    return f"✅ {plate}: штрафов и задолженности в GIB не найдено."


def avrasya_no_debt_text(plate: str) -> str:
    """См. design report Stage 2B: "clear user-facing success message
    that no unpaid Avrasya passages were found" — HTTP 404 + пустое тело
    (см. avrasya/parser.py — реально увиденная вживую форма)."""
    return f"✅ {plate}: неоплаченных проездов по Avrasya Tüneli не найдено."


def kgm_no_debt_text(plate: str) -> str:
    """См. reader/turkey_bot/kgm/parser.py::parse_result_panel — "общий
    no_debt" требует, чтобы ВСЕ 10 операторов страницы явно сообщили
    "Kayıt yok." — ПОДТВЕРЖДЕНО вживую (design report "KGM no_debt live
    confirmation", plate A123AA180, см. tests/fixtures/
    kgm_sorgulama_a123aa180_no_debt.html)."""
    return f"✅ {plate}: неоплаченных проездов по платным дорогам и мостам Турции (KGM) не найдено."


def _format_try_amount(amount: Decimal) -> str:
    """Российский числовой формат (пробел — разделитель тысяч), суффикс
    "₺" (см. design report Stage 2C, желаемый вывод: "780 ₺"/"1 800 ₺") —
    БЕЗ дробной части, когда сумма целая (реально увиденные вживую суммы
    все целые, см. parser.py), иначе — 2 знака после запятой (Decimal-safe,
    см. avrasya/parser.py::_parse_decimal_amount про то, откуда приходит
    Decimal без float-артефактов)."""
    quantized = amount.quantize(Decimal("0.01"))
    if quantized == quantized.to_integral_value():
        return f"{int(quantized):,}".replace(",", " ") + " ₺"
    formatted = f"{quantized:,.2f}".replace(",", " ").replace(".", ",")
    return f"{formatted} ₺"


def format_avrasya_has_debt_message(plate: str, debt_items: tuple[AvrasyaDebtItem, ...]) -> str:
    """См. design report Stage 2C — строит текст ТОЛЬКО из уже
    типизированных AvrasyaDebtItem (см. reader/turkey_bot/avrasya/
    models.py), НИКОГДА из raw_data напрямую (см. модуль docstring про тот
    же принцип у GIB). НИКОГДА не показывает ExitDate/ExitStation/
    DebtorContactFullName/UniqueId/сырой JSON — AvrasyaDebtItem их вообще
    не несёt (не только "не показаны", см. models.py). "Начисленные
    штрафы" — строка показывается, ТОЛЬКО когда суммарный штраф > 0 (см.
    задачу: "when explicitly supplied") — тот же принцип "не показывать
    поле вовсе, когда оно отсутствует/пусто", что и у GIB
    _format_fine_block. Подписи "Стоимость проездов"/"Начисленные штрафы"
    (см. design report: "Improve Avrasya result terminology" — те же
    термины, что и в ℹ️ Справка -> 📖 Термины, см. HELP_TERMS_TEXT) —
    ТОЛЬКО текст меток изменился, расчёты (_format_try_amount/суммы)
    остались прежними."""
    total_principal = sum((item.principal_amount for item in debt_items), Decimal(0))
    total_penalty = sum(
        (item.penalty_amount for item in debt_items if item.penalty_amount is not None),
        Decimal(0),
    )
    total_payable = sum((item.total_amount for item in debt_items), Decimal(0))

    lines = [
        f"⚠️ {plate}: найдены неоплаченные проезды по Avrasya Tüneli.",
        "",
        f"Проездов: {len(debt_items)}",
        f"Стоимость проездов: {_format_try_amount(total_principal)}",
    ]
    if total_penalty > 0:
        lines.append(f"Начисленные штрафы: {_format_try_amount(total_penalty)}")
    lines.append(f"Итого к оплате: {_format_try_amount(total_payable)}")
    return "\n".join(lines)


# Эмодзи по operator_key (см. design report примера вывода: "🚧 KGM"/
# "🚇 Avrasya Tüneli") — для остальных 8 приватных операторов один общий
# 🛣 (см. задачу: конкретный emoji для них не был указан отдельно).
_KGM_OPERATOR_EMOJI = {"kgm": "🚧", "avrasya": "🚇"}
_KGM_DEFAULT_OPERATOR_EMOJI = "🛣"


def _kgm_operator_emoji(operator_key: str) -> str:
    return _KGM_OPERATOR_EMOJI.get(operator_key, _KGM_DEFAULT_OPERATOR_EMOJI)


def _format_kgm_item_block(item) -> str:
    """Одна запись ОДНОГО оператора (см. design report примера вывода) —
    entry_station/exit_station/penalty_free_deadline пропускаются, если
    их нет вовсе (см. reader/turkey_bot/kgm/models.py::KgmDebtItem — None
    означает "буквально пусто на сайте", не ошибку разбора)."""
    lines = [item.date_time.strftime("%d.%m.%Y %H:%M")]
    if item.entry_station and item.exit_station:
        lines.append(f"{item.entry_station} → {item.exit_station}")
    elif item.exit_station:
        lines.append(item.exit_station)
    elif item.entry_station:
        lines.append(item.entry_station)
    lines.append(f"Стоимость: {_format_try_amount(item.base_toll)}")
    lines.append(f"К оплате: {_format_try_amount(item.payable_amount)}")
    if item.penalty_free_deadline is not None:
        lines.append(f"Без штрафа до: {item.penalty_free_deadline.strftime('%d.%m.%Y')}")
    return "\n".join(lines)


def _format_kgm_operator_block(operator: KgmOperatorResult) -> str:
    header = f"{_kgm_operator_emoji(operator.operator_key)} {operator.operator_name}"
    item_blocks = [_format_kgm_item_block(item) for item in operator.items]
    return header + "\n" + "\n\n".join(item_blocks)


def format_kgm_has_debt_messages(
    plate: str, operators: tuple[KgmOperatorResult, ...], *,
    kgm_total: Decimal, yid_total: Decimal, grand_total: Decimal,
) -> list[str]:
    """См. design report примера вывода — строит текст ТОЛЬКО из уже
    типизированных KgmOperatorResult/KgmDebtItem (см.
    reader/turkey_bot/kgm/models.py), НИКОГДА из сырого HTML напрямую
    (тот же принцип, что и у GIB/Avrasya, см. модуль docstring). Пустые
    (без задолженности) операторы сюда вообще не попадают (см.
    reader/turkey_bot/kgm/parser.py — outcome.operators уже содержит
    ТОЛЬКО операторов с реальной задолженностью, "Не показывать пустые
    operator sections"). Возвращает СПИСОК готовых Telegram-сообщений
    (см. format_has_debt_messages выше — тот же _split_into_telegram_
    messages, KGM может агрегировать записи сразу нескольких операторов,
    потенциально больше, чем у одного GIB/Avrasya)."""
    blocks = [f"🛣 {plate}: платные дороги Турции — найдена задолженность."]
    blocks.extend(_format_kgm_operator_block(operator) for operator in operators)

    footer_lines = [
        f"Итого KGM: {_format_try_amount(kgm_total)}",
        f"Итого YİD: {_format_try_amount(yid_total)}",
        f"Всего: {_format_try_amount(grand_total)}",
    ]
    blocks.append("\n".join(footer_lines))

    return _split_into_telegram_messages(blocks)


HAS_DEBT_NO_PARSED_FINES_TEXT_TEMPLATE = (
    "⚠️ {plate}: по данным GIB, задолженность НАЙДЕНА, но бот не смог "
    "разобрать ни одной записи. За подробностями обратитесь на "
    "официальный сайт GIB (dijital.gib.gov.tr)."
)


def _keycap_number(n: int) -> str:
    return "".join(_KEYCAP_DIGITS[digit] for digit in str(n))


def _format_amount(amount: Decimal) -> str:
    return f"{amount.quantize(Decimal('0.01'))} TRY"


def _format_date_ru(value) -> str:
    return value.strftime("%d.%m.%Y")


def _format_fine_block(index: int, fine: GibFineRecord, *, show_authority: bool) -> str:
    """Ни одна строка не выводится, если соответствующее поле
    отсутствует/не распознано (см. задачу: "do not show a field at all
    when it is absent/empty rather than printing None, null, —, etc.")."""
    header = f"{_keycap_number(index)} Штраф"
    if fine.protocol_no:
        header += f" № {fine.protocol_no}"
    lines = [header]

    if fine.amount is not None:
        lines.append(f"💰 Сумма: {_format_amount(fine.amount)}")

    # Структурные поля (см. reader/turkey_bot/gib/fine_parser.py) - ЕСЛИ
    # хотя бы одно из двух распарсено, показываем их отдельно (с переводом
    # на русский, если доступен, см. `_ru or original` fallback ниже) и
    # НЕ дублируем сырой KK_ACIKLAMA целиком (см. задачу: "do not display
    # the raw combined KK_ACIKLAMA when structured fields were parsed
    # successfully"). Иначе - безопасный fallback на исходное поведение
    # (сырое описание целиком), см. задачу: "If structured parsing fails,
    # fall back gracefully to: 📍 Нарушение: <original KK_ACIKLAMA>".
    if fine.location is not None or fine.violation_description is not None:
        if fine.location:
            lines.append(f"📍 Место: {fine.location_ru or fine.location}")
        if fine.violation_description:
            lines.append(
                f"📝 Нарушение: {fine.violation_description_ru or fine.violation_description}"
            )
        if fine.law_article:
            lines.append(f"📜 Статья: {fine.law_article}")
    elif fine.description:
        lines.append(f"📍 Нарушение: {fine.description}")

    if fine.violation_date is not None:
        lines.append(f"📅 Дата нарушения: {_format_date_ru(fine.violation_date)}")
    if show_authority and fine.authority:
        lines.append(f"🏛 Орган: {fine.authority}")
    if fine.late_fee is not None and fine.late_fee > 0:
        lines.append(f"⚠️ Пеня: {_format_amount(fine.late_fee)}")
    if fine.discount is not None and fine.discount > 0:
        lines.append(f"🏷 Скидка: -{_format_amount(fine.discount)}")

    return "\n".join(lines)


def _split_into_telegram_messages(
    blocks: list[str], *, limit: int = _TELEGRAM_MESSAGE_LIMIT,
) -> list[str]:
    """Пакует блоки (уже готовые, атомарные куски текста) в минимальное
    число сообщений, каждое не длиннее limit, НИКОГДА не разрывая один
    блок между двумя сообщениями (см. задачу: "preserve fine boundaries
    when splitting") — если единственный блок сам по себе длиннее limit
    (на практике не наблюдалось — реальные описания заметно короче),
    он становится отдельным, единственным пере-лимитным сообщением, а не
    обрезается молча (см. задачу: "do not silently truncate fines")."""
    messages: list[str] = []
    current: list[str] = []
    current_len = 0

    for block in blocks:
        addition = len(block) if not current else len(block) + 2  # +2 = "\n\n"
        if current and current_len + addition > limit:
            messages.append("\n\n".join(current))
            current = []
            current_len = 0
            addition = len(block)
        current.append(block)
        current_len += addition

    if current:
        messages.append("\n\n".join(current))

    return messages


def format_has_debt_messages(plate: str, fines: tuple[GibFineRecord, ...]) -> list[str]:
    """Заменяет старую заглушку-плейсхолдер (см. задачу) реальным,
    структурированным результатом — построено ТОЛЬКО из GibFineRecord
    (см. reader/turkey_bot/gib/fine_parser.py), никогда из raw_data
    напрямую (см. модуль docstring). Возвращает СПИСОК готовых
    Telegram-сообщений (см. _split_into_telegram_messages) — вызывающий
    код (conversation.py) отправляет messages[0] как основной ответ и
    остальные как дополнительные сообщения (см. BotReply.extra_texts)."""
    if not fines:
        # Защитный случай: has_debt без единой распарсенной записи не
        # должен происходить в норме (см. reader/turkey_bot/gib/parser.py),
        # но лучше безопасная заглушка, чем пустое/сломанное сообщение.
        return [HAS_DEBT_NO_PARSED_FINES_TEXT_TEMPLATE.format(plate=plate)]

    authorities = {fine.authority for fine in fines if fine.authority}
    shared_authority = next(iter(authorities)) if len(authorities) == 1 else None
    show_per_fine_authority = shared_authority is None

    blocks = [f"⚠️ {plate}: найдены штрафы — {len(fines)}"]

    total = Decimal(0)
    for index, fine in enumerate(fines, start=1):
        blocks.append(_format_fine_block(index, fine, show_authority=show_per_fine_authority))
        if fine.amount is not None:
            total += fine.amount
        if fine.late_fee is not None:
            total += fine.late_fee
        if fine.discount is not None:
            total -= fine.discount

    footer_lines = []
    if shared_authority:
        footer_lines.append(f"🏛 Орган: {shared_authority}")
    footer_lines.append(f"💰 Общая задолженность: {_format_amount(total)}")
    blocks.append("\n".join(footer_lines))

    return _split_into_telegram_messages(blocks)


def format_statistics_header(stats: TurkeyStatistics) -> str:
    """"👥 Пользователи" — верхняя часть "📊 Статистика", ПЕРЕД debt-блоком
    (см. format_debt_summary_block/format_statistics_footer ниже) —
    вынесена отдельно, чтобы ConversationController мог переиспользовать
    ТОЛЬКО debt-блок в результате "🔄 Проверить авто с задолженностью" (см.
    задачу "manager Statistics / refresh для обоих ботов" п.7 — там НЕ
    нужны 👥 Пользователи/🔎 Проверки, только сам факт проверки + актуальный
    debt-блок)."""
    return "\n".join([
        STATISTICS_LABEL,
        "",
        "👥 Пользователи",
        f"Всего: {stats.total_users}",
        f"Новых сегодня: {stats.new_users_today}",
        f"Новых за 7 дней: {stats.new_users_7d}",
        f"Новых за 30 дней: {stats.new_users_30d}",
    ])


def format_debt_summary_block(*, debt_car_count: int, debt_total_amount: Decimal) -> str:
    """"🚨 Задолженность по последней проверке" (debt_car_count/
    debt_total_amount — см. TurkeyStatisticsService.get_debt_rows(),
    ТОЛЬКО последний ДОСТОВЕРНЫЙ, не-ERROR результат на машину).
    Переиспользуется И обычным 📊 Статистика, И результатом "🔄 Проверить
    авто с задолженностью" (см. ConversationController._build_debt_section)
    — один и тот же блок в обоих местах. См. задачу "manager Statistics /
    refresh для обоих ботов" — единый компактный формат для Georgia и
    Turkey: БЕЗ "≥"-пометки PARTIAL (PARTIAL по-прежнему учитывается в
    персистентном состоянии, см. get_debt_rows()/TurkeyDebtRow.is_partial,
    просто больше не выделяется визуально)."""
    return "\n".join([
        "🚨 Задолженность по последней проверке",
        f"Автомобилей: {debt_car_count}",
        f"Общая сумма: {_format_try_amount(debt_total_amount)}",
    ])


def format_statistics_footer(stats: TurkeyStatistics) -> str:
    """"🔎 Проверки"/подписки/ошибки провайдеров — НИЖНЯЯ часть "📊
    Статистика", ПОСЛЕ debt-блока — НЕ показывается в результате "🔄
    Проверить авто с задолженностью" (см. задачу п.7 — там только факт
    проверки + debt-блок, без остальной статистики)."""
    provider_names = {"gib": "GİB", "avrasya": "Avrasya", "kgm": "KGM"}
    lines = [
        "🔎 Проверки",
        f"Всего: {stats.total_checks}",
        f"Сегодня: {stats.checks_today}",
        f"За 7 дней: {stats.checks_7d}",
        f"За 30 дней: {stats.checks_30d}",
        f"Ручных: {stats.manual_checks}",
        f"По расписанию: {stats.scheduled_checks}",
        "",
        f"🔔 Активных подписок мониторинга: {stats.active_monitoring_subscriptions}",
        "",
        "Ошибки провайдеров:",
    ]
    for provider_key, count in stats.provider_error_counts.items():
        lines.append(f"  {provider_names.get(provider_key, provider_key)}: {count}")
    return "\n".join(lines)


def format_statistics(
    stats: TurkeyStatistics, *, debt_car_count: int, debt_total_amount: Decimal,
) -> str:
    """См. reader/turkey_bot/statistics_service.py::
    TurkeyStatisticsService — теперь на основе unified-check (все три
    провайдера одинаково, см. design report "Перестроить UX Turkey test
    bot" п.13), плюс мониторинг-метрики.

    Список username УБРАН целиком (см. задачу "доработать 📊 Статистика
    Turkey bot" п.1 — для просмотра конкретных пользователей есть
    "🚗 Мои авто"/"🔎 Поиск", агрегаты 👥 Пользователи остаются как были)."""
    return "\n\n".join([
        format_statistics_header(stats),
        format_debt_summary_block(debt_car_count=debt_car_count, debt_total_amount=debt_total_amount),
        format_statistics_footer(stats),
    ])


def format_debt_row(*, car_number: str, owner_display: str, total_amount: Decimal) -> str:
    """"🚗 CAR: owner: amount ₺" (см. задачу "manager Statistics / refresh
    для обоих ботов" п.1 — единый компактный формат, тот же, что и у
    Georgia, БЕЗ даты/checked_at/"сегодня"/"вчера"/"N дней назад" и БЕЗ
    " — "-разделителей/"≥"/"· частично"). total_amount — уже готовый
    authoritative unified total (см. TurkeyDebtRow), НЕ пересчитывается
    здесь."""
    amount_text = _format_try_amount(total_amount)
    return f"🚗 {car_number}: {owner_display}: {amount_text}"


def format_debt_list_messages(rows: list[str]) -> list[str]:
    """Список ВСЕХ автомобилей с задолженностью (см. задачу п.7: "если
    список большой — сохранить существующую pagination") — тот же
    packing-приём, что и раньше: каждая строка — атомарная единица
    (никогда не разрывается пополам). БЕЗ отдельного заголовка (см.
    задачу п.7 — пример показывает строки СРАЗУ после аггрегата, без
    "Автомобили с задолженностью:"). [] — если задолженностей нет вовсе
    (см. вызывающий код — тогда это сообщение не отправляется вообще)."""
    if not rows:
        return []

    messages: list[str] = []
    current: list[str] = []
    current_len = 0
    for row in rows:
        addition = len(row) + (1 if current else 0)  # +1 = "\n", кроме первой строки сообщения
        if current and current_len + addition > _TELEGRAM_MESSAGE_LIMIT:
            messages.append("\n".join(current))
            current = []
            current_len = 0
            addition = len(row)
        current.append(row)
        current_len += addition

    if current:
        messages.append("\n".join(current))

    return messages


# ---- "🔄 Проверить авто с задолженностью" (см. задачу "manual Turkey
# debt refresh") — trusted-manager-only, см. reader/turkey_bot/
# debt_refresh_service.py ----

DEBT_REFRESH_BUTTON_LABEL = "🔄 Проверить авто с задолженностью"
DEBT_REFRESH_NONE_TEXT = "✅ Автомобилей с задолженностью нет."
DEBT_REFRESH_IN_PROGRESS_TEXT = "⚠️ Проверка автомобилей уже выполняется."
DEBT_REFRESH_CANCELLED_TEXT = "Отменено."


def format_debt_refresh_prompt(car_count: int) -> str:
    return f"🔄 Будет проверено автомобилей: {car_count}"


def debt_refresh_confirm_button_label(car_count: int) -> str:
    return f"✅ Проверить {car_count} авто"


def format_debt_refresh_summary(outcome: TurkeyRefreshOutcome) -> str:
    """См. задачу "manager Statistics / refresh для обоих ботов" п.7 —
    БЕЗ "Задолженность погашена"/"Задолженность осталась"/Было-Стало-
    аналитики: только факт проверки, актуальный список — заново
    построенный "🚨 Задолженность по последней проверке" блок (см.
    ConversationController._build_debt_section), склеиваемый вызывающим
    кодом ПОСЛЕ этого текста, а не здесь (тот же принцип, что и у Georgia,
    см. reader/public_bot/texts.py::format_debt_refresh_summary — единый
    формат результата для обеих стран)."""
    lines = [
        "🔄 Проверка завершена",
        "",
        f"Проверено: {outcome.checked}",
        f"⚠️ Не удалось проверить: {outcome.failed}",
    ]
    if outcome.failed_car_numbers:
        lines.append("")
        lines.append("Не удалось проверить: " + ", ".join(outcome.failed_car_numbers))
    return "\n".join(lines)


# ---- Unified check rendering (см. design report "Перестроить UX Turkey
# test bot", reader/turkey_bot/unified/models.py; layout — см. задачу
# "Улучшить формат unified Turkey check и расчёт итоговой суммы") ----

_UNIFIED_PROVIDER_DISPLAY = {
    "gib": "🚔 GİB (Штрафы)",
    "avrasya": "🚇 Avrasya (Тунели)",
    "kgm": "🛣 KGM (Платные дороги, включая тунели)",
}


def _format_unified_provider_line(result: ProviderCheckResult) -> str:
    name = _UNIFIED_PROVIDER_DISPLAY.get(result.provider, result.provider)
    if result.status == ProviderStatus.ERROR:
        return f"{name} — ⚠️ временно не удалось проверить"
    if result.status == ProviderStatus.NO_DEBT:
        return f"{name} — задолженностей нет"
    return f"{name} — {_format_try_amount(result.total_amount)}"


def _format_unified_provider_detail_block(result: ProviderCheckResult) -> str | None:
    """Заголовок-название provider'а + ПОЛНАЯ детализация каждого
    найденного долга (item.description уже содержит человекочитаемую,
    многострочную расшифровку из реальных структурированных полей —
    см. reader/turkey_bot/unified/check_service.py::_gib_fine_description/
    _avrasya_item_description/_kgm_item_description, ничего не выдумывается
    здесь). None — если у provider'а вообще нет ни одного элемента с
    описанием (NO_DEBT/ERROR всегда имеют пустой items — им нечего
    детализировать, см. _*_outcome_to_result — эти статусы items=())."""
    descriptions = [item.description for item in result.items if item.description]
    if not descriptions:
        return None
    name = _UNIFIED_PROVIDER_DISPLAY.get(result.provider, result.provider)
    return name + "\n" + "\n\n".join(descriptions)


def _format_unified_total_line(result: UnifiedCheckResult) -> str:
    """См. задачу "Root cause: M295YB196 0 ₺" — presentation-only fix, БЕЗ
    изменения calculation/status model (total_amount/overall_status
    считаются и сохраняются ровно как раньше, см.
    reader/turkey_bot/unified/models.py::total_amount_for/
    derive_overall_status). Раньше "Итого: {total_amount}" печаталось
    безусловно — при overall_status ERROR/PARTIAL total_amount технически
    "правильные" 0/частичная сумма (ERROR-провайдеры в сумму не входят),
    но пользователю это читалось как подтверждённый нулевой/полный итог,
    хотя часть или все провайдеры вообще не были проверены."""
    if result.overall_status == OverallStatus.ERROR:
        return "Итоговая сумма не определена"
    if result.overall_status == OverallStatus.PARTIAL:
        return "Итоговая сумма может быть неполной"
    return f"Итого: {_format_try_amount(result.total_amount)}"


def format_unified_check_result(result: UnifiedCheckResult) -> str:
    """Новый layout (см. задачу "Улучшить формат unified Turkey check и
    расчёт итоговой суммы"): "Итого" сразу после заголовка — см.
    _format_unified_total_line, total_amount берётся из
    reader/turkey_bot/unified/models.py::total_amount_for(), который
    сознательно ИСКЛЮЧАЕТ Avrasya из суммы (её долг уже учтён внутри KGM
    "Платные дороги, включая тунели" — иначе задолженность задваивалась
    бы, см. total_amount_for docstring) — Avrasya's СОБСТВЕННЫЙ
    ProviderCheckResult.total_amount при этом не меняется, показывается
    как есть в своей сводной строке И в "Детализация:" ниже (она НЕ
    "теряется", просто не входит в агрегат). Далее — сводная строка на
    каждого provider (ERROR — честно ⚠️, НИКОГДА не "нет задолженности",
    см. design report п.4/п.7), затем ОТДЕЛЬНЫЙ раздел "Детализация:" —
    построчная расшифровка каждого найденного долга (см.
    _format_unified_provider_detail_block), только для provider'ов, у
    которых реально есть items — раздел целиком опускается, если
    детализировать нечего (все NO_DEBT/ERROR)."""
    lines = [f"🚗 {result.plate}", "", _format_unified_total_line(result)]
    lines.extend(_format_unified_provider_line(p) for p in result.providers)

    detail_blocks = []
    for provider_result in result.providers:
        block = _format_unified_provider_detail_block(provider_result)
        if block is not None:
            detail_blocks.append(block)
    if detail_blocks:
        lines.append("")
        lines.append("Детализация:")
        lines.append("")
        lines.append("\n\n".join(detail_blocks))

    lines.append("")
    checked_local = result.finished_at.astimezone(_DISPLAY_TZ)
    lines.append(f"Проверено: {checked_local.strftime('%d.%m.%Y %H:%M')}")
    return "\n".join(lines)


# ---- "🚗 Мои автомобили" — car-centric ON/OFF (см. задачу "Адаптация
# car-centric UX Georgian bot", reader/public_bot/texts.py::
# car_monitoring_state/format_car_button_label/format_car_details —
# СТРУКТУРНО тот же паттерн, но БЕЗ третьего "ИСТЁК" состояния и БЕЗ
# "📅 До DATE" (у Turkey monitoring нет периода/даты окончания — только
# active: bool, см. TurkeyMonitoringSubscription, бессрочно до ручного
# отключения) ----

def format_car_button_label(car: TurkeyUserCar, *, monitoring_active: bool) -> str:
    emoji = "🟢" if monitoring_active else "⚪"
    state = "ON" if monitoring_active else "OFF"
    return f"{emoji} {car.car_number} — {state}"


def format_owner_username_button(username: str | None) -> str:
    """Средняя из трёх кнопок manager-строки "🚗 Все автомобили" —
    [CAR_NUMBER] [@username/—] [STATUS] (см. задачу "унифицировать оба
    интерфейса", тот же формат, что и у Georgian bot
    reader/public_bot/texts.py::format_owner_username_button).
    "@username", если владелец известен (auto-captured, см.
    turkey_bot_known_users), иначе "—" (задача явно требует "никогда
    @None/None/пустая кнопка" — Telegram inline-кнопка не может быть
    пустой строкой)."""
    return f"@{username}" if username else "—"


def format_manager_car_status_label(*, monitoring_active: bool) -> str:
    """Правая кнопка manager-строки — ТОЛЬКО 🟢/⚪ (см. задачу пример
    "[M295YB196 @Mihailov_vm] [🟢]") — БЕЗ "ON"/"OFF"-текста и БЕЗ даты
    (в отличие от format_car_button_label выше, который остаётся
    НЕТРОНУТЫМ и продолжает использоваться self-service списком)."""
    return "🟢" if monitoring_active else "⚪"


def format_manager_car_detail(
    car: TurkeyUserCar, *, owner_username: str | None, monitoring_active: bool,
) -> str:
    """Read-only карточка ОДНОЙ строки manager-списка (см. задачу,
    референс — Georgian bot format_trusted_task_detail) — никаких действий
    с этого экрана, только просмотр (см. design report этой задачи: "не
    придумывай новую бизнес-логику" — check/monitor toggle/delete чужого
    автомобиля здесь сознательно не предлагаются, ТОЛЬКО просмотр +
    toggle мониторинга остаётся на самом списке, см.
    keyboards.py::manager_cars_page_keyboard)."""
    owner_line = f"👤 @{owner_username}" if owner_username else "👤 Владелец неизвестен"
    emoji, state = ("🟢", "ON") if monitoring_active else ("⚪", "OFF")
    return "\n".join([
        f"🚗 {car.car_number}",
        owner_line,
        f"Мониторинг: {emoji} {state}",
    ])


def format_car_card_text(car: TurkeyUserCar, *, monitoring_active: bool) -> str:
    """Карточка одного автомобиля (см. задачу, п.2): РОВНО две строки,
    БЕЗ даты/периода — Turkey monitoring бессрочен до ручного отключения."""
    emoji = "🟢" if monitoring_active else "⚪"
    state = "ON" if monitoring_active else "OFF"
    return f"🚗 {car.car_number}\nМониторинг: {emoji} {state}"


def format_delete_confirm_prompt(plate: str) -> str:
    """См. reader/public_bot/texts.py::format_delete_confirm_prompt — тот
    же Georgian pattern двухшагового подтверждения удаления (см. задачу,
    п.7: "Если Georgian bot использует confirmation перед удалением —
    повторить этот pattern")."""
    return (
        f"⚠️ Удалить {plate} из списка?\n"
        "Мониторинг будет отключён, автомобиль исчезнет из «🚗 Мои автомобили»."
    )


# ---- 📜 История (см. design report п.7) ----

def _format_history_line(run: UnifiedCheckResult) -> str:
    stamp = run.finished_at.astimezone(_DISPLAY_TZ).strftime("%d.%m %H:%M")
    if run.overall_status == OverallStatus.HAS_DEBT:
        value = _format_try_amount(run.total_amount)
    elif run.overall_status == OverallStatus.NO_DEBT:
        value = "задолженностей не найдено"
    elif run.overall_status == OverallStatus.PARTIAL:
        value = f"частично проверено ({_format_try_amount(run.total_amount)}, не все службы ответили)"
    else:
        value = "не удалось проверить (ошибка)"
    return f"{stamp} — {value}"


def format_history_messages(plate: str, runs: list[UnifiedCheckResult]) -> list[str]:
    """PARTIAL/ERROR отображаются честно (см. design report п.7: "не как
    0 ₺"), никогда не подменяются на "нет задолженности"."""
    if not runs:
        return [HISTORY_EMPTY_TEMPLATE.format(plate=plate)]
    header = f"📜 История {plate}"
    lines = [_format_history_line(run) for run in runs]
    return _split_into_telegram_messages([header, "\n".join(lines)])


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
# Та же формулировка, что и reader/public_bot/texts.py::CALLBACK_NOT_AUTHORIZED_TEXT
# (см. задачу п.14: forged/устаревшее состояние — безопасный отказ, а не
# падение/раскрытие данных).
SEARCH_NOT_AUTHORIZED_TEXT = "Это действие недоступно — начните заново через меню."


def format_search_not_found(query: str) -> str:
    return f"🔎 Ничего не найдено\n\nПо запросу:\n{query}"


# Telegram first_name/last_name иногда содержит placeholder-мусор вместо
# реального имени (см. задачу "manager Statistics / refresh для обоих
# ботов" п.2, тот же принцип, что и у reader/public_bot/texts.py::
# _sanitize_name_part) — ТОЛЬКО пунктуация/whitespace, без единой буквы/
# цифры, не является человеческим именем и не должно показываться как имя
# (username при этом НЕ теряется).
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
    trusted bot search" п.7, тот же принцип, что и
    reader/public_bot/texts.py::format_search_owner_display):
      имя + username -> "Имя Фамилия (@username)";
      только имя     -> "Имя Фамилия" (или просто "Имя");
      только username -> "@username";
      ничего         -> "—".
    ОТДЕЛЬНАЯ функция от format_owner_username_button (manager car list,
    задача явно требует его НЕ менять) — другой набор случаев (имя)."""
    name = _full_name(first_name, last_name)
    if name and username:
        return f"{name} (@{username})"
    if name:
        return name
    if username:
        return f"@{username}"
    return "—"


def format_search_monitoring_line(*, monitoring_active: bool) -> str:
    return f"Мониторинг: {'🟢' if monitoring_active else '⚪'}"


def format_search_block(
    *, owner_display: str, car_number: str, monitoring_active: bool, unified_result: UnifiedCheckResult | None,
) -> str:
    """Один блок Search-результата (одна машина/владелец) — ВСЕГДА
    начинается с "👤 {owner_display}" (см. задачу "add name search to
    trusted bot search" п.7 — единый формат независимо от того, что
    искали) — ПЕРЕИСПОЛЬЗУЕТ format_unified_check_result() as-is для
    GİB/Avrasya/KGM/Итого/"Проверено: ..." (см. задачу п.9: "НЕ
    рассчитывать Turkey total новой формулой внутри Search" —
    total_amount_for()/derive_overall_status() уже посчитаны ТАМ, где
    unified_result сохранялся, здесь — только сборка карточки).

    format_unified_check_result() САМА открывается строкой "🚗 {plate}",
    когда unified_result задан — "🚗 {car_number}" здесь добавляется
    ТОЛЬКО когда unified_result is None (там format_unified_check_result()
    вообще не вызывается — это единственный источник номера машины в
    блоке), иначе получилось бы "🚗 X" два раза подряд. unified_result=None
    — для этой машины/владельца ещё не было ни одной проверки (см.
    TurkeyCheckRunRepository.get_latest_for_owner) — честно показываем
    "Ещё не проверялось", а НЕ 0 ₺/подделанный статус."""
    lines = [f"👤 {owner_display}"]
    if unified_result is None:
        lines.append(f"🚗 {car_number}")
        lines.append("Ещё не проверялось")
    else:
        lines.append(format_unified_check_result(unified_result))
    lines.append(format_search_monitoring_line(monitoring_active=monitoring_active))
    return "\n".join(lines)


def format_search_results(*, blocks: list[str]) -> str:
    """Каждый block уже самодостаточен (владелец И машина вместе, см.
    format_search_block) — тот же принцип, что и
    reader/public_bot/texts.py::format_search_results (Georgian bot):
    имя может соответствовать НЕСКОЛЬКИМ разным telegram_user_id
    одновременно, единого "внешнего" owner для заголовка может не быть,
    поэтому больше нет отдельной "👤/🚗 {query}" шапки."""
    title = "🔎 Результат поиска" if len(blocks) == 1 else "🔎 Результаты поиска"
    return "\n".join([title, "", "\n\n".join(blocks)])


def format_search_pagination_footer(*, page: int, total_pages: int) -> str:
    return f"Страница {page + 1} из {total_pages}"
