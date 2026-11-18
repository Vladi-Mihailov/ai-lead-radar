"""Статусы оплаты штрафов Грузии (см. задачу "оплаченные штрафы, которые
продолжают отображаться на police.ge").

police.ge публикует квитанции ("Receipts of fines published") без какого-
либо признака оплаты: до оплаты и после неё запись одинакова, пока её не
уберут из базы. Поэтому статус квитанции (car_number + protocolNo) хранится
отдельно и складывается из двух независимых измерений:

- payment_status — что известно об оплате:
  'unpaid' (🔴, по умолчанию), 'paid' (🟢 — подтверждено квитанцией или
  явным сообщением официального сервиса), 'needs_review' (🟡 — оплата не
  прошла по неизвестной причине, например сайт МВД ответил
  "ჯარიმის გადახდა ვერ მოხერხდა!", или менеджер не уверен);
- presence — что показывает police.ge:
  'listed' (есть в последней успешной полной проверке), 'missing' (не найден
  один раз — ждём подтверждения), 'gone' (⚪ — не найден в двух успешных
  полных проверках с интервалом не меньше disappearance_min_gap),
  'unverified' (⏳ — квитанция из истории detected_fines, которой не было в
  последнем ответе police.ge ДО появления этих статусов: тот ответ мог быть
  ложно пустым, поэтому ни ⚪, ни подтверждённым долгом она не считается,
  пока две НОВЫЕ полные проверки с тем же интервалом её не подтвердят —
  отсутствие -> ⚪, присутствие -> снова 'listed').

Пока штраф не 'gone', он остаётся в своём статусе и, если не 'paid', — в
задолженности: одна проверка без штрафа не доказывает, что он исчез. Ошибки
и показ из кэша сюда вообще не попадают, неполный ответ (count !=
len(records)) не учитывается, а пустой ответ (police.ge под нагрузкой уже
отдавал его ложно) засчитывается, только если его подтвердил повторный
запрос (false-zero guard в check_service.py, который при известных
статусах номера срабатывает всегда).

Отказ платёжной страницы НИКОГДА не означает 🟢 сам по себе: шлюз Georgian
Card спрашивает МВД "можно ли оплатить" (ответ code 1/code 2 с произвольным
текстом, отдельного кода "уже оплачено" нет), а плательщик видит только
общий "ჯარიმის გადახდა ვერ მოხერხდა!" на police.ge/protocol/fail.php. 🟢 по
тексту страницы ставится, только если текст содержит маркер из
already_paid (по умолчанию пуст: достоверный ответ "уже оплачено" ни разу не
наблюдался).

Только SQLite той же БД (settings.app.users_db_file) — никакой сети и
Telegram здесь нет. Каждое изменение статуса пишется в журнал
georgia_fine_status_events вместе с источником и автором.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

from reader.fines.models import ParsedFineRecord
from reader.fines.suppression import is_fine_output_suppressed

logger = logging.getLogger(__name__)

PAYMENT_UNPAID = "unpaid"
PAYMENT_PAID = "paid"
PAYMENT_NEEDS_REVIEW = "needs_review"

PRESENCE_LISTED = "listed"
PRESENCE_MISSING = "missing"
PRESENCE_GONE = "gone"
PRESENCE_UNVERIFIED = "unverified"

# Что показывать (см. FineStatus.display_status): статус оплаты, пока штраф
# в базе полиции, и ⚪ — когда исчезновение подтверждено.
DISPLAY_GONE = "gone"
# ⏳ — неоплаченная квитанция в состоянии 'unverified' (см. модульный докстрок).
DISPLAY_UNVERIFIED = "unverified"

# Источник записи в журнале/последнего изменения статуса оплаты.
SOURCE_CHECK = "check"
SOURCE_FEED = "feed"
SOURCE_RECEIPT = "receipt"
SOURCE_PAYMENT_PAGE = "payment_page"
SOURCE_MANAGER = "manager"

# Наблюдаемый текст police.ge/protocol/fail.php после неудачной попытки
# оплаты (реальное наблюдение 2026-11-18): общий отказ — не доказательство
# оплаты, см. модульный докстрок.
MIA_PAYMENT_FAILED_MESSAGE = "ჯარიმის გადახდა ვერ მოხერხდა"

_NOTE_MAX_LENGTH = 300
# Короче этого маркер текста страницы оплаты не принимается: одно слово или
# пара символов совпали бы с чем угодно (см. validate_payment_page_markers).
_MIN_MARKER_LENGTH = 6
# Сколько дней после ⚪ номер ещё перепроверяется вечером — чтобы ложное ⚪
# (если оно всё же случится) сменилось на "снова найден" (см. reappeared).
GONE_FOLLOW_UP = timedelta(days=3)

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS georgia_fine_status (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        car_number          TEXT NOT NULL,
        protocol_no         TEXT NOT NULL,
        amount              REAL,
        payment_status      TEXT NOT NULL DEFAULT 'unpaid',
        presence            TEXT NOT NULL DEFAULT 'listed',
        first_listed_at     TIMESTAMP NOT NULL,
        last_listed_at      TIMESTAMP NOT NULL,
        missing_since       TIMESTAMP,
        gone_at             TIMESTAMP,
        payment_source      TEXT,
        payment_note        TEXT,
        payment_updated_at  TIMESTAMP,
        payment_updated_by  INTEGER,
        UNIQUE (car_number, protocol_no)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS georgia_fine_status_events (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        status_id      INTEGER NOT NULL REFERENCES georgia_fine_status(id),
        event          TEXT NOT NULL,
        source         TEXT NOT NULL,
        actor_user_id  INTEGER,
        note           TEXT,
        created_at     TIMESTAMP NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_georgia_fine_status_events_status ON georgia_fine_status_events (status_id)",
    """
    CREATE TABLE IF NOT EXISTS georgia_police_source_state (
        id                 INTEGER PRIMARY KEY CHECK (id = 1),
        source_updated_on  TEXT,
        observed_at        TIMESTAMP
    )
    """,
)

_SELECT_FIELDS = """
    id, car_number, protocol_no, amount, payment_status, presence,
    first_listed_at, last_listed_at, missing_since, gone_at,
    payment_source, payment_note, payment_updated_at, payment_updated_by
