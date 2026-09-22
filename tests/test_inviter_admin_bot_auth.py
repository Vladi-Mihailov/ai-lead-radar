"""Тесты reader/inviter_admin_bot/auth.py::AccountAuthCoordinator — login
flow (send_code_request/sign_in/2FA), реализованный поверх ТЕХ ЖЕ
reader/inviter/identity.py функций, что и reader/inviter/manage.py::
sync_accounts (см. модульный докстрок auth.py). Никакого реального
Telethon-подключения — только fake-клиент, тот же приём, что и
tests/test_inviter_candidates.py::_FakeTelegramClient.

БЕЗОПАСНОСТЬ — см. test_code_and_password_never_appear_in_logs_or_outcome:
код/пароль передаются РОВНО в sign_in() и нигде больше не оседают."""

import logging
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from telethon.errors import (
    PasswordHashInvalidError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)

from reader.inviter.repository import TelegramAccountRepository
from reader.inviter_admin_bot.auth import AccountAuthCoordinator
from reader.inviter_admin_bot.models import AuthResult

_SECRET_CODE = "13579"
_SECRET_PASSWORD = "hunter2-very-secret"


class _FakeAuthClient:
    """Ровно тот минимум, что AccountAuthCoordinator использует —
    connect()/send_code_request()/sign_in()/get_me()/is_user_authorized()/
    disconnect() (тот же приём, что и test_inviter_candidates.py::
    _FakeTelegramClient — фиксированный набор методов, случайный лишний
    вызов упал бы AttributeError)."""

    def __init__(
        self, *, connect_error=None, send_code_error=None, sign_in_errors=None,
        get_me_result=None, get_me_error=None, is_authorized=True, disconnect_error=None,
    ):
        self.connected = False
        self.disconnected = False
        self._connect_error = connect_error
        self._send_code_error = send_code_error
        self._sign_in_errors = list(sign_in_errors) if sign_in_errors is not None else []
        self._get_me_result = get_me_result or SimpleNamespace(id=555, username="newname", phone="995500000099")
        self._get_me_error = get_me_error
        self._is_authorized = is_authorized
        self._disconnect_error = disconnect_error
        self.sign_in_calls: list[dict] = []

    async def connect(self):
        if self._connect_error is not None:
            raise self._connect_error
        self.connected = True

    async def disconnect(self):
        if self._disconnect_error is not None:
            raise self._disconnect_error
        self.disconnected = True

    async def is_user_authorized(self):
        return self._is_authorized

    async def get_me(self):
        if self._get_me_error is not None:
            raise self._get_me_error
        return self._get_me_result

    async def send_code_request(self, phone):
        if self._send_code_error is not None:
            raise self._send_code_error
        return SimpleNamespace(phone_code_hash="hash-abc")

    async def sign_in(self, phone=None, code=None, *, password=None, phone_code_hash=None):
        self.sign_in_calls.append(
            {"phone": phone, "code": code, "password": password, "phone_code_hash": phone_code_hash}
        )
        if self._sign_in_errors:
            error = self._sign_in_errors.pop(0)
            if error is not None:
                raise error
        return object()


def _make_coordinator(tmp_path, client, *, account_repository=None):
    repo = account_repository or TelegramAccountRepository(tmp_path / "inviter.db")
    coordinator = AccountAuthCoordinator(
        lambda session_path: client, repo, sessions_dir=tmp_path / "sessions",
    )
    return coordinator, repo


# ---- 8/9. Успешная авторизация — телефон -> код -> get_me() -> запись ----


async def test_start_new_sends_code_and_returns_code_sent(tmp_path):
    client = _FakeAuthClient()
    coordinator, repo = _make_coordinator(tmp_path, client)
    try:
        outcome = await coordinator.start_new(chat_id=1, phone="+995500000001")

        assert outcome.result == AuthResult.CODE_SENT
        assert client.connected is True
        assert coordinator.has_pending(1) is True
    finally:
        repo.close()


