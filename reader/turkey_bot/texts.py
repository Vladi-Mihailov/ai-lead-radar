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

from decimal import Decimal

from reader.turkey_bot.avrasya.models import AvrasyaDebtItem
from reader.turkey_bot.gib.models import GibFineRecord
from reader.turkey_bot.statistics_service import TurkeyStatistics

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
    "🇹🇷 Проверка штрафов и задолженности по гос. номеру в Турции (GIB).\n\n"
    "Отправьте гос. номер автомобиля (например, А123АА123) — бот запросит "
    "CAPTCHA с сайта GIB, вы введёте код с картинки, и бот покажет результат.\n\n"
    "Проверка одноразовая — никакого постоянного мониторинга.\n"
    "/cancel — отменить текущую проверку."
)

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
GARAGE_LABEL = "🚗 Мои авто"
STATISTICS_LABEL = "📊 Статистика"
CHECK_FINES_LABEL = "🚔 Проверить штрафы"
CHECK_TOLLS_LABEL = "🛣 Проверить платные дороги"

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

GARAGE_HEADER = "🚗 Мои автомобили"
EMPTY_GARAGE_TEXT = (
    "🚗 У вас пока нет автомобилей.\n\n"
    "Проверьте автомобиль по номеру — после успешной проверки он появится здесь."
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
    не несёt (не только "не показаны", см. models.py). "Штрафы" — строка
    показывается, ТОЛЬКО когда суммарный штраф > 0 (см. задачу: "when
    explicitly supplied") — тот же принцип "не показывать поле вовсе,
    когда оно отсутствует/пусто", что и у GIB _format_fine_block."""
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
        f"Сумма проездов: {_format_try_amount(total_principal)}",
    ]
    if total_penalty > 0:
        lines.append(f"Штрафы: {_format_try_amount(total_penalty)}")
    lines.append(f"Итого к оплате: {_format_try_amount(total_payable)}")
    return "\n".join(lines)


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


def format_statistics(stats: TurkeyStatistics) -> str:
    """Только метрики, надёжно посчитанные reader/turkey_bot/
    statistics_service.py::TurkeyStatisticsService (см. design report,
    аудит) — никаких Георгия-специфичных полей (active/stopped
    subscriptions, monitoring tasks) — у Turkey нет мониторинга (см.
    задачу)."""
    lines = [
        STATISTICS_LABEL,
        "",
        "👥 Пользователи",
        f"Всего: {stats.total_users}",
        f"Новых сегодня: {stats.new_users_today}",
        f"Новых за 7 дней: {stats.new_users_7d}",
        f"Новых за 30 дней: {stats.new_users_30d}",
        "",
        "🔎 Проверки",
        f"Всего: {stats.total_checks}",
        f"Сегодня: {stats.checks_today}",
        f"За 7 дней: {stats.checks_7d}",
        f"За 30 дней: {stats.checks_30d}",
        "",
        f"🚨 С задолженностью: {stats.checks_has_debt}",
        f"✅ Без задолженности: {stats.checks_no_debt}",
    ]
    return "\n".join(lines)


def _format_user_line(index: int, telegram_user_id: int, telegram_username: str | None) -> str:
    """@username, когда известен, иначе "ID: <telegram_user_id>" (см.
    задачу) — НИКОГДА telegram_chat_id или что-либо ещё: сигнатура этой
    функции физически не получает ничего, кроме этих двух значений."""
    identity = f"@{telegram_username}" if telegram_username else f"ID: {telegram_user_id}"
    return f"{index}. {identity}"


def format_user_list_messages(users: list[tuple[int, str | None]]) -> list[str]:
    """Список ВСЕХ известных пользователей (см. reader/turkey_bot/
    known_users_repository.py::list_all — уже отсортирован
    детерминированно). Возвращает СПИСОК сообщений (см. задачу: "Handle
    Telegram's 4096-character limit safely... Do not truncate silently") -
    заголовок только в первом сообщении, каждая строка — атомарная единица
    (никогда не разрывается пополам), упаковка построчная (через "\\n", не
    "\\n\\n" — список компактнее, чем блоки со штрафами)."""
    header = f"👥 Пользователи ({len(users)}):"
    if not users:
        return [header]

    lines = [
        _format_user_line(index, telegram_user_id, telegram_username)
        for index, (telegram_user_id, telegram_username) in enumerate(users, start=1)
    ]

    messages: list[str] = []
    current: list[str] = [header]
    current_len = len(header)
    for line in lines:
        addition = len(line) + 1  # +1 = "\n"
        if current_len + addition > _TELEGRAM_MESSAGE_LIMIT:
            messages.append("\n".join(current))
            current = []
            current_len = 0
            addition = len(line)
        current.append(line)
        current_len += addition

    if current:
        messages.append("\n".join(current))

    return messages
