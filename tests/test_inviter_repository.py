"""
Тесты инфраструктуры reader.inviter (этап 1 — только БД/репозитории, без
бизнес-логики и без Telegram): автоматическая миграция, создание таблиц,
идемпотентность повторного открытия БД, CRUD у TelegramAccountRepository /
InviteCampaignRepository / UserCampaignInviteRepository.
"""

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from reader.inviter.repository import (  # noqa: E402
    InviteCampaignRepository,
    TelegramAccountRepository,
    UserCampaignInviteRepository,
)


def _table_names(db_path: Path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return {row[0] for row in rows}
    finally:
        conn.close()


def _index_names(db_path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
        return {row[1] for row in rows}
    finally:
        conn.close()


# ---- автоматическая миграция / создание таблиц ----


def test_opening_repositories_creates_all_three_tables(tmp_path):
    db_path = tmp_path / "inviter.db"
    account_repository = TelegramAccountRepository(db_path)
    campaign_repository = InviteCampaignRepository(db_path)
    invite_repository = UserCampaignInviteRepository(db_path)
    try:
        tables = _table_names(db_path)
        assert {"telegram_accounts", "invite_campaigns", "user_campaign_invites"} <= tables
    finally:
        account_repository.close()
        campaign_repository.close()
        invite_repository.close()


def test_user_campaign_invites_has_required_indexes(tmp_path):
    db_path = tmp_path / "inviter.db"
    repository = UserCampaignInviteRepository(db_path)
    try:
        index_names = _index_names(db_path, "user_campaign_invites")
        assert any("user_id" in name for name in index_names)
        assert any("campaign_id" in name for name in index_names)
        assert any("account_id" in name for name in index_names)
        assert any("status" in name for name in index_names)
    finally:
        repository.close()


def test_reopening_database_does_not_fail_and_preserves_data(tmp_path):
    """Повторный запуск миграции (открытие БД новым процессом/репозиторием)
    не должен ни падать, ни пересоздавать/очищать уже существующие таблицы."""
    db_path = tmp_path / "inviter.db"

    first_repository = TelegramAccountRepository(db_path)
    try:
        account = first_repository.create(
            name="Основной", phone="+995500000001",
            session_name="acc1", session_path="data/sessions/acc1.session",
        )
    finally:
        first_repository.close()

    # Второй репозиторий — как будто отдельный запуск/процесс: миграция
    # (CREATE TABLE IF NOT EXISTS/CREATE INDEX IF NOT EXISTS) выполняется
    # заново, но без ошибок и без потери ранее созданной записи.
    second_repository = TelegramAccountRepository(db_path)
    try:
        assert second_repository.get(account.id) is not None
        assert second_repository.get(account.id).name == "Основной"
    finally:
        second_repository.close()


def test_user_campaign_invites_migrates_legacy_table_without_verified_at(tmp_path):
    """БД, созданная до появления verified_at (подтверждённое вступление,
    см. задачу про pending -> joined), должна получить эту колонку при
    открытии — без пересоздания таблицы и без потери уже существующих
    записей."""
    db_path = tmp_path / "inviter.db"

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE user_campaign_invites (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                campaign_id  INTEGER NOT NULL,
                account_id   INTEGER,
                status       TEXT NOT NULL DEFAULT 'pending',
                error        TEXT,
                invited_at   TIMESTAMP,
                created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "INSERT INTO user_campaign_invites (user_id, campaign_id, status) "
            "VALUES (111, 1, 'invited')"
        )
        conn.commit()
    finally:
        conn.close()

    repository = UserCampaignInviteRepository(db_path)
    try:
        columns = {row[1] for row in repository._conn.execute(
            "PRAGMA table_info(user_campaign_invites)"
        )}
        assert "verified_at" in columns

        invites = repository.list()
        assert len(invites) == 1
        assert invites[0].user_id == 111
        assert invites[0].verified_at is None

        updated = repository.update(invites[0].id, status="joined", verified_at=datetime.now(timezone.utc))
        assert updated.verified_at is not None
    finally:
        repository.close()


def test_telegram_accounts_migrates_legacy_table_without_blocked_columns(tmp_path):
    """БД, созданная до появления blocked_until/blocked_reason (временная
    блокировка аккаунта самим Telegram, см. задачу про повторный FloodWait
    у @Mihailov_vm) и verify_membership (см. TelegramAccount.verify_membership/
    задачу про "Chat admin privileges are required..."), должна получить
    все три колонки при открытии — без пересоздания таблицы и без потери
    уже существующих записей. Именно этот сценарий обеспечивает безопасное
    применение к существующему data/users.db на сервере."""
    db_path = tmp_path / "inviter.db"

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE telegram_accounts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT NOT NULL,
                phone           TEXT NOT NULL,
                session_name    TEXT NOT NULL,
                session_path    TEXT NOT NULL,
                daily_limit     INTEGER NOT NULL DEFAULT 30,
                enabled         BOOLEAN NOT NULL DEFAULT TRUE,
                created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_used_at    TIMESTAMP
            )
            """
        )
        conn.execute(
            "INSERT INTO telegram_accounts (name, phone, session_name, session_path) "
            "VALUES ('Основной', '+995500000001', 'acc1', 'data/sessions/acc1.session')"
        )
        conn.commit()
    finally:
        conn.close()

    repository = TelegramAccountRepository(db_path)
    try:
        columns = {row[1] for row in repository._conn.execute(
            "PRAGMA table_info(telegram_accounts)"
        )}
        assert "blocked_until" in columns
        assert "blocked_reason" in columns
        assert "verify_membership" in columns

        accounts = repository.list()
        assert len(accounts) == 1
        assert accounts[0].name == "Основной"
        assert accounts[0].blocked_until is None
        assert accounts[0].blocked_reason is None
        # DEFAULT 1 — обратная совместимость: у уже существующих аккаунтов
        # проверка pending продолжает работать как раньше (см. задачу).
        assert accounts[0].verify_membership is True

        blocked_until = datetime.now(timezone.utc) + timedelta(hours=5)
        updated = repository.update(
            accounts[0].id, blocked_until=blocked_until, blocked_reason="flood_wait",
        )
        assert updated.blocked_until == blocked_until
        assert updated.blocked_reason == "flood_wait"
    finally:
        repository.close()


def test_telegram_accounts_migrates_existing_table_without_verify_membership(tmp_path):
    """Сценарий, реально ожидаемый на продакшене: таблица УЖЕ содержит
    blocked_until/blocked_reason (из предыдущей миграции), но ещё не имеет
    verify_membership — открытие репозитория должно добавить только её,
    без пересоздания таблицы и без потери существующих записей/значений."""
    db_path = tmp_path / "inviter.db"

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE telegram_accounts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT NOT NULL,
                phone           TEXT NOT NULL,
                session_name    TEXT NOT NULL,
                session_path    TEXT NOT NULL,
                daily_limit     INTEGER NOT NULL DEFAULT 30,
                enabled         BOOLEAN NOT NULL DEFAULT TRUE,
                created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_used_at    TIMESTAMP,
                blocked_until   TIMESTAMP,
                blocked_reason  TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO telegram_accounts (name, phone, session_name, session_path, daily_limit) "
            "VALUES ('@car_ins_account', '+995500000001', 'acc1', 'data/sessions/acc1.session', 24)"
        )
        conn.commit()
    finally:
        conn.close()

    repository = TelegramAccountRepository(db_path)
    try:
        columns = {row[1] for row in repository._conn.execute(
            "PRAGMA table_info(telegram_accounts)"
        )}
        assert "verify_membership" in columns

        accounts = repository.list()
        assert len(accounts) == 1
        assert accounts[0].name == "@car_ins_account"
        assert accounts[0].daily_limit == 24  # существующие значения не потеряны
        assert accounts[0].verify_membership is True  # DEFAULT 1

        updated = repository.update(accounts[0].id, verify_membership=False)
        assert updated.verify_membership is False
    finally:
        repository.close()


def test_telegram_accounts_migrates_existing_table_without_telegram_user_id(tmp_path):
    """Сценарий, реально ожидаемый на продакшене (см. задачу про
    физическую идентичность Telegram-аккаунта): таблица уже содержит все
    прежние колонки, но ещё не имеет telegram_user_id — открытие
    репозитория должно добавить её как NULLABLE, без пересоздания таблицы
    и без потери существующих записей/значений."""
    db_path = tmp_path / "inviter.db"

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE telegram_accounts (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                name                TEXT NOT NULL,
                phone               TEXT NOT NULL,
                session_name        TEXT NOT NULL,
                session_path        TEXT NOT NULL,
                daily_limit         INTEGER NOT NULL DEFAULT 30,
                enabled             BOOLEAN NOT NULL DEFAULT TRUE,
                created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_used_at        TIMESTAMP,
                blocked_until       TIMESTAMP,
                blocked_reason      TEXT,
                verify_membership   INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.execute(
            "INSERT INTO telegram_accounts (name, phone, session_name, session_path, daily_limit) "
            "VALUES ('@Iv_vla_sov', '+995568759201', 'Iv_vla_sov', 'data/sessions/Iv_vla_sov', 24)"
        )
        conn.commit()
    finally:
        conn.close()

    repository = TelegramAccountRepository(db_path)
    try:
        columns = {row[1] for row in repository._conn.execute(
            "PRAGMA table_info(telegram_accounts)"
        )}
        assert "telegram_user_id" in columns

        accounts = repository.list()
        assert len(accounts) == 1
        assert accounts[0].name == "@Iv_vla_sov"
        assert accounts[0].daily_limit == 24  # существующие значения не потеряны
        assert accounts[0].telegram_user_id is None  # NULLABLE, ничего не выдумывается

        updated = repository.update(accounts[0].id, telegram_user_id=8838087889)
        assert updated.telegram_user_id == 8838087889
    finally:
        repository.close()


def test_telegram_accounts_migrates_existing_table_without_is_old_columns(tmp_path):
    """Таблица уже содержит telegram_user_id (из предыдущей миграции), но
    ещё не имеет is_old/old_reason/previous_names (см. задачу про
    автоматическое обнаружение дублей после переименования/перелогина) —
    открытие репозитория должно добавить все три колонки как NULLABLE
    (is_old — NOT NULL DEFAULT 0), без пересоздания таблицы и без потери
    существующих записей/значений."""
    db_path = tmp_path / "inviter.db"

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE telegram_accounts (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                name                TEXT NOT NULL,
                phone               TEXT NOT NULL,
                session_name        TEXT NOT NULL,
                session_path        TEXT NOT NULL,
                daily_limit         INTEGER NOT NULL DEFAULT 30,
                enabled             BOOLEAN NOT NULL DEFAULT TRUE,
                created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_used_at        TIMESTAMP,
                blocked_until       TIMESTAMP,
                blocked_reason      TEXT,
                verify_membership   INTEGER NOT NULL DEFAULT 1,
                telegram_user_id    INTEGER
            )
            """
        )
        conn.execute(
            "INSERT INTO telegram_accounts "
            "(name, phone, session_name, session_path, daily_limit, telegram_user_id) "
            "VALUES ('@Iv_vla_sov', '+995568759201', 'Iv_vla_sov', 'data/sessions/Iv_vla_sov', 24, 8838087889)"
        )
        conn.commit()
    finally:
        conn.close()

    repository = TelegramAccountRepository(db_path)
    try:
        columns = {row[1] for row in repository._conn.execute(
            "PRAGMA table_info(telegram_accounts)"
        )}
        assert {"is_old", "old_reason", "previous_names"} <= columns

        accounts = repository.list()
        assert len(accounts) == 1
        assert accounts[0].telegram_user_id == 8838087889  # существующее значение не потеряно
        assert accounts[0].is_old is False  # DEFAULT 0 — обратная совместимость
        assert accounts[0].old_reason is None
        assert accounts[0].previous_names == []

        updated = repository.update(
            accounts[0].id, is_old=True, old_reason="duplicate_telegram_user_id",
            previous_names=["@Misha_Offroad"],
        )
        assert updated.is_old is True
        assert updated.old_reason == "duplicate_telegram_user_id"
        assert updated.previous_names == ["@Misha_Offroad"]
    finally:
        repository.close()


