"""Тесты чистой логики физической идентичности Telegram-аккаунта (см.
reader/inviter/identity.py и задачу про production-дубли: два разных
DB-ряда/session-файла, фактически авторизованные как ОДИН и тот же
Telegram-аккаунт). Без Telethon, без TelegramClient — только
TelegramAccountRepository + чистые функции."""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from reader.inviter.identity import (  # noqa: E402
    AccountIdentityMismatchError,
    SessionNotAuthorizedError,
    fetch_telegram_identity,
    find_duplicate_account,
    reconcile_account_identity,
)
from reader.inviter.repository import TelegramAccountRepository  # noqa: E402


class _FakeClient:
    def __init__(self, *, authorized=True, me=None):
        self._authorized = authorized
        self._me = me

    async def is_user_authorized(self) -> bool:
        return self._authorized

    async def get_me(self):
        return self._me


# ---- fetch_telegram_identity() ----


def test_fetch_telegram_identity_returns_id_username_phone():
    client = _FakeClient(me=SimpleNamespace(id=555, username="vladimihailov", phone="+995500000001"))

    identity = asyncio.run(fetch_telegram_identity(client))

    assert identity.telegram_user_id == 555
    assert identity.username == "vladimihailov"
    assert identity.phone == "+995500000001"


def test_fetch_telegram_identity_treats_empty_username_and_phone_as_none():
    client = _FakeClient(me=SimpleNamespace(id=555, username="", phone=""))

    identity = asyncio.run(fetch_telegram_identity(client))

    assert identity.username is None
    assert identity.phone is None


def test_fetch_telegram_identity_raises_when_not_authorized():
    client = _FakeClient(authorized=False)

    with pytest.raises(SessionNotAuthorizedError):
        asyncio.run(fetch_telegram_identity(client))


# ---- reconcile_account_identity() ----


def _identity(**overrides):
    defaults = {"telegram_user_id": 555, "username": None, "phone": None}
    defaults.update(overrides)
    from reader.inviter.identity import TelegramIdentity

    return TelegramIdentity(**defaults)


def test_reconcile_fills_empty_telegram_user_id(tmp_path):
    repository = TelegramAccountRepository(tmp_path / "inviter.db")
    try:
        account = repository.create(
            name="acc1", phone="+995500000001", session_name="acc1", session_path="acc1",
        )

        updated = reconcile_account_identity(repository, account, _identity(telegram_user_id=555))

        assert updated.telegram_user_id == 555
        assert updated.name == "acc1"  # username=None в identity — имя не меняется
        assert updated.phone == "+995500000001"
    finally:
        repository.close()


def test_reconcile_updates_name_on_username_change_same_tg_id(tmp_path):
    repository = TelegramAccountRepository(tmp_path / "inviter.db")
    try:
        account = repository.create(
            name="@alena_ogi", phone="+995500000001", session_name="alenaogir",
            session_path="alenaogir", telegram_user_id=6557324579,
        )

        updated = reconcile_account_identity(
            repository, account, _identity(telegram_user_id=6557324579, username="ao777oa777"),
        )

        assert updated.name == "@ao777oa777"
        assert updated.telegram_user_id == 6557324579
        assert updated.session_name == "alenaogir"
        assert updated.session_path == "alenaogir"
    finally:
        repository.close()


def test_reconcile_syncs_phone_when_changed(tmp_path):
    repository = TelegramAccountRepository(tmp_path / "inviter.db")
    try:
        account = repository.create(
            name="acc1", phone="+995500000001", session_name="acc1", session_path="acc1",
            telegram_user_id=555,
        )

        updated = reconcile_account_identity(
            repository, account, _identity(telegram_user_id=555, phone="+79495447392"),
        )

        assert updated.phone == "+79495447392"
    finally:
        repository.close()


