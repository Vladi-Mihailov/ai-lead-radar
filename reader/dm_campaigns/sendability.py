"""Требования к будущей отправке ЛС-черновика (Phase 3) — оценка БЕЗ сетевых
запросов, только по тому, что Reader уже знает о сообщении.

dm_outreach описывает получателя, исходное сообщение, черновик и ЭТИ
требования; конкретный sender-аккаунт к кандидату НЕ привязывается — он
выбирается только в момент ручной отправки (Phase 3). access_hash не
хранится: он привязан к аккаунту, и выбранный sender сам резолвит
получателя.

- premium_required — у получателя contact_require_premium: писать могут
  только Premium-аккаунты (и контакты). Резолв тот же, что ниже (username
  или исходное сообщение), но selector ОБЯЗАН выбрать Premium sender.
- username — у автора есть public @username: sender резолвит "@username".
- source_message — username нет, но есть recipient_user_id + исходное
  сообщение (source_chat_id + source_message_id): sender резолвит через
  InputPeerUserFromMessage(source_chat, source_message_id, recipient_user_id).
- unresolved — не оценивалось (строки до Phase 2.6, нет user_id).

Ни одно значение не фильтрует кандидата до черновика и не гарантирует
доставку (остальные настройки приватности и антиспам-лимиты видны только
при отправке). Для username/source_message будущий selector выбирает любой
подходящий sender (allowlist кампании, лимиты ЛС, FloodWait/PeerFlood,
lease сессии, доступ к исходной группе) — здесь это не реализуется."""

SENDABILITY_PREMIUM_REQUIRED = "premium_required"
SENDABILITY_USERNAME = "username"
SENDABILITY_SOURCE_MESSAGE = "source_message"
SENDABILITY_UNRESOLVED = "unresolved"

SENDABILITY_VALUES = frozenset({
    SENDABILITY_PREMIUM_REQUIRED, SENDABILITY_USERNAME, SENDABILITY_SOURCE_MESSAGE, SENDABILITY_UNRESOLVED,
})


def assess_sendability(
    *, username: str | None, recipient_user_id: int | None, source_chat_id: int | None,
    source_message_id: int | None, contact_require_premium: bool | None,
) -> str:
    if contact_require_premium:
        return SENDABILITY_PREMIUM_REQUIRED
    if username:
        return SENDABILITY_USERNAME
    if recipient_user_id is not None and source_chat_id is not None and source_message_id is not None:
        return SENDABILITY_SOURCE_MESSAGE
    return SENDABILITY_UNRESOLVED


def sender_meets_requirements(sendability: str, *, sender_is_premium: bool) -> bool:
    """Контракт для будущего sender selector (Phase 3): premium_required —
    ТОЛЬКО Premium sender; остальные значения Premium не требуют (прочие
    ограничения — лимиты, FloodWait, lease, доступ к группе — проверяет сам
    selector). Здесь нет ни выбора аккаунта, ни сессий, ни отправки."""
    if sendability not in SENDABILITY_VALUES:
        raise ValueError(f"Недопустимое значение sendability: {sendability!r}")
    if sendability == SENDABILITY_PREMIUM_REQUIRED:
        return sender_is_premium
    return True