# ---- TelegramAccountRepository: create/get/update/list ----


def test_telegram_account_create_get_update_list(tmp_path):
    repository = TelegramAccountRepository(tmp_path / "inviter.db")
    try:
        account = repository.create(
            name="Основной", phone="+995500000001",
            session_name="acc1", session_path="data/sessions/acc1.session",
        )
        assert account.id is not None
        assert account.daily_limit == 30  # значение по умолчанию
        assert account.enabled is True
        assert account.last_used_at is None
        assert account.verify_membership is True  # значение по умолчанию
        assert account.telegram_user_id is None  # значение по умолчанию — NULLABLE

        fetched = repository.get(account.id)
        assert fetched == account

        updated = repository.update(account.id, daily_limit=50, enabled=False)
        assert updated.daily_limit == 50
        assert updated.enabled is False
        # Поля, не переданные в update(), не изменились.
        assert updated.phone == "+995500000001"
        assert updated.verify_membership is True

        with_tg_id = repository.update(account.id, telegram_user_id=5502837130)
        assert with_tg_id.telegram_user_id == 5502837130

        second_account = repository.create(
            name="Второй", phone="+995500000002",
            session_name="acc2", session_path="data/sessions/acc2.session",
            daily_limit=10, enabled=False, verify_membership=False,
            telegram_user_id=6557324579,
        )
        assert second_account.verify_membership is False
        assert second_account.telegram_user_id == 6557324579
        listed = repository.list()
        assert [a.id for a in listed] == [account.id, second_account.id]
    finally:
        repository.close()


