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
    reconcile_account_identity,
    resolve_duplicate_group,
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
            session_path="alenaogir", telegram_user_id=6557324579, enabled=True,
        )

        updated = reconcile_account_identity(
            repository, account, _identity(telegram_user_id=6557324579, username="ao777oa777"),
        )

        assert updated.name == "@ao777oa777"
        assert updated.telegram_user_id == 6557324579
        assert updated.session_name == "alenaogir"
        assert updated.session_path == "alenaogir"
        # Смена username — не новый аккаунт и не OLD-запись: enabled и
        # is_old не тронуты reconcile'ом (это забота resolve_duplicate_group).
        assert updated.enabled is True
        assert updated.is_old is False
    finally:
        repository.close()


def test_reconcile_saves_old_name_to_previous_names(tmp_path):
    """Старый username не должен теряться безвозвратно — сохраняется в
    previous_names (см. задачу: "DB row 7 исторически была
    @Misha_Offroad", даже после того, как name синхронизировали на
    актуальный username)."""
    repository = TelegramAccountRepository(tmp_path / "inviter.db")
    try:
        account = repository.create(
            name="@alena_ogi", phone="+995500000001", session_name="alenaogir",
            session_path="alenaogir", telegram_user_id=6557324579,
        )
        assert account.previous_names == []

        updated = reconcile_account_identity(
            repository, account, _identity(telegram_user_id=6557324579, username="ao777oa777"),
        )

        assert updated.previous_names == ["@alena_ogi"]
    finally:
        repository.close()


def test_reconcile_multiple_renames_do_not_create_new_rows_and_accumulate_history(tmp_path):
    """Несколько переименований подряд — всё та же DB-запись (один id), и
    previous_names накапливает всю историю без дублей."""
    repository = TelegramAccountRepository(tmp_path / "inviter.db")
    try:
        account = repository.create(
            name="@name_one", phone="+995500000001", session_name="acc1",
            session_path="acc1", telegram_user_id=555,
        )

        after_first = reconcile_account_identity(
            repository, account, _identity(telegram_user_id=555, username="name_two"),
        )
        after_second = reconcile_account_identity(
            repository, after_first, _identity(telegram_user_id=555, username="name_three"),
        )

        assert after_second.id == account.id  # тот же ряд, не новый
        assert after_second.name == "@name_three"
        assert after_second.previous_names == ["@name_one", "@name_two"]
        assert len(repository.list()) == 1  # ни одной новой строки

        # Возврат к прежнему имени не плодит дубликат записи в истории.
        after_third = reconcile_account_identity(
            repository, after_second, _identity(telegram_user_id=555, username="name_one"),
        )
        assert after_third.previous_names == ["@name_one", "@name_two", "@name_three"]
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


# ---- resolve_duplicate_group() ----


def test_resolve_duplicate_group_noop_when_unique(tmp_path):
    repository = TelegramAccountRepository(tmp_path / "inviter.db")
    try:
        account = repository.create(
            name="acc1", phone="+995500000001", session_name="acc1", session_path="acc1",
            telegram_user_id=555,
        )

        resolve_duplicate_group(repository, 555)

        refreshed = repository.get(account.id)
        assert refreshed.is_old is False
        assert refreshed.old_reason is None
        assert refreshed.enabled is True
    finally:
        repository.close()


def test_resolve_duplicate_group_prefers_the_enabled_row_as_current(tmp_path):
    """Реальный случай из прода: id=6 @Iv_vla_sov (enabled) и id=7
    @Misha_Offroad (изначально тоже enabled в этом тесте) — обе физически
    telegram_user_id=8838087889. Ровно одна CURRENT, вторая OLD+disabled,
    ничего не удалено."""
    repository = TelegramAccountRepository(tmp_path / "inviter.db")
    try:
        primary = repository.create(
            name="@Iv_vla_sov", phone="+995568759201", session_name="Iv_vla_sov",
            session_path="Iv_vla_sov", telegram_user_id=8838087889, enabled=True,
        )
        duplicate = repository.create(
            name="@Misha_Offroad", phone="+995568759201", session_name="Misha_Offroad",
            session_path="Misha_Offroad", telegram_user_id=8838087889, enabled=False,
        )

        resolve_duplicate_group(repository, 8838087889)

        current = repository.get(primary.id)
        old = repository.get(duplicate.id)
        assert current.is_old is False
        assert current.enabled is True  # CURRENT не трогаем — уже был enabled
        assert old.is_old is True
        assert old.old_reason == "duplicate_telegram_user_id"
        assert old.enabled is False
        assert len(repository.list()) == 2  # ничего не удалено
    finally:
        repository.close()


