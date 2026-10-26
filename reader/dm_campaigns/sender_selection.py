"""Выбор ЛС-отправителя кампании — только по БД, без сети и без кода
отправки. Используют send_service.py (plan() при отправке) и Reader
(ожидаемый отправитель для формы речи в черновике; Reader сам ЛС не шлёт).

Фильтры: allowlist кампании, can_send_dm/Premium (dm_sender_eligible),
is_old, blocked_until, DM-блок отправителя (PeerFlood и т.п.), наличие
session-файла, FloodWait, общий потолок за 24 ч (cap; None/0 — без
потолка; cap_overrides — явный потолок конкретного аккаунта по id), лимит
пары кампания-аккаунт. Порядок: меньше отправок за 24 ч,
затем меньший id.

Черновик, получателя которого отправитель резолвит через исходное сообщение
(needs_source_access), не достаётся отправителю с действующим "нет доступа к
этой группе" (source_access.py); отправители с подтверждённым доступом —
первыми. Никого не осталось — NO_SENDER_FOR_SOURCE."""

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta

from reader.dm_campaigns.outreach_repository import DmOutreach, DmOutreachRepository
from reader.dm_campaigns.repository import DmCampaignRepository
from reader.dm_campaigns.sender_state import DmSenderStateRepository
from reader.dm_campaigns.sendability import (
    SENDABILITY_PREMIUM_REQUIRED,
    SENDABILITY_SOURCE_MESSAGE,
    dm_sender_eligible,
)
from reader.dm_campaigns.source_access import DmSourceAccessRepository

NO_ELIGIBLE_SENDER = "no_eligible_sender"
NO_PREMIUM_SENDER = "no_eligible_premium_sender"
NO_SENDER_FOR_SOURCE = "no_eligible_sender_for_source"
DAILY_LIMIT = "daily_limit_reached"
SENDER_FLOOD_WAIT = "sender_flood_wait"

_DAY = timedelta(hours=24)


def needs_source_access(item: DmOutreach) -> bool:
    """Тот же выбор пути, что и send_service._resolve: source_message и
    premium_required без username резолвятся через исходное сообщение."""
    return item.sendability == SENDABILITY_SOURCE_MESSAGE or (
        item.sendability == SENDABILITY_PREMIUM_REQUIRED and not item.recipient_username
    )


class SenderSelector:
    def __init__(
        self,
        outreach_repository: DmOutreachRepository,
        campaign_repository: DmCampaignRepository,
        account_repository,
        sender_states: DmSenderStateRepository,
        *,
        session_exists: Callable[[object], bool],
        source_access: DmSourceAccessRepository | None = None,
    ):
        self._outreach = outreach_repository
        self._campaigns = campaign_repository
        self._accounts = account_repository
        self._states = sender_states
        self._session_exists = session_exists
        self._source_access = source_access

    def rank(self, item: DmOutreach, campaign, now: datetime, *, cap: int | None,
             cap_overrides: Mapping[int, int] | None = None) -> tuple[str | None, tuple]:
        """(причина, ()) если подходящих нет, иначе (None, отправители по порядку)."""
        usable = []
        for entry in self._campaigns.list_campaign_accounts(campaign.id):
            account = self._accounts.get(entry.account_id)
            if account is None or account.is_old or not dm_sender_eligible(account, item.sendability):
                continue
            if account.blocked_until is not None and account.blocked_until > now:
                continue
            state = self._states.get(account.id)
            if state.blocked_reason or not self._session_exists(account):
                continue
            usable.append((account, entry, state))
        if not usable:
            return (NO_PREMIUM_SENDER if item.sendability == SENDABILITY_PREMIUM_REQUIRED else NO_ELIGIBLE_SENDER), ()

        confirmed: set[int] = set()
        if self._source_access is not None and needs_source_access(item):
            reachable = []
            for u in usable:
                access = self._source_access.get(u[0].id, item.source_chat_id)
                if access is not None and access.denies(now):
                    continue
                if access is not None and access.has_access:
                    confirmed.add(u[0].id)
                reachable.append(u)
            if not reachable:
                return NO_SENDER_FOR_SOURCE, ()
            usable = reachable

        awake = [u for u in usable if not (u[2].flood_wait_until and u[2].flood_wait_until > now)]
        if not awake:
            return SENDER_FLOOD_WAIT, ()

        candidates = []
        for account, entry, _ in awake:
            total = self._outreach.sends_since(sender_account_id=account.id, since=now - _DAY)
            limit = (cap_overrides or {}).get(account.id, cap)
            if limit and total >= limit:  # None/0 — потолок выключен
                continue
            if entry.daily_limit is not None and self._outreach.sends_since(
                sender_account_id=account.id, since=now - _DAY, campaign_id=campaign.id,
            ) >= entry.daily_limit:
                continue
            candidates.append((account.id not in confirmed, total, account.id, account))
        if not candidates:
            return DAILY_LIMIT, ()
        return None, tuple(c[3] for c in sorted(candidates, key=lambda c: c[:3]))

    def expected_sender(self, item: DmOutreach, now: datetime, *, cap: int | None,
                        cap_overrides: Mapping[int, int] | None = None):
        """Первый отправитель по тому же порядку, что и preview; None — нет.
        Фактический отправитель выбирается при отправке и может отличаться."""
        campaign = self._campaigns.get_campaign(item.campaign_id)
        if campaign is None:
            return None
        reason, ranked = self.rank(item, campaign, now, cap=cap, cap_overrides=cap_overrides)
        return None if reason else ranked[0]