def test_telegram_account_toggle_verify_membership_true_and_false(tmp_path):
    """verify_membership независим от enabled — переключается отдельно, в
    обе стороны (см. задачу: "также должна быть возможность снова
    включить его")."""
    repository = TelegramAccountRepository(tmp_path / "inviter.db")
    try:
        account = repository.create(
            name="@car_ins_account", phone="+995500000001",
            session_name="acc1", session_path="data/sessions/acc1.session",
        )
        assert account.enabled is True
        assert account.verify_membership is True

        disabled_verify = repository.update(account.id, verify_membership=False)
        assert disabled_verify.verify_membership is False
        assert disabled_verify.enabled is True  # enabled не затронут

        re_enabled_verify = repository.update(account.id, verify_membership=True)
        assert re_enabled_verify.verify_membership is True
    finally:
        repository.close()


def test_telegram_account_get_returns_none_for_unknown_id(tmp_path):
    repository = TelegramAccountRepository(tmp_path / "inviter.db")
    try:
        assert repository.get(999) is None
    finally:
        repository.close()


def test_telegram_account_update_rejects_unknown_field(tmp_path):
    repository = TelegramAccountRepository(tmp_path / "inviter.db")
    try:
        account = repository.create(
            name="Основной", phone="+995500000001",
            session_name="acc1", session_path="data/sessions/acc1.session",
        )
        with pytest.raises(ValueError):
            repository.update(account.id, id=999)
    finally:
        repository.close()