def test_resolve_duplicate_group_tie_breaks_by_lowest_id_when_none_enabled(tmp_path):
    """Обе записи disabled (реальный случай id=8/id=9 до момента, пока
    оператор явно не включит одну из них) — CURRENT определяется
    детерминированно (наименьший id), но enabled НЕ включается автоматически
    ни для одной из них (см. задачу: "если все disabled — не включать ни
    одну автоматически, просто определить primary/current для отображения")."""
    repository = TelegramAccountRepository(tmp_path / "inviter.db")
    try:
        lower = repository.create(
            name="@m_vlad_i_mir", phone="+79495447392", session_name="inviter_m_vlad_i_mir",
            session_path="inviter_m_vlad_i_mir", telegram_user_id=8847286898, enabled=False,
        )
        higher = repository.create(
            name="@bdlapq", phone="+79495447392", session_name="inviter_bdlapq",
            session_path="inviter_bdlapq", telegram_user_id=8847286898, enabled=False,
        )

        resolve_duplicate_group(repository, 8847286898)

        current = repository.get(lower.id)
        old = repository.get(higher.id)
        assert current.is_old is False
        assert current.enabled is False  # никогда не включать автоматически
        assert old.is_old is True
        assert old.enabled is False
    finally:
        repository.close()


def test_resolve_duplicate_group_is_sticky_even_if_old_row_manually_enabled(tmp_path):
    """OLD имеет приоритет: даже если оператор случайно (или сознательно)
    выставит enabled=1 записи, уже помеченной is_old=True, повторный
    resolve_duplicate_group не должен реклассифицировать её обратно в
    CURRENT — только принудительно возвращает enabled=False (защита в
    рантайме дополнительно проверяет is_old напрямую, см. service.py)."""
    repository = TelegramAccountRepository(tmp_path / "inviter.db")
    try:
        primary = repository.create(
            name="@Iv_vla_sov", phone="+995568759201", session_name="Iv_vla_sov",
            session_path="Iv_vla_sov", telegram_user_id=8838087889, enabled=True,
        )
        duplicate = repository.create(
            name="@Misha_Offroad", phone="+995568759201", session_name="Misha_Offroad",
            session_path="Misha_Offroad", telegram_user_id=8838087889, enabled=False,
        )

        resolve_duplicate_group(repository, 8838087889)
        assert repository.get(duplicate.id).is_old is True

        # Оператор (или баг) вручную включает уже помеченный OLD дубликат.
        repository.update(duplicate.id, enabled=True)

        resolve_duplicate_group(repository, 8838087889)

        refreshed_duplicate = repository.get(duplicate.id)
        refreshed_primary = repository.get(primary.id)
        assert refreshed_duplicate.is_old is True  # остаётся OLD, не реклассифицирован
        assert refreshed_duplicate.enabled is False  # принудительно возвращено
        assert refreshed_primary.is_old is False  # primary остаётся CURRENT
    finally:
        repository.close()


def test_resolve_duplicate_group_is_idempotent(tmp_path):
    """Повторный вызов без изменения входных данных не производит новых
    записей в БД (см. задачу: "repeated sync идемпотентен")."""
    repository = TelegramAccountRepository(tmp_path / "inviter.db")
    try:
        primary = repository.create(
            name="@Iv_vla_sov", phone="+995568759201", session_name="Iv_vla_sov",
            session_path="Iv_vla_sov", telegram_user_id=8838087889, enabled=True,
        )
        duplicate = repository.create(
            name="@Misha_Offroad", phone="+995568759201", session_name="Misha_Offroad",
            session_path="Misha_Offroad", telegram_user_id=8838087889, enabled=False,
        )

        resolve_duplicate_group(repository, 8838087889)
        first_pass = (repository.get(primary.id), repository.get(duplicate.id))

        resolve_duplicate_group(repository, 8838087889)
        second_pass = (repository.get(primary.id), repository.get(duplicate.id))

        assert first_pass == second_pass
    finally:
        repository.close()
