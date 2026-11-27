"""Привязка Telegram-клиента к автомобилю менеджера (Turkey bot).

Менеджер (trusted) добавил автомобиль — строка turkey_bot_user_cars с ЕГО
telegram_user_id и его подписка мониторинга (turkey_monitoring_subscriptions).
Клиент привязывается к этой машине ("якорь" = владелец строки + номер):

- клиент подтверждает привязку сам (запрос в боте или одноразовая ссылка
  t.me/<bot>?start=trlink_<token>), до подтверждения видит только номер;
- после подтверждения машина появляется в его «🚗 Мои автомобили» (обычная
  строка turkey_bot_user_cars), он проверяет штрафы сам;
- уведомления GİB/Avrasya/KGM приходят ему из ТОЙ ЖЕ плановой проверки
  якоря (см. TurkeyLinkedClientNotifier) — второй проверки номера и второй
  подписки не создаётся;
- изменения для клиента считаются от его собственной точки отсчёта
  (baseline): в момент подтверждения — последнее известное состояние
  номера, дальше — то, что ему уже отправлено, и его ручные проверки.
  Поэтому старые штрафы новыми не приходят.

Идентификатор клиента — telegram user_id; username только для поиска.
Токен приглашения хранится как sha256, одноразовый, со сроком действия.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from reader.turkey_bot.models import TurkeyUserCar
from reader.turkey_bot.monitoring.change_detector import detect_changes
from reader.turkey_bot.monitoring.notification_texts import format_change_notification
from reader.turkey_bot.unified.models import ProviderStatus, UnifiedCheckResult

logger = logging.getLogger(__name__)

LINK_TTL = timedelta(hours=72)
INVITE_PAYLOAD_PREFIX = "trlink_"
_PROVIDERS = ("gib", "avrasya", "kgm")

STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_DECLINED = "declined"
STATUS_CANCELLED = "cancelled"   # приглашение отменено/заменено новым до подтверждения
STATUS_REPLACED = "replaced"     # активная привязка заменена другим клиентом
STATUS_REVOKED = "revoked"       # менеджер отвязал клиента
STATUS_REMOVED = "removed"       # клиент удалил автомобиль из «Мои автомобили»

METHOD_USERNAME = "username"
METHOD_PICK = "pick"
METHOD_INVITE = "invite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turkey_client_links (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_telegram_user_id  INTEGER NOT NULL,
    plate                   TEXT NOT NULL,
    status                  TEXT NOT NULL,
    method                  TEXT NOT NULL,
    invited_user_id         INTEGER,
    client_telegram_user_id INTEGER,
    token_hash              TEXT UNIQUE,
    created_by              INTEGER NOT NULL,
    created_at              TEXT NOT NULL,
    expires_at              TEXT NOT NULL,
    confirmed_at            TEXT,
    closed_at               TEXT,
    closed_by               INTEGER,
    client_car_created      INTEGER NOT NULL DEFAULT 0,
    client_muted            INTEGER NOT NULL DEFAULT 0,
    baseline                TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_turkey_client_links_one_active
    ON turkey_client_links(owner_telegram_user_id, plate) WHERE status = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS idx_turkey_client_links_one_pending
    ON turkey_client_links(owner_telegram_user_id, plate) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_turkey_client_links_client
    ON turkey_client_links(client_telegram_user_id, plate, status);
"""

_COLUMNS = (
    "id, owner_telegram_user_id, plate, status, method, invited_user_id, client_telegram_user_id, "
    "created_by, created_at, expires_at, confirmed_at, closed_at, closed_by, client_car_created, "
    "client_muted, baseline"
)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class TurkeyClientLink:
    id: int
    owner_telegram_user_id: int
    plate: str
    status: str
    method: str
    invited_user_id: int | None
    client_telegram_user_id: int | None
    created_by: int
    created_at: datetime
    expires_at: datetime
    confirmed_at: datetime | None
    closed_at: datetime | None
    closed_by: int | None
    client_car_created: bool
    client_muted: bool
    baseline: dict[str, int] = field(default_factory=dict)

    def is_expired(self, now: datetime) -> bool:
        return self.status == STATUS_PENDING and self.expires_at <= now