def test_reconcile_keeps_phone_when_identity_phone_is_none(tmp_path):
    repository = TelegramAccountRepository(tmp_path / "inviter.db")
    try:
        account = repository.create(
            name="acc1", phone="+995500000001", session_name="acc1", session_path="acc1",
            telegram_user_id=555,
        )

        updated = reconcile_account_identity(repository, account, _identity(telegram_user_id=555))

        assert updated.phone == "+995500000001"
    finally:
        repository.close()


def test_reconcile_returns_same_values_without_extra_write_when_nothing_changed(tmp_path):
    """Если telegram_user_id/name/phone уже совпадают — reconcile не
    должен ничего писать в БД (см. docstring reconcile_account_identity),
    достаточно проверить, что возвращённый account эквивалентен исходному."""
    repository = TelegramAccountRepository(tmp_path / "inviter.db")
    try:
        account = repository.create(
            name="acc1", phone="+995500000001", session_name="acc1", session_path="acc1",
            telegram_user_id=555,
        )

        updated = reconcile_account_identity(repository, account, _identity(telegram_user_id=555))

        assert updated == account
    finally:
        repository.close()


def test_reconcile_raises_on_telegram_user_id_mismatch(tmp_path):
    repository = TelegramAccountRepository(tmp_path / "inviter.db")
    try:
        account = repository.create(
            name="acc1", phone="+995500000001", session_name="acc1", session_path="acc1",
            telegram_user_id=111,
        )

        with pytest.raises(AccountIdentityMismatchError):
            reconcile_account_identity(repository, account, _identity(telegram_user_id=999))

        # Ничего не перезаписано.
        assert repository.get(account.id).telegram_user_id == 111
    finally:
        repository.close()


# ---- find_duplicate_account() ----


def test_find_duplicate_account_returns_none_when_unique(tmp_path):
    repository = TelegramAccountRepository(tmp_path / "inviter.db")
    try:
        account = repository.create(
            name="acc1", phone="+995500000001", session_name="acc1", session_path="acc1",
            telegram_user_id=555,
        )

        assert find_duplicate_account(repository, account) is None
    finally:
        repository.close()


def test_find_duplicate_account_returns_none_when_telegram_user_id_not_set(tmp_path):
    repository = TelegramAccountRepository(tmp_path / "inviter.db")
    try:
        account = repository.create(
            name="acc1", phone="+995500000001", session_name="acc1", session_path="acc1",
        )

        assert find_duplicate_account(repository, account) is None
    finally:
        repository.close()


def test_find_duplicate_account_detects_lower_id_as_primary(tmp_path):
    repository = TelegramAccountRepository(tmp_path / "inviter.db")
    try:
        primary = repository.create(
            name="@Iv_vla_sov", phone="+995568759201", session_name="Iv_vla_sov",
            session_path="Iv_vla_sov", telegram_user_id=8838087889,
        )
        duplicate = repository.create(
            name="@Misha_Offroad", phone="+995568759201", session_name="Misha_Offroad",
            session_path="Misha_Offroad", telegram_user_id=8838087889,
        )

        assert find_duplicate_account(repository, primary) is None
        found = find_duplicate_account(repository, duplicate)
        assert found is not None
        assert found.id == primary.id
    finally:
        repository.close()


def test_find_duplicate_account_ignores_disabled_accounts(tmp_path):
    """Отключённый исторический дубликат (enabled=False, как реальные
    id=7/id=8 в проде) не должен считаться конфликтом для enabled-аккаунта."""
    repository = TelegramAccountRepository(tmp_path / "inviter.db")
    try:
        primary = repository.create(
            name="@Iv_vla_sov", phone="+995568759201", session_name="Iv_vla_sov",
            session_path="Iv_vla_sov", telegram_user_id=8838087889,
        )
        repository.create(
            name="@Misha_Offroad", phone="+995568759201", session_name="Misha_Offroad",
            session_path="Misha_Offroad", telegram_user_id=8838087889, enabled=False,
        )

        assert find_duplicate_account(repository, primary) is None
    finally:
        repository.close()
