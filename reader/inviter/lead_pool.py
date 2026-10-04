"""Пул лидов кампаний инвайтера с ограничением по источнику (см.
InviteCampaign.source_chats, например "🇦🇲 Армения — Садахло").

Зачем отдельный пул, а не users.keywords (как у кампании Грузии):
users.keywords — агрегат совпадений сценариев (config/scenarios.yaml) по
ВСЕМ группам сразу; по нему нельзя понять, в каком чате человек написал
совпавшее слово, а keyword кампании сравнивается с ним точным токеном (см.
reader/inviter/repository.py::_LEADS_WHERE). Кампания "только из @sadahlo"
этим механизмом не выражается — поэтому для неё лиды собираются сюда
сканированием истории САМОГО источника.

Что переиспользуется из существующего сбора лидов (reader/sync_users.py ->
reader/users/history_sync.py), а не пишется заново:
- KeywordMatcher (reader/scenarios.py) — та же нормализация текста
  (lower().strip()) и та же семантика "keyword — подстрока текста", просто
  с одним keyword кампании ("страх" совпадает со "страховка",
  "застраховать" и т.д. — без какого-либо захардкоженного списка форм);
- TelegramUserInfo.from_telethon_user + UserRepository.upsert — отправитель
  сохраняется в ту же таблицу users (username/access_hash), из которой
  инвайтер берёт кандидатов и резолвит их;
- то же правило диапазона: вся история чата без ограничения по дате (как
  history_sync), с checkpoint'ом по message_id, чтобы повторный прогон
  читал только новые сообщения;
- та же сессия чтения истории (settings.telegram.session_path_sync).

Один пользователь = один лид кампании (PRIMARY KEY (campaign_id, user_id)),
сколько бы совпавших сообщений он ни написал; число сообщений и
первое/последнее сообщение (id/дата) сохраняются как evidence.

status лида: 'new' — пригоден для приглашения (кандидат инвайтера, если
также есть username/access_hash — общие правила инвайтера); 'bot',
'deleted' — не человек/удалённый аккаунт; 'already_member' — уже участник
target_chat кампании по данным сканирования. Ничего из этого не удаляется:
неподходящие лиды остаются записанными со своим статусом.

Сканирование НИЧЕГО не отправляет в Telegram (только get_entity/
iter_messages/iter_participants) и само по себе ничего не пишет в БД —
запись происходит только в import_scan() (явное --import, см.
reader/inviter/manage.py build-lead-pool).
"""

import csv
import logging
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telethon import utils
from telethon.tl.types import User

from reader.inviter.models import InviteCampaign
from reader.scenarios import KeywordMatcher, Scenario
from reader.users.models import TelegramUserInfo

logger = logging.getLogger(__name__)

LEAD_STATUS_NEW = "new"
LEAD_STATUS_BOT = "bot"
LEAD_STATUS_DELETED = "deleted"
LEAD_STATUS_ALREADY_MEMBER = "already_member"

