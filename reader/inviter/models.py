from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class TelegramAccount:
    """Telegram-аккаунт, которым инвайтер может приглашать пользователей в
    target_chat кампаний (см. InviteCampaign). daily_limit/enabled —
    сырые данные конфигурации; сама логика лимитов/отбора аккаунта здесь
    не реализована (см. service.py).

    enabled и blocked_until — разные вещи, не путать: enabled=False —
    аккаунт отключён оператором вручную, до явного enabled=True снова не
    используется никогда; blocked_until — временное ограничение САМИМ
    Telegram (сейчас — только FloodWaitError, см. service.py
    _classify_invite_error/_persist_flood_wait_block), снимается
    автоматически по истечении времени, никакого ручного enabled=True не
    требует. blocked_reason — просто пояснение источника ограничения
    ("flood_wait"), не влияет на саму проверку (см. service.py
    _is_blocked_by_flood_wait).

    verify_membership — тоже НЕ то же самое, что enabled: enabled=False
    убирает аккаунт из инвайтера целиком, а verify_membership=False
    оставляет его приглашающим (InviteToChannelRequest/AddChatUserRequest
    работают как обычно, новый успешный RPC по-прежнему пишется как
    status='pending'), но запрещает именно
    InviterService._verify_pending_invites() — обычные (не админ) аккаунты
    Telegram может не давать права на GetParticipantRequest в конкретном
    target_chat, из-за чего эта проверка гарантированно проваливается на
    каждом pending (см. задачу про лог "Chat admin privileges are
    required..."/InvokeWithoutUpdatesRequest(GetParticipantRequest)) —
    без этого флага такие аккаунты бесполезно повторяли бы заведомо
    неработающий запрос на каждый цикл. По умолчанию True — обратная
    совместимость с уже существующими аккаунтами/поведением (см. задачу).

    telegram_user_id — СТАБИЛЬНЫЙ идентификатор физического Telegram-
    аккаунта (me.id из get_me(), см. reader/inviter/identity.py), в отличие
    от name/phone/session_name/session_path — все они изменяемые
    profile/конфигурационные атрибуты (см. задачу про обнаруженные дубли:
    два разных DB-ряда/session-файла, фактически авторизованные как ОДИН и
    тот же Telegram-аккаунт, и переименование @alena_ogi -> @ao777oa777 БЕЗ
    создания нового физического аккаунта). None — identity ещё не
    подтверждена/не заполнена (см. задачу про backfill существующих
    аккаунтов) — не значит "неизвестный/новый Telegram-аккаунт", просто
    "ещё не проверено через живую сессию"."""

    id: int
    name: str
    phone: str
    session_name: str
    session_path: str
    daily_limit: int
    enabled: bool
    created_at: datetime
    last_used_at: datetime | None
    blocked_until: datetime | None = None
    blocked_reason: str | None = None
    verify_membership: bool = True
    telegram_user_id: int | None = None


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
    переходы между ними здесь не определяются (без бизнес-логики).

    invited_at — момент, когда InviteToChannelRequest/AddChatUserRequest
    был принят Telegram (см. InviterService._record_invite_result) —
    ставится сразу для status='pending'/'joined', независимо от того,
    подтверждено ли участие. verified_at — момент, когда проверка pending
    (см. InviterService._verify_pending_invites) дала ДОСТОВЕРНЫЙ ответ:
    либо участие подтверждено (status='joined' — тоже сразу же, без
    отдельной проверки, для UserAlreadyParticipantError, см.
    _classify_invite_error), либо Telegram явно сказал, что участия нет
    (status='not_joined', UserNotParticipantError). None, пока status
    остаётся 'pending' — сбой самой проверки (не UserNotParticipantError)
    не считается достоверным ответом и НЕ переводит запись в 'not_joined'.

    daily_limit считается не по invited_at, а по joined_today +
    pending_today (см. InviterService._remaining_daily_budget) — pending
    временно резервирует место в лимите, пока не станет joined (место
    остаётся занятым) или not_joined (место освобождается — см. задачу
    про перелив лимита). status='invited'/'pending'/'joined' исключают
    запись из будущей выборки (см. _CANDIDATES_BASE_WHERE); 'not_joined',
    как и 'failed', НЕ исключает — остаётся кандидатом для следующего
    прогона."""

    id: int
    user_id: int
    campaign_id: int
    account_id: int | None
    status: str
    error: str | None
    invited_at: datetime | None
    verified_at: datetime | None
    created_at: datetime
    updated_at: datetime
