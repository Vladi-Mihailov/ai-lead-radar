"""AccountAuthCoordinator — авторизация Telegram-аккаунта инвайтера, ведомая
диалогом с админ-ботом.

reader/inviter/authorize.py делает то же самое, но через client.start() БЕЗ
phone=... — Telethon сам интерактивно читает телефон/код/2FA-пароль из
stdin. Это физически не подходит для Telegram-бота (нет stdin, ввод
приходит асинхронно, отдельными сообщениями) — здесь тот же итоговый
результат (создание/обновление .session-файла + telegram_accounts записи),
но собранный явно поверх connect()/send_code_request()/sign_in(), чтобы
код/пароль приходили из conversation.py по мере ввода, а не блокировали
процесс на stdin. Никакой НОВОЙ identity-логики не заводится — как только
get_me() дал результат, дальше используются ТЕ ЖЕ reader/inviter/identity.py
функции, что и reader/inviter/manage.py::sync_accounts (fetch_telegram_
identity/reconcile_account_identity/resolve_duplicate_group).

БЕЗОПАСНОСТЬ (см. задачу "Session Security"): code/password — ТОЛЬКО
аргументы submit_code()/submit_password(), переданные один раз напрямую в
client.sign_in(...). Ни один из них НИКОГДА не присваивается self.*/любой
структуре этого модуля, не логируется, не попадает ни в одно
AuthOutcome.error_summary (там — только заранее заданные безопасные
строки, никогда str(exc) при коде/пароле)."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from telethon.errors import (
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)

from reader.inviter.identity import (
    fetch_telegram_identity,
    reconcile_account_identity,
    resolve_duplicate_group,
)
from reader.inviter.models import TelegramAccount
from reader.inviter.repository import TelegramAccountRepository
from reader.inviter_admin_bot.models import AuthOutcome, AuthResult

logger = logging.getLogger(__name__)


class TelethonAuthClientLike(Protocol):
    """Часть TelegramClient, нужная именно для login-флоу — подмножество
    настоящего Telethon-клиента/fake в тестах (см.
    reader/inviter/identity.py::IdentityTelegramClientLike — то же
    подмножество + login-специфичные методы)."""

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def is_user_authorized(self) -> bool: ...

    async def get_me(self): ...

    async def send_code_request(self, phone: str): ...

    async def sign_in(self, phone=None, code=None, *, password=None, phone_code_hash=None): ...


@dataclass
class _PendingLogin:
    client: TelethonAuthClientLike
    phone: str
    phone_code_hash: str | None
    session_name: str
    session_path: str


_GENERIC_FAILURE_TEXT = "Не удалось авторизовать аккаунт. Попробуйте начать заново."


class AccountAuthCoordinator:
    """client_factory(session_path) -> TelethonAuthClientLike — та же
    инъекция, что и у InviterService(client_factory=...)/
    reader/inviter/manage.py::_build_sync_client_factory — production
    (main.py) подставляет настоящий TelegramClient(session_path,
    api_id, api_hash, receive_updates=False) (та же конвенция инлайн-
    создания клиента, что и везде в reader/inviter/), тесты — fake.

    Один незавершённый вход на chat_id одновременно (см. start_*/cancel) —
    достаточно для закрытого admin-бота на несколько доверенных
    администраторов, каждый со своим приватным чатом."""

    def __init__(
        self,
        client_factory: Callable[[str], TelethonAuthClientLike],
        account_repository: TelegramAccountRepository,
        *,
        sessions_dir: Path,
    ):
        self._client_factory = client_factory
        self._account_repository = account_repository
        self._sessions_dir = Path(sessions_dir)
        self._pending: dict[int, _PendingLogin] = {}

    @staticmethod
    def slug_for_phone(phone: str) -> str:
        """session_name/session_path НИКОГДА не меняются после создания
        (см. reader/inviter/identity.py::reconcile_account_identity) —
        детерминированный слаг от телефона даёт стабильное, предсказуемое
        имя сессии независимо от username (username на момент "Введите
        номер телефона" ещё не известен вовсе)."""
        digits = "".join(ch for ch in phone if ch.isdigit())
        return f"tg_{digits}"

    def _session_path_for(self, slug: str) -> str:
        return str(self._sessions_dir / slug)

    async def start_new(self, chat_id: int, phone: str) -> AuthOutcome:
        """➕ Добавить аккаунт — Шаг 1/2 (см. design). session_name/path
        выводятся из номера телефона (см. slug_for_phone)."""
        slug = self.slug_for_phone(phone)
        return await self._start(chat_id, phone, session_name=slug, session_path=self._session_path_for(slug))

    async def start_reauthorize(self, chat_id: int, account: TelegramAccount) -> AuthOutcome:
        """🔐 Переавторизовать — ПЕРЕИСПОЛЬЗУЕТ существующие session_name/
        session_path этого аккаунта (см. reconcile_account_identity про
        "session_name/session_path НИКОГДА не трогаются") — НЕ создаёт
        вторую сессию/запись."""
        return await self._start(
            chat_id, account.phone, session_name=account.session_name, session_path=account.session_path,
        )

    async def _start(self, chat_id: int, phone: str, *, session_name: str, session_path: str) -> AuthOutcome:
        await self.cancel(chat_id)  # предыдущая незавершённая попытка этого chat_id, если была

        client = self._client_factory(session_path)
        try:
            await client.connect()
            sent = await client.send_code_request(phone)
        except Exception as exc:
            await self._safe_disconnect(client)
            logger.warning(
                "Не удалось запросить код авторизации (chat_id=%s): %s", chat_id, type(exc).__name__,
            )
            return AuthOutcome(
                result=AuthResult.FAILED,
                error_summary="Не удалось отправить код. Проверьте номер телефона и попробуйте снова.",
            )

        self._pending[chat_id] = _PendingLogin(
            client=client, phone=phone,
            phone_code_hash=getattr(sent, "phone_code_hash", None),
            session_name=session_name, session_path=session_path,
        )
        return AuthOutcome(result=AuthResult.CODE_SENT)

    async def submit_code(self, chat_id: int, code: str) -> AuthOutcome:
        """code — ТОЛЬКО локальный параметр, передаётся в client.sign_in
        и больше нигде не используется (см. модульный докстрок про
        безопасность)."""
        pending = self._pending.get(chat_id)
        if pending is None:
            return AuthOutcome(
                result=AuthResult.FAILED, error_summary="Диалог авторизации устарел — начните заново.",
            )

        try:
            await pending.client.sign_in(pending.phone, code, phone_code_hash=pending.phone_code_hash)
        except SessionPasswordNeededError:
            return AuthOutcome(result=AuthResult.NEEDS_PASSWORD)
        except (PhoneCodeInvalidError, PhoneCodeExpiredError):
            return AuthOutcome(
                result=AuthResult.INVALID_CODE, error_summary="Код неверный или устарел. Введите код ещё раз.",
            )
        except Exception as exc:
            await self.cancel(chat_id)
            logger.warning("Ошибка авторизации по коду (chat_id=%s): %s", chat_id, type(exc).__name__)
            return AuthOutcome(result=AuthResult.FAILED, error_summary=_GENERIC_FAILURE_TEXT)

        return await self._finalize(chat_id, pending)

    async def submit_password(self, chat_id: int, password: str) -> AuthOutcome:
        """password — ТОЛЬКО локальный параметр, передаётся в
        client.sign_in и больше нигде не используется (см. модульный
        докстрок про безопасность)."""
        pending = self._pending.get(chat_id)
        if pending is None:
            return AuthOutcome(
                result=AuthResult.FAILED, error_summary="Диалог авторизации устарел — начните заново.",
            )

        try:
            await pending.client.sign_in(password=password)
        except PasswordHashInvalidError:
            return AuthOutcome(
                result=AuthResult.INVALID_PASSWORD, error_summary="Неверный пароль. Попробуйте ещё раз.",
            )
        except Exception as exc:
            await self.cancel(chat_id)
            logger.warning("Ошибка авторизации по паролю 2FA (chat_id=%s): %s", chat_id, type(exc).__name__)
            return AuthOutcome(result=AuthResult.FAILED, error_summary=_GENERIC_FAILURE_TEXT)

        return await self._finalize(chat_id, pending)

    async def _finalize(self, chat_id: int, pending: _PendingLogin) -> AuthOutcome:
        """me = await client.get_me() СРАЗУ после успешного sign_in (см.
        design шаг 5) — telegram_user_id/username/phone берутся
        ИСКЛЮЧИТЕЛЬНО отсюда, никогда из того, что ввёл админ на шаге
        "номер телефона" (Telegram может нормализовать номер иначе).

        Решение create-vs-update — ДВУХСТУПЕНЧАТОЕ, session_path НИКОГДА
        не единственный критерий (см. задачу про production-дубли id=6/7,
        id=8/9: тот же физический аккаунт добавляли повторно под НОВЫМ
        телефоном/session_path, из-за чего session_path-only матч не
        находил уже существующую запись и создавал вторую строку —
        resolve_duplicate_group ниже подчищала это лишь ПОСТФАКТУМ,
        оставляя лишнюю строку в БД навсегда):

        1. Сначала — по session_path (та же сессия, что уже используется
           этой DB-записью — самый точный и дешёвый матч, когда он есть;
           это, в частности, ВСЕГДА срабатывает для start_reauthorize, см.
           её докстрок — session_path там намеренно тот же, что и у
           account).
        2. Если по session_path не нашлось — по telegram_user_id среди
           ТЕКУЩИХ (is_old=False) записей (см. reader/inviter/identity.py
           module docstring: "session_path — НЕ identity, единственный
           стабильный идентификатор — telegram_user_id"). Найдена — canonical
           account.id СОХРАНЯЕТСЯ, обновляются только identity-поля
           (name/phone/previous_names через reconcile_account_identity,
           та же логика, что и для username rename) — ВТОРАЯ DB-запись НЕ
           создаётся. Только что созданный .session-файл (pending.
           session_path) сознательно НЕ привязывается ни к одной записи —
           canonical account продолжает использовать свой уже РАБОТАЮЩИЙ
           session_path (см. задачу "не потерять рабочую существующую
           session вслепую" — Telegram допускает множество параллельных
           авторизованных сессий одного аккаунта, менять уже рабочую
           session_path ради этой новой не нужно). Клиент уже отключён
           (см. finally выше) — новый .session-файл остаётся на диске,
           просто не используется никаким процессом инвайтера, ничего
           дополнительно закрывать не требуется.
        3. Иначе — действительно новый физический аккаунт (или единственный
           оставшийся кандидат внутри уже полностью is_old-группы, см.
           resolve_duplicate_group sticky-логику ниже) — создаётся новая
           запись с уже известным telegram_user_id (не оставляет его None
           до отдельного sync — в отличие от reader/inviter/manage.py
           add-account, здесь identity уже подтверждена живой сессией).

        resolve_duplicate_group() — та же финальная сверка, что и в
        sync_accounts(), защита от постороннего сценария (например, если
        именно эта попытка всё же создала новую запись, совпавшую с уже
        существующей is_old-группой). last_synced_at обновляется всегда —
        identity только что подтверждена живой сессией, независимо от
        того, какая из трёх веток сработала."""
        try:
            identity = await fetch_telegram_identity(pending.client)
        except Exception as exc:
            await self.cancel(chat_id)
            logger.warning(
                "get_me() не удался сразу после авторизации (chat_id=%s): %s", chat_id, type(exc).__name__,
            )
            return AuthOutcome(
                result=AuthResult.FAILED,
                error_summary="Авторизация прошла, но не удалось получить данные аккаунта.",
            )
        finally:
            await self._safe_disconnect(pending.client)
        self._pending.pop(chat_id, None)

        all_accounts = self._account_repository.list()
        existing_by_session = next(
            (a for a in all_accounts if a.session_path == pending.session_path), None,
        )
        if existing_by_session is not None:
            account = reconcile_account_identity(self._account_repository, existing_by_session, identity)
        else:
            existing_current = next(
                (
                    a for a in all_accounts
                    if a.telegram_user_id == identity.telegram_user_id and not a.is_old
                ),
                None,
            )
            if existing_current is not None:
                account = reconcile_account_identity(self._account_repository, existing_current, identity)
            else:
                display_name = f"@{identity.username}" if identity.username else pending.session_name
                account = self._account_repository.create(
                    name=display_name,
                    phone=identity.phone or pending.phone,
                    session_name=pending.session_name,
                    session_path=pending.session_path,
                    telegram_user_id=identity.telegram_user_id,
                )

        resolve_duplicate_group(self._account_repository, identity.telegram_user_id)
        account = self._account_repository.update(
            account.id, last_synced_at=datetime.now(timezone.utc),
        )
        return AuthOutcome(result=AuthResult.AUTHORIZED, account=account)

    async def cancel(self, chat_id: int) -> None:
        pending = self._pending.pop(chat_id, None)
        if pending is not None:
            await self._safe_disconnect(pending.client)

    def has_pending(self, chat_id: int) -> bool:
        return chat_id in self._pending

    @staticmethod
    async def _safe_disconnect(client: TelethonAuthClientLike) -> None:
        try:
            await client.disconnect()
        except Exception:
            logger.warning("Не удалось корректно отключить клиент авторизации.", exc_info=False)
