"""Форматирование клиентских уведомлений о новом штрафе — ДЛЯ ДВУХ
получателей (см. design report Stage 4): владельца ('owner') и trusted-
оператора ('trusted_operator'), поставившего машину на мониторинг за него.

Переиспользует reader.notifications.telegram_notification_service.
format_fine_block() — тот же расширенный информационный блок (штраф №/дата
нарушения/сумма/место/нарушение/срок оплаты/статус вручения), что и в
операторском уведомлении — без второй реализации форматирования.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from reader.fines.models import DetectedFine, NewFineEvent
from reader.notifications.telegram_notification_service import format_fine_block
from reader.plate_display import format_plate_for_telegram
from reader.public_bot.payable_amount_texts import (
    format_amount_increase_line,
    format_money,
    format_refreshed_at,
    format_trusted_amount_increase_line,
)

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
        f"🚗 Автомобиль: {format_plate_for_telegram(car_number)}",
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
        f"🚗 {format_plate_for_telegram(car_number)}",
    ]
    if owner_display:
        lines.append(f"👤 Владелец: @{owner_display}")
    lines.append(format_fine_block(event))
    return "\n".join(lines)


def format_check_now_fines_message(
    *, car_number: str, fines: list[NewFineEvent], status_labels: dict[str, str] | None = None,
    payable_lines: dict[str, list[str]] | None = None,
) -> str:
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
    "штрафов не найдено" отдельно, до вызова этой функции.

    status_labels — {protocolNo: "🔴 Не оплачен"/...} (см. статусы оплаты,
    reader/fines/payment_status.py): строка "Статус: ..." под блоком
    штрафа; None — статусы не ведутся, формат прежний.

    payable_lines — {protocolNo: ["💰 К оплате: ..."]} (сумма к оплате,
    см. reader/public_bot/payable_amount_texts.py): строка суммы блока
    становится «🧾 Штраф», под ней — «💰 К оплате». None — формат прежний."""
    formatted_car_number = format_plate_for_telegram(car_number)
    blocks = []
    for fine in fines:
        extra = payable_lines.get(fine.external_fine_id or "") if payable_lines is not None else None
        lines = [
            "🚨 Обнаружен штраф",
            "",
            f"🚗 Автомобиль: {formatted_car_number}",
            format_fine_block(fine, payable_lines=extra),
        ]
        if status_labels is not None:
            label = status_labels.get(fine.external_fine_id or "")
            if label:
                lines.append(f"Статус: {label}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _amount_card_lines(
    *, protocol_no: str, base_amount: float | None, new_amount: float, observed_at: datetime, tz: ZoneInfo,
) -> list[str]:
    lines = [f"📄 Штраф №: {protocol_no}"]
    if base_amount is not None:
        lines.append(f"🧾 Штраф: {format_money(base_amount)}")
    lines.append(f"💰 К оплате: {format_money(new_amount)}")
    lines.append(format_refreshed_at(observed_at, tz=tz))
    return lines


def format_owner_amount_increase_message(
    *, car_number: str, protocol_no: str, base_amount: float | None, old_amount: float, new_amount: float,
    observed_at: datetime, tz: ZoneInfo,
) -> str:
    """Подтверждённое увеличение суммы к оплате (см. AmountIncreaseDeliveryService)."""
    return "\n".join([
        format_amount_increase_line(old_amount, new_amount),
        "",
        f"🚗 Автомобиль: {format_plate_for_telegram(car_number)}",
        *_amount_card_lines(
            protocol_no=protocol_no, base_amount=base_amount, new_amount=new_amount, observed_at=observed_at, tz=tz,
        ),
    ])


def format_trusted_operator_amount_increase_message(
    *, car_number: str, protocol_no: str, base_amount: float | None, old_amount: float, new_amount: float,
    observed_at: datetime, tz: ZoneInfo, owner_display: str | None,
) -> str:
    lines = [
        format_trusted_amount_increase_line(old_amount, new_amount),
        "",
        f"🚗 {format_plate_for_telegram(car_number)}",
    ]
    if owner_display:
        lines.append(f"👤 Владелец: @{owner_display}")
    lines.extend(_amount_card_lines(
        protocol_no=protocol_no, base_amount=base_amount, new_amount=new_amount, observed_at=observed_at, tz=tz,
    ))
    return "\n".join(lines)
