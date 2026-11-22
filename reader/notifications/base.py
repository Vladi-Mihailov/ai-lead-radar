from abc import ABC, abstractmethod
from dataclasses import dataclass

from reader.fines.models import NewFineEvent


@dataclass(frozen=True)
class NotificationResult:
    """Результат попытки доставки — по detected_fine_id каждого события, а
    не просто True/False на весь вызов: FineJob использует это, чтобы
    отметить notification_sent_at только для реально доставленных штрафов
    и оставить недоставленные NULL для повторной отправки в следующем
    проходе (см. DetectedFineRepository.list_pending_notifications())."""

    delivered_event_ids: list[int]
    failed_event_ids: list[int]


class NotificationService(ABC):
    """Доставка доменных событий получателю. Сегодня единственный
    потребитель — новые штрафы (NewFineEvent), текущая реализация —
    TelegramNotificationService; интерфейс не завязан на конкретный канал,
    чтобы позже добавить Slack/Email/Push без изменения FineJob/FineCheckService."""

    @abstractmethod
    async def notify(self, events: list[NewFineEvent]) -> NotificationResult:
        """Доставить события о новых штрафах и вернуть, какие реально
        доставлены. Вызывается только когда events непусто — решение
        "отправлять или нет" принимает вызывающий код (FineJob), не сама
        реализация."""

    async def notify_text(self, text: str) -> bool:
        """Готовый текст тем же получателям (увеличение суммы к оплате, см.
        AmountIncreaseOperatorNotifier). True — дошёл хотя бы до одного."""
        raise NotImplementedError
