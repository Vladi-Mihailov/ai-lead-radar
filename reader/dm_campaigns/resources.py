"""Какие ресурсы можно упомянуть в ЛС-черновике — по resource_region ИСХОДНОЙ
группы (config/groups.yaml) и по смыслу вопроса, а не автоматически.

- Ресурсы кампании (например, @tplgee для insurance) — как раньше, из
  настроек кампании.
- @ProtocolGEbot / @ProtocolTRbot — ТОЛЬКО контекстно: GE-группа + вопрос
  про штрафы → @ProtocolGEbot; TR-группа + штрафы/платные дороги/HGS →
  @ProtocolTRbot. region am или unknown — ни один из них. Даже если
  менеджер добавил Protocol-бота в ресурсы кампании, без подходящих
  resource_region и смысла он из разрешённых убирается (не рекламный хвост).

Проверка на сервере — та же normalize_draft(allowed_resources=...): любое
упоминание ресурса вне этого набора делает черновик invalid."""

import re

from reader.groups import REGION_UNKNOWN

PROTOCOL_GE = "@ProtocolGEbot"
PROTOCOL_TR = "@ProtocolTRbot"
_PROTOCOL_HANDLES = frozenset({"protocolgebot", "protocoltrbot"})

_FINE_RE = re.compile(r"штраф|\bfines?\b", re.IGNORECASE)
_TOLL_RE = re.compile(r"платн\w*\s+(?:дорог|трасс|мост)|\btolls?\b|\bhgs\b|\bogs\b|начислени", re.IGNORECASE)

# Что можно честно сказать о ресурсе (для промпта) — без обещания сроков.
RESOURCE_HINTS = {
    PROTOCOL_GE: "может прислать штраф, как только он появится в базе (сроки появления не обещать)",
    PROTOCOL_TR: (
        "может прислать штрафы и начисления по платным дорогам, как только они появятся в базе "
        "(сроки появления не обещать)"
    ),
}


def _handle(resource: str) -> str:
    return resource.strip().lstrip("@").lower()


def has_fine_intent(text: str) -> bool:
    return bool(_FINE_RE.search(text or ""))


def has_toll_intent(text: str) -> bool:
    return bool(_TOLL_RE.search(text or ""))


def contextual_resource(*, resource_region: str, intent_text: str) -> str | None:
    if resource_region == "ge" and has_fine_intent(intent_text):
        return PROTOCOL_GE
    if resource_region == "tr" and (has_fine_intent(intent_text) or has_toll_intent(intent_text)):
        return PROTOCOL_TR
    return None


def allowed_resources(
    campaign_resources: tuple[str, ...], *, resource_region: str | None, intent_text: str,
) -> tuple[str, ...]:
    """Ресурсы кампании без Protocol-ботов + (если подходят region и смысл)
    ровно один страновой Protocol-бот (только для region ge/tr)."""
    base = [r for r in campaign_resources if _handle(r) not in _PROTOCOL_HANDLES]
    extra = contextual_resource(resource_region=resource_region or REGION_UNKNOWN, intent_text=intent_text)
    return tuple(base + [extra]) if extra else tuple(base)