def _row_to_link(row) -> TurkeyClientLink:
    (link_id, owner, plate, status, method, invited, client, created_by, created_at, expires_at,
     confirmed_at, closed_at, closed_by, car_created, muted, baseline) = row
    return TurkeyClientLink(
        id=link_id, owner_telegram_user_id=owner, plate=plate, status=status, method=method,
        invited_user_id=invited, client_telegram_user_id=client, created_by=created_by,
        created_at=_parse(created_at), expires_at=_parse(expires_at), confirmed_at=_parse(confirmed_at),
        closed_at=_parse(closed_at), closed_by=closed_by, client_car_created=bool(car_created),
        client_muted=bool(muted), baseline={k: int(v) for k, v in json.loads(baseline).items()} if baseline else {},
    )


class TurkeyClientLinkRepository:
    """turkey_client_links поверх того же SQLite-файла, что и остальные
    Turkey-таблицы. Только additive схема (CREATE ... IF NOT EXISTS)."""

    def __init__(self, db_path: Path | str):
        is_memory = db_path == ":memory:"
        self._path = db_path if is_memory else Path(db_path)
        if not is_memory:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path))
        if not is_memory:
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def _one(self, where: str, params: tuple) -> TurkeyClientLink | None:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM turkey_client_links WHERE {where} ORDER BY id DESC LIMIT 1", params,
        ).fetchone()
        return _row_to_link(row) if row else None

    def get(self, link_id: int) -> TurkeyClientLink | None:
        return self._one("id = ?", (link_id,))

    def get_by_token(self, token: str) -> TurkeyClientLink | None:
        return self._one("token_hash = ?", (hash_token(token),))

    def active_for_anchor(self, owner_telegram_user_id: int, plate: str) -> TurkeyClientLink | None:
        return self._one(
            "owner_telegram_user_id = ? AND plate = ? AND status = 'active'", (owner_telegram_user_id, plate),
        )

    def pending_for_anchor(self, owner_telegram_user_id: int, plate: str) -> TurkeyClientLink | None:
        return self._one(
            "owner_telegram_user_id = ? AND plate = ? AND status = 'pending'", (owner_telegram_user_id, plate),
        )

    def active_for_client(self, client_telegram_user_id: int, plate: str) -> TurkeyClientLink | None:
        return self._one(
            "client_telegram_user_id = ? AND plate = ? AND status = 'active'", (client_telegram_user_id, plate),
        )

    def active_links_for_client(self, client_telegram_user_id: int, plate: str) -> list[TurkeyClientLink]:
        """Все активные привязки клиента к этому номеру (номер мог быть у
        нескольких владельцев) — общая точка отсчёта, без дублей."""
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM turkey_client_links "
            "WHERE client_telegram_user_id = ? AND plate = ? AND status = 'active' ORDER BY id",
            (client_telegram_user_id, plate),
        ).fetchall()
        return [_row_to_link(row) for row in rows]

    def create_pending(
        self, *, owner_telegram_user_id: int, plate: str, method: str, created_by: int,
        invited_user_id: int | None, token: str | None, now: datetime, ttl: timedelta = LINK_TTL,
    ) -> TurkeyClientLink:
        """Новое приглашение; прежнее неподтверждённое приглашение этой
        машины отменяется (одно приглашение на машину)."""
        with self._conn:
            self._conn.execute(
                "UPDATE turkey_client_links SET status = 'cancelled', closed_at = ?, closed_by = ?, "
                "token_hash = NULL WHERE owner_telegram_user_id = ? AND plate = ? AND status = 'pending'",
                (_iso(now), created_by, owner_telegram_user_id, plate),
            )
            cursor = self._conn.execute(
                "INSERT INTO turkey_client_links (owner_telegram_user_id, plate, status, method, "
                "invited_user_id, token_hash, created_by, created_at, expires_at) "
                "VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?)",
                (
                    owner_telegram_user_id, plate, method, invited_user_id,
                    hash_token(token) if token else None, created_by, _iso(now), _iso(now + ttl),
                ),
            )
        return self.get(cursor.lastrowid)

    def bind_invite(self, link_id: int, telegram_user_id: int) -> bool:
        """Первый, кто открыл ссылку, закрепляет приглашение за собой:
        пересланную дальше ссылку другой пользователь уже не подтвердит."""
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE turkey_client_links SET invited_user_id = ? "
                "WHERE id = ? AND status = 'pending' AND invited_user_id IS NULL",
                (telegram_user_id, link_id),
            )
        return cursor.rowcount == 1

    def activate(
        self, link_id: int, *, client_telegram_user_id: int, now: datetime,
        baseline: dict[str, int], client_car_created: bool,
    ) -> tuple[TurkeyClientLink, TurkeyClientLink | None] | None:
        """Атомарно: приглашение всё ещё ждёт, адресовано этому клиенту и не
        истекло -> active; прежняя активная привязка машины -> replaced.
        Токен стирается (повторно не сработает). None — условие не выполнено
        (уже подтверждено, отклонено, отменено, истекло, чужое)."""
        with self._conn:
            current = self.get(link_id)
            if (
                current is None or current.status != STATUS_PENDING
                or current.invited_user_id != client_telegram_user_id or current.expires_at <= now
            ):
                return None
            previous = self.active_for_anchor(current.owner_telegram_user_id, current.plate)
            if previous is not None:
                self._conn.execute(
                    "UPDATE turkey_client_links SET status = 'replaced', closed_at = ?, closed_by = ? WHERE id = ?",
                    (_iso(now), current.created_by, previous.id),
                )
            cursor = self._conn.execute(
                "UPDATE turkey_client_links SET status = 'active', client_telegram_user_id = ?, "
                "confirmed_at = ?, token_hash = NULL, baseline = ?, client_car_created = ? "
                "WHERE id = ? AND status = 'pending'",
                (client_telegram_user_id, _iso(now), json.dumps(baseline), int(client_car_created), link_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("turkey client link changed during activation")
        return self.get(link_id), previous

    def close(self, link_id: int, *, status: str, closed_by: int, now: datetime, from_status: str) -> bool:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE turkey_client_links SET status = ?, closed_at = ?, closed_by = ?, token_hash = NULL "
                "WHERE id = ? AND status = ?",
                (status, _iso(now), closed_by, link_id, from_status),
            )
        return cursor.rowcount == 1

    def active_links(self) -> list[TurkeyClientLink]:
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM turkey_client_links WHERE status = 'active' ORDER BY id",
        ).fetchall()
        return [_row_to_link(row) for row in rows]

    def reanchor(self, link_id: int, *, new_owner_telegram_user_id: int) -> bool:
        """Активная привязка переходит к машине другого владельца того же
        номера; точка отсчёта и всё остальное в строке не меняются. False —
        привязки нет или у нового владельца уже есть активная привязка."""
        try:
            with self._conn:
                cursor = self._conn.execute(
                    "UPDATE turkey_client_links SET owner_telegram_user_id = ? WHERE id = ? AND status = 'active'",
                    (new_owner_telegram_user_id, link_id),
                )
        except sqlite3.IntegrityError:
            return False
        return cursor.rowcount == 1

    def mark_client_car_created(self, link_id: int) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE turkey_client_links SET client_car_created = 1 WHERE id = ? AND status = 'active'", (link_id,),
            )

    def set_muted(self, link_id: int, muted: bool) -> None:
        with self._conn:
            self._conn.execute("UPDATE turkey_client_links SET client_muted = ? WHERE id = ?", (int(muted), link_id))

    def update_baseline(self, link_id: int, baseline: dict[str, int]) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE turkey_client_links SET baseline = ? WHERE id = ? AND status = 'active'",
                (json.dumps(baseline), link_id),
            )

    def close_db(self) -> None:
        self._conn.close()


