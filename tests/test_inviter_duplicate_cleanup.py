"""Тесты reader/inviter/duplicate_cleanup.py — build_cleanup_plan (READ-ONLY)
/ apply_cleanup_plan (единственное изменение — enabled=False для
подтверждённых is_old=True duplicate-записей). Копия/fixture БД, НЕ
production (см. задачу п.11) — здесь моделируются ТЕ ЖЕ пары, что были
найдены в production READ-ONLY диагностике (id=6/7 @Iv_vla_sov,
id=8/9 @bdlapq), но на изолированной tmp_path БД."""

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from reader.inviter.duplicate_cleanup import apply_cleanup_plan, build_cleanup_plan
from reader.inviter.repository import (
    TelegramAccountRepository,
    UserCampaignInviteRepository,
)


@pytest.fixture
def fx(tmp_path):
    db_path = tmp_path / "inviter.db"
    accounts = TelegramAccountRepository(db_path)
    invites = UserCampaignInviteRepository(db_path)
    yield accounts, invites, tmp_path
    accounts.close()
    invites.close()


def _session_file(tmp_path, slug):
    return tmp_path / "sessions" / f"{slug}.session"


def _make_account(accounts, tmp_path, *, name, telegram_user_id, is_old, enabled, session_slug=None):
    slug = session_slug or name.lstrip("@")
    session_path = tmp_path / "sessions" / slug
    return accounts.create(
        name=name, phone="+995500000001", session_name=slug, session_path=str(session_path),
        telegram_user_id=telegram_user_id, is_old=is_old, enabled=enabled,
    )


# ---- I. dry-run ничего не пишет ----


def test_dry_run_does_not_write(fx):
    accounts, invites, tmp_path = fx
    canonical = _make_account(accounts, tmp_path, name="@bdlapq", telegram_user_id=9, is_old=False, enabled=True)
    duplicate = _make_account(accounts, tmp_path, name="@bdlapq", telegram_user_id=9, is_old=True, enabled=True)

    plan = build_cleanup_plan(accounts, invites)

    assert len(plan.actions) == 1
    assert plan.actions[0].account_id == duplicate.id
    # Ничего не записано — dry-run.
    assert accounts.get(duplicate.id).enabled is True
    assert accounts.get(canonical.id).enabled is True


def test_dry_run_report_includes_all_required_fields(fx):
    accounts, invites, tmp_path = fx
    canonical = _make_account(accounts, tmp_path, name="@bdlapq", telegram_user_id=9, is_old=False, enabled=True)
    duplicate = _make_account(accounts, tmp_path, name="@bdlapq", telegram_user_id=9, is_old=True, enabled=True)
    campaign_id = 1
    invites.create(user_id=1, campaign_id=campaign_id, account_id=duplicate.id, status="pending")
    invites.create(user_id=2, campaign_id=campaign_id, account_id=duplicate.id, status="failed")

    plan = build_cleanup_plan(accounts, invites)
    action = plan.actions[0]

    assert action.account_id == duplicate.id
    assert action.telegram_user_id == 9
    assert action.current_username == "@bdlapq"
    assert action.canonical_account_id == canonical.id
    assert action.is_old is True
    assert action.enabled_before is True
    assert action.invite_history_count == 2
    assert action.session_path == duplicate.session_path

    report = plan.format_report()
    assert str(duplicate.id) in report
    assert str(canonical.id) in report
    assert "enabled: True -> False" in report


# ---- J. execute пишет enabled=False только для OLD duplicates ----


def test_execute_sets_enabled_false_only_for_old_duplicates(fx):
    accounts, invites, tmp_path = fx
    canonical = _make_account(accounts, tmp_path, name="@bdlapq", telegram_user_id=9, is_old=False, enabled=True)
    duplicate = _make_account(accounts, tmp_path, name="@bdlapq", telegram_user_id=9, is_old=True, enabled=True)
    unrelated = _make_account(accounts, tmp_path, name="@unrelated", telegram_user_id=100, is_old=False, enabled=True)

    plan = build_cleanup_plan(accounts, invites)
    result = apply_cleanup_plan(accounts, plan)

    assert result.changed == (duplicate.id,)
    assert accounts.get(duplicate.id).enabled is False
    assert accounts.get(canonical.id).enabled is True  # canonical НЕ тронут
    assert accounts.get(unrelated.id).enabled is True  # не входит ни в одну duplicate-группу


