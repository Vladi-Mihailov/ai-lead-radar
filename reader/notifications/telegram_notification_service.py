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


def _format_amount(value: float | None) -> str | None:
    """"100 GEL"/"100.5 GEL" — без лишних ".0" (см. задачу про форматирование
    суммы), но не через "%g" (риск научной нотации для крупных сумм)."""
    if value is None:
        return None
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{text} GEL"


def format_fine_block(event: NewFineEvent) -> str:
    """Публичная (без ведущего "_") — переиспользуется
    reader/public_bot/delivery_texts.py для клиентских уведомлений (см.
    design report Stage 4 и новый расширенный формат уведомлений), чтобы не
    заводить вторую реализацию форматирования тех же полей штрафа для
    другого канала доставки. Каждая строка — опциональна (пропускается,
    если поле отсутствует, включая старые detected_fines без новых колонок,
    см. миграцию DetectedFineRepository — доставка таких штрафов не должна
    падать, просто эти строки не показываются)."""
    lines = []

    if event.external_fine_id:
        lines.append(f"📄 Штраф №: {event.external_fine_id}")

    violation_date = _format_date(event.violation_date)
    if violation_date:
        lines.append(f"📅 Дата нарушения: {violation_date}")
    else:
        # violationDate/protocolDate — НЕ смешивать (см. задачу): пустой
        # violation_date не подставляем сюда protocolDate под тем же
        # лейблом. Но для старых detected_fines (миграция, violation_date
        # ещё NULL) полностью скрывать дату тоже не нужно — показываем
        # именно "дату протокола" под собственным честным лейблом.
        penalty_date = _format_date(event.penalty_date)
        if penalty_date:
            lines.append(f"📅 Дата протокола: {penalty_date}")

    amount = _format_amount(event.amount)
    if amount:
        lines.append(f"💰 Сумма: {amount}")

    # Русский перевод (place_ru/violation_description_ru, см. reader/
    # fines/translation.py) — приоритет; если перевода ещё нет (temporary
    # translation outage ИЛИ штраф ещё не переведён) — safe fallback,
    # оригинальный грузинский текст, а не пустая строка (см. задачу:
    # "Ошибка translation API никогда не должна ломать мониторинг").
    place = event.place_ru or event.place
    if place:
        lines.append(f"📍 Место: {place}")

    violation_description = event.violation_description_ru or event.violation_description
    if violation_description:
        lines.append(f"📝 Нарушение: {violation_description}")

    due_date = _format_date(event.due_date)
    if due_date:
        lines.append(f"⏳ Оплатить до: {due_date}")

    if event.delivered_status:
        lines.append(f"📬 Статус вручения: {event.delivered_status}")

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
        body = f"{car_line}\n" + format_fine_block(events[0])
    else:
        header = f"🚨 Обнаружены новые опубликованные штрафы ({len(events)})"
        blocks = "\n\n".join(format_fine_block(event) for event in events)
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