# ---- InviteCampaignRepository: create/get/update/list ----


def test_invite_campaign_create_get_update_list(tmp_path):
    repository = InviteCampaignRepository(tmp_path / "inviter.db")
    try:
        campaign = repository.create(
            name="ОСАГО осень", keyword="осаго", target_chat="@target_chat",
        )
        assert campaign.enabled is True

        fetched = repository.get(campaign.id)
        assert fetched == campaign

        updated = repository.update(campaign.id, enabled=False)
        assert updated.enabled is False
        assert updated.keyword == "осаго"

        second_campaign = repository.create(
            name="Каско", keyword="каско", target_chat="@target_chat_2", enabled=False,
        )
        listed = repository.list()
        assert [c.id for c in listed] == [campaign.id, second_campaign.id]
    finally:
        repository.close()


# ---- UserCampaignInviteRepository: create/get/update/list ----


def test_user_campaign_invite_create_get_update_list(tmp_path):
    repository = UserCampaignInviteRepository(tmp_path / "inviter.db")
    try:
        invite = repository.create(user_id=111, campaign_id=1)
        assert invite.status == "pending"
        assert invite.account_id is None
        assert invite.error is None
        assert invite.invited_at is None
        assert invite.created_at == invite.updated_at

        fetched = repository.get(invite.id)
        assert fetched == invite

        updated = repository.update(invite.id, account_id=7, status="invited")
        assert updated.account_id == 7
        assert updated.status == "invited"
        # updated_at продвинулся вперёд относительно created_at.
        assert updated.updated_at >= updated.created_at

        second_invite = repository.create(user_id=222, campaign_id=1, status="failed", error="boom")
        assert second_invite.status == "failed"
        assert second_invite.error == "boom"

        listed = repository.list()
        assert [i.id for i in listed] == [invite.id, second_invite.id]
    finally:
        repository.close()


