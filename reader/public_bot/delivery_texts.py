"""Форматирование клиентских уведомлений о новом штрафе — ДЛЯ ДВУХ
получателей (см. design report Stage 4): владельца ('owner') и trusted-
оператора ('trusted_operator'), поставившего машину на мониторинг за него.

Переиспользует reader.notifications.telegram_notification_service.
format_fine_block() — те же поля (дата штрафа/срок оплаты/статус вручения),
что и в операторском уведомлении — без второй реализации форматирования.
"""

from reader.fines.models import DetectedFine, NewFineEvent
from reader.notifications.telegram_notification_service import format_fine_block


def format_owner_fine_message(*, car_number: str, fine: DetectedFine) -> str:
    event = NewFineEvent.from_detected_fine(fine, label=None)
    lines = [
        "🚨 Обнаружен новый штраф по вашему автомобилю",
        "",
        f"🚗 {car_number}",
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
