"""View-модели @-бота управления инвайтером — чистые dataclasses, никакой
Telethon/SQLite здесь нет (см. service.py, который их собирает поверх УЖЕ
существующих reader/inviter/repository.py репозиториев). Ничего из этого
не хранится в БД само по себе — это только форма ответа service.py для
conversation.py/texts.py."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from reader.inviter.models import TelegramAccount


@dataclass(frozen=True)
class AccountListEntry:
    """Одна строка "👤 Аккаунты" (см. design: "показывать существующие
    telegram_accounts"). display_name уже разрешён по правилу "username
    если есть, иначе Telegram ID, иначе номер сессии" (см.
    service.py::_display_name) — conversation.py/keyboards.py никогда
    сами не решают, что показывать вместо отсутствующего username."""

    id: int
    display_name: str
    enabled: bool


@dataclass(frozen=True)
class AccountUsage:
    """Один расчёт остатка дневного лимита — ТА ЖЕ формула, что и
    InviterService._remaining_daily_budget (daily_limit - joined_today -
    pending_today), см. service.py::_account_usage. Никакого отдельного
    счётчика — оба числа получены напрямую из
    UserCampaignInviteRepository, как и в самом инвайтере."""

    sent_today: int
    daily_limit: int

    @property
    def remaining(self) -> int:
        return max(0, self.daily_limit - self.sent_today)


@dataclass(frozen=True)
class AccountCard:
    """Полная карточка одного аккаунта (см. design: "Карточка аккаунта")."""

    account: TelegramAccount
    display_name: str
    session_exists: bool
    usage: AccountUsage


@dataclass(frozen=True)
class SyncAccountOutcome:
    """Один результат синхронизации ОДНОГО аккаунта — тот же набор
    статусов, что и reader/inviter/manage.py::AccountSyncResult
    (connect_failed/not_authorized/identity_mismatch/updated/unchanged),
    плюс display_name уже готовый для показа (см. service.py::sync_one_account/
    sync_all_accounts — оба переиспользуют reader/inviter/manage.py::
    sync_accounts()/resolve_all_duplicates() напрямую, не пересчитывают
    identity сами)."""

    account_id: int
    display_name: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class SyncSummary:
    """Итог "🔄 Синхронизировать" (см. design: "Проверено / Авторизовано /
    Требуют внимания / Username обновлён")."""

    checked: int
    authorized: int
    needs_attention: int
    username_updated: int
    results: tuple[SyncAccountOutcome, ...] = ()


@dataclass(frozen=True)
class AttentionItem:
    """Одна строка "⚠️ Требуют внимания" на экране "📊 Статус"."""

    display_name: str
    reason: str


@dataclass(frozen=True)
class StatusSnapshot:
    """Экран "📊 Статус" (см. design). worker_alive — application-level
    heartbeat (см. reader/inviter/runtime_state_repository.py), НЕ
    systemd-статус: admin-бот работает отдельным процессом от worker'а и
    не имеет безопасного способа спросить systemd напрямую (см. design:
    "если Telegram bot process не имеет безопасного способа узнать
    systemd status, показывать application-level worker status/heartbeat")."""

    inviter_enabled: bool
    worker_alive: bool
    last_tick_at: datetime | None
    next_tick_at: datetime | None
    active_count: int
    disabled_count: int
    blocked_count: int
    sent_today: int
    pending_today: int
    failed_today: int
    campaign_name: str | None
    campaign_target_chat: str | None
    attention: tuple[AttentionItem, ...] = ()


class AuthResult(StrEnum):
    """Итог одного шага авторизации нового аккаунта (см. auth.py) —
    conversation.py решает следующий текст/шаг ИСКЛЮЧИТЕЛЬНО по этому
    значению, никогда не по содержимому исключения (см. auth.py про
    запрет логировать/показывать код и 2FA-пароль)."""

    CODE_SENT = "code_sent"
    NEEDS_PASSWORD = "needs_password"
    AUTHORIZED = "authorized"
    INVALID_CODE = "invalid_code"
    INVALID_PASSWORD = "invalid_password"
    FAILED = "failed"


@dataclass(frozen=True)
class AuthOutcome:
    """result — см. AuthResult. account — задан ТОЛЬКО при
    result==AUTHORIZED (см. auth.py::AccountAuthCoordinator.submit_code/
    submit_password). error_summary — БЕЗОПАСНОЕ для показа пользователю
    сообщение (см. design: "не показывать traceback"), НИКОГДА не
    содержит сам код/пароль (их здесь просто нет — auth.py передаёт их
    напрямую в Telethon, не в это поле)."""

    result: AuthResult
    account: TelegramAccount | None = None
    error_summary: str | None = None


@dataclass(frozen=True)
class NewAccountDraft:
    """In-memory состояние диалога "➕ Добавить аккаунт" МЕЖДУ шагами (см.
    conversation_state_repository.py — ТОЛЬКО номер телефона персистится в
    БД; сам объект AccountAuthCoordinator/TelegramClient — исключительно в
    памяти процесса, см. auth.py) — phone НЕ секрет (в отличие от кода/
    пароля), поэтому в БД хранить можно."""

    phone: str
    fields: dict = field(default_factory=dict)