def test_user_campaign_invite_update_rejects_unknown_field(tmp_path):
    repository = UserCampaignInviteRepository(tmp_path / "inviter.db")
    try:
        invite = repository.create(user_id=111, campaign_id=1)
        with pytest.raises(ValueError):
            repository.update(invite.id, user_id=999)
    finally:
        repository.close()


# ---- UserCampaignInviteRepository.count_today_joined() ----


def test_count_today_joined_counts_only_todays_joined_for_this_account(tmp_path):
    """Считаются только status='joined' с verified_at начиная с начала
    текущих суток (UTC) И именно для этого account_id — другой аккаунт,
    вчерашнее подтверждение и статус, отличный от 'joined' (например
    'pending'/'failed'), не должны попадать в счёт (см. задачу про
    daily_limit, который должен считаться по фактически подтверждённым
    участникам, а не по успешным RPC/invited_at)."""
    repository = UserCampaignInviteRepository(tmp_path / "inviter.db")
    try:
        now = datetime.now(timezone.utc)
        yesterday = now - timedelta(days=1)

        repository.create(user_id=1, campaign_id=1, account_id=7, status="joined", invited_at=now, verified_at=now)
        repository.create(user_id=2, campaign_id=1, account_id=7, status="joined", invited_at=now, verified_at=now)
        # Не должны попасть в счёт account_id=7:
        repository.create(user_id=3, campaign_id=1, account_id=8, status="joined", invited_at=now, verified_at=now)  # другой аккаунт
        repository.create(user_id=4, campaign_id=1, account_id=7, status="joined", invited_at=yesterday, verified_at=yesterday)  # вчера
        repository.create(user_id=5, campaign_id=1, account_id=7, status="pending", invited_at=now)  # ещё не подтверждён
        repository.create(user_id=6, campaign_id=1, account_id=7, status="failed", error="boom")  # не joined

        assert repository.count_today_joined(7) == 2
        assert repository.count_today_joined(8) == 1
        assert repository.count_today_joined(999) == 0
    finally:
        repository.close()


def test_count_today_joined_sums_across_campaigns_for_same_account(tmp_path):
    """daily_limit — свойство самого TelegramAccount, а не пары
    (account, campaign) — счётчик должен суммировать подтверждённые
    вступления этого аккаунта по ВСЕМ кампаниям сразу, а не по одной."""
    repository = UserCampaignInviteRepository(tmp_path / "inviter.db")
    try:
        now = datetime.now(timezone.utc)
        repository.create(user_id=1, campaign_id=1, account_id=7, status="joined", invited_at=now, verified_at=now)
        repository.create(user_id=2, campaign_id=2, account_id=7, status="joined", invited_at=now, verified_at=now)

        assert repository.count_today_joined(7) == 2
    finally:
        repository.close()


