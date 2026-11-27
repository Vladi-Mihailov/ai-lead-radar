"""Пауза и граница уведомлений Грузии (см. задачу "восстановление истёкших
подписок": во время работ и первоначальной инициализации никому ничего не
отправляется, а после включения доставки уходят только новые события).

Одно состояние на всю базу (users.db). Его читают на каждом тике оба
отправителя — client delivery poller бота (reader/public_bot/
delivery_service.py) и операторский flush main-процесса (reader/fines/
notification_coordinator.py), — поэтому пауза и её снятие не требуют
перезапусков (scripts/georgia_notifications.py pause / resume).

- Пауза: ничего не отправляется — ни клиентам, ни менеджерам (создателям
  делегированных подписок и в чат менеджеров), ни в операторский чат, ни
  "штрафов не найдено" после bootstrap'а. Сбор данных (проверки, feed,
  статусы) идёт как обычно; неотправленное остаётся неотправленным.
- Снятие паузы фиксирует границу (boundary_at). Всё, что к этому моменту
  ждало отправки, явно исключается из рассылки (client_fine_deliveries.
  excluded_at, fine_operator_notification_exclusions) — с причиной и без
  отметки о доставке.
- После границы "новый штраф" — только квитанция, которую впервые увидели
  ПОСЛЕ неё. Квитанция, известная раньше (⏳, подтверждённая позже,
  восстановленная подписка, повторное обнаружение, включённая заново
  машина), не рассылается и исключается при первой встрече.

Пока границы нет (до первого resume), поведение прежнее.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Причины явного исключения из рассылки (client_fine_deliveries.
# exclusion_reason, fine_operator_notification_exclusions.reason).
EXCLUSION_NOTIFICATION_BOUNDARY = "notification_boundary"
EXCLUSION_KNOWN_BEFORE_BOUNDARY = "known_before_boundary"
EXCLUSION_SUBSCRIPTION_RESTORED = "subscription_restored"
# Поздняя привязка клиента к уже добавленной машине (см.
# SubscriptionService.link_client_to_task): известные штрафы новыми не считаются.
EXCLUSION_LATE_CLIENT_LINK = "late_client_link"

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS georgia_notification_control (
        id               INTEGER PRIMARY KEY CHECK (id = 1),
        delivery_paused  INTEGER NOT NULL DEFAULT 0,
        paused_at        TIMESTAMP,
        boundary_at      TIMESTAMP,
        updated_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS georgia_notification_control_log (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        action             TEXT NOT NULL,
        note               TEXT,
        client_excluded    INTEGER NOT NULL DEFAULT 0,
        operator_excluded  INTEGER NOT NULL DEFAULT 0,
        created_at         TIMESTAMP NOT NULL
    )
    """,
    "INSERT OR IGNORE INTO georgia_notification_control (id, delivery_paused) VALUES (1, 0)",
)


@dataclass(frozen=True)
class NotificationControlState:
    paused: bool
    paused_at: datetime | None
    boundary_at: datetime | None


def _fmt(value: datetime) -> str:
    """Тот же вид, что у SQLite CURRENT_TIMESTAMP: naive UTC без долей секунды."""
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=0).isoformat(sep=" ")


def parse_utc(value: str | None) -> datetime | None:
    """naive-но-UTC значение из SQLite (CURRENT_TIMESTAMP или isoformat) ->
    naive UTC, чтобы сравнения не зависели от формата записи."""
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


