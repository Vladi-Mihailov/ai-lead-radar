"""FineSubscriptionRepository — fine_monitoring_subscriptions поверх SQLite.

Отдельная таблица от fine_monitoring_tasks (см.
reader/fines/task_repository.py) — подписка КЛИЕНТА на мониторинг, а не сам
мониторинг: одна FineMonitoringTask (один car_number) может быть связана
сразу с несколькими подписками (несколько клиентов на одном автомобиле), и
наоборот — у одного Telegram-пользователя может быть несколько подписок
(разные машины).

Нормализация car_number/username — забота вызывающего кода
(reader/fines/validation.py, reader/public_bot/validation.py), как и у
FineMonitoringTaskRepository — репозиторий хранит то, что получил.
"""

import secrets
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from reader.public_bot.models import FineMonitoringSubscription

# telegram_user_id/telegram_chat_id НЕ NOT NULL (см. design report про
# trusted-operator delegated flow) — status='pending_claim' описывает
# ровно случай "машина уже мониторится, реальный владелец ещё не
# подтверждён" (см. reader/public_bot/models.py::SubscriptionStatus).
# owner_username_hint — COLLATE NOCASE прямо в схеме: Telegram username
# регистронезависим, а частичный уникальный индекс на (monitoring_task_id,
# owner_username_hint) ниже должен считать "@Ivan"/"@ivan" одним и тем же
# незавершённым приглашением без дополнительной нормализации на стороне
# Python при каждом сравнении.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS fine_monitoring_subscriptions (
    id                            INTEGER PRIMARY KEY AUTOINCREMENT,
    monitoring_task_id            INTEGER NOT NULL REFERENCES fine_monitoring_tasks(id),
    car_number                    TEXT NOT NULL,
    telegram_user_id              INTEGER,
    telegram_chat_id              INTEGER,
    telegram_username             TEXT,
    status                        TEXT NOT NULL DEFAULT 'active',
    start_date                    TEXT NOT NULL,
    end_date                      TEXT NOT NULL,
    source                        TEXT NOT NULL DEFAULT 'geshtrafbot',
    created_at                    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at                    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    stopped_at                    TIMESTAMP,
    owner_username_hint           TEXT COLLATE NOCASE,
    created_by_telegram_user_id   INTEGER,
    created_by_telegram_chat_id   INTEGER,
    claim_token                   TEXT,
    claim_token_expires_at        TIMESTAMP
)
"""

# Одновременно может быть только ОДНА активная подписка того же
# пользователя на ту же задачу мониторинга — повторное "Добавить авто" на
# уже отслеживаемый им номер должно ОБНОВИТЬ существующую подписку (см.
# update_period), а не создать вторую. Частичный индекс (WHERE status =
# 'active') не мешает копить сколько угодно НЕ активных (stopped/expired)
# исторических строк того же (task, user) — история не теряется,
# останавливать/возобновлять можно многократно. telegram_user_id IS NULL
# никогда не участвует в этом индексе как "конфликтующее" значение — SQL
# не считает два NULL равными, поэтому несколько pending_claim строк (у
# которых status != 'active' в любом случае) им не ограничены.
_UNIQUE_ACTIVE_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_fine_subscriptions_active_user_task
    ON fine_monitoring_subscriptions (monitoring_task_id, telegram_user_id)
    WHERE status = 'active'
"""

_CAR_STATUS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_fine_subscriptions_car_status
    ON fine_monitoring_subscriptions (car_number, status)
"""

# Не более одной "безвладельческой" (без клиента, см. design про Add Car
# для trusted-оператора без указания @username) активной подписки на
# (задачу, создавшего её trusted-оператора) одновременно — повторное
# "Добавить авто" тем же оператором для той же машины БЕЗ клиента должно
# продлить существующую строку (см. update_period), а не плодить дубли.
# telegram_user_id IS NULL — то же условие, что и в idx_fine_subscriptions_
# active_user_task выше, но по (task, creator) вместо (task, user): без
# него этот индекс не отличил бы "безвладельческую" строку от обычной
# claimed-подписки того же trusted-оператора на СВОЙ ЖЕ автомобиль
# (у той telegram_user_id тоже может совпадать с created_by_telegram_user_id,
# но она НЕ NULL).
_UNIQUE_ACTIVE_OWNERLESS_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_fine_subscriptions_active_ownerless_creator_task
    ON fine_monitoring_subscriptions (monitoring_task_id, created_by_telegram_user_id)
    WHERE status = 'active' AND telegram_user_id IS NULL
"""

_USER_STATUS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_fine_subscriptions_user_status
    ON fine_monitoring_subscriptions (telegram_user_id, status)
