"""Пользовательские тексты Turkey-бота. Русский язык — та же аудитория,
что и у @ProtocolGEbot (см. design report Stage 1).

ВАЖНО (см. задачу): сюда НИКОГДА не попадает GibSubmitOutcome.raw_data как
есть — format_has_debt_messages() ниже строит текст ТОЛЬКО из уже
типизированных GibFineRecord (см. reader/turkey_bot/gib/fine_parser.py),
которые сами по себе никогда не содержат KK_HASH/KK_KIMLIK (платёжные
токены GIB — этих полей нет в самом dataclass, не только "не показаны").
raw_data/messages целиком уходят только в TurkeyCheckRepository
(server-side), никогда в текст, который увидит пользователь."""

from decimal import Decimal

from reader.turkey_bot.gib.models import GibFineRecord

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
    "Отправьте гос. номер автомобиля (например, 34ABC123) — бот запросит "
    "CAPTCHA с сайта GIB, вы введёте код с картинки, и бот покажет результат.\n\n"
    "Проверка одноразовая — никакого постоянного мониторинга.\n"
    "/cancel — отменить текущую проверку."
)

INVALID_PLATE_TEXT = (
    "Не похоже на гос. номер. Отправьте номер латинскими буквами и цифрами, "
    "например: 34ABC123."
)

ASK_CAPTCHA_TEXT = "Введите код с картинки (регистр — как на картинке)."

CAPTCHA_FETCH_FAILED_TEXT = (
    "Не удалось получить CAPTCHA с сайта GIB (сбой соединения). "
    "Попробуйте ещё раз через минуту."
)

CAPTCHA_REJECTED_RETRY_TEXT = "Код введён неверно. Вот новая CAPTCHA — попробуйте ещё раз."

SESSION_EXPIRED_RETRY_TEXT = (
    "Предыдущая CAPTCHA-сессия больше не действительна (бот перезапускался "
    "или сессия истекла). Вот новая CAPTCHA для того же номера — введите код."
)

SESSION_LOST_TEXT = (
    "Не удалось восстановить проверку. Отправьте гос. номер ещё раз, чтобы начать заново."
)

TRANSPORT_ERROR_TEXT = "Не удалось связаться с сайтом GIB. Попробуйте ещё раз через минуту."

UNEXPECTED_ERROR_TEXT = (
    "GIB вернул ответ в формате, который бот пока не понимает. Мы уже знаем "
    "об этом — попробуйте позже."
)

CANCEL_CONFIRM_TEXT = "Проверка отменена. Отправьте гос. номер, чтобы начать заново."
NOTHING_TO_CANCEL_TEXT = "Сейчас нет активной проверки для отмены."

CANCEL_BUTTON_LABEL = "❌ Отмена"


def no_debt_text(plate: str) -> str:
    return f"✅ {plate}: штрафов и задолженности в GIB не найдено."


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
    if fine.description:
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
