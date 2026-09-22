"""InviterAdminService — вся не-Telethon-специфичная логика админ-бота
поверх УЖЕ существующих reader/inviter/ репозиториев (см. design report:
"не переписывать существующий inviter и не дублировать его бизнес-
логику"). Ничего из выборки/лимитов/identity здесь не реализуется заново —
только композиция уже существующих repository-методов и
reader/inviter/identity.py функций в форму, удобную для conversation.py/
texts.py (см. каждый метод про то, что именно он переиспользует)."""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from reader.inviter.identity import (
    AccountIdentityMismatchError,
    SessionNotAuthorizedError,
    fetch_telegram_identity,
    reconcile_account_identity,
)
from reader.inviter.manage import resolve_all_duplicates, sync_accounts
from reader.inviter.models import TelegramAccount
from reader.inviter.repository import (
    InviteCampaignRepository,
    TelegramAccountRepository,
    UserCampaignInviteRepository,
)
from reader.inviter.runtime_state_repository import InviterRuntimeStateRepository
from reader.inviter_admin_bot.auth import TelethonAuthClientLike
from reader.inviter_admin_bot.models import (
    AccountCard,
    AccountListEntry,
    AccountStatusEntry,
    AccountUsage,
    AttentionItem,
    LimitListEntry,
    StatusSnapshot,
    SyncAccountOutcome,
    SyncSummary,
)

logger = logging.getLogger(__name__)

# Тот же порог, что неявно используется InviterWorker (poll_interval_seconds,
# см. settings.inviter.worker) — worker_alive/next_tick_at считаются ниже по
# ЭТОМУ значению, передаваемому вызывающим кодом (main.py), а не хардкодом
# здесь: если poll_interval в config.yaml изменится, статус тут же
# пересчитается верно без правки этого файла.
_HEARTBEAT_GRACE_MULTIPLIER = 2


def _display_name(account: TelegramAccount) -> str:
    """"@username", если он реально известен (account.name начинается с
    "@" — реальный синхронизированный username, см. reader/inviter/
    identity.py::reconcile_account_identity), иначе "Telegram ID N" (см.
    design: "Если username отсутствует... а не падать"), иначе (даже
    telegram_user_id ещё не подтверждён) — сырой account.name (например,
    временный "tg_<phone>" слаг сразу после создания, до первой
    синхронизации identity)."""
    if account.name.startswith("@"):
        return account.name
    if account.telegram_user_id is not None:
        return f"Telegram ID {account.telegram_user_id}"
    return account.name


def _session_file_path(account: TelegramAccount) -> Path:
    """Тот же путь, что и InviterService/reader/inviter/service.py::
    _session_file_path (Telethon сам дописывает ".session") — не
    импортируется оттуда напрямую (имя приватное, ведущее подчёркивание) —
    та же конвенция "каждый модуль строит этот путь сам", что уже
    используется между reader/inviter/authorize.py и reader/inviter/manage.py
    для TelegramClient(...)."""
    return Path(f"{account.session_path}.session")


def _account_usage(invite_repository: UserCampaignInviteRepository, account: TelegramAccount) -> AccountUsage:
    """daily_limit - joined_today - pending_today — ТА ЖЕ формула, что и
    InviterService._remaining_daily_budget (см. reader/inviter/service.py) —
    переиспользует ТЕ ЖЕ repository-методы (count_today_joined/
    count_today_pending), не пересчитывает их сама."""
    joined = invite_repository.count_today_joined(account.id)
    pending = invite_repository.count_today_pending(account.id)
    return AccountUsage(sent_today=joined + pending, daily_limit=account.daily_limit)