"""

# Не более одного НЕЗАВЕРШЁННОГО приглашения на одного и того же
# owner_username_hint для одной и той же задачи мониторинга одновременно —
# повторный ввод того же @username тем же (или другим) trusted-оператором
# для уже отслеживаемой машины продлевает существующий pending_claim
# (см. refresh_pending_claim), а не плодит дубли с разными claim_token.
_PENDING_CLAIM_UNIQUE_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_fine_subscriptions_pending_claim_task_hint
    ON fine_monitoring_subscriptions (monitoring_task_id, owner_username_hint)
    WHERE status = 'pending_claim'
"""

_CLAIM_TOKEN_UNIQUE_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_fine_subscriptions_claim_token
    ON fine_monitoring_subscriptions (claim_token)
    WHERE claim_token IS NOT NULL
"""

_INSERT = """
INSERT INTO fine_monitoring_subscriptions (
    monitoring_task_id, car_number, telegram_user_id, telegram_chat_id,
    telegram_username, start_date, end_date, source,
    owner_username_hint, created_by_telegram_user_id, created_by_telegram_chat_id
) VALUES (
    :monitoring_task_id, :car_number, :telegram_user_id, :telegram_chat_id,
    :telegram_username, :start_date, :end_date, :source,
    :owner_username_hint, :created_by_telegram_user_id, :created_by_telegram_chat_id
)
"""

_INSERT_WITHOUT_OWNER = """
INSERT INTO fine_monitoring_subscriptions (
    monitoring_task_id, car_number, start_date, end_date, source,
    created_by_telegram_user_id, created_by_telegram_chat_id
) VALUES (
    :monitoring_task_id, :car_number, :start_date, :end_date, :source,
    :created_by_telegram_user_id, :created_by_telegram_chat_id
)
"""

_INSERT_PENDING_CLAIM = """
INSERT INTO fine_monitoring_subscriptions (
    monitoring_task_id, car_number, status, start_date, end_date, source,
    owner_username_hint, created_by_telegram_user_id, created_by_telegram_chat_id,
    claim_token, claim_token_expires_at
) VALUES (
    :monitoring_task_id, :car_number, 'pending_claim', :start_date, :end_date, :source,
    :owner_username_hint, :created_by_telegram_user_id, :created_by_telegram_chat_id,
    :claim_token, :claim_token_expires_at
)
"""

_SELECT_FIELDS = """
    id, monitoring_task_id, car_number, telegram_user_id, telegram_chat_id,
    telegram_username, status, start_date, end_date, source,
    created_at, updated_at, stopped_at,
    owner_username_hint, created_by_telegram_user_id, created_by_telegram_chat_id,
    claim_token, claim_token_expires_at
"""

_SELECT_BY_ID = f"SELECT {_SELECT_FIELDS} FROM fine_monitoring_subscriptions WHERE id = ?"

_SELECT_BY_CLAIM_TOKEN = f"""
    SELECT {_SELECT_FIELDS} FROM fine_monitoring_subscriptions WHERE claim_token = ?
"""

_SELECT_PENDING_CLAIM_FOR_TASK_AND_HINT = f"""
    SELECT {_SELECT_FIELDS} FROM fine_monitoring_subscriptions
    WHERE monitoring_task_id = :monitoring_task_id
      AND owner_username_hint = :owner_username_hint COLLATE NOCASE
      AND status = 'pending_claim'
"""

_SELECT_ACTIVE_FOR_USER_AND_CAR = f"""
    SELECT {_SELECT_FIELDS} FROM fine_monitoring_subscriptions
    WHERE telegram_user_id = :telegram_user_id
      AND car_number = :car_number
      AND status = 'active'
      AND end_date >= :today
"""

_SELECT_ACTIVE_OWNERLESS_FOR_CREATOR_AND_TASK = f"""
    SELECT {_SELECT_FIELDS} FROM fine_monitoring_subscriptions
    WHERE monitoring_task_id = :monitoring_task_id
      AND created_by_telegram_user_id = :created_by_telegram_user_id
      AND telegram_user_id IS NULL
      AND status = 'active'
"""

_SELECT_BY_USER = f"""
    SELECT {_SELECT_FIELDS} FROM fine_monitoring_subscriptions
    WHERE telegram_user_id = ?
    ORDER BY created_at DESC, id DESC
"""

_SELECT_BY_CREATOR = f"""
    SELECT {_SELECT_FIELDS} FROM fine_monitoring_subscriptions
    WHERE created_by_telegram_user_id = ?
    ORDER BY created_at DESC, id DESC
"""

_SELECT_ACTIVE_SUBSCRIBERS_FOR_CAR = f"""
    SELECT {_SELECT_FIELDS} FROM fine_monitoring_subscriptions
    WHERE car_number = :car_number
      AND status = 'active'
      AND end_date >= :today
    ORDER BY id ASC