@dataclass(frozen=True)
class OutgoingMessage:
    """Сообщение в ДРУГОЙ приватный чат (клиенту или менеджеру).
    confirm_link_id — под сообщением кнопки ✅/❌ этого приглашения."""

    chat_id: int
    text: str
    confirm_link_id: int | None = None
    # Не удалось отправить (бот заблокирован и т.п.) — это сообщение уходит
    # в текущий чат (менеджеру).
    on_failure_text: str | None = None


@dataclass(frozen=True)
class ConfirmOutcome:
    status: str  # ok | already | invalid | expired | not_for_you | own_car
    link: TurkeyClientLink | None = None
    client_car: TurkeyUserCar | None = None
    replaced: TurkeyClientLink | None = None


@dataclass(frozen=True)
class AdminOutcome:
    """Итог действия администратора над привязкой без менеджера.
    status: disabled | reanchored | moved | gone | not_orphan | not_manager |
    conflict | own_client | no_car."""

    status: str
    link: TurkeyClientLink | None = None
    monitoring_active: bool = False


class TurkeyClientLinkService:
    """Бизнес-логика привязки; ничего не знает про Telethon и тексты."""

    def __init__(
        self, links: TurkeyClientLinkRepository, garage, subscriptions, runs, known_users, *,
        bot_username: str, clock: Callable[[], datetime] | None = None,
        is_manager: Callable[[int], bool] | None = None,
    ):
        # is_manager — действующий менеджер (trusted). Привязкой, у машины
        # которой владелец больше не менеджер, никто из сотрудников управлять
        # не может: такие приглашения не подтверждаются, активные привязки
        # видит и решает администратор (orphan_links/disable/transfer).
        self._is_manager = is_manager or (lambda telegram_user_id: True)
        self._links = links
        self._garage = garage
        self._subscriptions = subscriptions
        self._runs = runs
        self._known_users = known_users
        self._bot_username = bot_username
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # ---- Пользователи бота (выбор клиента) ----

    def find_user_id_by_username(self, username: str) -> int | None:
        found = self._known_users.find_by_username(username)
        return found[0] if found else None

    def profile(self, telegram_user_id: int) -> tuple[str | None, str | None, str | None] | None:
        """(username, first_name, last_name) — только для показа менеджеру."""
        return self._known_users.get_profile(telegram_user_id)

    def pickable_users(
        self, page: int, *, page_size: int, exclude_ids: frozenset[int],
    ) -> tuple[list[tuple[int, str | None, str | None, str | None]], int, int]:
        """(страница пользователей, номер страницы после ограничения, всего страниц)."""
        total = self._known_users.count_excluding(exclude_ids)
        total_pages = max(1, -(-total // page_size))
        page = max(0, min(page, total_pages - 1))
        rows = self._known_users.list_recent_page(
            offset=page * page_size, limit=page_size, exclude_ids=exclude_ids,
        )
        return rows, page, total_pages

    @property
    def links(self) -> TurkeyClientLinkRepository:
        return self._links

    def now(self) -> datetime:
        return self._clock()

    def invite_url(self, token: str) -> str:
        return f"https://t.me/{self._bot_username}?start={INVITE_PAYLOAD_PREFIX}{token}"

    def create_invite(self, car: TurkeyUserCar, *, created_by: int) -> tuple[TurkeyClientLink, str]:
        token = secrets.token_urlsafe(16)
        link = self._links.create_pending(
            owner_telegram_user_id=car.telegram_user_id, plate=car.car_number, method=METHOD_INVITE,
            created_by=created_by, invited_user_id=None, token=token, now=self.now(),
        )
        return link, self.invite_url(token)

    def request_user(self, car: TurkeyUserCar, *, created_by: int, client_telegram_user_id: int, method: str):
        return self._links.create_pending(
            owner_telegram_user_id=car.telegram_user_id, plate=car.car_number, method=method,
            created_by=created_by, invited_user_id=client_telegram_user_id, token=None, now=self.now(),
        )

    def open_invite(self, token: str, *, telegram_user_id: int) -> ConfirmOutcome:
        """Переход по ссылке: только проверка и закрепление за открывшим —
        ни машины, ни уведомлений до подтверждения."""
        link = self._links.get_by_token(token) if token else None
        if link is None or link.status != STATUS_PENDING or not self._is_manager(link.owner_telegram_user_id):
            return ConfirmOutcome(status="invalid")
        if link.is_expired(self.now()):
            return ConfirmOutcome(status="expired", link=link)
        if link.owner_telegram_user_id == telegram_user_id:
            return ConfirmOutcome(status="own_car", link=link)
        if link.invited_user_id is None and self._links.bind_invite(link.id, telegram_user_id):
            link = self._links.get(link.id)
        if link.invited_user_id != telegram_user_id:
            return ConfirmOutcome(status="not_for_you", link=link)
        return ConfirmOutcome(status="ok", link=link)

    def check_request(self, link_id: int, *, telegram_user_id: int) -> ConfirmOutcome:
        link = self._links.get(link_id)
        if link is None:
            return ConfirmOutcome(status="invalid")
        if link.status == STATUS_ACTIVE and link.client_telegram_user_id == telegram_user_id:
            return ConfirmOutcome(status="already", link=link)
        if link.status != STATUS_PENDING or not self._is_manager(link.owner_telegram_user_id):
            return ConfirmOutcome(status="invalid", link=link)
        if link.invited_user_id != telegram_user_id:
            return ConfirmOutcome(status="not_for_you", link=link)
        if link.is_expired(self.now()):
            return ConfirmOutcome(status="expired", link=link)
        return ConfirmOutcome(status="ok", link=link)

    def confirm(self, link_id: int, *, telegram_user_id: int) -> ConfirmOutcome:
        checked = self.check_request(link_id, telegram_user_id=telegram_user_id)
        if checked.status != "ok":
            if checked.status == "already":
                car = self._garage.get_owned_car_by_number(telegram_user_id, checked.link.plate)
                return ConfirmOutcome(status="already", link=checked.link, client_car=car)
            return checked
        link = checked.link
        if link.owner_telegram_user_id == telegram_user_id:
            return ConfirmOutcome(status="own_car", link=link)

        # Тот же клиент подтверждает новое приглашение той же машины (замена
        # привязки на него же): машина и точка отсчёта переходят из прежней
        # привязки, его строку «Мои автомобили» не удаляем.
        current = self._links.active_for_anchor(link.owner_telegram_user_id, link.plate)
        same_client = current if current is not None and current.client_telegram_user_id == telegram_user_id else None
        existed = self._garage.get_owned_car_by_number(telegram_user_id, link.plate) is not None
        car = self._garage.add_car(telegram_user_id=telegram_user_id, car_number=link.plate)
        activated = self._links.activate(
            link.id, client_telegram_user_id=telegram_user_id, now=self.now(),
            baseline=same_client.baseline if same_client is not None else self._current_baseline(link.plate),
            client_car_created=same_client.client_car_created if same_client is not None else not existed,
        )
        if activated is None:
            if not existed:
                self._garage.delete_car(car.id, telegram_user_id=telegram_user_id)
            return ConfirmOutcome(status="invalid", link=self._links.get(link.id))
        active, replaced = activated
        if replaced is not None and replaced.client_telegram_user_id != telegram_user_id:
            self._release_client_car(replaced)
        # Мониторинг якоря при подтверждении НЕ включается: чужое состояние
        # мониторинга (в т.ч. осознанная остановка или общий ⛔) не меняется.
        return ConfirmOutcome(status="ok", link=active, client_car=car, replaced=replaced)

    def decline(self, link_id: int, *, telegram_user_id: int) -> ConfirmOutcome:
        checked = self.check_request(link_id, telegram_user_id=telegram_user_id)
        if checked.status not in ("ok", "expired"):
            return checked
        closed = self._links.close(
            link_id, status=STATUS_DECLINED, closed_by=telegram_user_id, now=self.now(), from_status=STATUS_PENDING,
        )
        return ConfirmOutcome(status="ok" if closed else "invalid", link=self._links.get(link_id))

    def cancel_pending(self, car: TurkeyUserCar, *, closed_by: int) -> TurkeyClientLink | None:
        pending = self._links.pending_for_anchor(car.telegram_user_id, car.car_number)
        if pending is None:
            return None
        self._links.close(pending.id, status=STATUS_CANCELLED, closed_by=closed_by, now=self.now(),
                          from_status=STATUS_PENDING)
        return pending

    def unlink(self, car: TurkeyUserCar, *, closed_by: int) -> TurkeyClientLink | None:
        active = self._links.active_for_anchor(car.telegram_user_id, car.car_number)
        if active is None:
            return None
        if not self._links.close(active.id, status=STATUS_REVOKED, closed_by=closed_by, now=self.now(),
                                 from_status=STATUS_ACTIVE):
            return None
        self._release_client_car(active)
        return active

    def anchor_removed(self, car: TurkeyUserCar, *, closed_by: int) -> TurkeyClientLink | None:
        """Владелец удалил машину-якорь: приглашение отменяется, активная
        привязка закрывается так же, как при отвязке (история проверок
        остаётся, машина клиента убирается, только если её создала привязка).
        Возвращает закрытую активную привязку (клиенту — короткое уведомление)."""
        self.cancel_pending(car, closed_by=closed_by)
        return self.unlink(car, closed_by=closed_by)

    # ---- Администратор: привязки без менеджера ----

    def orphan_links(self) -> list[TurkeyClientLink]:
        """Активные привязки, у машины которых владелец больше не менеджер."""
        return [link for link in self._links.active_links() if not self._is_manager(link.owner_telegram_user_id)]

    def _orphan(self, link_id: int) -> tuple[TurkeyClientLink | None, AdminOutcome | None]:
        link = self._links.get(link_id)
        if link is None or link.status != STATUS_ACTIVE:
            return None, AdminOutcome(status="gone", link=link)
        if self._is_manager(link.owner_telegram_user_id):
            return None, AdminOutcome(status="not_orphan", link=link)
        return link, None

    def disable_orphan(self, link_id: int, *, closed_by: int) -> AdminOutcome:
        """Закрыть привязку без менеджера так же, как отвязку: история
        проверок остаётся, машина клиента убирается, только если её создала
        привязка. Машины и мониторинг бывшего менеджера не трогаются."""
        link, refused = self._orphan(link_id)
        if refused is not None:
            return refused
        if not self._links.close(link.id, status=STATUS_REVOKED, closed_by=closed_by, now=self.now(),
                                 from_status=STATUS_ACTIVE):
            return AdminOutcome(status="gone", link=link)
        self._release_client_car(link)
        return AdminOutcome(status="disabled", link=link)

    def transfer_plan(self, link_id: int, *, new_owner: int) -> AdminOutcome:
        """Что сделает передача (без изменений): reanchored — у менеджера уже
        есть эта машина; moved — к нему перейдут машина бывшего менеджера и
        её мониторинг; иначе причина отказа."""
        link, refused = self._orphan(link_id)
        if refused is not None:
            return refused
        if not self._is_manager(new_owner):
            return AdminOutcome(status="not_manager", link=link)
        if link.client_telegram_user_id == new_owner:
            return AdminOutcome(status="own_client", link=link)
        if self._links.active_for_anchor(new_owner, link.plate) is not None:
            return AdminOutcome(status="conflict", link=link)
        if self._garage.get_owned_car_by_number(new_owner, link.plate) is not None:
            return AdminOutcome(status="reanchored", link=link)
        if self._garage.get_owned_car_by_number(link.owner_telegram_user_id, link.plate) is None:
            return AdminOutcome(status="no_car", link=link)
        return AdminOutcome(status="moved", link=link)

    def transfer_orphan(self, link_id: int, *, new_owner: int) -> AdminOutcome:
        """Передать привязку без менеджера действующему менеджеру — без
        второй проверки номера:
        - у менеджера уже есть эта машина -> привязка переходит на неё
          (уведомления клиенту — из её проверки), строки бывшего менеджера
          остаются как есть;
        - машины нет -> к нему переходят строка машины бывшего менеджера и
          её подписка мониторинга (то же расписание и состояние ВКЛ/ВЫКЛ):
          мониторинг остаётся ОДИН.
        Точка отсчёта клиента хранится в самой привязке и не меняется: ни
        повторов старых штрафов, ни пропусков."""
        plan = self.transfer_plan(link_id, new_owner=new_owner)
        if plan.status not in ("reanchored", "moved"):
            return plan
        link = plan.link
        old_owner = link.owner_telegram_user_id
        if plan.status == "moved":
            old_car = self._garage.get_owned_car_by_number(old_owner, link.plate)
            self._garage.transfer_car(old_car.id, new_owner_telegram_user_id=new_owner)
            self._subscriptions.transfer(
                from_telegram_user_id=old_owner, plate=link.plate, to_telegram_user_id=new_owner,
            )
        else:
            target = self._garage.get_owned_car_by_number(new_owner, link.plate)
            old_car = self._garage.get_owned_car_by_number(old_owner, link.plate)
            if old_car is not None and self._garage.get_client_display_name(target.id) is None:
                name = self._garage.get_client_display_name(old_car.id)
                if name is not None:
                    self._garage.set_client_display_name(target.id, name)
        if not self._links.reanchor(link.id, new_owner_telegram_user_id=new_owner):
            return AdminOutcome(status="conflict", link=link)
        moved = self._links.get(link.id)
        subscription = self._subscriptions.get(telegram_user_id=new_owner, plate=link.plate)
        return AdminOutcome(
            status=plan.status, link=moved, monitoring_active=subscription is not None and subscription.active,
        )

    def client_removed_car(self, *, telegram_user_id: int, plate: str) -> None:
        """Клиент сам удалил машину из «Мои автомобили» — привязка закрыта,
        уведомления через менеджера ему больше не идут."""
        for active in self._links.active_links_for_client(telegram_user_id, plate):
            self._links.close(active.id, status=STATUS_REMOVED, closed_by=telegram_user_id, now=self.now(),
                              from_status=STATUS_ACTIVE)

    def set_client_muted(self, *, telegram_user_id: int, plate: str, muted: bool) -> None:
        """⏹/▶️ клиента — для ВСЕХ его привязок к этому номеру."""
        for active in self._links.active_links_for_client(telegram_user_id, plate):
            if active.owner_telegram_user_id != telegram_user_id:
                self._links.set_muted(active.id, muted)

    def client_view(self, *, telegram_user_id: int, plate: str) -> tuple[TurkeyClientLink | None, bool]:
        """(активная привязка клиента к этому номеру, приходят ли ему
        уведомления через менеджера: привязка не заглушена и мониторинг
        якоря включён)."""
        links = [
            link for link in self._links.active_links_for_client(telegram_user_id, plate)
            if link.owner_telegram_user_id != telegram_user_id
        ]
        if not links:
            return None, False
        receiving = any(not link.client_muted and self.anchor_monitoring_active(link) for link in links)
        return links[-1], receiving

    def any_anchor_monitoring_active(self, *, telegram_user_id: int, plate: str) -> bool:
        return any(
            self.anchor_monitoring_active(link) for link in self._links.active_links_for_client(telegram_user_id, plate)
            if link.owner_telegram_user_id != telegram_user_id
        )

    def anchor_monitoring_active(self, link: TurkeyClientLink) -> bool:
        anchor = self._subscriptions.get(telegram_user_id=link.owner_telegram_user_id, plate=link.plate)
        return anchor is not None and anchor.active

    def _release_client_car(self, link: TurkeyClientLink) -> None:
        """Отвязка/замена: строку «Мои автомобили», созданную привязкой,
        убираем, если клиент не включил собственный мониторинг. История
        проверок (turkey_check_runs) не трогается."""
        client = link.client_telegram_user_id
        if client is None or not link.client_car_created:
            return
        remaining = self._links.active_links_for_client(client, link.plate)
        if remaining:
            # Номер у клиента ещё через другую привязку — машина остаётся, а
            # отметка «машину создала привязка» переходит к оставшейся: её
            # отвязка потом уберёт машину.
            self._links.mark_client_car_created(remaining[0].id)
            return
        own = self._subscriptions.get(telegram_user_id=client, plate=link.plate)
        if own is not None and own.active:
            return
        car = self._garage.get_owned_car_by_number(client, link.plate)
        if car is not None:
            self._garage.delete_car(car.id, telegram_user_id=client)

    def _current_baseline(self, plate: str) -> dict[str, int]:
        """Точка отсчёта клиента — последнее известное состояние номера
        (последний успешный ответ каждого провайдера по любым проверкам)."""
        baseline = {}
        for provider in _PROVIDERS:
            result_id = self._runs.latest_successful_provider_result_id(plate, provider)
            if result_id is not None:
                baseline[provider] = result_id
        return baseline


class TurkeyLinkedClientNotifier:
    """Уведомления привязанным клиентам из плановой проверки якоря (см.
    TurkeyMonitoringService) — без второй проверки номера. Клиенту с
    собственным активным мониторингом этого номера не дублируем: его
    подписка уведомит его сама."""

    def __init__(self, links: TurkeyClientLinkRepository, runs, subscriptions):
        self._links = links
        self._runs = runs
        self._subscriptions = subscriptions

    async def notify(
        self, *, owner_telegram_user_id: int, plate: str, result: UnifiedCheckResult, run_id: int, notifier,
    ) -> None:
        link = self._links.active_for_anchor(owner_telegram_user_id, plate)
        if link is None or link.client_muted or link.client_telegram_user_id in (None, owner_telegram_user_id):
            return
        client = link.client_telegram_user_id
        own = self._subscriptions.get(telegram_user_id=client, plate=plate)
        if own is not None and own.active:
            return

        # Точка отсчёта — общая для ВСЕХ привязок клиента к этому номеру (номер
        # мог быть у нескольких владельцев): то же изменение из проверки
        # второго якоря уже не придёт повторно.
        family = self._links.active_links_for_client(client, plate) or [link]
        previous = {p.provider: self._baseline_result(family, plate, p.provider, client) for p in result.providers}
        failed: set[str] = set()
        for change in detect_changes(previous, result):
            try:
                await notifier.send(telegram_chat_id=client, text=format_change_notification(plate, change))
            except Exception:
                failed.add(change.provider)
                logger.exception("Turkey linked client: не удалось отправить уведомление (link_id=%s)", link.id)
        # Двигаем точку отсчёта по каждому провайдеру отдельно: недоставленное
        # изменение придёт со следующей проверкой, доставленное — не повторится.
        ids = self._runs.provider_result_ids_for_run(run_id)
        for member in family:
            baseline = dict(member.baseline)
            for provider_result in result.providers:
                provider = provider_result.provider
                if provider_result.status != ProviderStatus.ERROR and provider in ids and provider not in failed:
                    baseline[provider] = ids[provider]
            if baseline != member.baseline:
                self._links.update_baseline(member.id, baseline)

    def _baseline_result(self, family: list[TurkeyClientLink], plate: str, provider: str, client: int):
        """Самое новое из: что клиенту уже отправлено/было состоянием при
        привязке (по всем его привязкам к номеру) и его ручная проверка."""
        candidates = [
            self._runs.get_provider_result(member.baseline[provider])
            for member in family if provider in member.baseline
        ]
        candidates.append(self._runs.get_last_successful_provider_result_for_subscriber(
            plate, provider, telegram_user_id=client,
        ))
        candidates = [result for result in candidates if result is not None]
        return max(candidates, key=lambda result: result.checked_at) if candidates else None