def test_execute_is_idempotent_for_already_disabled_duplicate(fx):
    accounts, invites, tmp_path = fx
    _make_account(accounts, tmp_path, name="@bdlapq", telegram_user_id=9, is_old=False, enabled=True)
    duplicate = _make_account(accounts, tmp_path, name="@bdlapq", telegram_user_id=9, is_old=True, enabled=False)

    plan = build_cleanup_plan(accounts, invites)
    result = apply_cleanup_plan(accounts, plan)

    assert result.changed == ()
    assert result.unchanged == (duplicate.id,)
    assert accounts.get(duplicate.id).enabled is False


def test_execute_never_touches_is_old_or_old_reason(fx):
    accounts, invites, tmp_path = fx
    _make_account(accounts, tmp_path, name="@bdlapq", telegram_user_id=9, is_old=False, enabled=True)
    duplicate = _make_account(accounts, tmp_path, name="@bdlapq", telegram_user_id=9, is_old=True, enabled=True)
    accounts.update(duplicate.id, old_reason="duplicate_telegram_user_id")

    plan = build_cleanup_plan(accounts, invites)
    apply_cleanup_plan(accounts, plan)

    refreshed = accounts.get(duplicate.id)
    assert refreshed.is_old is True
    assert refreshed.old_reason == "duplicate_telegram_user_id"


# ---- K. invite history до/после идентична ----


def test_invite_history_unchanged_by_cleanup(fx):
    accounts, invites, tmp_path = fx
    canonical = _make_account(accounts, tmp_path, name="@bdlapq", telegram_user_id=9, is_old=False, enabled=True)
    duplicate = _make_account(accounts, tmp_path, name="@bdlapq", telegram_user_id=9, is_old=True, enabled=True)
    now = datetime.now(timezone.utc)
    invites.create(user_id=1, campaign_id=1, account_id=duplicate.id, status="pending", invited_at=now)
    invites.create(user_id=2, campaign_id=1, account_id=duplicate.id, status="failed")
    invites.create(user_id=3, campaign_id=1, account_id=canonical.id, status="joined", invited_at=now, verified_at=now)

    before = sorted((i.id, i.account_id, i.status) for i in invites.list())

    plan = build_cleanup_plan(accounts, invites)
    apply_cleanup_plan(accounts, plan)

    after = sorted((i.id, i.account_id, i.status) for i in invites.list())
    assert before == after  # ни один account_id/status/id не изменился


# ---- L. session-файлы не удаляются/не изменяются ----


def test_session_files_untouched_by_cleanup(fx):
    accounts, invites, tmp_path = fx
    _make_account(accounts, tmp_path, name="@bdlapq", telegram_user_id=9, is_old=False, enabled=True, session_slug="inviter_bdlapq")
    duplicate = _make_account(
        accounts, tmp_path, name="@bdlapq", telegram_user_id=9, is_old=True, enabled=True,
        session_slug="inviter_m_vlad_i_mir",
    )

    session_file = _session_file(tmp_path, "inviter_m_vlad_i_mir")
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_bytes(b"fake-session-bytes")
    original_bytes = session_file.read_bytes()

    plan = build_cleanup_plan(accounts, invites)
    apply_cleanup_plan(accounts, plan)

    assert session_file.exists()
    assert session_file.read_bytes() == original_bytes
    assert accounts.get(duplicate.id).session_path == duplicate.session_path  # путь в БД тоже не менялся


# ---- систематическое определение duplicate-группы (не hardcoded id) ----


