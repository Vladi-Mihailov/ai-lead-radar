"""FineSubscriptionRepository — fine_monitoring_subscriptions поверх SQLite.

Отдельная таблица от fine_monitoring_tasks (см.
reader/fines/task_repository.py) — подписка КЛИЕНТА на мониторинг, а не сам
мониторинг: одна FineMonitoringTask (один car_number) может быть связана
сразу с несколькими подписками (несколько клиентов на одном автомобиле), и
наоборот — у одного Telegram-пользователя может быть несколько подписок
(разные машины).

Нормализация car_number — забота вызывающего кода (reader/fines/validation.py),
как и у FineMonitoringTaskRepository — репозиторий хранит то, что получил.
"""

import sqlite3
from datetime import date, datetime
from pathlib import Path

from reader.public_bot.models import FineMonitoringSubscription

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fine_monitoring_subscriptions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    monitoring_task_id  INTEGER NOT NULL REFERENCES fine_monitoring_tasks(id),
    car_number          TEXT NOT NULL,
    telegram_user_id    INTEGER NOT NULL,
    telegram_chat_id    INTEGER NOT NULL,
    telegram_username   TEXT,
    status              TEXT NOT NULL DEFAULT 'active',
    start_date          TEXT NOT NULL,
    end_date            TEXT NOT NULL,
    source              TEXT NOT NULL DEFAULT 'geshtrafbot',
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    stopped_at          TIMESTAMP
)
"""

# Одновременно может быть только ОДНА активная подписка того же
# пользователя на ту же задачу мониторинга — повторное "Добавить авто" на
# уже отслеживаемый им номер должно ОБНОВИТЬ существующую подписку (см.
# update_period), а не создать вторую. Частичный индекс (WHERE status =
# 'active') не мешает копить сколько угодно НЕ активных (stopped/expired)
# исторических строк того же (task, user) — история не теряется,
# останавливать/возобновлять можно многократно.
_UNIQUE_ACTIVE_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_fine_subscriptions_active_user_task
    ON fine_monitoring_subscriptions (monitoring_task_id, telegram_user_id)
    WHERE status = 'active'
"""

_CAR_STATUS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_fine_subscriptions_car_status
    ON fine_monitoring_subscriptions (car_number, status)
"""

_USER_STATUS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_fine_subscriptions_user_status
    ON fine_monitoring_subscriptions (telegram_user_id, status)
"""

_INSERT = """
INSERT INTO fine_monitoring_subscriptions (
    monitoring_task_id, car_number, telegram_user_id, telegram_chat_id,
    telegram_username, start_date, end_date, source
) VALUES (
    :monitoring_task_id, :car_number, :telegram_user_id, :telegram_chat_id,
    :telegram_username, :start_date, :end_date, :source
)
"""

_SELECT_FIELDS = """
    id, monitoring_task_id, car_number, telegram_user_id, telegram_chat_id,
    telegram_username, status, start_date, end_date, source,
    created_at, updated_at, stopped_at
"""

_SELECT_BY_ID = f"SELECT {_SELECT_FIELDS} FROM fine_monitoring_subscriptions WHERE id = ?"

_SELECT_ACTIVE_FOR_USER_AND_CAR = f"""
    SELECT {_SELECT_FIELDS} FROM fine_monitoring_subscriptions
    WHERE telegram_user_id = :telegram_user_id
      AND car_number = :car_number
      AND status = 'active'
      AND end_date >= :today
"""

_SELECT_BY_USER = f"""
    SELECT {_SELECT_FIELDS} FROM fine_monitoring_subscriptions
    WHERE telegram_user_id = ?
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

_STOP_OWN = """
UPDATE fine_monitoring_subscriptions
SET status = 'stopped', stopped_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
WHERE id = :id AND telegram_user_id = :telegram_user_id AND status = 'active'
"""

_EXPIRE_ELAPSED = """
UPDATE fine_monitoring_subscriptions
SET status = 'expired', updated_at = CURRENT_TIMESTAMP
WHERE status = 'active' AND end_date < :today
"""


class DuplicateActiveSubscriptionError(Exception):
    """У этого telegram_user_id уже есть активная подписка на эту же
    задачу мониторинга (см. idx_fine_subscriptions_active_user_task).
    Вызывающий код (будущие bot-хендлеры) должен в этом случае обновить
    существующую подписку (update_period), а не пытаться создать новую."""


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
        self._conn.execute(_UNIQUE_ACTIVE_INDEX)
        self._conn.execute(_CAR_STATUS_INDEX)
        self._conn.execute(_USER_STATUS_INDEX)
        self._conn.commit()

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
    ) -> FineMonitoringSubscription:
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
        вторую строку, в отличие от create()."""
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

    def stop_own_subscription(self, subscription_id: int, *, telegram_user_id: int) -> bool:
        """Останавливает подписку, ТОЛЬКО если telegram_user_id совпадает
        с её реальным владельцем — защита на уровне репозитория
        (defense-in-depth), а не только будущих bot-хендлеров: чужую
        подписку остановить нельзя, даже подобрав/подделав subscription_id
        (см. design про forwarded-сообщения с inline-кнопками и
        callback_data). Возвращает False, если ничего не остановлено
        (подписка не найдена, принадлежит другому пользователю, либо уже
        не 'active') — вызывающий код не должен показывать "остановлено"
        в этом случае."""
        cursor = self._conn.execute(
            _STOP_OWN, {"id": subscription_id, "telegram_user_id": telegram_user_id},
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def expire_elapsed(self, *, today: date) -> int:
        """Массово переводит в status='expired' все ещё 'active' подписки
        с end_date раньше today. Это ГИГИЕНА отображения/консистентности
        статуса, а НЕ источник истины: get_active_for_user_and_car() и
        list_active_subscribers_for_car() и без вызова этого метода никогда
        не вернут просроченную подписку (см. их AND end_date >= :today) —
        корректность доставки/списков не зависит от того, вызван ли и как
        часто этот метод. Возвращает число обновлённых строк."""
        cursor = self._conn.execute(_EXPIRE_ELAPSED, {"today": today.isoformat()})
        self._conn.commit()
        return cursor.rowcount

    def close(self) -> None:
        self._conn.close()