MATCH_RULE_SUBSTRING = "substring"
MATCH_RULE_RU_INSURANCE = "ru_insurance"
MATCH_RULES = (MATCH_RULE_SUBSTRING, MATCH_RULE_RU_INSURANCE)

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS campaign_leads (
        campaign_id        INTEGER NOT NULL,
        user_id            INTEGER NOT NULL,
        source_chat_id     INTEGER NOT NULL,
        source_ref         TEXT,
        matched_keyword    TEXT NOT NULL,
        status             TEXT NOT NULL DEFAULT 'new',
        match_count        INTEGER NOT NULL DEFAULT 0,
        first_message_id   INTEGER,
        first_message_at   TIMESTAMP,
        last_message_id    INTEGER,
        last_message_at    TIMESTAMP,
        username           TEXT,
        access_hash        INTEGER,
        display_name       TEXT,
        created_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (campaign_id, user_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_campaign_leads_status ON campaign_leads (campaign_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_campaign_leads_user ON campaign_leads (user_id)",
    """
    CREATE TABLE IF NOT EXISTS campaign_lead_scan_state (
        campaign_id        INTEGER NOT NULL,
        source_chat_id     INTEGER NOT NULL,
        source_ref         TEXT,
        last_message_id    INTEGER NOT NULL DEFAULT 0,
        messages_scanned   INTEGER NOT NULL DEFAULT 0,
        last_scan_at       TIMESTAMP,
        PRIMARY KEY (campaign_id, source_chat_id)
    )
    """,
)


# Additive — для таблицы, созданной до появления этих колонок.
_LEAD_COLUMN_MIGRATIONS = {
    "username": "ALTER TABLE campaign_leads ADD COLUMN username TEXT",
    "access_hash": "ALTER TABLE campaign_leads ADD COLUMN access_hash INTEGER",
    "display_name": "ALTER TABLE campaign_leads ADD COLUMN display_name TEXT",
}


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass(frozen=True)
class CampaignLead:
    campaign_id: int
    user_id: int
    source_chat_id: int
    source_ref: str | None
    matched_keyword: str
    status: str
    match_count: int
    first_message_id: int | None
    first_message_at: datetime | None
    last_message_id: int | None
    last_message_at: datetime | None


@dataclass(frozen=True)
class ScanCheckpoint:
    campaign_id: int
    source_chat_id: int
    source_ref: str | None
    last_message_id: int
    messages_scanned: int
    last_scan_at: datetime | None


_LEAD_COLUMNS = (
    "campaign_id, user_id, source_chat_id, source_ref, matched_keyword, status, match_count, "
    "first_message_id, first_message_at, last_message_id, last_message_at"
)


def _row_to_lead(row) -> CampaignLead:
    (
        campaign_id, user_id, source_chat_id, source_ref, matched_keyword, status, match_count,
        first_message_id, first_message_at, last_message_id, last_message_at,
    ) = row
    return CampaignLead(
        campaign_id=campaign_id, user_id=user_id, source_chat_id=source_chat_id,
        source_ref=source_ref, matched_keyword=matched_keyword, status=status,
        match_count=match_count, first_message_id=first_message_id,
        first_message_at=_parse_datetime(first_message_at),
        last_message_id=last_message_id, last_message_at=_parse_datetime(last_message_at),
    )


class CampaignLeadRepository:
    """CRUD поверх campaign_leads/campaign_lead_scan_state — тот же стиль,
    что и у остальных репозиториев reader/inviter/ (additive CREATE ...
    IF NOT EXISTS при открытии)."""

    def __init__(self, db_path: Path):
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        for statement in _SCHEMA:
            self._conn.execute(statement)
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(campaign_leads)")}
        for column, statement in _LEAD_COLUMN_MIGRATIONS.items():
            if column not in columns:
                self._conn.execute(statement)
        self._conn.commit()

    def get(self, campaign_id: int, user_id: int) -> CampaignLead | None:
        row = self._conn.execute(
            f"SELECT {_LEAD_COLUMNS} FROM campaign_leads WHERE campaign_id = ? AND user_id = ?",
            (campaign_id, user_id),
        ).fetchone()
        return _row_to_lead(row) if row else None

    def list(self, campaign_id: int) -> list[CampaignLead]:
        rows = self._conn.execute(
            f"SELECT {_LEAD_COLUMNS} FROM campaign_leads WHERE campaign_id = ? ORDER BY user_id",
            (campaign_id,),
        ).fetchall()
        return [_row_to_lead(row) for row in rows]

    def known_user_ids(self, campaign_id: int) -> set[int]:
        rows = self._conn.execute(
            "SELECT user_id FROM campaign_leads WHERE campaign_id = ?", (campaign_id,),
        ).fetchall()
        return {row[0] for row in rows}

    def status_counts(self, campaign_id: int) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) FROM campaign_leads WHERE campaign_id = ? GROUP BY status",
            (campaign_id,),
        ).fetchall()
        return {status: count for status, count in rows}

    def upsert_observation(self, campaign_id: int, observation: "UserObservation", *, status: str) -> None:
        """Идемпотентно: повторный импорт тех же сообщений не увеличивает
        match_count — учитываются только сообщения за пределами уже
        записанного диапазона [first_message_id, last_message_id]."""
        existing = self.get(campaign_id, observation.user_id)
        info = observation.info
        identity = {
            "username": info.username if info else None,
            "access_hash": info.access_hash if info else None,
            "display_name": info.full_name if info else None,
        }
        if existing is None:
            self._conn.execute(
                f"""
                INSERT INTO campaign_leads ({_LEAD_COLUMNS}, username, access_hash, display_name)
                VALUES (:campaign_id, :user_id, :source_chat_id, :source_ref, :matched_keyword,
                        :status, :match_count, :first_message_id, :first_message_at,
                        :last_message_id, :last_message_at, :username, :access_hash, :display_name)
                """,
                {
                    "campaign_id": campaign_id, "user_id": observation.user_id,
                    "source_chat_id": observation.source_chat_id, "source_ref": observation.source_ref,
                    "matched_keyword": observation.matched_keyword, "status": status,
                    "match_count": len(observation.message_ids),
                    "first_message_id": observation.first_message_id,
                    "first_message_at": _isoformat(observation.first_message_at),
                    "last_message_id": observation.last_message_id,
                    "last_message_at": _isoformat(observation.last_message_at),
                    **identity,
                },
            )
            self._conn.commit()
            return

        low = existing.first_message_id or 0
        high = existing.last_message_id or 0
        added = sum(1 for mid in observation.message_ids if mid < low or mid > high)
        first_id, first_at = existing.first_message_id, existing.first_message_at
        if first_id is None or observation.first_message_id < first_id:
            first_id, first_at = observation.first_message_id, observation.first_message_at
        last_id, last_at = existing.last_message_id, existing.last_message_at
        if last_id is None or observation.last_message_id > last_id:
            last_id, last_at = observation.last_message_id, observation.last_message_at
        # bot — свойство самого аккаунта, не "понижается" обратно в new.
        new_status = existing.status if existing.status == LEAD_STATUS_BOT else status
        self._conn.execute(
            """
            UPDATE campaign_leads
            SET status = :status, match_count = match_count + :added,
                first_message_id = :first_id, first_message_at = :first_at,
                last_message_id = :last_id, last_message_at = :last_at,
                username = COALESCE(:username, username),
                access_hash = COALESCE(:access_hash, access_hash),
                display_name = COALESCE(:display_name, display_name),
                updated_at = CURRENT_TIMESTAMP
            WHERE campaign_id = :campaign_id AND user_id = :user_id
            """,
            {
                "status": new_status, "added": added,
                "first_id": first_id, "first_at": _isoformat(first_at),
                "last_id": last_id, "last_at": _isoformat(last_at),
                "campaign_id": campaign_id, "user_id": observation.user_id,
                **identity,
            },
        )
        self._conn.commit()

    def mark_members(self, campaign_id: int, member_ids: set[int]) -> int:
        """'new' -> 'already_member' для уже записанных лидов, которые
        оказались участниками target_chat (повторная проверка при каждом
        импорте/обновлении). Только статус, никаких удалений."""
        changed = 0
        for user_id in member_ids:
            cursor = self._conn.execute(
                "UPDATE campaign_leads SET status = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE campaign_id = ? AND user_id = ? AND status = ?",
                (LEAD_STATUS_ALREADY_MEMBER, campaign_id, user_id, LEAD_STATUS_NEW),
            )
            changed += cursor.rowcount
        self._conn.commit()
        return changed

    def get_checkpoint(self, campaign_id: int, source_chat_id: int) -> ScanCheckpoint | None:
        row = self._conn.execute(
            "SELECT campaign_id, source_chat_id, source_ref, last_message_id, messages_scanned, last_scan_at "
            "FROM campaign_lead_scan_state WHERE campaign_id = ? AND source_chat_id = ?",
            (campaign_id, source_chat_id),
        ).fetchone()
        if row is None:
            return None
        return ScanCheckpoint(*row[:5], last_scan_at=_parse_datetime(row[5]))

    def list_checkpoints(self, campaign_id: int) -> list[ScanCheckpoint]:
        rows = self._conn.execute(
            "SELECT campaign_id, source_chat_id, source_ref, last_message_id, messages_scanned, last_scan_at "
            "FROM campaign_lead_scan_state WHERE campaign_id = ? ORDER BY source_chat_id",
            (campaign_id,),
        ).fetchall()
        return [ScanCheckpoint(*row[:5], last_scan_at=_parse_datetime(row[5])) for row in rows]

    def save_checkpoint(
        self, campaign_id: int, source_chat_id: int, *, source_ref: str | None,
        last_message_id: int, messages_scanned: int, at: datetime,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO campaign_lead_scan_state (
                campaign_id, source_chat_id, source_ref, last_message_id, messages_scanned, last_scan_at
            ) VALUES (:campaign_id, :source_chat_id, :source_ref, :last_message_id, :messages_scanned, :at)
            ON CONFLICT(campaign_id, source_chat_id) DO UPDATE SET
                source_ref = excluded.source_ref,
                last_message_id = MAX(campaign_lead_scan_state.last_message_id, excluded.last_message_id),
                messages_scanned = campaign_lead_scan_state.messages_scanned + excluded.messages_scanned,
                last_scan_at = excluded.last_scan_at
            """,
            {
                "campaign_id": campaign_id, "source_chat_id": source_chat_id, "source_ref": source_ref,
                "last_message_id": last_message_id, "messages_scanned": messages_scanned,
                "at": at.isoformat(),
            },
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


# ---- сканирование ----


@dataclass
class UserObservation:
    """Все совпавшие сообщения ОДНОГО отправителя в одном источнике за
    один проход сканирования."""

    user_id: int
    source_chat_id: int
    source_ref: str | None
    matched_keyword: str
    info: TelegramUserInfo | None
    is_deleted: bool
    message_ids: list[int] = field(default_factory=list)
    # Совпавшие словоформы ("страховка", "застраховать") — только одно
    # слово, содержащее keyword, никогда не текст сообщения (см.
    # matched_word_forms) — для проверки качества фильтра оператором.
    forms: Counter = field(default_factory=Counter)
    first_message_id: int = 0
    first_message_at: datetime | None = None
    last_message_id: int = 0
    last_message_at: datetime | None = None

    def add(self, message_id: int, date: datetime | None) -> None:
        if not self.message_ids or message_id < self.first_message_id:
            self.first_message_id, self.first_message_at = message_id, date
        if not self.message_ids or message_id > self.last_message_id:
            self.last_message_id, self.last_message_at = message_id, date
        self.message_ids.append(message_id)


@dataclass
class SourceScan:
    source_ref: str
    source_chat_id: int
    source_title: str | None
    min_message_id: int
    max_message_id: int = 0
    messages_scanned: int = 0
    matching_messages: int = 0
    non_user_matches: int = 0


@dataclass
class ScanResult:
    campaign: InviteCampaign
    sources: list[SourceScan] = field(default_factory=list)
    observations: dict[int, UserObservation] = field(default_factory=dict)
    # None — проверка участников target не выполнялась/не удалась (тогда
    # уже-участники отсеются только при приглашении — UserAlreadyParticipant
    # -> status='joined', существующее поведение InviterService).
    target_member_ids: set[int] | None = None
    target_check_error: str | None = None
    matched_forms: Counter = field(default_factory=Counter)
    # Сообщения/пользователи, которые совпали бы по ПОДСТРОКЕ, но отсеяны
    # правилом кампании (match_rule != substring) — "Астрахань",
    # "канистрах", "на свой страх и риск"... Только для отчёта.
    false_positive_messages: int = 0
    false_positive_forms: Counter = field(default_factory=Counter)
    false_positive_last_at: dict[int, datetime | None] = field(default_factory=dict)

    @property
    def target_check_status(self) -> str:
        return "checked" if self.target_member_ids is not None else "unavailable"

    @property
    def false_positive_users(self) -> set[int]:
        """Пользователи, у которых были ТОЛЬКО отсеянные совпадения."""
        return set(self.false_positive_last_at) - set(self.observations)

    @property
    def messages_scanned(self) -> int:
        return sum(s.messages_scanned for s in self.sources)

    @property
    def matching_messages(self) -> int:
        return sum(s.matching_messages for s in self.sources)


def campaign_matcher(campaign: InviteCampaign) -> KeywordMatcher:
    """KeywordMatcher с ОДНИМ keyword кампании — та же семантика
    (lower().strip(), подстрока), что и у сценариев Reader/history_sync."""
    return KeywordMatcher([Scenario(name=campaign.slug or campaign.name, enabled=True, keywords=(campaign.keyword,))])


def matched_word_forms(text: str, keyword: str) -> list[str]:
    """Слова текста, содержащие keyword — та же нормализация, что и у
    KeywordMatcher (lower().strip()). Только для отчёта: решение о
    совпадении по-прежнему принимает KeywordMatcher."""
    normalized_keyword = keyword.lower().strip()
    if not normalized_keyword:
        return []
    return re.findall(rf"\w*{re.escape(normalized_keyword)}\w*", (text or "").lower().strip())


def _ru_insurance_words(text: str, keyword: str) -> list[str]:
    """Слова (целиком, по границам слова), где keyword — корень со
    страховой морфологией: за корнем идёт "ов…" (страховка, страхование,
    застрахован, автостраховка, медстраховка, страховщик) или "у" + ещё
    буквы (страхуйтесь, застрахуй). Любая приставка допустима (за-, авто-,
    мед-), КРОМЕ "пере-": "перестраховаться"/"перестраховка" в источнике —
    идиома "подстраховаться" (проверено по реальным сообщениям @sadahlo),
    а не страхование. Не совпадают: "Астрахань"/"астрахани" ("страх" +
    "ань"), "канистрах" (ничего после корня), голое "страх"/"страха"/
    "страхом" ("на свой страх и риск" — страх, а не страховка)."""
    root = keyword.lower().strip()
    if not root:
        return []
    pattern = re.compile(rf"\w*{re.escape(root)}(?:ов\w*|у\w+)")
    words = []
    for word in re.findall(r"\w+", (text or "").lower()):
        if not pattern.fullmatch(word):
            continue
        if word[: word.find(root)].endswith("пере"):
            continue
        words.append(word)
    return words


def campaign_match_words(campaign: InviteCampaign, text: str) -> list[str]:
    """Совпавшие слова сообщения по правилу кампании (см.
    InviteCampaign.match_rule); пустой список — сообщение не совпало."""
    rule = campaign.match_rule or MATCH_RULE_SUBSTRING
    if rule == MATCH_RULE_SUBSTRING:
        if not campaign_matcher(campaign).match(text or ""):
            return []
        return matched_word_forms(text, campaign.keyword) or [campaign.keyword.lower().strip()]
    if rule == MATCH_RULE_RU_INSURANCE:
        return _ru_insurance_words(text, campaign.keyword)
    raise ValueError(f"Неизвестное match_rule кампании: {rule!r}")


def marked_chat_id(entity) -> int:
    return utils.get_peer_id(entity)


class MemberListUnavailableError(Exception):
    """Список участников target не виден этому аккаунту (скрыт настройкой
    группы — тогда Telegram отдаёт пустой список даже участнику)."""


async def fetch_member_ids(client, target_ref: str) -> set[int]:
    """Только чтение (iter_participants) — тот же способ, что и
    reader/users/sync.py для списка участников групп. Пустой результат —
    НЕ "0 участников": у группы с участниками он означает, что список
    скрыт (participants_hidden) и аккаунт не админ — такая проверка
    считается невыполненной."""
    entity = await client.get_entity(target_ref)
    members = {participant.id async for participant in client.iter_participants(entity)}
    if not members:
        raise MemberListUnavailableError(
            f"список участников {target_ref} пуст/скрыт для этого аккаунта — "
            "нужен аккаунт-админ target (--membership-account-id)"
        )
    return members


async def scan_campaign_sources(
    client,
    campaign: InviteCampaign,
    checkpoints: dict[str, int] | None = None,
    *,
    member_client=None,
) -> ScanResult:
    """Читает историю каждого source_chats кампании начиная ПОСЛЕ
    checkpoint (message_id, по возрастанию; 0/нет — вся история, тот же
    диапазон, что и у history_sync), собирает отправителей совпавших
    сообщений. Ничего не пишет ни в Telegram, ни в БД.

    checkpoints — {source_ref: last_message_id}, см. load_checkpoints().
    member_client — клиент для проверки участников target_chat (по
    умолчанию тот же client)."""
    if not campaign.source_chats:
        raise ValueError(f"У кампании '{campaign.label}' не задан источник (source_chats).")

    rule = campaign.match_rule or MATCH_RULE_SUBSTRING
    substring_matcher = campaign_matcher(campaign)
    result = ScanResult(campaign=campaign)
    checkpoints = checkpoints or {}

    for source_ref in campaign.source_chats:
        entity = await client.get_entity(source_ref)
        chat_id = marked_chat_id(entity)
        min_id = checkpoints.get(source_ref, 0)
        source = SourceScan(
            source_ref=source_ref, source_chat_id=chat_id,
            source_title=getattr(entity, "title", None), min_message_id=min_id,
        )
        result.sources.append(source)

        async for message in client.iter_messages(entity, min_id=min_id, reverse=True):
            source.messages_scanned += 1
            source.max_message_id = max(source.max_message_id, message.id)
            text = message.raw_text or ""
            forms = campaign_match_words(campaign, text)
            if not forms:
                if rule != MATCH_RULE_SUBSTRING and substring_matcher.match(text):
                    result.false_positive_messages += 1
                    result.false_positive_forms.update(matched_word_forms(text, campaign.keyword))
                    if isinstance(message.sender, User):
                        previous = result.false_positive_last_at.get(message.sender.id)
                        if previous is None or (message.date and message.date > previous):
                            result.false_positive_last_at[message.sender.id] = message.date
                continue
            source.matching_messages += 1

            sender = message.sender
            # Только реальные пользователи: посты от имени канала/анонимных
            # админов (sender — Channel или None) лидами не являются.
            if not isinstance(sender, User):
                source.non_user_matches += 1
                continue

            observation = result.observations.get(sender.id)
            if observation is None:
                observation = UserObservation(
                    user_id=sender.id, source_chat_id=chat_id, source_ref=source_ref,
                    matched_keyword=campaign.keyword,
                    info=TelegramUserInfo.from_telethon_user(sender),
                    is_deleted=bool(getattr(sender, "deleted", False)),
                )
                result.observations[sender.id] = observation
            observation.add(message.id, message.date)
            observation.forms.update(forms)
            result.matched_forms.update(forms)

    try:
        result.target_member_ids = await fetch_member_ids(member_client or client, campaign.target_chat)
    except Exception as exc:
        result.target_check_error = f"{type(exc).__name__}: {exc}"
        logger.warning("Не удалось получить участников %s: %s", campaign.target_chat, result.target_check_error)

    return result


def load_checkpoints(lead_repository: CampaignLeadRepository, campaign: InviteCampaign) -> dict[str, int]:
    return {
        cp.source_ref: cp.last_message_id
        for cp in lead_repository.list_checkpoints(campaign.id)
        if cp.source_ref
    }


# ---- классификация / превью ----


PREVIEW_ELIGIBLE = "eligible"
PREVIEW_ELIGIBLE_NO_USERNAME = "eligible_no_username"
PREVIEW_ALREADY_KNOWN = "already_known"
PREVIEW_ALREADY_MEMBER = "already_member"
PREVIEW_BOT = "bot"
PREVIEW_DELETED = "deleted"
PREVIEW_TOO_OLD = "too_old"


def recency_cutoff(campaign: InviteCampaign, now: datetime, max_age_days: int | None = None) -> datetime | None:
    """max_age_days (явный override, например из CLI) или
    campaign.lead_max_age_days; None — без ограничения."""
    days = max_age_days if max_age_days is not None else campaign.lead_max_age_days
    return now - timedelta(days=days) if days else None


def lead_status_for(observation: UserObservation, target_member_ids: set[int] | None) -> str:
    if observation.info is not None and observation.info.is_bot:
        return LEAD_STATUS_BOT
    if observation.is_deleted:
        return LEAD_STATUS_DELETED
    if target_member_ids is not None and observation.user_id in target_member_ids:
        return LEAD_STATUS_ALREADY_MEMBER
    return LEAD_STATUS_NEW


@dataclass(frozen=True)
class PreviewRow:
    user_id: int
    username: str | None
    display_name: str | None
    source: str
    first_message_at: datetime | None
    last_message_at: datetime | None
    matching_messages: int
    matched_keyword: str
    campaign: str
    status: str
    matched_forms: str = ""


@dataclass
class ScanSummary:
    messages_scanned: int
    matching_messages: int
    non_user_matches: int
    unique_users: int
    already_known: int
    already_member: int
    bots: int
    deleted: int
    eligible: int
    eligible_without_username: int
    target_checked: bool
    target_check_error: str | None
    rows: list[PreviewRow]
    top_forms: list[tuple[str, int]] = field(default_factory=list)
    too_old: int = 0
    false_positive_users: int = 0
    cutoff: datetime | None = None

    @property
    def target_check_status(self) -> str:
        return "checked" if self.target_checked else "unavailable"

    def match_distribution(self) -> dict[str, int]:
        """Пригодные (с username и без) по числу совпавших сообщений."""
        buckets = {"1": 0, "2-5": 0, "6-10": 0, "11-20": 0, "21+": 0}
        for row in self.rows:
            if row.status not in (PREVIEW_ELIGIBLE, PREVIEW_ELIGIBLE_NO_USERNAME):
                continue
            n = row.matching_messages
            key = "1" if n == 1 else "2-5" if n <= 5 else "6-10" if n <= 10 else "11-20" if n <= 20 else "21+"
            buckets[key] += 1
        return buckets


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def summarize_scan(
    result: ScanResult, *, known_user_ids: set[int], since: datetime | None = None,
) -> ScanSummary:
    """known_user_ids — уже есть в пуле этой кампании или в её
    user_campaign_invites (см. known_campaign_user_ids()). Только ЭТА
    кампания: участие пользователя в другой кампании (Грузия) его отсюда
    не исключает.

    since — окно давности (см. recency_cutoff): пользователь, чьё
    ПОСЛЕДНЕЕ совпавшее сообщение старше since, получает статус too_old и
    не входит в остальные счётчики (unique_users — только в окне)."""
    since = _as_utc(since)
    counts = dict.fromkeys(
        (PREVIEW_ELIGIBLE, PREVIEW_ELIGIBLE_NO_USERNAME, PREVIEW_ALREADY_KNOWN,
         PREVIEW_ALREADY_MEMBER, PREVIEW_BOT, PREVIEW_DELETED, PREVIEW_TOO_OLD), 0,
    )
    rows = []
    for observation in sorted(result.observations.values(), key=lambda o: o.last_message_id, reverse=True):
        lead_status = lead_status_for(observation, result.target_member_ids)
        last_at = _as_utc(observation.last_message_at)
        too_old = since is not None and (last_at is None or last_at < since)
        if too_old:
            preview = PREVIEW_TOO_OLD
        elif lead_status == LEAD_STATUS_BOT:
            preview = PREVIEW_BOT
        elif lead_status == LEAD_STATUS_DELETED:
            preview = PREVIEW_DELETED
        elif lead_status == LEAD_STATUS_ALREADY_MEMBER:
            preview = PREVIEW_ALREADY_MEMBER
        elif observation.user_id in known_user_ids:
            preview = PREVIEW_ALREADY_KNOWN
        elif not (observation.info and observation.info.username):
            preview = PREVIEW_ELIGIBLE_NO_USERNAME
        else:
            preview = PREVIEW_ELIGIBLE
        counts[preview] += 1
        info = observation.info
        rows.append(PreviewRow(
            user_id=observation.user_id,
            username=info.username if info else None,
            display_name=info.full_name if info else None,
            source=observation.source_ref or str(observation.source_chat_id),
            first_message_at=observation.first_message_at,
            last_message_at=observation.last_message_at,
            matching_messages=len(observation.message_ids),
            matched_keyword=observation.matched_keyword,
            campaign=result.campaign.slug or result.campaign.name,
            status=preview,
            matched_forms="; ".join(form for form, _n in observation.forms.most_common(3)),
        ))
    fp_users = {
        user_id for user_id in result.false_positive_users
        if since is None or (_as_utc(result.false_positive_last_at[user_id]) or since) >= since
    }
    return ScanSummary(
        messages_scanned=result.messages_scanned,
        matching_messages=result.matching_messages,
        non_user_matches=sum(s.non_user_matches for s in result.sources),
        unique_users=len(result.observations) - counts[PREVIEW_TOO_OLD],
        already_known=counts[PREVIEW_ALREADY_KNOWN],
        already_member=counts[PREVIEW_ALREADY_MEMBER],
        bots=counts[PREVIEW_BOT],
        deleted=counts[PREVIEW_DELETED],
        eligible=counts[PREVIEW_ELIGIBLE],
        eligible_without_username=counts[PREVIEW_ELIGIBLE_NO_USERNAME],
        target_checked=result.target_member_ids is not None,
        target_check_error=result.target_check_error,
        rows=rows,
        top_forms=result.matched_forms.most_common(30),
        too_old=counts[PREVIEW_TOO_OLD],
        false_positive_users=len(fp_users),
        cutoff=since,
    )


RECENCY_WINDOWS = (("30d", 30), ("90d", 90), ("180d", 180), ("2026", None), ("all", None))


def recency_report(
    result: ScanResult, *, known_user_ids: set[int], now: datetime,
) -> list[tuple[str, datetime | None, ScanSummary]]:
    """Тот же summarize_scan для нескольких окон давности (только
    анализ — ничего не меняет в кампании)."""
    report = []
    for label, days in RECENCY_WINDOWS:
        if label == "2026":
            since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        elif days is not None:
            since = now - timedelta(days=days)
        else:
            since = None
        report.append((label, since, summarize_scan(result, known_user_ids=known_user_ids, since=since)))
    return report


def format_recency_report(report, *, target_checked: bool) -> str:
    header = "window | since | unique users | eligible w/ username | eligible w/o username | bots/deleted | false-positive users excluded | already target members"
    lines = ["Recency comparison (by LAST matching message):", header]
    for label, since, s in report:
        members = str(s.already_member) if target_checked else "unavailable"
        lines.append(
            f"{label} | {since.date() if since else '—'} | {s.unique_users} | {s.eligible} | "
            f"{s.eligible_without_username} | {s.bots}/{s.deleted} | {s.false_positive_users} | {members}"
        )
    return "\n".join(lines)


def known_campaign_user_ids(db_path: Path, campaign_id: int) -> set[int]:
    """Пул ЭТОЙ кампании + её user_campaign_invites (если таблица уже
    есть). Только чтение."""
    conn = sqlite3.connect(Path(db_path))
    try:
        ids = {row[0] for row in conn.execute(
            "SELECT user_id FROM campaign_leads WHERE campaign_id = ?", (campaign_id,),
        )}
        has_invites = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_campaign_invites'",
        ).fetchone()
        if has_invites:
            ids |= {row[0] for row in conn.execute(
                "SELECT user_id FROM user_campaign_invites WHERE campaign_id = ?", (campaign_id,),
            )}
        return ids
    finally:
        conn.close()


_PREVIEW_FIELDS = (
    "telegram_user_id", "username", "display_name", "source", "first_message_date",
    "last_message_date", "matching_messages", "matched_keyword", "matched_forms", "campaign", "status",
)


def write_preview_csv(rows: list[PreviewRow], path: Path) -> Path:
    """Только поля, которые инвайтер и так показывает в своих логах
    (user_id/username/имя) + evidence сканирования; ни телефонов, ни
    текстов сообщений."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(_PREVIEW_FIELDS)
        for row in rows:
            writer.writerow([
                row.user_id, f"@{row.username}" if row.username else "", row.display_name or "",
                row.source, _isoformat(row.first_message_at) or "", _isoformat(row.last_message_at) or "",
                row.matching_messages, row.matched_keyword, row.matched_forms, row.campaign, row.status,
            ])
    return path


def format_summary(campaign: InviteCampaign, summary: ScanSummary, *, mode: str) -> str:
    target_line = (
        f"{summary.already_member} (checked)" if summary.target_checked
        else f"unavailable — not checked ({summary.target_check_error})"
    )
    distribution = ", ".join(f"{k}: {v}" for k, v in summary.match_distribution().items())
    return "\n".join([
        f"[{mode}]",
        f"Campaign: {campaign.label}",
        f"Source: {', '.join(campaign.source_chats)}",
        f"Target: {campaign.target_chat}",
        f"Filter: {campaign.keyword} (match_rule={campaign.match_rule or MATCH_RULE_SUBSTRING})",
        f"Recency: {f'last match since {summary.cutoff:%Y-%m-%d}' if summary.cutoff else 'no limit'}",
        "",
        f"Messages scanned: {summary.messages_scanned}",
        f"Matching messages: {summary.matching_messages}",
        f"  of them not from a user (channel/anonymous admin): {summary.non_user_matches}",
        f"Unique users: {summary.unique_users}",
        f"Already known in this campaign: {summary.already_known}",
        f"Already in target group: {target_line}",
        f"Bots: {summary.bots}",
        f"Deleted accounts: {summary.deleted}",
        f"Eligible new leads: {summary.eligible}",
        f"Eligible without username (resolved via their source message): {summary.eligible_without_username}",
        f"Older than recency window: {summary.too_old}",
        f"False-positive users excluded by match_rule: {summary.false_positive_users}",
        f"Eligible by number of matching messages: {distribution}",
        "",
        "Top matched word forms (word containing the keyword, not message text):",
        *[f"  {form}: {count}" for form, count in summary.top_forms],
    ])


# ---- импорт (явная запись в БД) ----


@dataclass(frozen=True)
class ImportOutcome:
    leads_written: int
    members_marked: int


def _ensure_user_row(user_repository, info: TelegramUserInfo) -> None:
    """users — общая таблица, из которой кампания Грузии выбирает кандидатов
    (ORDER BY last_seen_at DESC, обязательны username и access_hash).
    Импорт пула НИКОГДА не меняет существующие строки: upsert() сдвинул бы
    last_seen_at (переставил бы очередь Грузии), а дописанный username/
    access_hash сделал бы часть пользователей НОВЫМИ кандидатами Грузии.
    username/access_hash из сканирования хранятся в campaign_leads и
    используются только для кампании пула (см. repository.py
    _EFFECTIVE_USERNAME). Создаётся только отсутствующая строка (без
    keywords — Грузию она не затрагивает)."""
    if user_repository.get(info.user_id) is None:
        user_repository.upsert(info)


def import_scan(
    result: ScanResult,
    lead_repository: CampaignLeadRepository,
    user_repository,
    *,
    now: datetime,
) -> ImportOutcome:
    """Записывает результат scan_campaign_sources() — идемпотентно (см.
    CampaignLeadRepository.upsert_observation), затем сдвигает checkpoint
    каждого источника до последнего прочитанного message_id. Отправитель
    сохраняется в users тем же UserRepository.upsert, что и у
    history_sync (username/access_hash нужны инвайтеру для резолва).
    НЕ включает кампанию и НЕ создаёт user_campaign_invites."""
    campaign = result.campaign
    written = 0
    for observation in result.observations.values():
        if observation.info is not None:
            _ensure_user_row(user_repository, observation.info)
        lead_repository.upsert_observation(
            campaign.id, observation, status=lead_status_for(observation, result.target_member_ids),
        )
        written += 1

    members_marked = 0
    if result.target_member_ids is not None:
        members_marked = lead_repository.mark_members(campaign.id, result.target_member_ids)

    for source in result.sources:
        lead_repository.save_checkpoint(
            campaign.id, source.source_chat_id, source_ref=source.source_ref,
            last_message_id=max(source.max_message_id, source.min_message_id),
            messages_scanned=source.messages_scanned, at=now,
        )
    return ImportOutcome(leads_written=written, members_marked=members_marked)
