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
# 'stopped' — клиент (или создавший delegated-подписку trusted-оператор)
# сам остановил ("Остановить мониторинг").
# 'expired' — end_date прошёл (см. FineSubscriptionRepository.expire_elapsed);
# это ГИГИЕНА отображения, а не источник истины — все "активные" выборки
# и без этого статуса никогда не вернут подписку с прошедшим end_date.
# 'pending_claim' — trusted-оператор поставил машину клиента на мониторинг,
# но указанный @username не удалось надёжно резолвить в numeric Telegram
# user_id (см. design report про trusted-flow) — telegram_user_id/
# telegram_chat_id у такой строки НЕ заполнены (см. ниже), сама FineMonitoringTask
# при этом уже создана/продлена и проверяется — ждём только owner claim.
SubscriptionStatus = Literal["active", "stopped", "expired", "pending_claim"]


@dataclass(frozen=True)
class FineMonitoringSubscription:
    """Один Telegram-пользователь ↔ один автомобиль ↔ период мониторинга.
    Несколько подписок могут указывать на одну и ту же monitoring_task_id
    (несколько клиентов на одну машину) — это НЕ конфликт, каждая подписка
    независима: остановка одной не должна останавливать другую и не должна
    останавливать саму FineMonitoringTask (см. design).

    telegram_user_id/telegram_chat_id — НЕ NULL для обычной (self-service
    или уже claimed delegated) подписки; NULL ТОЛЬКО для status=
    'pending_claim' (см. SubscriptionStatus) — это единственный случай,
    когда у подписки ещё нет подтверждённого владельца."""

    id: int
    monitoring_task_id: int
    car_number: str
    # Стабильный идентификатор клиента — численный Telegram user_id, а НЕ
    # username (см. design: username — контактный/отображаемый атрибут).
    telegram_user_id: int | None
    telegram_chat_id: int | None
    telegram_username: str | None
    status: SubscriptionStatus
    start_date: date
    end_date: date
    source: str
    created_at: datetime
    updated_at: datetime
    stopped_at: datetime | None
    # Всё, что ниже — trusted-operator delegated flow (см. design report).
    # None для обычной self-service подписки во всех пяти полях.
    owner_username_hint: str | None = None
    created_by_telegram_user_id: int | None = None
    created_by_telegram_chat_id: int | None = None
    claim_token: str | None = None
    claim_token_expires_at: datetime | None = None

    def is_effectively_active(self, *, today: date) -> bool:
        """status='active' — необходимое, но не достаточное условие: end_date
        может быть уже в прошлом, даже если expire_elapsed() ещё не
        прошёлся по этой строке — поэтому любой код, которому нужно
        "активна ли подписка ПРЯМО СЕЙЧАС", обязан учитывать end_date, а
        не только сохранённый status (см. design про lifecycle подписки)."""
        return self.status == "active" and self.end_date >= today

    def is_delegated(self) -> bool:
        """True — подписка заведена trusted-оператором ДЛЯ ДРУГОГО
        человека (см. design про created_by_telegram_user_id), независимо
        от того, claimed она уже реальным владельцем или ещё pending_claim."""
        return self.created_by_telegram_user_id is not None


@dataclass(frozen=True)
class ClientFineDelivery:
    """Факт (попытки) доставки одного обнаруженного штрафа
    (detected_fines.id) одному конкретному получателю в рамках одной
    подписки (fine_monitoring_subscriptions.id) — recipient_role различает
    ДВУХ возможных получателей одной delegated-подписки: 'owner' (реальный
    владелец машины) и 'trusted_operator' (тот, кто поставил машину на
    мониторинг за него) — независимая, отдельно ретраящаяся доставка
    каждому (см. design report про idempotent delivery). Для обычной
    (не-delegated) подписки существует только recipient_role='owner'.

    Намеренно ОТДЕЛЬНО от detected_fines.notification_sent_at (то поле
    означает "оператор в существующем operator-чате уведомлён", см.
    FineNotificationCoordinator) — не смешивается с клиентской доставкой."""

    id: int
    detected_fine_id: int
    subscription_id: int
    recipient_role: Literal["owner", "trusted_operator"]
    delivered_at: datetime | None
    last_attempt_at: datetime | None
    attempt_count: int


@dataclass(frozen=True)
class BotKnownUser:
    """Единственный источник истины "написал ли этот numeric Telegram
    user_id боту хотя бы раз" (см. design report: Telegram не позволяет
    боту первым писать пользователю, который никогда не начинал с ним
    диалог — резолв username в id не гарантирует возможность доставки).
    Обновляется на КАЖДОЕ входящее событие (сообщение или callback),
    независимо от его содержимого — см. reader/public_bot/handlers.py."""

    telegram_user_id: int
    telegram_chat_id: int
    telegram_username: str | None
    first_seen_at: datetime
    last_seen_at: datetime


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