def test_count_today_joined_returns_zero_when_no_invites_at_all(tmp_path):
    repository = UserCampaignInviteRepository(tmp_path / "inviter.db")
    try:
        assert repository.count_today_joined(1) == 0
    finally:
        repository.close()


def test_count_today_joined_ignores_pending_even_if_invited_at_is_today(tmp_path):
    """Приглашение, которое ещё ждёт подтверждения (status='pending',
    verified_at не задан) — не должно попадать в count_today_joined(),
    даже если invited_at (момент отправки) — сегодня. Это и есть суть
    исправления: считаем реально вступивших, а не отправленные приглашения."""
    repository = UserCampaignInviteRepository(tmp_path / "inviter.db")
    try:
        now = datetime.now(timezone.utc)
        repository.create(user_id=1, campaign_id=1, account_id=7, status="pending", invited_at=now)

        assert repository.count_today_joined(7) == 0
    finally:
        repository.close()


# ---- UserCampaignInviteRepository.count_today_pending() ----


def test_count_today_pending_counts_only_todays_pending_for_this_account(tmp_path):
    """Считаются только status='pending' с invited_at начиная с начала
    текущих суток (UTC) И именно для этого account_id — другой аккаунт,
    вчерашние отправки и статус, отличный от 'pending' (joined/not_joined/
    failed), не должны попадать в счёт (см. задачу про перелив лимита:
    pending временно резервирует место, пока не разрешится)."""
    repository = UserCampaignInviteRepository(tmp_path / "inviter.db")
    try:
        now = datetime.now(timezone.utc)
        yesterday = now - timedelta(days=1)

        repository.create(user_id=1, campaign_id=1, account_id=7, status="pending", invited_at=now)
        repository.create(user_id=2, campaign_id=1, account_id=7, status="pending", invited_at=now)
        # Не должны попасть в счёт account_id=7:
        repository.create(user_id=3, campaign_id=1, account_id=8, status="pending", invited_at=now)  # другой аккаунт
        repository.create(user_id=4, campaign_id=1, account_id=7, status="pending", invited_at=yesterday)  # вчера
        repository.create(user_id=5, campaign_id=1, account_id=7, status="joined", invited_at=now, verified_at=now)  # уже разрешён
        repository.create(user_id=6, campaign_id=1, account_id=7, status="not_joined", invited_at=now, verified_at=now)  # уже разрешён
        repository.create(user_id=7, campaign_id=1, account_id=7, status="failed", error="boom")  # не pending

        assert repository.count_today_pending(7) == 2
        assert repository.count_today_pending(8) == 1
        assert repository.count_today_pending(999) == 0
    finally:
        repository.close()


def test_count_today_pending_sums_across_campaigns_for_same_account(tmp_path):
    """daily_limit — свойство самого TelegramAccount, а не пары (account,
    campaign) — счётчик должен суммировать pending этого аккаунта по ВСЕМ
    кампаниям сразу, а не по одной (как и count_today_joined)."""
    repository = UserCampaignInviteRepository(tmp_path / "inviter.db")
    try:
        now = datetime.now(timezone.utc)
        repository.create(user_id=1, campaign_id=1, account_id=7, status="pending", invited_at=now)
        repository.create(user_id=2, campaign_id=2, account_id=7, status="pending", invited_at=now)

        assert repository.count_today_pending(7) == 2
    finally:
        repository.close()


def test_count_today_pending_returns_zero_when_no_invites_at_all(tmp_path):
    repository = UserCampaignInviteRepository(tmp_path / "inviter.db")
    try:
        assert repository.count_today_pending(1) == 0
    finally:
        repository.close()


# ---- UserCampaignInviteRepository.count_recent_sent() ----