class GeorgiaNotificationControl:
    def __init__(self, db_path: Path):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        for statement in _SCHEMA:
            self._conn.execute(statement)
        self._conn.commit()

    def state(self) -> NotificationControlState:
        paused, paused_at, boundary_at = self._conn.execute(
            "SELECT delivery_paused, paused_at, boundary_at FROM georgia_notification_control WHERE id = 1"
        ).fetchone()
        return NotificationControlState(
            paused=bool(paused), paused_at=parse_utc(paused_at), boundary_at=parse_utc(boundary_at),
        )

    def is_paused(self) -> bool:
        return self.state().paused

    def pause(self, *, note: str | None = None, at: datetime | None = None) -> bool:
        """Идемпотентно: уже стоящая пауза не меняется (paused_at — момент
        первой постановки). True — пауза поставлена этим вызовом."""
        at = at or _now()
        cursor = self._conn.execute(
            "UPDATE georgia_notification_control SET delivery_paused = 1, paused_at = ?, updated_at = ? "
            "WHERE id = 1 AND delivery_paused = 0",
            (_fmt(at), _fmt(at)),
        )
        if cursor.rowcount:
            self._log("pause", note=note, at=at)
        self._conn.commit()
        return bool(cursor.rowcount)

    def resume(
        self, *, client_excluded: int, operator_excluded: int, note: str | None = None, at: datetime | None = None,
    ) -> datetime:
        """Снимает паузу и фиксирует границу. Вызывать ПОСЛЕ явного
        исключения накопленного (см. scripts/georgia_notifications.py):
        всё, что появится между исключением и этим вызовом, раньше границы
        и будет исключено при первой встрече (known_before_boundary)."""
        at = at or _now()
        if not self.is_paused():
            raise RuntimeError("Уведомления Грузии не на паузе — снимать нечего")
        self._conn.execute(
            "UPDATE georgia_notification_control SET delivery_paused = 0, boundary_at = ?, updated_at = ? WHERE id = 1",
            (_fmt(at), _fmt(at)),
        )
        self._log("resume", note=note, at=at, client_excluded=client_excluded, operator_excluded=operator_excluded)
        self._conn.commit()
        return at

    def receipt_first_seen_at(
        self, *, car_number: str, protocol_no: str | None, row_first_detected_at: datetime | None,
    ) -> datetime | None:
        """Когда эту квитанцию этого номера увидели впервые — по любой задаче
        (detected_fines) и по статусам оплаты (georgia_fine_status). Строка
        без номера квитанции (старые записи) — сама по себе."""
        if not protocol_no:
            return row_first_detected_at
        seen: list[datetime] = []
        row = self._conn.execute(
            "SELECT MIN(first_detected_at) FROM detected_fines WHERE car_number = ? AND external_fine_id = ?",
            (car_number, protocol_no),
        ).fetchone()
        if row and row[0]:
            seen.append(parse_utc(row[0]))
        try:
            row = self._conn.execute(
                "SELECT first_listed_at FROM georgia_fine_status WHERE car_number = ? AND protocol_no = ?",
                (car_number, protocol_no),
            ).fetchone()
        except sqlite3.OperationalError:
            row = None  # статусы оплаты ещё не заведены в этой базе
        if row and row[0]:
            seen.append(parse_utc(row[0]))
        if row_first_detected_at is not None:
            seen.append(row_first_detected_at)
        return min(seen) if seen else None

    def known_before(
        self, boundary_at: datetime, *, car_number: str, protocol_no: str | None,
        row_first_detected_at: datetime | None,
    ) -> bool:
        """True — квитанцию впервые увидели не позже границы. Та же секунда,
        что и граница, — тоже "до": лучше не отправить, чем отправить
        накопленное за паузу."""
        first_seen = self.receipt_first_seen_at(
            car_number=car_number, protocol_no=protocol_no, row_first_detected_at=row_first_detected_at,
        )
        return first_seen is not None and first_seen <= boundary_at

    def log(self) -> list[tuple]:
        return self._conn.execute(
            "SELECT action, note, client_excluded, operator_excluded, created_at "
            "FROM georgia_notification_control_log ORDER BY id"
        ).fetchall()

    def _log(
        self, action: str, *, note: str | None, at: datetime, client_excluded: int = 0, operator_excluded: int = 0,
    ) -> None:
        self._conn.execute(
            "INSERT INTO georgia_notification_control_log "
            "(action, note, client_excluded, operator_excluded, created_at) VALUES (?, ?, ?, ?, ?)",
            (action, note, client_excluded, operator_excluded, _fmt(at)),
        )

    def close(self) -> None:
        self._conn.close()
