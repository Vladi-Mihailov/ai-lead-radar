"""Форматирование клиентских уведомлений о новом штрафе — ДЛЯ ДВУХ
получателей (см. design report Stage 4): владельца ('owner') и trusted-
оператора ('trusted_operator'), поставившего машину на мониторинг за него.

Переиспользует reader.notifications.telegram_notification_service.
format_fine_block() — тот же расширенный информационный блок (штраф №/дата
нарушения/сумма/место/нарушение/срок оплаты/статус вручения), что и в
операторском уведомлении — без второй реализации форматирования.
"""

from reader.fines.models import DetectedFine, NewFineEvent
from reader.notifications.telegram_notification_service import format_fine_block

# Коммерческие CTA-кнопки — ТОЛЬКО в owner-уведомлении (не в
# trusted_operator и не в существующем операторском чате, см. design
# report) — строит reader/public_bot/keyboards.py::owner_fine_cta_buttons,
# destination берётся из config (settings.public_bot.
# payment_help_contact_username). Отдельного рекламного текста под кнопками
# больше нет (см. задачу про новый формат уведомлений) — кнопки говорят
# сами за себя.


def format_owner_fine_message(*, car_number: str, fine: DetectedFine) -> str:
    event = NewFineEvent.from_detected_fine(fine, label=None)
    lines = [
        "🚨 Обнаружен новый штраф",
        "",
        f"🚗 Автомобиль: {car_number}",
        format_fine_block(event),
    ]
    return "\n".join(lines)


def format_trusted_operator_fine_message(
    *, car_number: str, fine: DetectedFine, owner_display: str | None,
) -> str:
    event = NewFineEvent.from_detected_fine(fine, label=None)
    lines = [
        "🚨 Новый штраф по автомобилю, поставленному вами на мониторинг",
        "",
        f"🚗 {car_number}",
    ]
    if owner_display:
        lines.append(f"👤 Владелец: @{owner_display}")
    lines.append(format_fine_block(event))
    return "\n".join(lines)


def format_check_now_fines_message(*, car_number: str, fines: list[NewFineEvent]) -> str:
    """Manual "🔎 Проверить сейчас" (см. design report про UX manual
    check) — показывает ВСЕ штрафы, которые police.ge вернул на этой
    проверке (CheckNowOutcome.fines = CheckResult.current_fines), а не
    только новые: пользователь явно спросил о текущем состоянии машины.

    Каждый штраф — отдельным блоком в том же расширенном формате, что и
    format_owner_fine_message ("🚨 Обнаружен штраф" + автомобиль + fine
    block), а не одним общим заголовком со списком — так один и тот же
    формат штрафа используется везде в проекте (фоновые уведомления и
    manual check неотличимы по виду одного штрафа). Пустой fines сюда не
    передаётся — вызывающий код (texts.format_check_now_result) обрабатывает
    "штрафов не найдено" отдельно, до вызова этой функции."""
    blocks = []
    for fine in fines:
        lines = [
            "🚨 Обнаружен штраф",
            "",
            f"🚗 Автомобиль: {car_number}",
            format_fine_block(fine),
        ]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