"""

_UPDATE_PERIOD = """
UPDATE fine_monitoring_subscriptions
SET start_date = :start_date, end_date = :end_date, updated_at = CURRENT_TIMESTAMP
WHERE id = :id
"""

_REFRESH_PENDING_CLAIM = """
UPDATE fine_monitoring_subscriptions
SET start_date = :start_date, end_date = :end_date,
    claim_token = :claim_token, claim_token_expires_at = :claim_token_expires_at,
    updated_at = CURRENT_TIMESTAMP
WHERE id = :id AND status = 'pending_claim'
"""

# Владелец ИЛИ создавший delegated-подписку trusted-оператор (см. design
# report: "trusted operator должен иметь возможность управлять delegated
# subscription, которую создал он"). created_by_telegram_user_id IS NULL
# для обычных self-service подписок — "created_by_telegram_user_id = :id"
# в SQL никогда не истинно при NULL слева, поэтому это условие безвредно
# (никогда не срабатывает) для не-delegated строк, и старое поведение
# "может остановить только владелец" для них сохраняется бит в бит.
# status IN ('active', 'pending_claim') — см. design report Stage 4: trusted-
# оператор должен уметь отменить ещё НЕ claimed приглашение через ⛔
# Остановить мониторинг, а не только уже активную подписку.
_STOP_BY_OWNER_OR_CREATOR = """
UPDATE fine_monitoring_subscriptions
SET status = 'stopped', stopped_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
WHERE id = :id
  AND (telegram_user_id = :telegram_user_id OR created_by_telegram_user_id = :telegram_user_id)
  AND status IN ('active', 'pending_claim')
"""

_EXPIRE_ELAPSED = """
UPDATE fine_monitoring_subscriptions
SET status = 'expired', updated_at = CURRENT_TIMESTAMP
WHERE status = 'active' AND end_date < :today
"""

# Все подписки, которым машина ЕЩЁ нужна прямо сейчас — либо у неё уже
# есть подтверждённый владелец (status='active'), либо она ждёт claim
# (status='pending_claim', см. design report) — и при этом есть хоть один
# получатель для доставки (owner ИЛИ trusted-оператор). Используется client
# delivery poller'ом (см. reader/public_bot/delivery_service.py) как
# основной, БОЛЕЕ ДЕШЁВЫЙ вход в join с detected_fines (число подписок
# заведомо меньше и ограниченнее числа исторических штрафов).
_SELECT_ALL_DELIVERABLE = f"""
    SELECT {_SELECT_FIELDS} FROM fine_monitoring_subscriptions
    WHERE status IN ('active', 'pending_claim')
      AND end_date >= :today
      AND (telegram_user_id IS NOT NULL OR created_by_telegram_user_id IS NOT NULL)
    ORDER BY id ASC
"""

# См. design report Stage 4, раздел "Task lifecycle" —
# extend_client_bot_task_if_still_needed() пересчитывает, до какого
# end_date задача ВСЁ ЕЩЁ нужна кому-то из активных/pending_claim
# подписчиков, прежде чем FineJob пометит её completed.
_SELECT_MAX_RELEVANT_END_DATE_FOR_CAR = """
    SELECT MAX(end_date) FROM fine_monitoring_subscriptions
    WHERE car_number = :car_number
      AND status IN ('active', 'pending_claim')
      AND end_date >= :today
"""

_CLAIM = """
UPDATE fine_monitoring_subscriptions
SET telegram_user_id = :telegram_user_id,
    telegram_chat_id = :telegram_chat_id,
    telegram_username = :telegram_username,
    status = 'active',
    claim_token = NULL,
    claim_token_expires_at = NULL,
    updated_at = CURRENT_TIMESTAMP