def _today_start_utc() -> datetime:
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def _as_utc(value: datetime) -> datetime:
    """Naive datetime (как приходит из SQLite CURRENT_TIMESTAMP через
    репозитории reader/inviter/) трактуется как UTC ЯВНО — тот же приём,
    что и у reader/time_display.py::to_tbilisi."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class InviterAdminService:
    def __init__(
        self,
        account_repository: TelegramAccountRepository,
        campaign_repository: InviteCampaignRepository,
        invite_repository: UserCampaignInviteRepository,
        runtime_state_repository: InviterRuntimeStateRepository,
        *,
        db_path: Path,
        trusted_admin_user_ids: frozenset[int] = frozenset(),
        worker_poll_interval_seconds: int = 600,
    ):
        self._accounts = account_repository
        self._campaigns = campaign_repository
        self._invites = invite_repository
        self._runtime_state = runtime_state_repository
        # sync_accounts() (reader/inviter/manage.py) открывает и закрывает
        # СВОЁ СОБСТВЕННОЕ соединение TelegramAccountRepository(db_path) —
        # ей нужен путь, а не уже открытый self._accounts (см.
        # sync_all_accounts). Тот же users_db_file, что и у остальных
        # репозиториев этого сервиса.
        self._db_path = db_path
        self._trusted_admin_user_ids = frozenset(trusted_admin_user_ids)
        self._worker_poll_interval_seconds = worker_poll_interval_seconds

    # ---- доступ (см. design "ДОСТУП К ADMIN BOT") ----

    def is_trusted(self, telegram_user_id: int) -> bool:
        """ТОЛЬКО numeric telegram_user_id (см. design "КЛЮЧЕВОЕ ПРАВИЛО
        IDENTITY"/"ДОСТУП К ADMIN BOT") — тот же security-инвариант, что и
        у reader/public_bot/conversation.py::_is_trusted."""
        return telegram_user_id in self._trusted_admin_user_ids

    # ---- 👤 Аккаунты ----

    def _current_accounts(self) -> list[TelegramAccount]:
        """Все ОБЫЧНЫЕ operational-экраны (👤 Аккаунты/⚙️ Лимиты/📊 Статус)
        по умолчанию показывают ТОЛЬКО is_old=False (см. design "History-
        only records не должны выглядеть как обычные рабочие accounts") —
        is_old=True записи НИКУДА из БД не удаляются (см.
        resolve_duplicate_group), просто исключены из этих списков.
        account_card()/get_account() (прямой доступ по id) этот фильтр
        сознательно НЕ применяют — они не участвуют в rotation и не
        показывают ничего разрушительного сами по себе."""
        return [a for a in self._accounts.list() if not a.is_old]

    def list_accounts(self) -> list[AccountListEntry]:
        return [
            AccountListEntry(id=a.id, display_name=_display_name(a), enabled=a.enabled)
            for a in self._current_accounts()
        ]

    def get_account(self, account_id: int) -> TelegramAccount | None:
        return self._accounts.get(account_id)

    # ---- ⚙️ Лимиты ----

    def list_accounts_for_limits(self) -> list[LimitListEntry]:
        """Список для "⚙️ Лимиты" (см. design: показывать USED / daily_limit
        каждого аккаунта, НЕ enabled) — тот же _current_accounts(), что и
        list_accounts() (is_old=False), плюс _account_usage() (ТА ЖЕ
        формула, что и account_card()/InviterService._remaining_daily_budget
        — joined_today + pending_today), никакого нового счётчика."""
        return [
            LimitListEntry(
                id=a.id, display_name=_display_name(a),
                sent_today=_account_usage(self._invites, a).sent_today,
                daily_limit=a.daily_limit,
            )
            for a in self._current_accounts()
        ]

    def account_card(self, account_id: int) -> AccountCard | None:
        account = self._accounts.get(account_id)
        if account is None:
            return None
        return AccountCard(
            account=account,
            display_name=_display_name(account),
            session_exists=_session_file_path(account).exists(),
            usage=_account_usage(self._invites, account),
        )

    def toggle_enabled(self, account_id: int) -> TelegramAccount | None:
        """🟢/⚪ переключатель (см. design "Нажатие должно включать/
        выключать account.enabled") — TelegramAccountRepository.update()
        уже существует, отдельного enable()/disable() метода заводить не
        нужно (см. аудит: "нет отдельных методов — всё через generic
        update(**fields)").

        FAIL-CLOSED для is_old=True (см. design "Нельзя включить OLD
        account" — даже если вручную сформировать callback с чужим/старым
        account_id: это проверяется здесь, а не только скрытием из
        списков, см. _current_accounts) — возвращает account БЕЗ изменений
        (не None — account найден, просто переключение отклонено).
        conversation.py различает этот случай по account.is_old в
        возвращённом объекте."""
        account = self._accounts.get(account_id)
        if account is None:
            return None
        if account.is_old:
            return account
        return self._accounts.update(account_id, enabled=not account.enabled)

    def set_daily_limit(self, account_id: int, value: int) -> TelegramAccount | None:
        if value < 1:
            raise ValueError("daily_limit должен быть положительным числом")
        account = self._accounts.get(account_id)
        if account is None:
            return None
        return self._accounts.update(account_id, daily_limit=value)

    # ---- 🔄 Синхронизировать / 🔄 Проверить (один аккаунт) ----

    async def sync_one_account(
        self, account_id: int, client_factory: Callable[[TelegramAccount], TelethonAuthClientLike],
    ) -> SyncAccountOutcome | None:
        """Карточка -> "🔄 Проверить/синхронизировать" — ОДИН аккаунт, теми
        же примитивами, что и sync_all_accounts()/reader/inviter/manage.py::
        sync_accounts(), но без затрагивания остальных (см. design ERROR
        HANDLING: "один broken account не должен валить весь Admin Bot" —
        здесь тем более не должен трогать другие аккаунты вовсе)."""
        account = self._accounts.get(account_id)
        if account is None:
            return None

        client = client_factory(account)
        try:
            await client.connect()
        except Exception as exc:
            logger.warning("Sync: connect() не удался для account_id=%s: %s", account_id, type(exc).__name__)
            return SyncAccountOutcome(account_id, _display_name(account), "connect_failed", "Session error")

        try:
            try:
                identity = await fetch_telegram_identity(client)
            except SessionNotAuthorizedError:
                return SyncAccountOutcome(
                    account_id, _display_name(account), "not_authorized", "Требуется повторная авторизация",
                )

            try:
                updated = reconcile_account_identity(self._accounts, account, identity)
            except AccountIdentityMismatchError:
                return SyncAccountOutcome(
                    account_id, _display_name(account), "identity_mismatch",
                    "Сессия авторизована под другим аккаунтом",
                )
            # last_synced_at — ОТДЕЛЬНОЕ поле от того, что пишет
            # reconcile_account_identity (см. TelegramAccount.last_synced_at) —
            # намеренно не встроено в саму identity.py, чтобы не менять
            # поведение reader/inviter/manage.py sync-accounts CLI.
            updated = self._accounts.update(updated.id, last_synced_at=datetime.now(timezone.utc))
        finally:
            try:
                await client.disconnect()
            except Exception:
                logger.warning("Sync: disconnect() не удался для account_id=%s.", account_id)

        status = "username_updated" if updated.name != account.name else "unchanged"
        return SyncAccountOutcome(account_id, _display_name(updated), status)

    async def sync_all_accounts(
        self, client_factory: Callable[[TelegramAccount], TelethonAuthClientLike],
    ) -> SyncSummary:
        """🔄 Синхронизировать (главное меню) — переиспользует
        reader/inviter/manage.py::sync_accounts()/resolve_all_duplicates()
        НАПРЯМУЮ (см. design report "USERNAME SYNC" — та же identity-
        логика, никакой второй реализации), просто пересобирает уже
        готовые AccountSyncResult в форму для texts.py."""
        results = await sync_accounts(self._db_path, client_factory=client_factory)
        resolve_all_duplicates(self._accounts)

        # last_synced_at — ОТДЕЛЬНОЕ поле, которое sync_accounts() (см.
        # reader/inviter/manage.py) не знает и не обязано знать (см.
        # sync_one_account про то же самое) — проставляется здесь, ПОСЛЕ
        # успешной identity-проверки (updated/unchanged), отдельным
        # проходом, не меняя саму sync_accounts().
        now = datetime.now(timezone.utc)
        for r in results:
            if r.status in ("updated", "unchanged"):
                self._accounts.update(r.account_id, last_synced_at=now)

        accounts_by_id = {a.id: a for a in self._accounts.list()}
        outcomes = []
        for r in results:
            account = accounts_by_id.get(r.account_id)
            outcomes.append(SyncAccountOutcome(
                r.account_id, _display_name(account) if account else r.name, r.status, r.detail,
            ))
        authorized = sum(1 for r in results if r.status in ("updated", "unchanged"))
        username_updated = sum(1 for r in results if r.status == "updated")
        needs_attention = sum(1 for r in results if r.status in ("connect_failed", "not_authorized", "identity_mismatch"))
        return SyncSummary(
            checked=len(results), authorized=authorized,
            needs_attention=needs_attention, username_updated=username_updated,
            results=tuple(outcomes),
        )

    # ---- ▶️/⏸ глобальный pause/resume (см. design про inviter_enabled) ----

    def set_global_enabled(self, enabled: bool) -> bool:
        return self._runtime_state.set_enabled(enabled).inviter_enabled

    def is_global_enabled(self) -> bool:
        return self._runtime_state.get().inviter_enabled

    # ---- 📊 Статус ----

    def list_account_statuses(self) -> list[AccountStatusEntry]:
        """Список для "📊 Статус" (см. design: "статус КАЖДОГО Telegram-
        аккаунта... enabled и blocked — два разных состояния, не
        смешивать") — is_blocked считается ТОЙ ЖЕ _is_blocked(), что и
        status_snapshot()/_needs_attention() ниже, никакого нового понятия
        "статус" не вводится (см. design "Не придумывать новый статус":
        только account.enabled + account.blocked_until/blocked_reason,
        уже существующие поля TelegramAccount). is_old=True — тот же
        _current_accounts(), что и list_accounts()/list_accounts_for_limits
        (см. design "History-only records не должны выглядеть как обычные
        рабочие accounts")."""
        now = datetime.now(timezone.utc)
        return [
            AccountStatusEntry(
                display_name=_display_name(a),
                enabled=a.enabled,
                is_blocked=_is_blocked(a, now),
                blocked_until=a.blocked_until,
                blocked_reason=a.blocked_reason,
                sent_today=_account_usage(self._invites, a).sent_today,
                daily_limit=a.daily_limit,
            )
            for a in self._current_accounts()
        ]

    def status_snapshot(self) -> StatusSnapshot:
        accounts = self._accounts.list()
        now = datetime.now(timezone.utc)

        active = sum(1 for a in accounts if a.enabled and not _is_blocked(a, now))
        disabled = sum(1 for a in accounts if not a.enabled)
        blocked = sum(1 for a in accounts if a.enabled and _is_blocked(a, now))

        today_start = _today_start_utc()
        all_invites = self._invites.list()
        # created_at приходит naive (см. UserCampaignInviteRepository._parse_datetime —
        # SQLite CURRENT_TIMESTAMP без offset) — трактуется как UTC явно,
        # тот же принцип, что и у reader/time_display.py::to_tbilisi.
        today_invites = [
            inv for inv in all_invites if inv.created_at and _as_utc(inv.created_at) >= today_start
        ]
        sent_today = sum(1 for inv in today_invites if inv.status in ("pending", "joined"))
        pending_today = sum(1 for inv in today_invites if inv.status == "pending")
        failed_today = sum(1 for inv in today_invites if inv.status == "failed")

        campaigns = [c for c in self._campaigns.list() if c.enabled]
        campaign = campaigns[0] if campaigns else None

        runtime_state = self._runtime_state.get()
        worker_alive = (
            runtime_state.last_tick_at is not None
            and (now - runtime_state.last_tick_at) < timedelta(
                seconds=self._worker_poll_interval_seconds * _HEARTBEAT_GRACE_MULTIPLIER,
            )
        )
        next_tick_at = (
            runtime_state.last_tick_at + timedelta(seconds=self._worker_poll_interval_seconds)
            if runtime_state.last_tick_at else None
        )

        attention = tuple(_attention_item(a, now) for a in accounts if _needs_attention(a, now))

        return StatusSnapshot(
            inviter_enabled=runtime_state.inviter_enabled,
            worker_alive=worker_alive,
            last_tick_at=runtime_state.last_tick_at,
            next_tick_at=next_tick_at,
            active_count=active,
            disabled_count=disabled,
            blocked_count=blocked,
            sent_today=sent_today,
            pending_today=pending_today,
            failed_today=failed_today,
            campaign_name=campaign.name if campaign else None,
            campaign_target_chat=campaign.target_chat if campaign else None,
            attention=attention,
        )


def _is_blocked(account: TelegramAccount, now: datetime) -> bool:
    return account.blocked_until is not None and account.blocked_until > now


def _needs_attention(account: TelegramAccount, now: datetime) -> bool:
    if not account.enabled:
        return False
    if _is_blocked(account, now):
        return True
    return not _session_file_path(account).exists()


def _attention_item(account: TelegramAccount, now: datetime) -> AttentionItem:
    if _is_blocked(account, now):
        reason = account.blocked_reason or "blocked"
    elif not _session_file_path(account).exists():
        reason = "session error"
    else:
        reason = "требуется внимание"
    return AttentionItem(display_name=_display_name(account), reason=reason)
