"""Доменные модели фундамента @GEShtrafbot — отдельно от reader/fines/models.py,
т.к. это не часть самого Fine Monitor (проверка/дедуп штрафов), а надстройка
над ним для публичного клиентского Telegram-бота (см. design report).

FineMonitoringSubscription.monitoring_task_id всегда указывает на уже
существующую reader.fines.models.FineMonitoringTask — второй, независимый
Fine Monitor не заводится: одна и та же задача мониторинга может быть
связана сразу с несколькими подписками (несколько клиентов на одну машину)
и с существующим операторским мониторингом одновременно.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

# 'active' — подписка реально мониторится/доставляется (см.
# FineMonitoringSubscription.is_effectively_active — status='active' само
# по себе НЕ достаточно, end_date тоже обязателен к проверке).
# 'stopped' — клиент сам остановил ("Остановить мониторинг").
# 'expired' — end_date прошёл (см. FineSubscriptionRepository.expire_elapsed);
# это ГИГИЕНА отображения, а не источник истины — все "активные" выборки
# и без этого статуса никогда не вернут подписку с прошедшим end_date.
SubscriptionStatus = Literal["active", "stopped", "expired"]


@dataclass(frozen=True)
class FineMonitoringSubscription:
    """Один Telegram-пользователь ↔ один автомобиль ↔ период мониторинга,
    заведённый через клиентского бота (source). Несколько подписок могут
    указывать на одну и ту же monitoring_task_id (несколько клиентов на
    одну машину) — это НЕ конфликт, каждая подписка независима: остановка
    одной не должна останавливать другую и не должна останавливать саму
    FineMonitoringTask (см. design)."""

    id: int
    monitoring_task_id: int
    car_number: str
    # Стабильный идентификатор клиента — численный Telegram user_id, а НЕ
    # username (см. design: username — контактный/отображаемый атрибут).
    telegram_user_id: int
    telegram_chat_id: int
    telegram_username: str | None
    status: SubscriptionStatus
    start_date: date
    end_date: date
    source: str
    created_at: datetime
    updated_at: datetime
    stopped_at: datetime | None

    def is_effectively_active(self, *, today: date) -> bool:
        """status='active' — необходимое, но не достаточное условие: end_date
        может быть уже в прошлом, даже если expire_elapsed() ещё не
        прошёлся по этой строке — поэтому любой код, которому нужно
        "активна ли подписка ПРЯМО СЕЙЧАС", обязан учитывать end_date, а
        не только сохранённый status (см. design про lifecycle подписки)."""
        return self.status == "active" and self.end_date >= today


@dataclass(frozen=True)
class ClientFineDelivery:
    """Факт (попытки) доставки одного обнаруженного штрафа
    (detected_fines.id) одному конкретному подписчику
    (fine_monitoring_subscriptions.id). Намеренно ОТДЕЛЬНО от
    detected_fines.notification_sent_at — то поле означает "оператор
    уведомлён" (см. FineNotificationCoordinator) и не должно смешиваться с
    доставкой клиенту: у одного штрафа может быть ноль, один или несколько
    подписчиков, каждый со своим собственным, независимо ретраящимся
    статусом доставки."""

    id: int
    detected_fine_id: int
    subscription_id: int
    delivered_at: datetime | None
    last_attempt_at: datetime | None
    attempt_count: int


@dataclass(frozen=True)
class ConversationState:
    """Состояние одного пошагового диалога (например, "Добавить авто") в
    ОДНОМ приватном чате с ботом — переживает рестарт процесса (хранится в
    sqlite, тот же приём, что и reader/checkout/lock_repository.py). Один
    диалог на chat_id одновременно: новый /start или новая попытка того же
    флоу перезаписывает предыдущее состояние этого chat_id целиком, а не
    накапливает несколько параллельных диалогов."""

    chat_id: int
    telegram_user_id: int
    step: str
    payload: dict | None
    updated_at: datetime