def test_count_recent_sent_counts_pending_and_joined_since_cutoff(tmp_path):
    """Скользящее окно (см. reader/inviter/worker.py) — считает и
    status='pending', и status='joined' (оба означают успешно ОТПРАВЛЕННОЕ
    приглашение, см. InviterService._record_invite_result: invited_at
    заполняется для обоих), но не 'failed'/'invalid'/'not_joined' (RPC не
    было успешным или было отменено ДО отправки) и не приглашения другого
    аккаунта или отправленные раньше cutoff."""
    repository = UserCampaignInviteRepository(tmp_path / "inviter.db")
    try:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=1)
        before_cutoff = cutoff - timedelta(minutes=1)

        repository.create(user_id=1, campaign_id=1, account_id=7, status="pending", invited_at=now)
        repository.create(user_id=2, campaign_id=1, account_id=7, status="joined", invited_at=now, verified_at=now)
        # Не должны попасть в счёт:
        repository.create(user_id=3, campaign_id=1, account_id=8, status="pending", invited_at=now)  # другой аккаунт
        repository.create(user_id=4, campaign_id=1, account_id=7, status="pending", invited_at=before_cutoff)  # раньше окна
        repository.create(user_id=5, campaign_id=1, account_id=7, status="failed", error="boom")  # RPC не успешен
        repository.create(user_id=6, campaign_id=1, account_id=7, status="invalid")  # отменено до отправки

        assert repository.count_recent_sent(7, cutoff) == 2
        assert repository.count_recent_sent(8, cutoff) == 1
        assert repository.count_recent_sent(999, cutoff) == 0
    finally:
        repository.close()


def test_count_recent_sent_sums_across_campaigns_for_same_account(tmp_path):
    """Как и count_today_joined/count_today_pending — по ВСЕМ кампаниям
    сразу, а не по одной (account.daily_limit и часовой лимит воркера —
    свойства аккаунта, не пары (account, campaign))."""
    repository = UserCampaignInviteRepository(tmp_path / "inviter.db")
    try:
        now = datetime.now(timezone.utc)
        repository.create(user_id=1, campaign_id=1, account_id=7, status="pending", invited_at=now)
        repository.create(user_id=2, campaign_id=2, account_id=7, status="joined", invited_at=now, verified_at=now)

        assert repository.count_recent_sent(7, now - timedelta(hours=1)) == 2
    finally:
        repository.close()


def test_count_recent_sent_returns_zero_when_no_invites_at_all(tmp_path):
    repository = UserCampaignInviteRepository(tmp_path / "inviter.db")
    try:
        assert repository.count_recent_sent(1, datetime.now(timezone.utc) - timedelta(hours=1)) == 0
    finally:
        repository.close()


def test_count_recent_sent_excludes_exactly_at_cutoff_boundary_inclusive(tmp_path):
    """cutoff (:since) — включительно, как и today_start у count_today_*
    (invited_at >= :since)."""
    repository = UserCampaignInviteRepository(tmp_path / "inviter.db")
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        repository.create(user_id=1, campaign_id=1, account_id=7, status="pending", invited_at=cutoff)

        assert repository.count_recent_sent(7, cutoff) == 1
    finally:
        repository.close()


# ---- UserCampaignInviteRepository.list_pending() ----


def test_list_pending_returns_only_pending_for_this_account_and_campaign(tmp_path):
    repository = UserCampaignInviteRepository(tmp_path / "inviter.db")
    try:
        now = datetime.now(timezone.utc)
        pending = repository.create(
            user_id=1, campaign_id=1, account_id=7, status="pending", invited_at=now,
        )
        repository.create(user_id=2, campaign_id=1, account_id=7, status="joined", invited_at=now, verified_at=now)
        repository.create(user_id=3, campaign_id=2, account_id=7, status="pending", invited_at=now)  # другая кампания
        repository.create(user_id=4, campaign_id=1, account_id=8, status="pending", invited_at=now)  # другой аккаунт

        result = repository.list_pending(7, 1)

        assert [i.id for i in result] == [pending.id]
        assert result[0].user_id == 1
        assert result[0].status == "pending"
    finally:
        repository.close()


def test_list_pending_returns_empty_when_none_pending(tmp_path):
    repository = UserCampaignInviteRepository(tmp_path / "inviter.db")
    try:
        assert repository.list_pending(7, 1) == []
    finally:
        repository.close()