async def test_full_flow_authorizes_and_creates_account_with_telegram_user_id(tmp_path):
    """6/8. get_me() СРАЗУ после sign_in — telegram_user_id/username
    сохраняются немедленно, аккаунт не остаётся с telegram_user_id=None
    до отдельного sync."""
    client = _FakeAuthClient(get_me_result=SimpleNamespace(id=777, username="freshacc", phone="995500000077"))
    coordinator, repo = _make_coordinator(tmp_path, client)
    try:
        await coordinator.start_new(chat_id=1, phone="+995500000001")
        outcome = await coordinator.submit_code(chat_id=1, code=_SECRET_CODE)

        assert outcome.result == AuthResult.AUTHORIZED
        assert outcome.account is not None
        assert outcome.account.telegram_user_id == 777
        assert outcome.account.name == "@freshacc"
        assert outcome.account.phone == "995500000077"
        assert client.disconnected is True
        assert coordinator.has_pending(1) is False

        [account] = repo.list()
        assert account.id == outcome.account.id
    finally:
        repo.close()


# ---- 9/10. Telegram code flow / 2FA flow ----


async def test_submit_code_needs_password_when_2fa_enabled(tmp_path):
    client = _FakeAuthClient(sign_in_errors=[SessionPasswordNeededError(request=None)])
    coordinator, repo = _make_coordinator(tmp_path, client)
    try:
        await coordinator.start_new(chat_id=1, phone="+995500000001")
        outcome = await coordinator.submit_code(chat_id=1, code=_SECRET_CODE)

        assert outcome.result == AuthResult.NEEDS_PASSWORD
        assert outcome.account is None
        # Диалог остаётся "в ожидании" — sign_in ещё не был отключён/сброшен.
        assert coordinator.has_pending(1) is True
    finally:
        repo.close()


async def test_submit_code_invalid_lets_admin_retry(tmp_path):
    client = _FakeAuthClient(sign_in_errors=[PhoneCodeInvalidError(request=None)])
    coordinator, repo = _make_coordinator(tmp_path, client)
    try:
        await coordinator.start_new(chat_id=1, phone="+995500000001")
        outcome = await coordinator.submit_code(chat_id=1, code="00000")

        assert outcome.result == AuthResult.INVALID_CODE
        assert coordinator.has_pending(1) is True  # не сброшено — можно ввести код ещё раз
    finally:
        repo.close()


async def test_submit_password_authorizes_after_2fa(tmp_path):
    client = _FakeAuthClient(sign_in_errors=[SessionPasswordNeededError(request=None), None])
    coordinator, repo = _make_coordinator(tmp_path, client)
    try:
        await coordinator.start_new(chat_id=1, phone="+995500000001")
        await coordinator.submit_code(chat_id=1, code=_SECRET_CODE)
        outcome = await coordinator.submit_password(chat_id=1, password=_SECRET_PASSWORD)

        assert outcome.result == AuthResult.AUTHORIZED
        assert outcome.account is not None
        assert coordinator.has_pending(1) is False
    finally:
        repo.close()


async def test_submit_password_invalid_lets_admin_retry(tmp_path):
    client = _FakeAuthClient(
        sign_in_errors=[SessionPasswordNeededError(request=None), PasswordHashInvalidError(request=None)],
    )
    coordinator, repo = _make_coordinator(tmp_path, client)
    try:
        await coordinator.start_new(chat_id=1, phone="+995500000001")
        await coordinator.submit_code(chat_id=1, code=_SECRET_CODE)
        outcome = await coordinator.submit_password(chat_id=1, password="wrong-password")

        assert outcome.result == AuthResult.INVALID_PASSWORD
        assert coordinator.has_pending(1) is True
    finally:
        repo.close()


# ---- 11. Секреты никогда не сохраняются/не логируются ----


