"""Сумма к оплате штрафа Грузии с начислениями (см. docs/georgia-payable-amount.md).

police.ge отдаёт только исходную сумму квитанции (protocolAmount) — она не
меняется. Сумму с начислениями знает МВД: Georgian Card спрашивает её при
открытии платёжной страницы (check -> payment-avail-response). Поэтому
каждое получение суммы — это техническая платёжная сессия Georgian Card
(без карты, без авторизации и без списания), и число таких сессий жёстко
ограничено:

- автоматически — только в контрольных периодах: первый начинается на
  следующий день после окончательного lastDate (конец первоначального срока
  оплаты, 30 дней с вручения; ещё 30 дней сверху не прибавляются), следующие
  — через каждые 30 дней, всего CONTROL_PERIOD_COUNT периодов (30/60/.../180
  дней). Окончательный lastDate — только с activeDate (квитанция считается
  врученной): до этого police.ge отдаёт предварительный срок и потом
  пересчитывает его (см. term_end_of);
- в каждом периоде — не больше одной проверки в сутки и не больше
  MAX_AUTO_CHECKS_PER_PERIOD проверок, и только в первые CONTROL_WINDOW_DAYS
  календарных дней периода;
- период закрывается сразу, как только найдено увеличение (в том числе
  ручным «Уточнить сумму» внутри окна) или МВД отказало в оплате;
- между окнами и после последнего периода автоматических проверок нет.

Коэффициенты (×2, ×3, ...) нигде не считаются: бот только сравнивает
фактические суммы Georgian Card. Первое полученное значение сохраняется
молча (без уведомления), даже если оно больше исходной суммы; уведомление
создаёт только подтверждённое увеличение относительно ранее сохранённой
суммы Georgian Card (georgia_fine_amount_events, одно событие на новую
сумму квитанции). Ошибки, отказы, пустые и нулевые ответы суммой не
считаются и оплатой тоже — статус оплаты (reader/fines/payment_status.py)
этот модуль не трогает.

Только SQLite той же БД (settings.app.users_db_file) — сети и Telegram
здесь нет. Лимиты (одна сессия на квитанцию одновременно, одна
автоматическая проверка в сутки, MAX_AUTO_CHECKS_PER_PERIOD на период,
дневной лимит сессий) проверяются в том же INSERT, которым начинается
проверка (begin_check), — их нельзя обойти гонкой двух вызовов.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

CONTROL_PERIOD_COUNT = 6
CONTROL_PERIOD_DAYS = 30
CONTROL_WINDOW_DAYS = 10
MAX_AUTO_CHECKS_PER_PERIOD = 10

TRIGGER_AUTO = "auto"
TRIGGER_MANUAL = "manual"
TRIGGER_PILOT = "pilot"
# Разовая первичная актуализация всех неоплаченных квитанций (решение владельца
# 2026-11-21): свой лимит у команды, в общий дневной лимит не входит — иначе
# она съела бы ручные уточнения клиентов на весь день.
TRIGGER_INITIAL = "initial"

# Исход одной сессии (georgia_fine_amount_checks.outcome).
OUTCOME_IN_PROGRESS = "in_progress"
OUTCOME_OK = "ok"
# Шлюз вернул на police.ge/protocol/fail.php: МВД не разрешило оплату
# (оплачено, передано на исполнение, ошибка МВД — причина не видна).
OUTCOME_REFUSED = "refused"
# Сумма есть, но ответ не подтверждает, что он про эту квитанцию.
OUTCOME_RECEIPT_UNCONFIRMED = "receipt_unconfirmed"
OUTCOME_CURRENCY_MISMATCH = "currency_mismatch"
# Ответ не распознан (другая страница, нет суммы, неправдоподобная сумма).
OUTCOME_INVALID_RESPONSE = "invalid_response"
# HTTP 403/429 — шлюз ограничивает запросы: сессии приостанавливаются.
OUTCOME_BLOCKED = "blocked"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_ERROR = "error"
# Строка in_progress, которую не закрыли дольше CHECK_LEASE (процесс
# перезапустился посреди сессии) — сессия могла быть создана, поэтому в
# лимитах она по-прежнему считается.
OUTCOME_ABANDONED = "abandoned"

# Сколько строка in_progress держит квитанцию (защита от параллельных сессий
# одной квитанции); дольше любой сессии с её тайм-аутом (тайм-аут запроса —
# не больше MAX_REQUEST_TIMEOUT_SECONDS, вся сессия — не больше трёх таких).
CHECK_LEASE = timedelta(minutes=5)
MAX_REQUEST_TIMEOUT_SECONDS = 60.0

_AMOUNT_EPSILON = 0.005

_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

_SCHEMA = (
    # Последняя подтверждённая сумма Georgian Card по квитанции. observed_at
    # — начало сессии, которая её вернула: запоздавший ответ более старой
    # сессии не перезаписывает более свежее значение (см. store_amount).
    """
    CREATE TABLE IF NOT EXISTS georgia_fine_payable_amounts (
        car_number         TEXT NOT NULL,
        protocol_no        TEXT NOT NULL,
        amount             REAL NOT NULL,
        currency           TEXT NOT NULL,
        observed_at        TIMESTAMP NOT NULL,
        source             TEXT NOT NULL,
        check_id           INTEGER,
        first_amount       REAL NOT NULL,
        first_observed_at  TIMESTAMP NOT NULL,
        updated_at         TIMESTAMP NOT NULL,
        PRIMARY KEY (car_number, protocol_no)
    )
    """,
    # Каждая сессия Georgian Card (и каждая попытка её начать, прошедшая
    # лимиты): аудит, лимиты и учёт контрольных периодов.
    """
    CREATE TABLE IF NOT EXISTS georgia_fine_amount_checks (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        car_number       TEXT NOT NULL,
        protocol_no      TEXT NOT NULL,
        trigger          TEXT NOT NULL,
        period_index     INTEGER,
        requested_by     INTEGER,
        check_date       TEXT NOT NULL,
        started_at       TIMESTAMP NOT NULL,
        finished_at      TIMESTAMP,
        outcome          TEXT NOT NULL,
        amount           REAL,
        previous_amount  REAL,
        increased        INTEGER NOT NULL DEFAULT 0,
        detail           TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_georgia_fine_amount_checks_receipt "
    "ON georgia_fine_amount_checks (car_number, protocol_no)",
    "CREATE INDEX IF NOT EXISTS idx_georgia_fine_amount_checks_date ON georgia_fine_amount_checks (check_date)",
    # Подтверждённые увеличения суммы — кандидаты на уведомление. UNIQUE по
    # новой сумме: повторное получение той же суммы (в том числе после
    # снижения) события не создаёт.
    """
    CREATE TABLE IF NOT EXISTS georgia_fine_amount_events (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        car_number   TEXT NOT NULL,
        protocol_no  TEXT NOT NULL,
        old_amount   REAL NOT NULL,
        new_amount   REAL NOT NULL,
        observed_at  TIMESTAMP NOT NULL,
        check_id     INTEGER,
        created_at   TIMESTAMP NOT NULL,
        UNIQUE (car_number, protocol_no, new_amount)
    )
    """,
    # Доставка события получателям подписок (та же схема, что
    # client_fine_deliveries: попытки, доставка, явное исключение).
    """
    CREATE TABLE IF NOT EXISTS georgia_fine_amount_deliveries (
        event_id           INTEGER NOT NULL REFERENCES georgia_fine_amount_events(id),
        subscription_id    INTEGER NOT NULL,
        recipient_role     TEXT NOT NULL,
        recipient_chat_id  INTEGER NOT NULL,
        attempt_count      INTEGER NOT NULL DEFAULT 0,
        last_attempt_at    TIMESTAMP,
        delivered_at       TIMESTAMP,
        excluded_at        TIMESTAMP,
        exclusion_reason   TEXT,
        PRIMARY KEY (event_id, subscription_id, recipient_role)
    )
    """,
    # Приостановка всех сессий (после HTTP 403/429) — переживает перезапуск.
    """
    CREATE TABLE IF NOT EXISTS georgia_payable_amount_control (
        id               INTEGER PRIMARY KEY CHECK (id = 1),
        suspended_until  TIMESTAMP,
        suspend_reason   TEXT,
        updated_at       TIMESTAMP
    )
    """,
    "INSERT OR IGNORE INTO georgia_payable_amount_control (id) VALUES (1)",
    # Уведомления об увеличении суммы: с какого момента включены (NULL —
    # выключены). События, созданные до этого момента, не рассылаются никогда
    # (см. AmountIncreaseDeliveryService) — ни при первом включении, ни при
    # повторном.
    """
    CREATE TABLE IF NOT EXISTS georgia_amount_notification_state (
        id             INTEGER PRIMARY KEY CHECK (id = 1),
        enabled_since  TIMESTAMP,
        updated_at     TIMESTAMP
    )
    """,
    "INSERT OR IGNORE INTO georgia_amount_notification_state (id) VALUES (1)",
    # Дневной автоматический проход (один на дату).
    """
    CREATE TABLE IF NOT EXISTS georgia_payable_amount_runs (
        run_date     TEXT PRIMARY KEY,
        started_at   TIMESTAMP NOT NULL,
        finished_at  TIMESTAMP,
        status       TEXT NOT NULL,
        stop_reason  TEXT,
        sessions     INTEGER NOT NULL DEFAULT 0,
        errors       INTEGER NOT NULL DEFAULT 0
    )
    """,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


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


def round_amount(value: float) -> float:
    return round(float(value), 2)


def amounts_equal(a: float, b: float) -> bool:
    return abs(a - b) < _AMOUNT_EPSILON


def is_increase(new: float, old: float) -> bool:
    return new - old >= _AMOUNT_EPSILON


def display_payable(amount: float | None, base_amount: float | None) -> float | None:
    """Сумма «к оплате» для показа и для сравнения «было/стало»: не меньше
    исходной суммы штрафа (решение владельца 2026-11-21 — Georgian Card
    иногда отвечает меньшей суммой). Ответ шлюза хранится как есть
    (georgia_fine_payable_amounts.amount, georgia_fine_amount_checks)."""
    if amount is None or base_amount is None:
        return amount
    return max(amount, base_amount)


# ---- Контрольные периоды ----


@dataclass(frozen=True)
class ControlPeriod:
    index: int  # 1..CONTROL_PERIOD_COUNT
    start: date
    window_end: date  # включительно: start + CONTROL_WINDOW_DAYS - 1

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.window_end


def control_periods(term_end: date) -> list[ControlPeriod]:
    """term_end — последний день первоначального срока оплаты (lastDate).
    Первый период — со следующего дня (30 дней с вручения уже прошли: lastDate
    сам обозначает их конец), каждый следующий — через CONTROL_PERIOD_DAYS."""
    first_start = term_end + timedelta(days=1)
    periods = []
    for index in range(1, CONTROL_PERIOD_COUNT + 1):
        start = first_start + timedelta(days=CONTROL_PERIOD_DAYS * (index - 1))
        periods.append(ControlPeriod(index=index, start=start, window_end=start + timedelta(days=CONTROL_WINDOW_DAYS - 1)))
    return periods


def open_period(term_end: date, day: date) -> ControlPeriod | None:
    """Период, в окне которого лежит day; None — между окнами, до первого
    периода или после последнего (автоматических проверок нет)."""
    for period in control_periods(term_end):
        if period.contains(day):
            return period
    return None


def latest_started_period(term_end: date, day: date) -> ControlPeriod | None:
    """Последний период, начавшийся не позже day (None — первоначальный срок
    ещё идёт). Сумма, полученная раньше его начала, устарела: в этом периоде
    она могла вырасти."""
    latest = None
    for period in control_periods(term_end):
        if period.start <= day:
            latest = period
    return latest


def _parse_source_date(value) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    try:
        return datetime.strptime(text, "%d.%m.%Y").date()
    except ValueError:
        return None


def term_end_of(last_date: date | None, active_date: date | None) -> date | None:
    """Последний день первоначального срока оплаты — lastDate police.ge, но
    только окончательный: пока квитанция не считается врученной (activeDate
    пуст), police.ge отдаёт предварительный lastDate и после вручения
    пересчитывает его (наблюдение 2026-11-21: 25.10 до вручения, 26.10 после,
    сайт показывает "Payment term" ровно как lastDate). Предварительный срок
    может оказаться раньше настоящего — None: автоматических проверок нет,
    пока не придёт окончательный (обычная проверка номера), ручные — можно.
    Ничего не прибавляется и не вычисляется."""
    if last_date is None or active_date is None:
        return None
    return last_date


# ---- Модели ----


@dataclass(frozen=True)
class PayableAmount:
    car_number: str
    protocol_no: str
    amount: float
    currency: str
    observed_at: datetime
    source: str
    first_amount: float
    first_observed_at: datetime


@dataclass(frozen=True)
class AmountCheck:
    id: int
    car_number: str
    protocol_no: str
    trigger: str
    period_index: int | None
    requested_by: int | None
    check_date: date
    started_at: datetime
    finished_at: datetime | None
    outcome: str
    amount: float | None
    previous_amount: float | None
    increased: bool
    detail: str | None


@dataclass(frozen=True)
class StoreResult:
    stored: bool  # False — более свежее значение уже сохранено (запоздавший ответ)
    first: bool  # первое значение Georgian Card по квитанции
    previous_amount: float | None


@dataclass(frozen=True)
class AmountEvent:
    id: int
    car_number: str
    protocol_no: str
    old_amount: float
    new_amount: float
    observed_at: datetime
    created_at: datetime


@dataclass(frozen=True)
class AmountDelivery:
    event_id: int
    subscription_id: int
    recipient_role: str
    recipient_chat_id: int
    attempt_count: int
    last_attempt_at: datetime | None
    delivered_at: datetime | None
    excluded_at: datetime | None
    exclusion_reason: str | None


@dataclass(frozen=True)
class AutoRun:
    run_date: date
    started_at: datetime
    finished_at: datetime | None
    status: str
    stop_reason: str | None
    sessions: int
    errors: int


# Почему begin_check не начал проверку.
REFUSAL_BUSY = "busy"  # другая сессия этой квитанции ещё идёт
REFUSAL_DAILY_LIMIT = "daily_limit"  # дневной лимит сессий исчерпан
REFUSAL_ALREADY_TODAY = "already_today"  # автоматическая проверка сегодня уже была
REFUSAL_PERIOD_LIMIT = "period_limit"  # MAX_AUTO_CHECKS_PER_PERIOD исчерпан
REFUSAL_USER_LIMIT = "user_limit"  # дневной лимит ручных уточнений пользователя
REFUSAL_AUTO_DAILY_LIMIT = "auto_daily_limit"  # дневной лимит автоматических проверок
REFUSAL_ALREADY_INITIAL = "already_initial"  # первичная актуализация квитанции уже была


_AMOUNT_FIELDS = (
    "car_number, protocol_no, amount, currency, observed_at, source, first_amount, first_observed_at"
)
_CHECK_FIELDS = (
    "id, car_number, protocol_no, trigger, period_index, requested_by, check_date, started_at, finished_at, "
    "outcome, amount, previous_amount, increased, detail"
)
_DELIVERY_FIELDS = (
    "event_id, subscription_id, recipient_role, recipient_chat_id, attempt_count, last_attempt_at, "
    "delivered_at, excluded_at, exclusion_reason"
)


def _row_to_amount(row) -> PayableAmount:
    return PayableAmount(
        car_number=row[0], protocol_no=row[1], amount=row[2], currency=row[3],
        observed_at=_parse_ts(row[4]), source=row[5], first_amount=row[6], first_observed_at=_parse_ts(row[7]),
    )


def _row_to_check(row) -> AmountCheck:
    return AmountCheck(
        id=row[0], car_number=row[1], protocol_no=row[2], trigger=row[3], period_index=row[4],
        requested_by=row[5], check_date=date.fromisoformat(row[6]), started_at=_parse_ts(row[7]),
        finished_at=_parse_ts(row[8]), outcome=row[9], amount=row[10], previous_amount=row[11],
        increased=bool(row[12]), detail=row[13],
    )


def _row_to_delivery(row) -> AmountDelivery:
    return AmountDelivery(
        event_id=row[0], subscription_id=row[1], recipient_role=row[2], recipient_chat_id=row[3],
        attempt_count=row[4], last_attempt_at=_parse_ts(row[5]), delivered_at=_parse_ts(row[6]),
        excluded_at=_parse_ts(row[7]), exclusion_reason=row[8],
    )


def _row_to_event(row) -> AmountEvent:
    return AmountEvent(
        id=row[0], car_number=row[1], protocol_no=row[2], old_amount=row[3], new_amount=row[4],
        observed_at=_parse_ts(row[5]), created_at=_parse_ts(row[6]),
    )


class PayableAmountRepository:
    def __init__(self, db_path: Path):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # timeout — begin_check держит запись на время одного INSERT; второй
        # процесс подождёт, а не упадёт с "database is locked".
        self._conn = sqlite3.connect(self._path, timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        for statement in _SCHEMA:
            self._conn.execute(statement)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---- Суммы ----

    def get_amount(self, car_number: str, protocol_no: str) -> PayableAmount | None:
        row = self._conn.execute(
            f"SELECT {_AMOUNT_FIELDS} FROM georgia_fine_payable_amounts WHERE car_number = ? AND protocol_no = ?",
            (car_number, protocol_no),
        ).fetchone()
        return _row_to_amount(row) if row else None

    def amounts_for_cars(self, car_numbers: list[str]) -> dict[tuple[str, str], PayableAmount]:
        result: dict[tuple[str, str], PayableAmount] = {}
        for start in range(0, len(car_numbers), 500):
            chunk = car_numbers[start:start + 500]
            placeholders = ",".join("?" * len(chunk))
            for row in self._conn.execute(
                f"SELECT {_AMOUNT_FIELDS} FROM georgia_fine_payable_amounts WHERE car_number IN ({placeholders})",
                chunk,
            ):
                amount = _row_to_amount(row)
                result[(amount.car_number, amount.protocol_no)] = amount
        return result

    def store_amount(
        self, *, car_number: str, protocol_no: str, amount: float, currency: str, observed_at: datetime,
        source: str, check_id: int | None,
    ) -> StoreResult:
        """Сохраняет сумму, если более свежей (по observed_at — началу сессии)
        ещё нет. Первое значение запоминается отдельно (first_amount)."""
        observed = _fmt(observed_at)
        now = _fmt(_utc_now())
        existing = self.get_amount(car_number, protocol_no)
        if existing is None:
            self._conn.execute(
                "INSERT INTO georgia_fine_payable_amounts (car_number, protocol_no, amount, currency, observed_at, "
                "source, check_id, first_amount, first_observed_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (car_number, protocol_no, amount, currency, observed, source, check_id, amount, observed, now),
            )
            self._conn.commit()
            return StoreResult(stored=True, first=True, previous_amount=None)
        if _as_naive_utc(observed_at) < existing.observed_at:
            return StoreResult(stored=False, first=False, previous_amount=existing.amount)
        self._conn.execute(
            "UPDATE georgia_fine_payable_amounts SET amount = ?, currency = ?, observed_at = ?, source = ?, "
            "check_id = ?, updated_at = ? WHERE car_number = ? AND protocol_no = ?",
            (amount, currency, observed, source, check_id, now, car_number, protocol_no),
        )
        self._conn.commit()
        return StoreResult(stored=True, first=False, previous_amount=existing.amount)

    def count_amounts(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM georgia_fine_payable_amounts").fetchone()[0]

    # ---- Проверки (сессии) ----

    def begin_check(
        self, *, car_number: str, protocol_no: str, trigger: str, period_index: int | None,
        requested_by: int | None, check_date: date, started_at: datetime, max_sessions_per_day: int,
        max_manual_per_user: int | None = None, max_auto_per_day: int | None = None,
    ) -> int | str:
        """Атомарно начинает проверку (строка in_progress) и возвращает её id —
        или причину отказа (REFUSAL_*). Все лимиты — в одном INSERT ... SELECT
        ... WHERE: две одновременные попытки не обойдут их гонкой.

        Лимиты: одна сессия квитанции одновременно (in_progress моложе
        CHECK_LEASE); не больше max_sessions_per_day сессий в сутки (все
        триггеры); для TRIGGER_AUTO — одна автоматическая проверка квитанции
        в сутки и не больше MAX_AUTO_CHECKS_PER_PERIOD в периоде; для
        TRIGGER_MANUAL — не больше max_manual_per_user ручных сессий
        пользователя в сутки; для TRIGGER_INITIAL — одна такая сессия на
        квитанцию за всё время; для TRIGGER_AUTO — ещё и не больше
        max_auto_per_day автоматических сессий в сутки (внутри общего
        max_sessions_per_day)."""
        self.abandon_stale_checks(now=started_at)
        lease_cutoff = _fmt(_as_naive_utc(started_at) - CHECK_LEASE)
        params = {
            "car_number": car_number, "protocol_no": protocol_no, "trigger": trigger,
            "period_index": period_index, "requested_by": requested_by, "check_date": check_date.isoformat(),
            "started_at": _fmt(started_at), "in_progress": OUTCOME_IN_PROGRESS, "lease_cutoff": lease_cutoff,
            "max_per_day": max_sessions_per_day, "auto": TRIGGER_AUTO, "max_per_period": MAX_AUTO_CHECKS_PER_PERIOD,
            "manual": TRIGGER_MANUAL, "max_user": max_manual_per_user, "max_auto": max_auto_per_day,
            "initial": TRIGGER_INITIAL,
        }
        cursor = self._conn.execute(
            """
            INSERT INTO georgia_fine_amount_checks (
                car_number, protocol_no, trigger, period_index, requested_by, check_date, started_at, outcome
            )
            SELECT :car_number, :protocol_no, :trigger, :period_index, :requested_by, :check_date, :started_at,
                   :in_progress
            WHERE NOT EXISTS (
                    SELECT 1 FROM georgia_fine_amount_checks
                    WHERE car_number = :car_number AND protocol_no = :protocol_no
                      AND outcome = :in_progress AND started_at > :lease_cutoff
                )
              AND (
                    SELECT COUNT(*) FROM georgia_fine_amount_checks
                    WHERE check_date = :check_date AND trigger != :initial
                ) < :max_per_day
              AND (
                    :trigger != :initial OR NOT EXISTS (
                        SELECT 1 FROM georgia_fine_amount_checks
                        WHERE car_number = :car_number AND protocol_no = :protocol_no AND trigger = :initial
                    )
                )
              AND (
                    :trigger != :auto OR (
                        NOT EXISTS (
                            SELECT 1 FROM georgia_fine_amount_checks
                            WHERE car_number = :car_number AND protocol_no = :protocol_no
                              AND trigger = :auto AND check_date = :check_date
                        )
                        AND (
                            SELECT COUNT(*) FROM georgia_fine_amount_checks
                            WHERE car_number = :car_number AND protocol_no = :protocol_no
                              AND trigger = :auto AND period_index = :period_index
                        ) < :max_per_period
                    )
                )
              AND (
                    :trigger != :auto OR :max_auto IS NULL OR (
                        SELECT COUNT(*) FROM georgia_fine_amount_checks
                        WHERE check_date = :check_date AND trigger = :auto
                    ) < :max_auto
                )
              AND (
                    :trigger != :manual OR :max_user IS NULL OR (
                        SELECT COUNT(*) FROM georgia_fine_amount_checks
                        WHERE requested_by = :requested_by AND check_date = :check_date AND trigger = :manual
                    ) < :max_user
                )
            """,
            params,
        )
        self._conn.commit()
        if cursor.rowcount:
            return cursor.lastrowid
        return self._refusal_reason(params)

    def _refusal_reason(self, params: dict) -> str:
        busy = self._conn.execute(
            "SELECT 1 FROM georgia_fine_amount_checks WHERE car_number = :car_number AND protocol_no = :protocol_no "
            "AND outcome = :in_progress AND started_at > :lease_cutoff",
            params,
        ).fetchone()
        if busy:
            return REFUSAL_BUSY
        if params["trigger"] == TRIGGER_INITIAL and self._conn.execute(
            "SELECT 1 FROM georgia_fine_amount_checks WHERE car_number = :car_number AND protocol_no = :protocol_no "
            "AND trigger = :initial",
            params,
        ).fetchone():
            return REFUSAL_ALREADY_INITIAL
        today = self._conn.execute(
            "SELECT COUNT(*) FROM georgia_fine_amount_checks WHERE check_date = :check_date AND trigger != :initial",
            params,
        ).fetchone()[0]
        if today >= params["max_per_day"]:
            return REFUSAL_DAILY_LIMIT
        if params["trigger"] == TRIGGER_MANUAL and params["max_user"] is not None:
            return REFUSAL_USER_LIMIT
        if params["trigger"] == TRIGGER_AUTO and params["max_auto"] is not None:
            auto_today = self._conn.execute(
                "SELECT COUNT(*) FROM georgia_fine_amount_checks WHERE check_date = :check_date AND trigger = :auto",
                params,
            ).fetchone()[0]
            if auto_today >= params["max_auto"]:
                return REFUSAL_AUTO_DAILY_LIMIT
        already = self._conn.execute(
            "SELECT 1 FROM georgia_fine_amount_checks WHERE car_number = :car_number AND protocol_no = :protocol_no "
            "AND trigger = :auto AND check_date = :check_date",
            params,
        ).fetchone()
        if already:
            return REFUSAL_ALREADY_TODAY
        return REFUSAL_PERIOD_LIMIT

    def finish_check(
        self, check_id: int, *, outcome: str, finished_at: datetime, amount: float | None = None,
        previous_amount: float | None = None, increased: bool = False, detail: str | None = None,
    ) -> None:
        # abandoned тоже: строку могли закрыть по CHECK_LEASE, пока сессия ещё
        # шла (другой процесс) — настоящий исход важнее.
        self._conn.execute(
            "UPDATE georgia_fine_amount_checks SET outcome = ?, finished_at = ?, amount = ?, previous_amount = ?, "
            "increased = ?, detail = ? WHERE id = ? AND outcome IN (?, ?)",
            (outcome, _fmt(finished_at), amount, previous_amount, 1 if increased else 0, detail, check_id,
             OUTCOME_IN_PROGRESS, OUTCOME_ABANDONED),
        )
        self._conn.commit()

    def abandon_stale_checks(self, *, now: datetime) -> int:
        """in_progress старше CHECK_LEASE (процесс перезапустился посреди
        сессии) -> abandoned. Строка остаётся и считается в лимитах: сессия
        могла быть создана."""
        cursor = self._conn.execute(
            "UPDATE georgia_fine_amount_checks SET outcome = ?, finished_at = ? WHERE outcome = ? AND started_at <= ?",
            (OUTCOME_ABANDONED, _fmt(now), OUTCOME_IN_PROGRESS, _fmt(_as_naive_utc(now) - CHECK_LEASE)),
        )
        self._conn.commit()
        return cursor.rowcount

    def get_check(self, check_id: int) -> AmountCheck | None:
        row = self._conn.execute(
            f"SELECT {_CHECK_FIELDS} FROM georgia_fine_amount_checks WHERE id = ?", (check_id,),
        ).fetchone()
        return _row_to_check(row) if row else None

    def checks_for_receipt(self, car_number: str, protocol_no: str) -> list[AmountCheck]:
        rows = self._conn.execute(
            f"SELECT {_CHECK_FIELDS} FROM georgia_fine_amount_checks WHERE car_number = ? AND protocol_no = ? "
            "ORDER BY id",
            (car_number, protocol_no),
        ).fetchall()
        return [_row_to_check(row) for row in rows]

    def auto_checks_in_period(self, car_number: str, protocol_no: str, period_index: int) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM georgia_fine_amount_checks WHERE car_number = ? AND protocol_no = ? "
            "AND trigger = ? AND period_index = ?",
            (car_number, protocol_no, TRIGGER_AUTO, period_index),
        ).fetchone()[0]

    def period_closed_reason(
        self, car_number: str, protocol_no: str, period: ControlPeriod, *, tz: ZoneInfo,
    ) -> str | None:
        """Почему автоматические проверки периода больше не нужны: найдено
        увеличение в окне периода (любой проверкой, включая ручную), МВД
        отказало в оплате автоматической проверке периода, или исчерпан
        MAX_AUTO_CHECKS_PER_PERIOD. None — период открыт."""
        start_utc = _fmt(datetime.combine(period.start, datetime.min.time(), tzinfo=tz))
        end_utc = _fmt(datetime.combine(period.window_end + timedelta(days=1), datetime.min.time(), tzinfo=tz))
        increased = self._conn.execute(
            "SELECT 1 FROM georgia_fine_amount_checks WHERE car_number = ? AND protocol_no = ? AND increased = 1 "
            "AND started_at >= ? AND started_at < ?",
            (car_number, protocol_no, start_utc, end_utc),
        ).fetchone()
        if increased:
            return "increased"
        refused = self._conn.execute(
            "SELECT 1 FROM georgia_fine_amount_checks WHERE car_number = ? AND protocol_no = ? AND trigger = ? "
            "AND period_index = ? AND outcome = ?",
            (car_number, protocol_no, TRIGGER_AUTO, period.index, OUTCOME_REFUSED),
        ).fetchone()
        if refused:
            return "refused"
        if self.auto_checks_in_period(car_number, protocol_no, period.index) >= MAX_AUTO_CHECKS_PER_PERIOD:
            return "max_checks"
        return None

    def checked_on(self, car_number: str, protocol_no: str, check_date: date, *, trigger: str | None = None) -> bool:
        if trigger is None:
            row = self._conn.execute(
                "SELECT 1 FROM georgia_fine_amount_checks WHERE car_number = ? AND protocol_no = ? AND check_date = ?",
                (car_number, protocol_no, check_date.isoformat()),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT 1 FROM georgia_fine_amount_checks WHERE car_number = ? AND protocol_no = ? AND check_date = ? "
                "AND trigger = ?",
                (car_number, protocol_no, check_date.isoformat(), trigger),
            ).fetchone()
        return row is not None

    def had_check(self, car_number: str, protocol_no: str, *, trigger: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM georgia_fine_amount_checks WHERE car_number = ? AND protocol_no = ? AND trigger = ?",
            (car_number, protocol_no, trigger),
        ).fetchone()
        return row is not None

    def last_check_started_at(self, car_number: str, protocol_no: str) -> datetime | None:
        row = self._conn.execute(
            "SELECT MAX(started_at) FROM georgia_fine_amount_checks WHERE car_number = ? AND protocol_no = ?",
            (car_number, protocol_no),
        ).fetchone()
        return _parse_ts(row[0]) if row else None

    def sessions_on(self, check_date: date) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM georgia_fine_amount_checks WHERE check_date = ?", (check_date.isoformat(),),
        ).fetchone()[0]

    def manual_sessions_by_user_on(self, telegram_user_id: int, check_date: date) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM georgia_fine_amount_checks WHERE requested_by = ? AND check_date = ? "
            "AND trigger = ?",
            (telegram_user_id, check_date.isoformat(), TRIGGER_MANUAL),
        ).fetchone()[0]

    def outcome_counts_on(self, check_date: date) -> dict[tuple[str, str], int]:
        return {
            (trigger, outcome): count
            for trigger, outcome, count in self._conn.execute(
                "SELECT trigger, outcome, COUNT(*) FROM georgia_fine_amount_checks WHERE check_date = ? "
                "GROUP BY trigger, outcome",
                (check_date.isoformat(),),
            )
        }

    # ---- События увеличения ----

    def add_increase_event(
        self, *, car_number: str, protocol_no: str, old_amount: float, new_amount: float, observed_at: datetime,
        check_id: int | None,
    ) -> int | None:
        """None — событие с этой новой суммой по квитанции уже было."""
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO georgia_fine_amount_events (car_number, protocol_no, old_amount, new_amount, "
            "observed_at, check_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (car_number, protocol_no, old_amount, new_amount, _fmt(observed_at), check_id, _fmt(_utc_now())),
        )
        self._conn.commit()
        return cursor.lastrowid if cursor.rowcount else None

    def events_since(self, since: datetime) -> list[AmountEvent]:
        rows = self._conn.execute(
            "SELECT id, car_number, protocol_no, old_amount, new_amount, observed_at, created_at "
            "FROM georgia_fine_amount_events WHERE created_at >= ? ORDER BY id",
            (_fmt(since),),
        ).fetchall()
        return [_row_to_event(row) for row in rows]

    def get_event(self, event_id: int) -> AmountEvent | None:
        row = self._conn.execute(
            "SELECT id, car_number, protocol_no, old_amount, new_amount, observed_at, created_at "
            "FROM georgia_fine_amount_events WHERE id = ?",
            (event_id,),
        ).fetchone()
        return _row_to_event(row) if row else None

    def count_events(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM georgia_fine_amount_events").fetchone()[0]

    # ---- Доставка событий ----

    def get_delivery(self, event_id: int, subscription_id: int, role: str) -> AmountDelivery | None:
        row = self._conn.execute(
            f"SELECT {_DELIVERY_FIELDS} FROM georgia_fine_amount_deliveries "
            "WHERE event_id = ? AND subscription_id = ? AND recipient_role = ?",
            (event_id, subscription_id, role),
        ).fetchone()
        return _row_to_delivery(row) if row else None

    def chat_handled(self, event_id: int, chat_id: int) -> bool:
        """Событие уже доставлено (или явно исключено) этому чату по другой
        подписке — второе такое же сообщение не нужно."""
        row = self._conn.execute(
            "SELECT 1 FROM georgia_fine_amount_deliveries WHERE event_id = ? AND recipient_chat_id = ? "
            "AND (delivered_at IS NOT NULL OR excluded_at IS NOT NULL)",
            (event_id, chat_id),
        ).fetchone()
        return row is not None

    def record_delivery_attempt(self, event_id: int, subscription_id: int, role: str, chat_id: int) -> None:
        now = _fmt(_utc_now())
        self._conn.execute(
            "INSERT INTO georgia_fine_amount_deliveries (event_id, subscription_id, recipient_role, "
            "recipient_chat_id, attempt_count, last_attempt_at) VALUES (?, ?, ?, ?, 1, ?) "
            "ON CONFLICT (event_id, subscription_id, recipient_role) DO UPDATE SET "
            "attempt_count = attempt_count + 1, last_attempt_at = excluded.last_attempt_at",
            (event_id, subscription_id, role, chat_id, now),
        )
        self._conn.commit()

    def mark_delivered(self, event_id: int, subscription_id: int, role: str) -> None:
        self._conn.execute(
            "UPDATE georgia_fine_amount_deliveries SET delivered_at = ? "
            "WHERE event_id = ? AND subscription_id = ? AND recipient_role = ?",
            (_fmt(_utc_now()), event_id, subscription_id, role),
        )
        self._conn.commit()

    def exclude_delivery(self, event_id: int, subscription_id: int, role: str, chat_id: int, *, reason: str) -> None:
        """Явное исключение из рассылки (не отметка о доставке)."""
        now = _fmt(_utc_now())
        self._conn.execute(
            "INSERT INTO georgia_fine_amount_deliveries (event_id, subscription_id, recipient_role, "
            "recipient_chat_id, excluded_at, exclusion_reason) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (event_id, subscription_id, recipient_role) DO UPDATE SET "
            "excluded_at = excluded.excluded_at, exclusion_reason = excluded.exclusion_reason "
            "WHERE delivered_at IS NULL AND excluded_at IS NULL",
            (event_id, subscription_id, role, chat_id, now, reason),
        )
        self._conn.commit()

    def delivery_counts(self) -> dict[str, int]:
        row = self._conn.execute(
            "SELECT SUM(delivered_at IS NOT NULL), SUM(excluded_at IS NOT NULL), "
            "SUM(delivered_at IS NULL AND excluded_at IS NULL) FROM georgia_fine_amount_deliveries"
        ).fetchone()
        return {"delivered": row[0] or 0, "excluded": row[1] or 0, "pending": row[2] or 0}

    # ---- Приостановка ----

    def suspended_until(self) -> tuple[datetime, str | None] | None:
        until, reason = self._conn.execute(
            "SELECT suspended_until, suspend_reason FROM georgia_payable_amount_control WHERE id = 1"
        ).fetchone()
        parsed = _parse_ts(until)
        return (parsed, reason) if parsed else None

    def suspend(self, *, until: datetime, reason: str) -> None:
        """Продлевает, но не сокращает уже действующую приостановку."""
        current = self.suspended_until()
        if current is not None and current[0] >= _as_naive_utc(until):
            return
        self._conn.execute(
            "UPDATE georgia_payable_amount_control SET suspended_until = ?, suspend_reason = ?, updated_at = ? "
            "WHERE id = 1",
            (_fmt(until), reason, _fmt(_utc_now())),
        )
        self._conn.commit()

    def clear_suspension(self) -> None:
        self._conn.execute(
            "UPDATE georgia_payable_amount_control SET suspended_until = NULL, suspend_reason = NULL, updated_at = ? "
            "WHERE id = 1",
            (_fmt(_utc_now()),),
        )
        self._conn.commit()

    # ---- Дневной автоматический проход ----

    def get_run(self, run_date: date) -> AutoRun | None:
        row = self._conn.execute(
            "SELECT run_date, started_at, finished_at, status, stop_reason, sessions, errors "
            "FROM georgia_payable_amount_runs WHERE run_date = ?",
            (run_date.isoformat(),),
        ).fetchone()
        if row is None:
            return None
        return AutoRun(
            run_date=date.fromisoformat(row[0]), started_at=_parse_ts(row[1]), finished_at=_parse_ts(row[2]),
            status=row[3], stop_reason=row[4], sessions=row[5], errors=row[6],
        )

    def start_run(self, run_date: date, *, at: datetime) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO georgia_payable_amount_runs (run_date, started_at, status) VALUES (?, ?, 'running')",
            (run_date.isoformat(), _fmt(at)),
        )
        self._conn.commit()

    def add_run_progress(self, run_date: date, *, sessions: int = 0, errors: int = 0) -> None:
        self._conn.execute(
            "UPDATE georgia_payable_amount_runs SET sessions = sessions + ?, errors = errors + ? WHERE run_date = ?",
            (sessions, errors, run_date.isoformat()),
        )
        self._conn.commit()

    def finish_run(self, run_date: date, *, status: str, stop_reason: str | None, at: datetime) -> None:
        self._conn.execute(
            "UPDATE georgia_payable_amount_runs SET status = ?, stop_reason = ?, finished_at = ? WHERE run_date = ?",
            (status, stop_reason, _fmt(at), run_date.isoformat()),
        )
        self._conn.commit()

    # ---- Сроки квитанций (detected_fines той же БД, только чтение) ----

    def term_ends_for_cars(self, car_numbers: list[str]) -> dict[tuple[str, str], date | None]:
        """Конец первоначального срока каждой квитанции (см. term_end_of):
        по последнему ответу police.ge (georgia_fine_terms, пишется при каждой
        полной проверке номера), а если его ещё нет — по самой свежей строке
        detected_fines (данные первого обнаружения)."""
        tables = {row[0] for row in self._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        result: dict[tuple[str, str], date | None] = {}
        for start in range(0, len(car_numbers), 500):
            chunk = car_numbers[start:start + 500]
            placeholders = ",".join("?" * len(chunk))
            if "detected_fines" in tables:
                rows = self._conn.execute(
                    f"SELECT car_number, external_fine_id, due_date, raw_data FROM detected_fines "
                    f"WHERE car_number IN ({placeholders}) AND external_fine_id IS NOT NULL ORDER BY id",
                    chunk,
                ).fetchall()
                for car_number, protocol_no, due_date, raw_data in rows:
                    try:
                        raw = json.loads(raw_data) if raw_data else None
                    except (TypeError, ValueError):
                        raw = None
                    raw = raw if isinstance(raw, dict) else {}
                    last_date = _parse_source_date(raw.get("lastDate")) or _parse_source_date(due_date)
                    result[(car_number, protocol_no)] = term_end_of(
                        last_date, _parse_source_date(raw.get("activeDate")),
                    )
            if "georgia_fine_terms" in tables:
                for car_number, protocol_no, last_date, active_date in self._conn.execute(
                    f"SELECT car_number, protocol_no, last_date, active_date FROM georgia_fine_terms "
                    f"WHERE car_number IN ({placeholders})",
                    chunk,
                ):
                    result[(car_number, protocol_no)] = term_end_of(
                        _parse_source_date(last_date), _parse_source_date(active_date),
                    )
        return result

    # ---- Уведомления об увеличении: граница включения ----

    def set_increase_notifications(self, *, enabled: bool, at: datetime) -> datetime | None:
        """Включены — enabled_since остаётся прежним (перезапуск ничего не
        сдвигает) или ставится сейчас (первое включение / включение после
        выключения); выключены — NULL, и следующее включение начнёт с нуля.
        Возвращает действующую границу."""
        if enabled:
            self._conn.execute(
                "UPDATE georgia_amount_notification_state SET enabled_since = COALESCE(enabled_since, ?), "
                "updated_at = ? WHERE id = 1",
                (_fmt(at), _fmt(at)),
            )
        else:
            self._conn.execute(
                "UPDATE georgia_amount_notification_state SET enabled_since = NULL, updated_at = ? WHERE id = 1",
                (_fmt(at),),
            )
        self._conn.commit()
        return self.increase_notifications_since()

    def increase_notifications_since(self) -> datetime | None:
        row = self._conn.execute(
            "SELECT enabled_since FROM georgia_amount_notification_state WHERE id = 1"
        ).fetchone()
        return _parse_ts(row[0]) if row else None


def amount_is_current(amount: PayableAmount | None, term_end: date | None, *, today: date, tz: ZoneInfo) -> bool:
    """Сумма Georgian Card актуальна, если получена не раньше начала
    последнего начавшегося контрольного периода (в нём сумма могла вырасти).
    Пока идёт первоначальный срок или срок неизвестен — любая полученная
    сумма актуальна."""
    if amount is None:
        return False
    if term_end is None:
        return True
    latest = latest_started_period(term_end, today)
    if latest is None:
        return True
    observed_day = amount.observed_at.replace(tzinfo=timezone.utc).astimezone(tz).date()
    return observed_day >= latest.start