def test_no_duplicates_when_all_telegram_user_ids_unique(fx):
    accounts, invites, tmp_path = fx
    _make_account(accounts, tmp_path, name="@one", telegram_user_id=1, is_old=False, enabled=True)
    _make_account(accounts, tmp_path, name="@two", telegram_user_id=2, is_old=False, enabled=True)

    plan = build_cleanup_plan(accounts, invites)

    assert plan.actions == ()
    assert plan.ambiguous == ()


def test_null_telegram_user_id_never_treated_as_duplicate(fx):
    accounts, invites, tmp_path = fx
    accounts.create(
        name="tg_1", phone="+995500000001", session_name="a1",
        session_path=str(tmp_path / "sessions" / "a1"),
    )
    accounts.create(
        name="tg_2", phone="+995500000002", session_name="a2",
        session_path=str(tmp_path / "sessions" / "a2"),
    )

    plan = build_cleanup_plan(accounts, invites)

    assert plan.actions == ()
    assert plan.ambiguous == ()


def test_ambiguous_group_with_two_current_rows_is_skipped_not_guessed(tmp_path):
    """Два CURRENT (is_old=0) с одним telegram_user_id — теперь физически
    невозможно создать через TelegramAccountRepository.create() (см. partial
    UNIQUE index, tests/test_inviter_repository.py), поэтому здесь такая
    "legacy"-ситуация симулируется напрямую через sqlite3, НЕ через открытый
    репозиторий (иначе индекс успел бы создаться на чистых данных раньше,
    чем возник конфликт) — build_cleanup_plan обязан оставаться безопасным
    и для БД, где эта защита ещё не применена (например, до deploy
    миграции)."""
    db_path = tmp_path / "inviter.db"
    raw = sqlite3.connect(db_path)
    try:
        raw.execute(
            """
            CREATE TABLE telegram_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, phone TEXT NOT NULL, session_name TEXT NOT NULL,
                session_path TEXT NOT NULL, daily_limit INTEGER NOT NULL DEFAULT 30,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_used_at TIMESTAMP, telegram_user_id INTEGER,
                is_old INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        raw.execute(
            "INSERT INTO telegram_accounts (name, phone, session_name, session_path, telegram_user_id, is_old) "
            "VALUES ('@one', '+995500000001', 'one', 'data/sessions/one', 777, 0)"
        )
        raw.execute(
            "INSERT INTO telegram_accounts (name, phone, session_name, session_path, telegram_user_id, is_old) "
            "VALUES ('@two', '+995500000002', 'two', 'data/sessions/two', 777, 0)"
        )
        raw.commit()
    finally:
        raw.close()

    accounts = TelegramAccountRepository(db_path)
    invites = UserCampaignInviteRepository(db_path)
    try:
        a, b = accounts.list()

        plan = build_cleanup_plan(accounts, invites)

        assert plan.actions == ()
        assert len(plan.ambiguous) == 1
        assert plan.ambiguous[0].telegram_user_id == 777
        assert set(plan.ambiguous[0].account_ids) == {a.id, b.id}
        assert "SKIPPED" in plan.format_report()
    finally:
        accounts.close()
        invites.close()


def test_ambiguous_group_with_zero_current_rows_is_skipped(fx):
    """Защитный случай — не должен происходить при штатной работе
    resolve_duplicate_group (всегда оставляет ровно одну CURRENT), но
    build_cleanup_plan не полагается на это молча."""
    accounts, invites, tmp_path = fx
    a = _make_account(accounts, tmp_path, name="@one", telegram_user_id=777, is_old=True, enabled=False)
    b = _make_account(accounts, tmp_path, name="@two", telegram_user_id=777, is_old=True, enabled=False)

    plan = build_cleanup_plan(accounts, invites)

    assert plan.actions == ()
    assert len(plan.ambiguous) == 1
    assert set(plan.ambiguous[0].account_ids) == {a.id, b.id}


def test_multiple_old_duplicates_in_same_group_all_flagged(fx):
    accounts, invites, tmp_path = fx
    canonical = _make_account(accounts, tmp_path, name="@x", telegram_user_id=1, is_old=False, enabled=True)
    dup1 = _make_account(accounts, tmp_path, name="@x", telegram_user_id=1, is_old=True, enabled=True, session_slug="x1")
    dup2 = _make_account(accounts, tmp_path, name="@x", telegram_user_id=1, is_old=True, enabled=True, session_slug="x2")

    plan = build_cleanup_plan(accounts, invites)

    assert {a.account_id for a in plan.actions} == {dup1.id, dup2.id}
    assert all(a.canonical_account_id == canonical.id for a in plan.actions)


# ---- Раздел 11: fixture, воспроизводящий production-пары 6/7 и 8/9 ----


def test_fixture_reproducing_production_pairs_6_7_and_8_9(fx):
    """Моделирует ТОЧНО найденные production-пары (см. READ-ONLY
    диагностику): telegram_user_id=8838087889 (id=6 canonical/id=7
    duplicate), telegram_user_id=8847286898 (id=8 duplicate/id=9
    canonical) — оба duplicate сейчас enabled=1 в production (баг, см.
    задачу), cleanup должен привести ИМЕННО их к enabled=False, ничего
    больше не тронув."""
    accounts, invites, tmp_path = fx

    acc6 = _make_account(
        accounts, tmp_path, name="@Iv_vla_sov", telegram_user_id=8838087889,
        is_old=False, enabled=True, session_slug="Iv_vla_sov",
    )
    acc7 = _make_account(
        accounts, tmp_path, name="@Iv_vla_sov", telegram_user_id=8838087889,
        is_old=True, enabled=True, session_slug="Misha_Offroad",
    )
    accounts.update(acc7.id, old_reason="duplicate_telegram_user_id", previous_names=["@Misha_Offroad"])

    acc8 = _make_account(
        accounts, tmp_path, name="@bdlapq", telegram_user_id=8847286898,
        is_old=True, enabled=True, session_slug="inviter_m_vlad_i_mir",
    )
    accounts.update(acc8.id, old_reason="duplicate_telegram_user_id", previous_names=["@m_vlad_i_mir"])
    acc9 = _make_account(
        accounts, tmp_path, name="@bdlapq", telegram_user_id=8847286898,
        is_old=False, enabled=True, session_slug="inviter_bdlapq",
    )

    # Раздельная invite-история — как в production, никак не пересекается.
    for i in range(3):
        invites.create(user_id=100 + i, campaign_id=1, account_id=acc6.id, status="pending")
    for i in range(2):
        invites.create(user_id=200 + i, campaign_id=1, account_id=acc7.id, status="failed")
    for i in range(1):
        invites.create(user_id=300 + i, campaign_id=1, account_id=acc8.id, status="pending")
    for i in range(4):
        invites.create(user_id=400 + i, campaign_id=1, account_id=acc9.id, status="pending")

    history_before = sorted((i.id, i.account_id, i.status) for i in invites.list())

    plan = build_cleanup_plan(accounts, invites)
    assert {a.account_id for a in plan.actions} == {acc7.id, acc8.id}
    assert plan.ambiguous == ()

    result = apply_cleanup_plan(accounts, plan)
    assert set(result.changed) == {acc7.id, acc8.id}

    assert accounts.get(acc6.id).enabled is True   # unchanged
    assert accounts.get(acc7.id).enabled is False  # duplicate -> False
    assert accounts.get(acc8.id).enabled is False  # duplicate -> False
    assert accounts.get(acc9.id).enabled is True   # unchanged

    # is_old/canonical id не менялись.
    assert accounts.get(acc6.id).is_old is False
    assert accounts.get(acc9.id).is_old is False
    assert accounts.get(acc7.id).is_old is True
    assert accounts.get(acc8.id).is_old is True

    history_after = sorted((i.id, i.account_id, i.status) for i in invites.list())
    assert history_before == history_after
