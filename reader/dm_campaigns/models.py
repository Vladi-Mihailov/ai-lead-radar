"""Конфигурация персональных ЛС-кампаний (Phase 1 — только настройки).

Ни одна сущность здесь ничего не отправляет и ни к чему не подключается:
dm_campaigns.enabled/allowlist аккаунтов — это то, что МОЖНО будет
использовать, когда появится обработка ЛС (Phase 2/3), а не сигнал
что-либо запускать сейчас.

Аккаунты НЕ копируются: DmCampaignAccount.account_id — это
telegram_accounts.id (см. reader/inviter/models.py::TelegramAccount),
сессии/состояние аккаунта по-прежнему живут только там."""

from dataclasses import dataclass
from datetime import datetime

# Ограничения ввода менеджера — только защита от случайного мусора/
# переполнения карточки Telegram (4096 символов), не бизнес-политика.
MAX_GUIDELINE_LENGTH = 2000
MAX_LIST_ITEMS = 30
MAX_LIST_ITEM_LENGTH = 100

# Дневной лимит ЛС на пару (кампания, аккаунт): None — не настроен (по
# умолчанию). Phase 1 лимит только хранит — ни отправки, ни подсчёта нет.
DAILY_LIMIT_MIN = 1
DAILY_LIMIT_MAX = 50


@dataclass(frozen=True)
class DmCampaign:
    """source_chats — пустой кортеж означает "все отслеживаемые группы"
    Reader (config/groups.yaml); непустой — только перечисленные
    идентификаторы (Group.identifier: username или числовой id, тот же,
    что и в groups.yaml). fresh_context_chats — пока только хранится."""

    id: int
    key: str
    title: str
    enabled: bool
    scenario_name: str | None
    ai_guideline: str | None
    resources: tuple[str, ...]
    source_chats: tuple[str, ...]
    fresh_context_chats: tuple[str, ...]
    follow_up_enabled: bool
    follow_up_guideline: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class DmCampaignAccount:
    campaign_id: int
    account_id: int
    daily_limit: int | None


def normalize_list_input(raw: str | None) -> tuple[str, ...]:
    """Список из ввода менеджера: через запятую и/или с новой строки.
    trim, без пустых элементов, без повторов (регистр не важен — Telegram
    username регистронезависим), порядок первого появления сохраняется."""
    if not raw:
        return ()
    seen: set[str] = set()
    items: list[str] = []
    for part in raw.replace("\n", ",").split(","):
        item = part.strip()
        if not item or item.lower() in seen:
            continue
        seen.add(item.lower())
        items.append(item)
    return tuple(items)


def normalize_text_input(raw: str | None) -> str | None:
    """Свободный текст инструкции: trim, пустое -> None (очищено)."""
    if raw is None:
        return None
    text = raw.strip()
    return text or None


def same_chat_identifier(a: str, b: str) -> bool:
    """Сравнение идентификаторов групп из groups.yaml: регистр и ведущий
    "@" не важны (Telegram username регистронезависим)."""
    return a.strip().lstrip("@").lower() == b.strip().lstrip("@").lower()


def source_chat_allowed(campaign: DmCampaign, chat_identifier: str | None) -> bool:
    """Пустой source_chats — все отслеживаемые группы; иначе только
    перечисленные (сравнение по Group.identifier, не по числовому chat_id)."""
    if not campaign.source_chats:
        return True
    if not chat_identifier:
        return False
    return any(same_chat_identifier(chat_identifier, source) for source in campaign.source_chats)