"""

_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _fmt(value: datetime) -> str:
    return _as_naive_utc(value).strftime(_TIMESTAMP_FORMAT)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return _as_naive_utc(datetime.fromisoformat(value))


@dataclass(frozen=True)
class FineStatus:
    id: int
    car_number: str
    protocol_no: str
    amount: float | None
    payment_status: str
    presence: str
    first_listed_at: datetime
    last_listed_at: datetime
    missing_since: datetime | None
    gone_at: datetime | None
    payment_source: str | None
    payment_note: str | None
    payment_updated_at: datetime | None
    payment_updated_by: int | None

    @property
    def display_status(self) -> str:
        """'unpaid'/'paid'/'needs_review', пока штраф в базе полиции
        (включая 'missing' — исчезновение ещё не подтверждено),
        DISPLAY_GONE (⚪) после подтверждения и DISPLAY_UNVERIFIED (⏳) —
        неоплаченная квитанция, чьё присутствие ещё не перепроверено после
        перехода на статусы."""
        if self.presence == PRESENCE_GONE:
            return DISPLAY_GONE
        if self.presence == PRESENCE_UNVERIFIED and self.payment_status == PAYMENT_UNPAID:
            return DISPLAY_UNVERIFIED
        return self.payment_status

    @property
    def counts_as_listed(self) -> bool:
        """В задолженности/оплаченном: есть в базе полиции или не найден
        только один раз. 'unverified' — отдельная категория (см.
        DebtBreakdown), 'gone' — нигде."""
        return self.presence in (PRESENCE_LISTED, PRESENCE_MISSING)


def _row_to_status(row) -> FineStatus:
    return FineStatus(
        id=row[0],
        car_number=row[1],
        protocol_no=row[2],
        amount=row[3],
        payment_status=row[4],
        presence=row[5],
        first_listed_at=_parse_ts(row[6]),
        last_listed_at=_parse_ts(row[7]),
        missing_since=_parse_ts(row[8]),
        gone_at=_parse_ts(row[9]),
        payment_source=row[10],
        payment_note=row[11],
        payment_updated_at=_parse_ts(row[12]),
        payment_updated_by=row[13],
    )


@dataclass(frozen=True)
class DebtBreakdown:
    """Суммы одной машины (или всего списка) по статусам — только штрафы,
    которые ещё в базе полиции (presence != 'gone')."""

    unpaid_amount: float = 0.0
    unpaid_count: int = 0
    paid_amount: float = 0.0
    paid_count: int = 0
    needs_review_amount: float = 0.0
    needs_review_count: int = 0
    # ⏳ 'unverified' — ни долг, ни оплачено: присутствие ещё не перепроверено.
    unverified_amount: float = 0.0
    unverified_count: int = 0

    @property
    def listed_count(self) -> int:
        return self.unpaid_count + self.paid_count + self.needs_review_count

    def add(self, other: DebtBreakdown) -> DebtBreakdown:
        return DebtBreakdown(
            unpaid_amount=self.unpaid_amount + other.unpaid_amount,
            unpaid_count=self.unpaid_count + other.unpaid_count,
            paid_amount=self.paid_amount + other.paid_amount,
            paid_count=self.paid_count + other.paid_count,
            needs_review_amount=self.needs_review_amount + other.needs_review_amount,
            needs_review_count=self.needs_review_count + other.needs_review_count,
            unverified_amount=self.unverified_amount + other.unverified_amount,
            unverified_count=self.unverified_count + other.unverified_count,
        )


def breakdown_of(statuses: Iterable[FineStatus]) -> DebtBreakdown:
    unpaid_amount = paid_amount = review_amount = unverified_amount = 0.0
    unpaid_count = paid_count = review_count = unverified_count = 0
    for status in statuses:
        amount = status.amount or 0.0
        if status.presence == PRESENCE_UNVERIFIED:
            unverified_amount += amount
            unverified_count += 1
            continue
        if not status.counts_as_listed:
            continue
        if status.payment_status == PAYMENT_PAID:
            paid_amount += amount
            paid_count += 1
        elif status.payment_status == PAYMENT_NEEDS_REVIEW:
            review_amount += amount
            review_count += 1
        else:
            unpaid_amount += amount
            unpaid_count += 1
    return DebtBreakdown(
        unpaid_amount=unpaid_amount, unpaid_count=unpaid_count,
        paid_amount=paid_amount, paid_count=paid_count,
        needs_review_amount=review_amount, needs_review_count=review_count,
        unverified_amount=unverified_amount, unverified_count=unverified_count,
    )


# ---- Текст со страницы оплаты ----


class PaymentPageVerdict(Enum):
    ALREADY_PAID = "already_paid"
    PAYMENT_FAILED = "payment_failed"
    TECHNICAL_ERROR = "technical_error"
    UNRECOGNIZED = "unrecognized"


@dataclass(frozen=True)
class PaymentPageMarkers:
    """Фрагменты текста, по которым распознаётся ответ платёжной страницы
    (settings.fine_monitor.payment_page_*_markers). already_paid по
    умолчанию пуст: отдельный достоверный ответ "уже оплачено" не
    наблюдался, и угадывать его формулировку нельзя — 🟢 по тексту
    страницы появится только после того, как реальный текст добавят в
    конфиг."""

    already_paid: tuple[str, ...] = ()
    payment_failed: tuple[str, ...] = (MIA_PAYMENT_FAILED_MESSAGE,)
    technical_error: tuple[str, ...] = ()


_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_page_text(value: str | None) -> str:
    return _WHITESPACE_RE.sub(" ", (value or "").casefold()).strip(" !. ")


def validate_payment_page_markers(markers: PaymentPageMarkers) -> None:
    """Защита от конфигурации, при которой общий отказ МВД
    ("ჯარიმის გადახდა ვერ მოხერხდა!") превратился бы в "уже оплачено" или
    "техническую ошибку": маркер короче _MIN_MARKER_LENGTH символов или
    являющийся частью этого сообщения не принимается. ValueError с
    понятным текстом — процесс бота не стартует с такой настройкой."""
    generic = _normalize_page_text(MIA_PAYMENT_FAILED_MESSAGE)
    for kind, values in (
        ("already_paid", markers.already_paid),
        ("payment_failed", markers.payment_failed),
        ("technical_error", markers.technical_error),
    ):
        for value in values:
            marker = _normalize_page_text(value)
            if len(marker) < _MIN_MARKER_LENGTH:
                raise ValueError(
                    f"payment_page_{kind}_markers: маркер {value!r} короче {_MIN_MARKER_LENGTH} символов"
                )
            if kind != "payment_failed" and marker in generic:
                raise ValueError(
                    f"payment_page_{kind}_markers: маркер {value!r} — часть общего отказа МВД, "
                    "он не может означать ни оплату, ни техническую ошибку"
                )


def classify_payment_page_text(text: str | None, markers: PaymentPageMarkers) -> PaymentPageVerdict:
    """Явное "уже оплачено" важнее всего; техническая ошибка важнее общего
    отказа (при ней задолженность не трогаем); общий отказ и любой
    нераспознанный текст — "оплата требует проверки". Слишком короткие
    маркеры игнорируются (см. validate_payment_page_markers)."""
    normalized = _normalize_page_text(text)
    if not normalized:
        return PaymentPageVerdict.UNRECOGNIZED
    for verdict, values in (
        (PaymentPageVerdict.ALREADY_PAID, markers.already_paid),
        (PaymentPageVerdict.TECHNICAL_ERROR, markers.technical_error),
        (PaymentPageVerdict.PAYMENT_FAILED, markers.payment_failed),
    ):
        for value in values:
            marker = _normalize_page_text(value)
            if len(marker) >= _MIN_MARKER_LENGTH and marker in normalized:
                return verdict
    return PaymentPageVerdict.UNRECOGNIZED


def payment_status_for_verdict(verdict: PaymentPageVerdict) -> str | None:
    """Какой статус оплаты ставит распознанный текст; None — статус не
    меняется (техническая ошибка: задолженность сохраняется)."""
    if verdict is PaymentPageVerdict.ALREADY_PAID:
        return PAYMENT_PAID
    if verdict is PaymentPageVerdict.TECHNICAL_ERROR:
        return None
    return PAYMENT_NEEDS_REVIEW


def clean_note(value: str | None) -> str:
    return _WHITESPACE_RE.sub(" ", value or "").strip()[:_NOTE_MAX_LENGTH]


# ---- Хранилище ----


class FineStatusRepository:
    def __init__(self, db_path: Path):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        # FULL — отметка "оплачен" и журнал не должны теряться при
        # аварийном завершении (тот же аргумент, что и у
        # DetectedFineRepository для notification_sent_at).
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        for statement in _SCHEMA:
            self._conn.execute(statement)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def get(self, status_id: int) -> FineStatus | None:
        row = self._conn.execute(
            f"SELECT {_SELECT_FIELDS} FROM georgia_fine_status WHERE id = ?", (status_id,),
        ).fetchone()
        return _row_to_status(row) if row else None

    def get_by_protocol(self, car_number: str, protocol_no: str) -> FineStatus | None:
        row = self._conn.execute(
            f"SELECT {_SELECT_FIELDS} FROM georgia_fine_status WHERE car_number = ? AND protocol_no = ?",
            (car_number, protocol_no),
        ).fetchone()
        return _row_to_status(row) if row else None

    def list_for_car(self, car_number: str, *, include_gone: bool) -> list[FineStatus]:
        query = f"SELECT {_SELECT_FIELDS} FROM georgia_fine_status WHERE car_number = ?"
        if not include_gone:
            query += f" AND presence != '{PRESENCE_GONE}'"
        rows = self._conn.execute(query + " ORDER BY first_listed_at, id", (car_number,)).fetchall()
        return [_row_to_status(row) for row in rows]

    def list_listed_for_cars(self, car_numbers: list[str]) -> list[FineStatus]:
        if not car_numbers:
            return []
        placeholders = ",".join("?" for _ in car_numbers)
        rows = self._conn.execute(
            f"SELECT {_SELECT_FIELDS} FROM georgia_fine_status "
            f"WHERE car_number IN ({placeholders}) AND presence != '{PRESENCE_GONE}' "
            "ORDER BY car_number, first_listed_at, id",
            list(car_numbers),
        ).fetchall()
        return [_row_to_status(row) for row in rows]

    def list_listed_car_numbers(self) -> list[str]:
        rows = self._conn.execute(
            f"SELECT DISTINCT car_number FROM georgia_fine_status WHERE presence != '{PRESENCE_GONE}' "
            "ORDER BY car_number",
        ).fetchall()
        return [row[0] for row in rows]

    def list_car_numbers_with_statuses(self, car_numbers: list[str]) -> set[str]:
        if not car_numbers:
            return set()
        placeholders = ",".join("?" for _ in car_numbers)
        rows = self._conn.execute(
            f"SELECT DISTINCT car_number FROM georgia_fine_status WHERE car_number IN ({placeholders})",
            list(car_numbers),
        ).fetchall()
        return {row[0] for row in rows}

    def latest_observation_by_car(self, car_numbers: list[str]) -> dict[str, datetime]:
        """Когда статусы машины последний раз сверялись с ответом police.ge
        (максимум из last_listed_at/missing_since/gone_at по её квитанциям)."""
        if not car_numbers:
            return {}
        placeholders = ",".join("?" for _ in car_numbers)
        rows = self._conn.execute(
            "SELECT car_number, MAX(MAX(last_listed_at, COALESCE(missing_since, last_listed_at), "
            "COALESCE(gone_at, last_listed_at))) FROM georgia_fine_status "
            f"WHERE car_number IN ({placeholders}) GROUP BY car_number",
            list(car_numbers),
        ).fetchall()
        return {row[0]: _parse_ts(row[1]) for row in rows if row[1]}

    def list_recently_gone_car_numbers(self, *, since: datetime) -> list[str]:
        rows = self._conn.execute(
            f"SELECT DISTINCT car_number FROM georgia_fine_status WHERE presence = '{PRESENCE_GONE}' "
            "AND gone_at >= ? ORDER BY car_number",
            (_fmt(since),),
        ).fetchall()
        return [row[0] for row in rows]

    def list_events(self, status_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT event, source, actor_user_id, note, created_at FROM georgia_fine_status_events "
            "WHERE status_id = ? ORDER BY id",
            (status_id,),
        ).fetchall()
        return [
            {"event": row[0], "source": row[1], "actor_user_id": row[2], "note": row[3], "created_at": row[4]}
            for row in rows
        ]

    def insert_listed(
        self, *, car_number: str, protocol_no: str, amount: float | None, at: datetime, source: str,
    ) -> FineStatus:
        """Новая квитанция в базе полиции — 🔴, если такой строки ещё нет
        (INSERT OR IGNORE: параллельный путь мог создать её секундой раньше)."""
        with self._conn:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO georgia_fine_status "
                "(car_number, protocol_no, amount, first_listed_at, last_listed_at) VALUES (?, ?, ?, ?, ?)",
                (car_number, protocol_no, amount, _fmt(at), _fmt(at)),
            )
            if cursor.rowcount:
                self._log(cursor.lastrowid, "listed", source, None, None, at)
        status = self.get_by_protocol(car_number, protocol_no)
        assert status is not None
        return status

    def touch_listed(self, status_id: int, *, amount: float | None, at: datetime) -> None:
        self._conn.execute(
            "UPDATE georgia_fine_status SET last_listed_at = ?, amount = COALESCE(?, amount) WHERE id = ?",
            (_fmt(at), amount, status_id),
        )
        self._conn.commit()

    def update_presence(
        self, status_id: int, *, presence: str, at: datetime, event: str, source: str,
        amount: float | None = None,
    ) -> None:
        """Переход presence + запись в журнал — одной транзакцией."""
        if presence == PRESENCE_LISTED:
            assignments = (
                "presence = ?, last_listed_at = ?, missing_since = NULL, gone_at = NULL, "
                "amount = COALESCE(?, amount)"
            )
            params: tuple = (presence, _fmt(at), amount, status_id)
        elif presence in (PRESENCE_MISSING, PRESENCE_UNVERIFIED):
            # 'unverified' остаётся 'unverified', но запоминает первое НОВОЕ
            # отсутствие (см. FineStatusTracker.record_full_check).
            assignments = "presence = ?, missing_since = ?"
            params = (presence, _fmt(at), status_id)
        else:
            assignments = "presence = ?, gone_at = ?"
            params = (presence, _fmt(at), status_id)
        with self._conn:
            self._conn.execute(f"UPDATE georgia_fine_status SET {assignments} WHERE id = ?", params)
            self._log(status_id, event, source, None, None, at)

    def update_payment(
        self, status_id: int, *, payment_status: str, source: str, note: str | None,
        actor_user_id: int | None, at: datetime, event: str,
    ) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE georgia_fine_status SET payment_status = ?, payment_source = ?, payment_note = ?, "
                "payment_updated_at = ?, payment_updated_by = ? WHERE id = ?",
                (payment_status, source, note, _fmt(at), actor_user_id, status_id),
            )
            self._log(status_id, event, source, actor_user_id, note, at)

    def add_event(
        self, status_id: int, *, event: str, source: str, actor_user_id: int | None, note: str | None, at: datetime,
    ) -> None:
        with self._conn:
            self._log(status_id, event, source, actor_user_id, note, at)

    def _log(self, status_id, event, source, actor_user_id, note, at) -> None:
        self._conn.execute(
            "INSERT INTO georgia_fine_status_events (status_id, event, source, actor_user_id, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (status_id, event, source, actor_user_id, note, _fmt(at)),
        )

    def record_source_updated_on(self, updated_on: date, *, observed_at: datetime) -> None:
        self._conn.execute(
            "INSERT INTO georgia_police_source_state (id, source_updated_on, observed_at) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET source_updated_on = excluded.source_updated_on, "
            "observed_at = excluded.observed_at",
            (updated_on.isoformat(), _fmt(observed_at)),
        )
        self._conn.commit()

    def get_source_updated_on(self) -> tuple[date, datetime] | None:
        row = self._conn.execute(
            "SELECT source_updated_on, observed_at FROM georgia_police_source_state WHERE id = 1",
        ).fetchone()
        if row is None or not row[0]:
            return None
        return date.fromisoformat(row[0]), _parse_ts(row[1])

    def seed_from_detected_fines(self, *, at: datetime) -> int:
        """Однократное (идемпотентное, INSERT OR IGNORE) заполнение статусов
        из detected_fines — до появления этой таблицы штрафы жили только
        там. Ключ — номер ЗАДАЧИ (тот же, что передают проверки и
        подписки), а не protocolAuto строки. Квитанция, которую police.ge
        показал на последней успешной проверке хотя бы одной задачи номера
        (или нашёл позже feed), — 'listed'; остальные — 'unverified' (⏳):
        их не было в старом ответе, полученном ещё без нынешней защиты от
        ложно пустых ответов, поэтому исчезновение не считается
        подтверждённым — ⚪ только после двух НОВЫХ полных проверок (см.
        record_full_check). Сравнение дат — в Python, а не в SQL:
        last_seen_at и last_successful_checked_at пишутся разными путями.
        Возвращает число созданных строк."""
        tasks = {
            row[0]: (row[1], _parse_ts(row[2]))
            for row in self._conn.execute(
                "SELECT id, car_number, last_successful_checked_at FROM fine_monitoring_tasks"
            )
        }
        seeds: dict[tuple[str, str], dict] = {}
        rows = self._conn.execute(
            "SELECT monitoring_task_id, external_fine_id, amount, first_detected_at, last_seen_at "
            "FROM detected_fines WHERE external_fine_id IS NOT NULL AND external_fine_id != ''"
        ).fetchall()
        for task_id, protocol_no, amount, first_detected_at, last_seen_at in rows:
            if task_id not in tasks:
                continue
            car_number, snapshot_at = tasks[task_id]
            if is_fine_output_suppressed(car_number):
                continue
            seen_at = _parse_ts(last_seen_at)
            first_at = _parse_ts(first_detected_at)
            listed = snapshot_at is None or seen_at >= snapshot_at - timedelta(seconds=1)
            seed = seeds.setdefault((car_number, protocol_no), {
                "amount": amount, "first_at": first_at, "seen_at": seen_at, "listed": False,
            })
            if seed["amount"] is None:
                seed["amount"] = amount
            seed["first_at"] = min(seed["first_at"], first_at)
            seed["seen_at"] = max(seed["seen_at"], seen_at)
            if listed:
                seed["listed"] = True
        created = 0
        with self._conn:
            for (car_number, protocol_no), seed in seeds.items():
                presence = PRESENCE_LISTED if seed["listed"] else PRESENCE_UNVERIFIED
                cursor = self._conn.execute(
                    "INSERT OR IGNORE INTO georgia_fine_status "
                    "(car_number, protocol_no, amount, presence, first_listed_at, last_listed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (car_number, protocol_no, seed["amount"], presence, _fmt(seed["first_at"]),
                     _fmt(seed["seen_at"])),
                )
                if cursor.rowcount:
                    created += 1
                    self._log(cursor.lastrowid, "seeded" if seed["listed"] else "seeded_unverified",
                              SOURCE_CHECK, None, None, at)
        return created


# ---- Логика ----


@dataclass(frozen=True)
class PaymentPageOutcome:
    verdict: PaymentPageVerdict
    status: FineStatus | None
    changed: bool


@dataclass
class FullCheckSummary:
    """Что изменилось после одной полной проверки номера — для журналов
    вечерней перепроверки (без номеров и квитанций)."""

    listed: int = 0
    new: int = 0
    missing: int = 0
    gone: int = 0
    reappeared: int = 0
    skipped_incomplete: bool = False
    gone_protocols: list[str] = field(default_factory=list)


class FineStatusTracker:
    def __init__(
        self,
        repository: FineStatusRepository,
        *,
        disappearance_min_gap: timedelta = timedelta(hours=12),
        markers: PaymentPageMarkers | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ):
        self._repository = repository
        self._min_gap = disappearance_min_gap
        self._markers = markers or PaymentPageMarkers()
        validate_payment_page_markers(self._markers)
        self._clock = clock

    @property
    def markers(self) -> PaymentPageMarkers:
        return self._markers

    def _now(self, at: datetime | None) -> datetime:
        return _as_naive_utc(at) if at is not None else self._clock()

    # -- присутствие в базе полиции --

    def record_full_check(
        self, car_number: str, records: list[ParsedFineRecord], *, complete: bool,
        empty_confirmed: bool = False, at: datetime | None = None, source: str = SOURCE_CHECK,
    ) -> FullCheckSummary:
        """Вызывается после КАЖДОЙ успешной полной проверки номера
        (searchByAuto: вечерняя перепроверка, "Проверить сейчас", добавление
        машины, обновление у менеджера) — никогда после ошибки или показа
        из кэша. Сверка — по номеру квитанции (protocolNo), не по сумме.

        complete=False (источник сообщил другое число записей) — найденные
        квитанции отмечаются, но отсутствующие не считаются исчезнувшими.
        Пустой ответ считается полным, только если empty_confirmed (его
        подтвердил повторный запрос, см. FineCheckService._fetch_trusted)."""
        now = self._now(at)
        summary = FullCheckSummary()
        listed: dict[str, ParsedFineRecord] = {}
        for record in records:
            if record.external_fine_id:
                listed[record.external_fine_id] = record
        if not listed and not empty_confirmed:
            complete = False
        for protocol_no, record in listed.items():
            outcome = self._note_listed(car_number, protocol_no, record.amount, at=now, source=source)
            summary.listed += 1
            if outcome == "new":
                summary.new += 1
            elif outcome == "reappeared":
                summary.reappeared += 1
        if not complete:
            summary.skipped_incomplete = True
            return summary

        for status in self._repository.list_for_car(car_number, include_gone=False):
            if status.protocol_no in listed:
                continue
            if status.presence == PRESENCE_LISTED:
                self._repository.update_presence(
                    status.id, presence=PRESENCE_MISSING, at=now, event="missing", source=source,
                )
                summary.missing += 1
            elif status.presence == PRESENCE_UNVERIFIED and status.missing_since is None:
                # Первое НОВОЕ отсутствие квитанции из истории — запоминаем,
                # но ни в долг, ни в ⚪ она пока не переходит.
                self._repository.update_presence(
                    status.id, presence=PRESENCE_UNVERIFIED, at=now, event="unverified_missing", source=source,
                )
                summary.missing += 1
            elif status.presence in (PRESENCE_MISSING, PRESENCE_UNVERIFIED):
                missing_since = status.missing_since or now
                if now - missing_since >= self._min_gap:
                    self._repository.update_presence(
                        status.id, presence=PRESENCE_GONE, at=now, event="gone", source=source,
                    )
                    summary.gone += 1
                    summary.gone_protocols.append(status.protocol_no)
        return summary

    def record_listed(
        self, car_number: str, record: ParsedFineRecord, *, at: datetime | None = None, source: str = SOURCE_FEED,
    ) -> None:
        """Одна квитанция из публичного feed'а: она точно в базе полиции, но
        это не полная проверка номера — остальные квитанции не трогаем."""
        if record.external_fine_id:
            self._note_listed(car_number, record.external_fine_id, record.amount, at=self._now(at), source=source)

    def _note_listed(self, car_number: str, protocol_no: str, amount: float | None, *, at: datetime, source: str) -> str:
        existing = self._repository.get_by_protocol(car_number, protocol_no)
        if existing is None:
            self._repository.insert_listed(
                car_number=car_number, protocol_no=protocol_no, amount=amount, at=at, source=source,
            )
            return "new"
        if existing.presence == PRESENCE_GONE:
            self._repository.update_presence(
                existing.id, presence=PRESENCE_LISTED, at=at, event="reappeared", source=source, amount=amount,
            )
            return "reappeared"
        if existing.presence in (PRESENCE_MISSING, PRESENCE_UNVERIFIED):
            self._repository.update_presence(
                existing.id, presence=PRESENCE_LISTED, at=at, event="found_again", source=source, amount=amount,
            )
            return "found_again"
        self._repository.touch_listed(existing.id, amount=amount, at=at)
        return "listed"

    def seed_from_detected_fines(self, *, at: datetime | None = None) -> int:
        return self._repository.seed_from_detected_fines(at=self._now(at))

    def record_source_updated_on(self, updated_on: date, *, at: datetime | None = None) -> None:
        self._repository.record_source_updated_on(updated_on, observed_at=self._now(at))

    def source_updated_on(self) -> tuple[date, datetime] | None:
        return self._repository.get_source_updated_on()

    # -- статус оплаты (менеджер) --

    def mark_paid(self, status_id: int, *, actor_user_id: int, note: str, source: str = SOURCE_RECEIPT,
                  at: datetime | None = None) -> FineStatus | None:
        """🟢 — только с основанием (номер квитанции/операции или текст
        официального сервиса): пустое основание не принимается (защита от
        ошибочной отметки, см. задачу)."""
        cleaned = clean_note(note)
        if not cleaned:
            raise ValueError("Для отметки «Оплачен» нужно основание (номер квитанции или операции)")
        return self._set_payment(status_id, PAYMENT_PAID, source=source, note=cleaned,
                                 actor_user_id=actor_user_id, at=at, event="payment_paid")

    def mark_needs_review(self, status_id: int, *, actor_user_id: int | None, note: str | None,
                          source: str = SOURCE_MANAGER, at: datetime | None = None) -> FineStatus | None:
        return self._set_payment(status_id, PAYMENT_NEEDS_REVIEW, source=source, note=clean_note(note) or None,
                                 actor_user_id=actor_user_id, at=at, event="payment_needs_review")

    def mark_unpaid(self, status_id: int, *, actor_user_id: int, note: str | None = None,
                    at: datetime | None = None) -> FineStatus | None:
        return self._set_payment(status_id, PAYMENT_UNPAID, source=SOURCE_MANAGER, note=clean_note(note) or None,
                                 actor_user_id=actor_user_id, at=at, event="payment_cleared")

    def apply_payment_page_text(self, status_id: int, text: str, *, actor_user_id: int,
                                at: datetime | None = None) -> PaymentPageOutcome:
        """Текст, который менеджер увидел на странице оплаты: явное "уже
        оплачено" (маркер из конфига) — 🟢; техническая ошибка — статус и
        задолженность не меняются, только запись в журнале; общий отказ
        ("ჯარიმის გადახდა ვერ მოხერხდა!") и нераспознанный текст — 🟡."""
        verdict = classify_payment_page_text(text, self._markers)
        note = clean_note(text)
        target = payment_status_for_verdict(verdict)
        if target is None:
            status = self._repository.get(status_id)
            if status is not None:
                self._repository.add_event(
                    status_id, event="payment_page_technical_error", source=SOURCE_PAYMENT_PAGE,
                    actor_user_id=actor_user_id, note=note or None, at=self._now(at),
                )
            return PaymentPageOutcome(verdict=verdict, status=status, changed=False)
        event = "payment_paid" if target == PAYMENT_PAID else "payment_needs_review"
        status = self._set_payment(status_id, target, source=SOURCE_PAYMENT_PAGE, note=note or None,
                                   actor_user_id=actor_user_id, at=at, event=event)
        return PaymentPageOutcome(verdict=verdict, status=status, changed=status is not None)

    def _set_payment(self, status_id: int, payment_status: str, *, source: str, note: str | None,
                     actor_user_id: int | None, at: datetime | None, event: str) -> FineStatus | None:
        status = self._repository.get(status_id)
        if status is None:
            return None
        self._repository.update_payment(
            status_id, payment_status=payment_status, source=source, note=note,
            actor_user_id=actor_user_id, at=self._now(at), event=event,
        )
        return self._repository.get(status_id)

    # -- чтение --

    def get(self, status_id: int) -> FineStatus | None:
        return self._repository.get(status_id)

    def statuses_for_car(self, car_number: str, *, include_gone: bool = True) -> list[FineStatus]:
        return self._repository.list_for_car(car_number, include_gone=include_gone)

    def statuses_by_protocol(self, car_number: str) -> dict[str, FineStatus]:
        return {status.protocol_no: status for status in self._repository.list_for_car(car_number, include_gone=True)}

    def gone_protocols(self, car_number: str) -> set[str]:
        """Квитанции, которых police.ge больше не показывает (⚪) — о них НЕ
        нужно отправлять "новый штраф" (см. ClientDeliveryService). Не
        найденные один раз ('missing') сюда не входят: это ещё не
        доказательство, и настоящий штраф не должен потеряться."""
        return {
            status.protocol_no
            for status in self._repository.list_for_car(car_number, include_gone=True)
            if status.presence == PRESENCE_GONE
        }

    def has_listed_receipts(self, car_number: str) -> bool:
        """Есть ли у номера квитанции, ещё не признанные исчезнувшими — это
        "свидетельство долга" для false-zero guard (см.
        FineCheckService._fetch_trusted): пустой ответ по такому номеру
        всегда перепроверяется."""
        return bool(self._repository.list_for_car(car_number, include_gone=False))

    def listed_car_numbers(self) -> list[str]:
        return [car for car in self._repository.list_listed_car_numbers() if not is_fine_output_suppressed(car)]

    def recently_gone_car_numbers(self, *, within: timedelta = GONE_FOLLOW_UP, at: datetime | None = None) -> list[str]:
        since = self._now(at) - within
        return [
            car for car in self._repository.list_recently_gone_car_numbers(since=since)
            if not is_fine_output_suppressed(car)
        ]

    def car_numbers_with_statuses(self, car_numbers: list[str]) -> set[str]:
        return self._repository.list_car_numbers_with_statuses(car_numbers)

    def latest_observation_by_car(self, car_numbers: list[str]) -> dict[str, datetime]:
        return self._repository.latest_observation_by_car(car_numbers)

    def breakdown_for_cars(self, car_numbers: list[str]) -> dict[str, DebtBreakdown]:
        by_car: dict[str, list[FineStatus]] = {}
        for status in self._repository.list_listed_for_cars(car_numbers):
            by_car.setdefault(status.car_number, []).append(status)
        return {car: breakdown_of(statuses) for car, statuses in by_car.items()}

    def events(self, status_id: int) -> list[dict]:
        return self._repository.list_events(status_id)


def summary_to_log_fields(summary: FullCheckSummary) -> str:
    """Счётчики без номеров и квитанций — для журналов (см. правило проекта:
    не писать госномера в логи)."""
    return json.dumps({
        "listed": summary.listed, "new": summary.new, "missing": summary.missing,
        "gone": summary.gone, "reappeared": summary.reappeared, "incomplete": summary.skipped_incomplete,
    })
