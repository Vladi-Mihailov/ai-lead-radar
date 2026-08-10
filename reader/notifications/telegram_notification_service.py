"""TelegramNotificationService — реализация NotificationService поверх уже
существующего Telegram-клиента проекта. Второе подключение к Telegram не
создаётся: тот же TelegramClient, что у TelegramSource/TelegramSink/
CommandDispatcher (доступен через TelegramSource.client), передаётся сюда
готовым при сборке в main.py.
"""

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

from telethon import TelegramClient

from reader.fines.models import NewFineEvent
from reader.notifications.base import NotificationResult, NotificationService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ResolvedTarget:
    entity: Any
    label: str


def _label(target: int | str) -> str:
    return f"@{target}" if isinstance(target, str) else str(target)


def _format_date(value: date | None) -> str | None:
    return value.strftime("%d.%m.%Y") if value else None


def _format_fine_block(event: NewFineEvent) -> str:
    lines = []

    penalty_date = _format_date(event.penalty_date)
    if penalty_date:
        lines.append(f"Дата штрафа: {penalty_date}")

    due_date = _format_date(event.due_date)
    if due_date:
        lines.append(f"Срок оплаты: {due_date}")

    if event.delivered_status:
        lines.append(f"Статус вручения: {event.delivered_status}")

    return "\n".join(lines)


def _format_car_line(car_number: str, events: list[NewFineEvent]) -> str:
    """"Автомобиль: X" + "Telegram: ..." — ровно один раз на группу (см.
    _group_by_car), а не на каждый штраф внутри неё. Берём значение с
    первого события группы: все события в группе относятся к одному
    car_number, а car_owner_display определяется именно по car_number (см.
    FineNotificationCoordinator), а не привязан к конкретному штрафу.

    car_owner_display почти всегда непустая строка — "не найден"/"найдено
    несколько пользователей", если владельца нельзя однозначно показать
    (единый вариант вместо тихого пропуска строки, см. задачу). None
    бывает, только если саму задачу мониторинга не удалось найти вообще —
    тогда строку не показываем, показывать нечего даже "не найден"."""
    line = f"Автомобиль: {car_number}"
    car_owner_display = events[0].car_owner_display
    if car_owner_display:
        line += f"\nTelegram: {car_owner_display}"
    return line


def _format_message(car_number: str, events: list[NewFineEvent], source_url: str) -> str:
    car_line = _format_car_line(car_number, events)

    if len(events) == 1:
        header = "🚨 Обнаружен новый опубликованный штраф"
        body = f"{car_line}\n" + _format_fine_block(events[0])
    else:
        header = f"🚨 Обнаружены новые опубликованные штрафы ({len(events)})"
        blocks = "\n\n".join(_format_fine_block(event) for event in events)
        body = f"{car_line}\n\n{blocks}"

    lines = [header, "", body, "", f"🔗 [Открыть источник]({source_url})"]
    return "\n".join(lines)


def _group_by_car(events: list[NewFineEvent]) -> dict[str, list[NewFineEvent]]:
    grouped: dict[str, list[NewFineEvent]] = {}
    for event in events:
        grouped.setdefault(event.car_number, []).append(event)
    return grouped


class TelegramNotificationService(NotificationService):
    def __init__(self, client: TelegramClient, chat_ids: list[int | str], source_url: str):
        self._client = client
        self._chat_ids = chat_ids
        self._source_url = source_url
        self._resolved: list[_ResolvedTarget] = []

    async def start(self) -> None:
        for chat_id in self._chat_ids:
            label = _label(chat_id)
            try:
                entity = await self._client.get_entity(chat_id)
            except Exception as exc:
                logger.error("✖ Получатель уведомлений о штрафах %s не найден", label)
                raise RuntimeError(f"Не найден получатель {label}") from exc

            self._resolved.append(_ResolvedTarget(entity=entity, label=label))
            logger.info("✔ Получатель уведомлений о штрафах %s найден", label)

    async def notify(self, events: list[NewFineEvent]) -> NotificationResult:
        if not events:
            return NotificationResult(delivered_event_ids=[], failed_event_ids=[])

        delivered_ids: list[int] = []
        failed_ids: list[int] = []

        for car_number, car_events in _group_by_car(events).items():
            text = _format_message(car_number, car_events, self._source_url)
            group_ids = [event.detected_fine_id for event in car_events]

            # Рассылаем во все настроенные чаты независимо от того, удалось
            # ли уже доставить в предыдущий из них — каждый получатель
            # должен увидеть уведомление сам по себе.
            delivered_to_any = False
            for target in self._resolved:
                try:
                    await self._client.send_message(
                        target.entity, text, parse_mode="md", link_preview=False
                    )
                    delivered_to_any = True
                except Exception:
                    logger.exception(
                        "Не удалось отправить уведомление о штрафе %s в %s",
                        car_number,
                        target.label,
                    )

            # Событие считается доставленным, если сообщение дошло хотя бы
            # до одного из настроенных получателей.
            if delivered_to_any:
                delivered_ids.extend(group_ids)
            else:
                failed_ids.extend(group_ids)

        return NotificationResult(delivered_event_ids=delivered_ids, failed_event_ids=failed_ids)