WHERE id = :id AND status = 'pending_claim' AND claim_token = :claim_token
"""


class DuplicateActiveSubscriptionError(Exception):
    """У этого telegram_user_id уже есть активная подписка на эту же
    задачу мониторинга (см. idx_fine_subscriptions_active_user_task).
    Вызывающий код (будущие bot-хендлеры) должен в этом случае обновить
    существующую подписку (update_period), а не пытаться создать новую."""


class DuplicatePendingClaimError(Exception):
    """Уже есть незавершённое приглашение (pending_claim) на именно этот
    owner_username_hint для этой же задачи мониторинга (см.
    idx_fine_subscriptions_pending_claim_task_hint). Вызывающий код должен
    продлить существующее (refresh_pending_claim), а не создавать второе."""


class DuplicateActiveOwnerlessSubscriptionError(Exception):
    """У этого trusted-оператора уже есть активная "безвладельческая"
    (без клиента) подписка на эту же задачу мониторинга (см.
    idx_fine_subscriptions_active_ownerless_creator_task). Вызывающий код
    должен продлить существующую (update_period), а не создавать вторую."""


def generate_claim_token() -> str:
    """Криптографически случайный, непредсказуемый, single-use токен для
    deep-link claim (см. design report: "нельзя подменить owner/user_id
    через callback payload" — тот же принцип распространяется и на
    deep-link payload, токен непредсказуем и никогда не содержит внутри
    себя ничего, что можно было бы подменить/угадать)."""
    return secrets.token_urlsafe(24)


def default_claim_token_expiry(now: datetime) -> datetime:
    return now + timedelta(days=7)


def _row_to_subscription(row) -> FineMonitoringSubscription:
    (
        id_,
        monitoring_task_id,
        car_number,
        telegram_user_id,
        telegram_chat_id,
        telegram_username,
        status,
        start_date,
        end_date,
        source,
        created_at,
        updated_at,
        stopped_at,
        owner_username_hint,
        created_by_telegram_user_id,
        created_by_telegram_chat_id,
        claim_token,
        claim_token_expires_at,
    ) = row

    return FineMonitoringSubscription(
        id=id_,
        monitoring_task_id=monitoring_task_id,
        car_number=car_number,
        telegram_user_id=telegram_user_id,
        telegram_chat_id=telegram_chat_id,
        telegram_username=telegram_username,
        status=status,
        start_date=date.fromisoformat(start_date),
        end_date=date.fromisoformat(end_date),
        source=source,
        created_at=datetime.fromisoformat(created_at),
        updated_at=datetime.fromisoformat(updated_at),
        stopped_at=datetime.fromisoformat(stopped_at) if stopped_at else None,
        owner_username_hint=owner_username_hint,
        created_by_telegram_user_id=created_by_telegram_user_id,
        created_by_telegram_chat_id=created_by_telegram_chat_id,
        claim_token=claim_token,
        claim_token_expires_at=(
            datetime.fromisoformat(claim_token_expires_at) if claim_token_expires_at else None
        ),
    )


class FineSubscriptionRepository:
    """Та же БД, что и у остальных репозиториев проекта
    (settings.app.users_db_file) — отдельная таблица, отдельное соединение,
    по тому же образцу, что и FineMonitoringTaskRepository/UserRepository."""

    def __init__(self, db_path: Path):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute(_SCHEMA)
        self._migrate_nullable_owner_columns_if_needed()
        self._conn.execute(_UNIQUE_ACTIVE_INDEX)
        self._conn.execute(_CAR_STATUS_INDEX)
        self._conn.execute(_USER_STATUS_INDEX)
        self._conn.execute(_PENDING_CLAIM_UNIQUE_INDEX)
        self._conn.execute(_CLAIM_TOKEN_UNIQUE_INDEX)
        self._conn.execute(_UNIQUE_ACTIVE_OWNERLESS_INDEX)
        self._conn.commit()

    def _migrate_nullable_owner_columns_if_needed(self) -> None:
        """SQLite не поддерживает "ALTER TABLE ... ALTER COLUMN ... DROP
        NOT NULL" — единственный официально документированный безопасный
        способ ослабить ограничение — пересоздать таблицу (SQLite "12-step"
        procedure): переименовать старую -> создать новую (уже в актуальном,
        nullable, виде — той же _SCHEMA, что и обычный CREATE TABLE IF NOT
        EXISTS выше) -> скопировать данные С СОХРАНЕНИЕМ id (чтобы
        существующие ссылки client_fine_deliveries.subscription_id остались
        валидны) -> удалить старую -> пересоздать индексы -> поправить
        sqlite_sequence (явная вставка id при копировании не продвигает
        автоинкремент сама по себе, иначе следующий INSERT рискует
        попытаться переиспользовать уже занятый id). Всё — в одной
        транзакции: либо применяется целиком, либо не применяется вовсе.

        No-op, если таблица только что создана CREATE TABLE IF NOT EXISTS
        (уже в новом виде) — определяем необходимость миграции по факту,
        что telegram_user_id всё ещё NOT NULL (PRAGMA table_info(...)[3]),
        единственному различию старой (Stage 1) и новой схемы, которое
        нельзя устранить через ADD COLUMN."""
        columns = {
            row[1]: row for row in self._conn.execute(
                "PRAGMA table_info(fine_monitoring_subscriptions)"
            )
        }
        # cid, name, type, notnull, dflt_value, pk
        telegram_user_id_column = columns.get("telegram_user_id")
        if telegram_user_id_column is None or telegram_user_id_column[3] == 0:
            # Таблицы ещё нет (не должно происходить — CREATE TABLE IF NOT
            # EXISTS уже отработал выше) либо она уже в новом (nullable) виде.
            return

        self._conn.execute("PRAGMA foreign_keys=OFF")
        try:
            with self._conn:
                self._conn.execute(
                    "ALTER TABLE fine_monitoring_subscriptions "
                    "RENAME TO fine_monitoring_subscriptions_pre_nullable_owner"
                )
                self._conn.execute(_SCHEMA)
                self._conn.execute(
                    """
                    INSERT INTO fine_monitoring_subscriptions (
                        id, monitoring_task_id, car_number, telegram_user_id,
                        telegram_chat_id, telegram_username, status, start_date,
                        end_date, source, created_at, updated_at, stopped_at
                    )
                    SELECT
                        id, monitoring_task_id, car_number, telegram_user_id,
                        telegram_chat_id, telegram_username, status, start_date,
                        end_date, source, created_at, updated_at, stopped_at
                    FROM fine_monitoring_subscriptions_pre_nullable_owner
                    """
                )
                self._conn.execute(
                    "DROP TABLE fine_monitoring_subscriptions_pre_nullable_owner"
                )
                self._conn.execute(_UNIQUE_ACTIVE_INDEX)
                self._conn.execute(_CAR_STATUS_INDEX)
                self._conn.execute(_USER_STATUS_INDEX)
                self._conn.execute(_PENDING_CLAIM_UNIQUE_INDEX)
                self._conn.execute(_CLAIM_TOKEN_UNIQUE_INDEX)
                # AUTOINCREMENT continuity — см. докстрок выше.
                self._conn.execute(
                    "INSERT INTO sqlite_sequence (name, seq) "
                    "SELECT 'fine_monitoring_subscriptions', COALESCE(MAX(id), 0) "
                    "FROM fine_monitoring_subscriptions "
                    "WHERE NOT EXISTS ("
                    "  SELECT 1 FROM sqlite_sequence "
                    "  WHERE name = 'fine_monitoring_subscriptions'"
                    ")"
                )
                self._conn.execute(
                    "UPDATE sqlite_sequence SET seq = ("
                    "  SELECT COALESCE(MAX(id), 0) FROM fine_monitoring_subscriptions"
                    ") WHERE name = 'fine_monitoring_subscriptions'"
                )
        finally:
            self._conn.execute("PRAGMA foreign_keys=ON")

    def create(
        self,
        *,
        monitoring_task_id: int,
        car_number: str,
        telegram_user_id: int,
        telegram_chat_id: int,
        telegram_username: str | None,
        start_date: date,
        end_date: date,
        source: str = "geshtrafbot",
        owner_username_hint: str | None = None,
        created_by_telegram_user_id: int | None = None,
        created_by_telegram_chat_id: int | None = None,
    ) -> FineMonitoringSubscription:
        """telegram_user_id/telegram_chat_id здесь ВСЕГДА реальный,
        известный numeric id — для случая "владелец ещё не резолвлен"
        используйте create_pending_claim(), а не None (типы здесь
        намеренно не Optional, несмотря на то что колонка в БД уже
        nullable — так сигнатура отражает предполагаемое использование).

        created_by_telegram_user_id/created_by_telegram_chat_id — trusted-
        оператор, поставивший машину на мониторинг ДЛЯ ДРУГОГО человека
        (см. design report); None (по умолчанию) — обычная self-service
        подписка, поведение не отличается от Stage 1."""
        try:
            cursor = self._conn.execute(
                _INSERT,
                {
                    "monitoring_task_id": monitoring_task_id,
                    "car_number": car_number,
                    "telegram_user_id": telegram_user_id,
                    "telegram_chat_id": telegram_chat_id,
                    "telegram_username": telegram_username,
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                    "source": source,
                    "owner_username_hint": owner_username_hint,
                    "created_by_telegram_user_id": created_by_telegram_user_id,
                    "created_by_telegram_chat_id": created_by_telegram_chat_id,
                },
            )
        except sqlite3.IntegrityError as exc:
            # Незакоммиченная транзакция после нарушения UNIQUE держит
            # блокировку записи на этом соединении, пока её явно не
            # откатить — тот же приём, что и в DetectedFineRepository.create().
            self._conn.rollback()
            raise DuplicateActiveSubscriptionError(
                f"У пользователя {telegram_user_id} уже есть активная подписка "
                f"на задачу мониторинга {monitoring_task_id}"
            ) from exc

        self._conn.commit()

        subscription = self.get(cursor.lastrowid)
        if subscription is None:
            raise RuntimeError("Не удалось прочитать только что созданную подписку")
        return subscription

    def create_pending_claim(
        self,
        *,
        monitoring_task_id: int,
        car_number: str,
        owner_username_hint: str,
        created_by_telegram_user_id: int,
        created_by_telegram_chat_id: int,
        start_date: date,
        end_date: date,
        claim_token: str,
        claim_token_expires_at: datetime,
        source: str = "geshtrafbot",
    ) -> FineMonitoringSubscription:
        """Машина УЖЕ мониторится (FineMonitoringTask создана/продлена
        вызывающим кодом ДО этого вызова), но указанный владелец не
        резолвлен в numeric id — telegram_user_id/telegram_chat_id
        остаются NULL до claim() (см. design report). Ровно один
        незавершённый pending_claim на (monitoring_task_id,
        owner_username_hint) одновременно — см.
        idx_fine_subscriptions_pending_claim_task_hint."""
        try:
            cursor = self._conn.execute(
                _INSERT_PENDING_CLAIM,
                {
                    "monitoring_task_id": monitoring_task_id,
                    "car_number": car_number,
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                    "source": source,
                    "owner_username_hint": owner_username_hint,
                    "created_by_telegram_user_id": created_by_telegram_user_id,
                    "created_by_telegram_chat_id": created_by_telegram_chat_id,
                    "claim_token": claim_token,
                    "claim_token_expires_at": claim_token_expires_at.isoformat(),
                },
            )
        except sqlite3.IntegrityError as exc:
            self._conn.rollback()
            raise DuplicatePendingClaimError(
                f"Уже есть незавершённое приглашение для @{owner_username_hint} "
                f"на задачу мониторинга {monitoring_task_id}"
            ) from exc

        self._conn.commit()

        subscription = self.get(cursor.lastrowid)
        if subscription is None:
            raise RuntimeError("Не удалось прочитать только что созданный pending_claim")
        return subscription

    def create_without_owner(
        self,
        *,
        monitoring_task_id: int,
        car_number: str,
        created_by_telegram_user_id: int,
        created_by_telegram_chat_id: int,
        start_date: date,
        end_date: date,
        source: str = "geshtrafbot",
    ) -> FineMonitoringSubscription:
        """Trusted-оператор ставит машину на мониторинг БЕЗ указания
        клиента (см. design: "👤 Добавить Telegram клиента?" → "Отмена") —
        telegram_user_id/telegram_chat_id/telegram_username/
        owner_username_hint остаются NULL, status сразу 'active' (в
        отличие от create_pending_claim — здесь никого не ждём, клиента
        просто нет и, возможно, не будет никогда). Доставка штрафов такой
        подписке (см. reader/public_bot/delivery_service.py::
        _applicable_roles) получает роль ТОЛЬКО 'trusted_operator' —
        'owner' невозможна без telegram_user_id.

        Не более одной такой строки на (monitoring_task_id,
        created_by_telegram_user_id) одновременно — см.
        idx_fine_subscriptions_active_ownerless_creator_task; повторное
        "Добавить авто" без клиента для той же машины тем же оператором
        должно продлить эту же строку (см.
        SubscriptionService._create_or_update_ownerless_subscription), а
        не создать вторую."""
        try:
            cursor = self._conn.execute(
                _INSERT_WITHOUT_OWNER,
                {
                    "monitoring_task_id": monitoring_task_id,
                    "car_number": car_number,
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                    "source": source,
                    "created_by_telegram_user_id": created_by_telegram_user_id,
                    "created_by_telegram_chat_id": created_by_telegram_chat_id,
                },
            )
        except sqlite3.IntegrityError as exc:
            self._conn.rollback()
            raise DuplicateActiveOwnerlessSubscriptionError(
                f"У оператора {created_by_telegram_user_id} уже есть активная "
                f"безвладельческая подписка на задачу мониторинга {monitoring_task_id}"
            ) from exc

        self._conn.commit()

        subscription = self.get(cursor.lastrowid)
        if subscription is None:
            raise RuntimeError("Не удалось прочитать только что созданную подписку")
        return subscription

    def get_active_ownerless_for_creator_and_task(
        self, monitoring_task_id: int, created_by_telegram_user_id: int,
    ) -> FineMonitoringSubscription | None:
        """Существующая активная "безвладельческая" подписка ЭТОГО
        trusted-оператора на ЭТУ задачу мониторинга, если есть — см.
        create_without_owner про то, зачем нужна дедупликация повторного
        "Добавить авто без клиента"."""
        row = self._conn.execute(
            _SELECT_ACTIVE_OWNERLESS_FOR_CREATOR_AND_TASK,
            {
                "monitoring_task_id": monitoring_task_id,
                "created_by_telegram_user_id": created_by_telegram_user_id,
            },
        ).fetchone()
        return _row_to_subscription(row) if row else None

    def get_pending_claim_for_task_and_hint(
        self, monitoring_task_id: int, owner_username_hint: str,
    ) -> FineMonitoringSubscription | None:
        row = self._conn.execute(
            _SELECT_PENDING_CLAIM_FOR_TASK_AND_HINT,
            {"monitoring_task_id": monitoring_task_id, "owner_username_hint": owner_username_hint},
        ).fetchone()
        return _row_to_subscription(row) if row else None

    def refresh_pending_claim(
        self,
        subscription_id: int,
        *,
        start_date: date,
        end_date: date,
        claim_token: str,
        claim_token_expires_at: datetime,
    ) -> FineMonitoringSubscription:
        """Продлевает уже существующий pending_claim (новый период + новый
        токен/срок действия) — вместо создания второго дубля на тот же
        (task, owner_username_hint), см. design про повторное "Добавить
        авто" тем же trusted-оператором для уже приглашённого владельца."""
        self._conn.execute(
            _REFRESH_PENDING_CLAIM,
            {
                "id": subscription_id,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "claim_token": claim_token,
                "claim_token_expires_at": claim_token_expires_at.isoformat(),
            },
        )
        self._conn.commit()

        subscription = self.get(subscription_id)
        if subscription is None:
            raise RuntimeError(f"Подписка {subscription_id} не найдена")
        return subscription

    def claim(
        self,
        claim_token: str,
        *,
        telegram_user_id: int,
        telegram_chat_id: int,
        telegram_username: str | None,
        now: datetime,
    ) -> FineMonitoringSubscription | None:
        """Связывает pending_claim с РЕАЛЬНЫМ отправителем "/start claim_...".
        telegram_user_id/telegram_chat_id/telegram_username здесь —
        ВСЕГДА event.sender_id и данные ИЗ САМОГО claim-события, никогда
        не что-либо, что могло бы быть передано через сам claim_token/
        payload (см. design report про security-инвариант "нельзя
        подменить owner/user_id через callback payload" — тот же принцип
        для deep-link claim).

        None, если токен не найден, уже использован (status уже не
        pending_claim — например, повторный переход по той же ссылке
        после первого успешного claim) либо истёк (claim_token_expires_at
        < now) — во всех случаях ничего не меняется, вызывающий код должен
        показать понятную ошибку, а не тихо создать ложную ownership-запись."""
        row = self._conn.execute(_SELECT_BY_CLAIM_TOKEN, (claim_token,)).fetchone()
        if row is None:
            return None

        candidate = _row_to_subscription(row)
        if candidate.status != "pending_claim":
            return None
        if candidate.claim_token_expires_at is not None and candidate.claim_token_expires_at < now:
            return None

        cursor = self._conn.execute(
            _CLAIM,
            {
                "id": candidate.id,
                "claim_token": claim_token,
                "telegram_user_id": telegram_user_id,
                "telegram_chat_id": telegram_chat_id,
                "telegram_username": telegram_username,
            },
        )
        self._conn.commit()

        if cursor.rowcount == 0:
            # Гонка: кто-то другой (или повторное нажатие) уже claimed
            # между SELECT и UPDATE — токен уже погашен, ничего не меняем
            # повторно.
            return None

        return self.get(candidate.id)

    def get(self, subscription_id: int) -> FineMonitoringSubscription | None:
        row = self._conn.execute(_SELECT_BY_ID, (subscription_id,)).fetchone()
        return _row_to_subscription(row) if row else None

    def get_active_for_user_and_car(
        self, telegram_user_id: int, car_number: str, *, today: date,
    ) -> FineMonitoringSubscription | None:
        """Активная подписка ЭТОГО пользователя на ЭТОТ номер — учитывает
        end_date (см. design: подписка с прошедшим end_date не считается
        активной, даже если status в БД ещё не переведён в 'expired')."""
        row = self._conn.execute(
            _SELECT_ACTIVE_FOR_USER_AND_CAR,
            {
                "telegram_user_id": telegram_user_id,
                "car_number": car_number,
                "today": today.isoformat(),
            },
        ).fetchone()
        return _row_to_subscription(row) if row else None

    def list_by_user(self, telegram_user_id: int) -> list[FineMonitoringSubscription]:
        """ВСЕ подписки пользователя, любого статуса — для "Мои авто":
        отображение само решает, как показать "истёк"/"остановлен"/"активен"
        (см. FineMonitoringSubscription.is_effectively_active)."""
        rows = self._conn.execute(_SELECT_BY_USER, (telegram_user_id,)).fetchall()
        return [_row_to_subscription(row) for row in rows]

    def list_managed_by_creator(self, created_by_telegram_user_id: int) -> list[FineMonitoringSubscription]:
        """Все delegated-подписки, заведённые ЭТИМ trusted-оператором для
        других людей (включая ещё не claimed) — "Мои авто" для trusted
        показывает их ОТДЕЛЬНО от собственных подписок оператора (см.
        design report: trusted-режим даёт управление тем, что оператор
        создал сам, а не бланкетный доступ ко всем подпискам системы)."""
        rows = self._conn.execute(_SELECT_BY_CREATOR, (created_by_telegram_user_id,)).fetchall()
        return [_row_to_subscription(row) for row in rows]

    def list_active_subscribers_for_car(
        self, car_number: str, *, today: date,
    ) -> list[FineMonitoringSubscription]:
        """Все подписчики конкретного номера, чья подписка активна ПРЯМО
        СЕЙЧАС (учитывает end_date, как и get_active_for_user_and_car) —
        используется для fan-out доставки нескольким клиентам одной машины."""
        rows = self._conn.execute(
            _SELECT_ACTIVE_SUBSCRIBERS_FOR_CAR,
            {"car_number": car_number, "today": today.isoformat()},
        ).fetchall()
        return [_row_to_subscription(row) for row in rows]

    def update_period(
        self, subscription_id: int, *, start_date: date, end_date: date,
    ) -> FineMonitoringSubscription:
        """Обновляет период уже существующей подписки (например, повторное
        "Добавить авто" на тот же номер тем же клиентом) — не создаёт
        вторую строку, в отличие от create(). Не трогает owner_username_hint/
        created_by_*: атрибуция "кто и для кого" фиксируется один раз при
        создании и не переписывается повторным продлением."""
        self._conn.execute(
            _UPDATE_PERIOD,
            {
                "id": subscription_id,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            },
        )
        self._conn.commit()

        subscription = self.get(subscription_id)
        if subscription is None:
            raise RuntimeError(f"Подписка {subscription_id} не найдена")
        return subscription

    def stop_by_owner_or_creator(self, subscription_id: int, *, telegram_user_id: int) -> bool:
        """Останавливает подписку, ТОЛЬКО если telegram_user_id совпадает
        с её реальным владельцем ИЛИ с trusted-оператором, создавшим её
        как delegated (см. design report) — защита на уровне репозитория
        (defense-in-depth), а не только будущих bot-хендлеров: ни чужую
        обычную, ни чужую delegated-подписку остановить нельзя, даже
        подобрав/подделав subscription_id. Работает и для ещё НЕ claimed
        pending_claim (trusted-оператор отменяет своё приглашение), и для
        обычной активной подписки. Возвращает False, если ничего не
        остановлено (подписка не найдена, принадлежит/создана другим
        пользователем, либо уже не active/pending_claim) — вызывающий код
        не должен показывать "остановлено" в этом случае."""
        cursor = self._conn.execute(
            _STOP_BY_OWNER_OR_CREATOR,
            {"id": subscription_id, "telegram_user_id": telegram_user_id},
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def list_all_deliverable(self, *, today: date) -> list[FineMonitoringSubscription]:
        """Все подписки, которым машина ещё нужна и у которых есть хоть
        один потенциальный получатель (owner и/или trusted-оператор) —
        точка входа client delivery poller'а (см. design report Stage 4:
        "точную последовательность check → detected_fines → client
        delivery"). Не решает, КАКАЯ именно роль применима к конкретной
        подписке — это решает вызывающий код (см.
        reader/public_bot/delivery_service.py) по её собственным полям
        (status/telegram_user_id/created_by_telegram_user_id)."""
        rows = self._conn.execute(_SELECT_ALL_DELIVERABLE, {"today": today.isoformat()}).fetchall()
        return [_row_to_subscription(row) for row in rows]

    def max_relevant_end_date_for_car(self, car_number: str, *, today: date) -> date | None:
        """Максимальный end_date среди ещё действующих (active ИЛИ
        pending_claim, см. design report) подписок этой машины — None,
        если таких не осталось вовсе. Используется ТОЛЬКО для client_bot-
        scope задач (см. reader/public_bot/subscription_service.py::
        extend_client_bot_task_if_still_needed) — операторские задачи эту
        логику не используют."""
        row = self._conn.execute(
            _SELECT_MAX_RELEVANT_END_DATE_FOR_CAR,
            {"car_number": car_number, "today": today.isoformat()},
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return date.fromisoformat(row[0])

    def expire_elapsed(self, *, today: date) -> int:
        """Массово переводит в status='expired' все ещё 'active' подписки
        с end_date раньше today. Это ГИГИЕНА отображения/консистентности
        статуса, а НЕ источник истины: get_active_for_user_and_car() и
        list_active_subscribers_for_car() и без вызова этого метода никогда
        не вернут просроченную подписку (см. их AND end_date >= :today) —
        корректность доставки/списков не зависит от того, вызван ли и как
        часто этот метод. pending_claim-строки не затрагиваются (у них
        нет status='active'). Возвращает число обновлённых строк."""
        cursor = self._conn.execute(_EXPIRE_ELAPSED, {"today": today.isoformat()})
        self._conn.commit()
        return cursor.rowcount

    def close(self) -> None:
        self._conn.close()