async def test_code_and_password_never_appear_in_logs_or_outcome(tmp_path, caplog):
    """Код/пароль передаются РОВНО в client.sign_in(...) (см. fake —
    sign_in_calls записывает их для проверки, что они дошли туда, куда
    нужно) и НИГДЕ БОЛЬШЕ: ни в логах (даже при ошибке), ни в
    AuthOutcome.error_summary, ни в состоянии координатора после запроса."""
    client = _FakeAuthClient(
        sign_in_errors=[PhoneCodeInvalidError(request=None), PasswordHashInvalidError(request=None)],
    )
    coordinator, repo = _make_coordinator(tmp_path, client)
    try:
        with caplog.at_level(logging.DEBUG):
            await coordinator.start_new(chat_id=1, phone="+995500000001")
            code_outcome = await coordinator.submit_code(chat_id=1, code=_SECRET_CODE)
            # Sign_in дошёл до реального кода (fake-клиент записал его) —
            # значит поток вообще был протестирован по-настоящему, а не
            # оборвался раньше времени.
            assert client.sign_in_calls[-1]["code"] == _SECRET_CODE

            password_outcome = await coordinator.submit_password(chat_id=1, password=_SECRET_PASSWORD)
            assert client.sign_in_calls[-1]["password"] == _SECRET_PASSWORD

        assert _SECRET_CODE not in (code_outcome.error_summary or "")
        assert _SECRET_PASSWORD not in (password_outcome.error_summary or "")

        for record in caplog.records:
            assert _SECRET_CODE not in record.getMessage()
            assert _SECRET_PASSWORD not in record.getMessage()
    finally:
        repo.close()


async def test_authorized_account_is_never_logged_with_secrets(tmp_path, caplog):
    """Полный успешный флоу — тоже проверяем логи (не только error-путь)."""
    client = _FakeAuthClient()
    coordinator, repo = _make_coordinator(tmp_path, client)
    try:
        with caplog.at_level(logging.DEBUG):
            await coordinator.start_new(chat_id=1, phone="+995500000001")
            await coordinator.submit_code(chat_id=1, code=_SECRET_CODE)

        for record in caplog.records:
            assert _SECRET_CODE not in record.getMessage()
    finally:
        repo.close()


# ---- 15. Sync/re-authorize обновляет username, а не создаёт дубликат ----


async def test_reauthorize_updates_existing_account_not_duplicate(tmp_path):
    """6/15. Тот же telegram_user_id, что уже сохранён — username
    обновляется у СУЩЕСТВУЮЩЕЙ записи, вторая НЕ создаётся (см. design
    "USERNAME SYNC": "Telegram ID совпадает, username изменился ->
    обновить существующую запись")."""
    repo = TelegramAccountRepository(tmp_path / "inviter.db")
    try:
        existing = repo.create(
            name="@oldname", phone="995500000099", session_name="existing",
            session_path=str(tmp_path / "sessions" / "existing"), telegram_user_id=555,
        )

        client = _FakeAuthClient(get_me_result=SimpleNamespace(id=555, username="newname", phone="995500000099"))
        coordinator = AccountAuthCoordinator(
            lambda session_path: client, repo, sessions_dir=tmp_path / "sessions",
        )

        await coordinator.start_reauthorize(chat_id=1, account=existing)
        outcome = await coordinator.submit_code(chat_id=1, code=_SECRET_CODE)

        assert outcome.result == AuthResult.AUTHORIZED
        assert outcome.account.id == existing.id
        assert outcome.account.name == "@newname"

        all_accounts = repo.list()
        assert len(all_accounts) == 1  # НЕ создана вторая запись
        assert "@oldname" in all_accounts[0].previous_names
    finally:
        repo.close()


async def test_reauthorize_reuses_existing_session_path_not_a_new_one(tmp_path):
    """🔐 Переавторизовать — использует УЖЕ существующие session_name/
    session_path этого аккаунта, не создаёт вторую сессию (см. design
    "session_name/session_path НИКОГДА не трогаются")."""
    repo = TelegramAccountRepository(tmp_path / "inviter.db")
    try:
        existing = repo.create(
            name="@vladimihailov", phone="995500000099",
            session_name="vladimihailov", session_path=str(tmp_path / "sessions" / "vladimihailov"),
        )

        captured_paths = []

        def factory(session_path):
            captured_paths.append(session_path)
            return _FakeAuthClient(get_me_result=SimpleNamespace(id=999, username="vladimihailov", phone=None))

        coordinator = AccountAuthCoordinator(factory, repo, sessions_dir=tmp_path / "sessions")
        await coordinator.start_reauthorize(chat_id=1, account=existing)

        assert captured_paths == [str(tmp_path / "sessions" / "vladimihailov")]
    finally:
        repo.close()


