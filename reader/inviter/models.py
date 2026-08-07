from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class TelegramAccount:
    """Telegram-аккаунт, которым инвайтер может приглашать пользователей в
    target_chat кампаний (см. InviteCampaign). daily_limit/enabled —
    сырые данные конфигурации; сама логика лимитов/отбора аккаунта здесь
    не реализована (см. service.py)."""

    id: int
    name: str
    phone: str
    session_name: str
    session_path: str
    daily_limit: int
    enabled: bool
    created_at: datetime
    last_used_at: datetime | None


@dataclass(frozen=True)
class InviteCampaign:
    """Кампания приглашений: пользователи, у которых сохранён keyword,
    приглашаются в target_chat. Подбор пользователей по keyword и сама
    отправка приглашений — за пределами этого этапа (см. service.py)."""

    id: int
    name: str
    keyword: str
    target_chat: str
    enabled: bool
    created_at: datetime


@dataclass(frozen=True)
class InviteCandidate:
    """Один пользователь-кандидат на приглашение — срез данных из users
    (не полноценная TelegramUserInfo из reader/users/models.py), собранный
    под условия конкретной кампании (см.
    UserCampaignInviteRepository.select_candidates)."""

    user_id: int
    username: str | None
    keywords: list[str]
    access_hash: int
    last_seen_at: datetime | None
    # users.is_bot на момент выборки — True уже не должно попадать сюда
    # вовсе (см. _CANDIDATES_BASE_WHERE), False — статус подтверждён
    # Telethon при последней синхронизации, None — неизвестен (см.
    # InviterService._resolve_input_peer — только для None делается
    # дополнительная проверка перед отправкой приглашения).
    is_bot: bool | None = None


@dataclass(frozen=True)
class UserCampaignInvite:
    """Одна попытка приглашения конкретного пользователя в рамках кампании.

    account_id — какой TelegramAccount выполнил (или должен выполнить)
    приглашение; None, пока аккаунт не выбран. status/error — сырые
    значения, задаваемые вызывающим кодом; набор допустимых статусов и
    переходы между ними здесь не определяются (без бизнес-логики)."""

    id: int
    user_id: int
    campaign_id: int
    account_id: int | None
    status: str
    error: str | None
    invited_at: datetime | None
    created_at: datetime
    updated_at: datetime