# ---- root cause fix: ➕ Добавить аккаунт с НОВЫМ phone/session_path, но
# УЖЕ существующим telegram_user_id (см. задачу про production-дубли
# id=6/7, id=8/9 — session_path НЕ identity, telegram_user_id — ДА) ----


async def test_add_account_with_existing_telegram_user_id_does_not_create_duplicate(tmp_path):
    """A. Тот же физический Telegram-аккаунт (telegram_user_id) добавляют
    повторно под НОВЫМ телефоном/session slug — account count НЕ
    увеличивается (было: создавала вторую строку, см. задачу)."""
    repo = TelegramAccountRepository(tmp_path / "inviter.db")
    try:
        existing = repo.create(
            name="@Iv_vla_sov", phone="995568759201", session_name="Iv_vla_sov",
            session_path=str(tmp_path / "sessions" / "Iv_vla_sov"), telegram_user_id=8838087889,
        )
        assert len(repo.list()) == 1

        client = _FakeAuthClient(
            get_me_result=SimpleNamespace(id=8838087889, username="Iv_vla_sov", phone="995500000999"),
        )
        coordinator = AccountAuthCoordinator(
            lambda session_path: client, repo, sessions_dir=tmp_path / "sessions",
        )

        await coordinator.start_new(chat_id=1, phone="+995500000999")
        outcome = await coordinator.submit_code(chat_id=1, code=_SECRET_CODE)

        assert outcome.result == AuthResult.AUTHORIZED
        all_accounts = repo.list()
        assert len(all_accounts) == 1  # B. НЕ увеличилось — тот же canonical id
        assert all_accounts[0].id == existing.id  # B. canonical account.id сохранился
        assert outcome.account.id == existing.id
    finally:
        repo.close()


async def test_add_account_existing_telegram_user_id_updates_username(tmp_path):
    """C. username изменился на живой сессии -> canonical row обновился
    (та же reconcile_account_identity, что и раньше)."""
    repo = TelegramAccountRepository(tmp_path / "inviter.db")
    try:
        existing = repo.create(
            name="@oldname", phone="995500000001", session_name="oldname",
            session_path=str(tmp_path / "sessions" / "oldname"), telegram_user_id=42,
        )

        client = _FakeAuthClient(
            get_me_result=SimpleNamespace(id=42, username="newname", phone="995500000002"),
        )
        coordinator = AccountAuthCoordinator(
            lambda session_path: client, repo, sessions_dir=tmp_path / "sessions",
        )

        await coordinator.start_new(chat_id=1, phone="+995500000002")
        outcome = await coordinator.submit_code(chat_id=1, code=_SECRET_CODE)

        assert outcome.account.id == existing.id
        assert outcome.account.name == "@newname"
        assert "@oldname" in outcome.account.previous_names
        assert repo.get(existing.id).session_path == existing.session_path  # session_path canonical не тронут
    finally:
        repo.close()


async def test_add_account_existing_telegram_user_id_does_not_create_old_row(tmp_path):
    """D. OLD-запись не появляется в результате повторного add-account —
    результат сразу СТАБИЛЕН (is_old=False у единственной строки), не
    полагается на постфактумную подчистку resolve_duplicate_group."""
    repo = TelegramAccountRepository(tmp_path / "inviter.db")
    try:
        repo.create(
            name="@bdlapq", phone="79495447392", session_name="inviter_bdlapq",
            session_path=str(tmp_path / "sessions" / "inviter_bdlapq"), telegram_user_id=8847286898,
        )

        client = _FakeAuthClient(
            get_me_result=SimpleNamespace(id=8847286898, username="bdlapq", phone="79495447393"),
        )
        coordinator = AccountAuthCoordinator(
            lambda session_path: client, repo, sessions_dir=tmp_path / "sessions",
        )

        await coordinator.start_new(chat_id=1, phone="+79495447393")
        await coordinator.submit_code(chat_id=1, code=_SECRET_CODE)

        all_accounts = repo.list()
        assert len(all_accounts) == 1
        assert all_accounts[0].is_old is False
        assert all_accounts[0].enabled is True
    finally:
        repo.close()


async def test_add_account_existing_telegram_user_id_sets_last_synced_at(tmp_path):
    repo = TelegramAccountRepository(tmp_path / "inviter.db")
    try:
        existing = repo.create(
            name="@oldname", phone="995500000001", session_name="oldname",
            session_path=str(tmp_path / "sessions" / "oldname"), telegram_user_id=42,
        )
        assert existing.last_synced_at is None

        client = _FakeAuthClient(get_me_result=SimpleNamespace(id=42, username="oldname", phone=None))
        coordinator = AccountAuthCoordinator(
            lambda session_path: client, repo, sessions_dir=tmp_path / "sessions",
        )

        await coordinator.start_new(chat_id=1, phone="+995500000002")
        outcome = await coordinator.submit_code(chat_id=1, code=_SECRET_CODE)

        assert outcome.account.last_synced_at is not None
    finally:
        repo.close()


async def test_add_account_genuinely_new_telegram_user_id_still_creates_row(tmp_path):
    """Регрессия: НЕ связанный физический аккаунт (другой telegram_user_id)
    по-прежнему создаёт новую запись как раньше — фикс не должен мешать
    обычному первому добавлению аккаунта."""
    repo = TelegramAccountRepository(tmp_path / "inviter.db")
    try:
        repo.create(
            name="@existing", phone="995500000001", session_name="existing",
            session_path=str(tmp_path / "sessions" / "existing"), telegram_user_id=1,
        )

        client = _FakeAuthClient(get_me_result=SimpleNamespace(id=2, username="brandnew", phone=None))
        coordinator = AccountAuthCoordinator(
            lambda session_path: client, repo, sessions_dir=tmp_path / "sessions",
        )

        await coordinator.start_new(chat_id=1, phone="+995500000002")
        outcome = await coordinator.submit_code(chat_id=1, code=_SECRET_CODE)

        assert outcome.result == AuthResult.AUTHORIZED
        all_accounts = repo.list()
        assert len(all_accounts) == 2
        assert outcome.account.telegram_user_id == 2
        assert outcome.account.is_old is False
    finally:
        repo.close()


# ---- ошибки/отмена ----


async def test_send_code_failure_returns_failed_without_pending_state(tmp_path):
    client = _FakeAuthClient(send_code_error=ValueError("boom"))
    coordinator, repo = _make_coordinator(tmp_path, client)
    try:
        outcome = await coordinator.start_new(chat_id=1, phone="+995500000001")

        assert outcome.result == AuthResult.FAILED
        assert coordinator.has_pending(1) is False
        assert client.disconnected is True  # клиент отключён после неудачи
    finally:
        repo.close()


async def test_cancel_disconnects_pending_client(tmp_path):
    client = _FakeAuthClient()
    coordinator, repo = _make_coordinator(tmp_path, client)
    try:
        await coordinator.start_new(chat_id=1, phone="+995500000001")
        assert coordinator.has_pending(1) is True

        await coordinator.cancel(1)

        assert coordinator.has_pending(1) is False
        assert client.disconnected is True
    finally:
        repo.close()


async def test_starting_new_login_cancels_previous_pending_for_same_chat(tmp_path):
    first_client = _FakeAuthClient()
    second_client = _FakeAuthClient()
    clients = [first_client, second_client]
    repo = TelegramAccountRepository(tmp_path / "inviter.db")
    try:
        coordinator = AccountAuthCoordinator(
            lambda session_path: clients.pop(0), repo, sessions_dir=tmp_path / "sessions",
        )
        await coordinator.start_new(chat_id=1, phone="+995500000001")
        await coordinator.start_new(chat_id=1, phone="+995500000002")

        assert first_client.disconnected is True
        assert coordinator.has_pending(1) is True
    finally:
        repo.close()


async def test_submit_code_with_no_pending_session_fails_safely(tmp_path):
    client = _FakeAuthClient()
    coordinator, repo = _make_coordinator(tmp_path, client)
    try:
        outcome = await coordinator.submit_code(chat_id=999, code=_SECRET_CODE)
        assert outcome.result == AuthResult.FAILED
    finally:
        repo.close()
