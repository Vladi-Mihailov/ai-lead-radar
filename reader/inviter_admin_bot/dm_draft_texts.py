"""Тексты раздела "📨 Черновики" — только форматирование. Карточка НЕ
содержит user_id/username получателя, access_hash, ref-меток контекста
(used_context_refs), числовых id аккаунтов и данных сессий/авторизации."""

from reader.dm_campaigns import send_service as reasons
from reader.dm_campaigns.outreach_repository import (
    STATUS_APPROVED,
    STATUS_BLOCKED,
    STATUS_DRAFT,
    STATUS_FAILED,
    STATUS_SEND_FAILED,
    STATUS_SENDING,
    STATUS_SENT,
    STATUS_SKIPPED,
    DmOutreach,
)
from reader.dm_campaigns.sendability import (
    SENDABILITY_PREMIUM_REQUIRED,
    SENDABILITY_SOURCE_MESSAGE,
    SENDABILITY_USERNAME,
)
from reader.inviter_admin_bot import dm_draft_callbacks as cb
from reader.time_display import format_tbilisi

DRAFTS_LABEL = "📨 Черновики"
APPROVE_LABEL = "✅ Отправить"
CONFIRM_SEND_LABEL = "✅ Да, отправить"
EDIT_LABEL = "✏️ Изменить"
SKIP_LABEL = "⏭ Пропустить"
BACK_LABEL = "⬅️ Назад"

QUEUE_LABELS = {
    cb.QUEUE_NEW: "🆕 Новые",
    cb.QUEUE_SENT: "📤 Отправленные",
    cb.QUEUE_NOT_SENT: "⚠️ Не отправлено",
    cb.QUEUE_SKIPPED: "⏭ Пропущенные",
    cb.QUEUE_FAILED: "❌ Ошибки",
    cb.QUEUE_APPROVED: "🗄 Одобрены до отправки (не отправлялись)",
}
QUEUE_STATUSES = {
    cb.QUEUE_NEW: (STATUS_DRAFT,),
    cb.QUEUE_SENT: (STATUS_SENT, STATUS_SENDING),
    cb.QUEUE_NOT_SENT: (STATUS_BLOCKED, STATUS_SEND_FAILED),
    cb.QUEUE_SKIPPED: (STATUS_SKIPPED,),
    cb.QUEUE_FAILED: (STATUS_FAILED,),
    cb.QUEUE_APPROVED: (STATUS_APPROVED,),
}

MANUAL_MODE_NOTE = (
    "ℹ️ Ручной режим: ЛС отправляется только после нажатия оператора и "
    "подтверждения. Автоматической отправки нет."
)
LEGACY_APPROVED_NOTE = (
    "ℹ️ Одобрен до появления отправки — не отправлялся и автоматически отправлен не будет."
)
ALREADY_PROCESSED_TEXT = "⚠️ Этот черновик уже обработан."
DRAFT_NOT_FOUND_TEXT = "⚠️ Черновик не найден."
STALE_BUTTON_TEXT = "⚠️ Кнопка устарела — откройте «📨 Черновики» заново."
SKIPPED_TEXT = "⏭ Пропущено."
EDITED_TEXT = "✏️ Текст черновика обновлён."
SENT_TEXT = "✅ Отправлено"
NOT_SENT_TEXT = "⚠️ Не отправлено"

_SENDABILITY_LABELS = {
    SENDABILITY_USERNAME: "username — можно написать по @username",
    SENDABILITY_SOURCE_MESSAGE: "source_message — через исходное сообщение",
    SENDABILITY_PREMIUM_REQUIRED: "premium_required — нужен Premium-отправитель",
}
_STATUS_LABELS = {
    STATUS_DRAFT: "🆕 ждёт решения",
    STATUS_SENDING: "⏳ отправляется",
    STATUS_SENT: "✅ отправлен",
    STATUS_BLOCKED: "⚠️ не отправлен",
    STATUS_SEND_FAILED: "⚠️ не отправлен",
    STATUS_APPROVED: "🗄 одобрен до отправки",
    STATUS_SKIPPED: "⏭ пропущен",
    STATUS_FAILED: "❌ ошибка генерации",
}

_REASONS = {
    reasons.SYNTHETIC: "тестовая (синтетическая) запись — отправка запрещена",
    reasons.CAMPAIGN_NOT_MANUAL: "кампания не в ручном режиме",
    reasons.NO_RECIPIENT: "получатель не определён (нет username и исходного сообщения)",
    reasons.NO_TEXT: "пустой текст черновика",
    reasons.NO_ELIGIBLE_SENDER: "нет подходящего отправителя среди аккаунтов кампании",
    reasons.NO_PREMIUM_SENDER: "нужен Premium-отправитель — подходящего Premium-аккаунта в кампании нет",
    reasons.RECENT_DM: "этому человеку уже писали недавно (кулдаун получателя)",
    reasons.DAILY_LIMIT: "у отправителей исчерпан дневной лимит ЛС — черновик остаётся в очереди",
    reasons.SENDER_FLOOD_WAIT: "отправитель на паузе Telegram (FloodWait) — черновик остаётся в очереди",
    reasons.SENDER_UNAUTHORIZED: "сессия отправителя не авторизована — аккаунт снят с отправки до проверки",
    reasons.NOT_IN_SOURCE_CHAT: "у отправителя нет доступа к исходной группе или сообщению",
    reasons.IDENTITY_MISMATCH: "найденный пользователь не совпадает с автором вопроса",
    reasons.USERNAME_NOT_FOUND: "username получателя не найден",
    reasons.PRIVACY: "настройки приватности получателя запрещают сообщения",
    reasons.USER_BLOCKED: "получатель заблокировал отправителя",
    reasons.CHAT_WRITE_FORBIDDEN: "писать этому получателю нельзя",
    reasons.PEER_INVALID: "Telegram не принял получателя",
    reasons.USER_DEACTIVATED: "аккаунт получателя удалён",
    reasons.PEER_FLOOD: (
        "Telegram ограничил отправителя (PeerFlood) — аккаунт снят с отправки до ручной проверки, "
        "черновик возвращён в очередь"
    ),
}


def reason_text(reason: str | None) -> str:
    if not reason:
        return "неизвестно"
    if reason in _REASONS:
        return _REASONS[reason]
    code, _, detail = reason.partition(":")
    if code == reasons.FLOOD_WAIT:
        return f"Telegram попросил подождать {detail} с — отправитель на паузе, черновик возвращён в очередь"
    if code.startswith("uncertain"):
        return ("связь оборвалась во время отправки — неизвестно, дошло ли сообщение. Автоматически не "
                "повторяется: проверьте вручную в чате отправителя")
    if code == "transport_before_send":
        return "сбой связи до отправки — сообщение не отправлено, черновик возвращён в очередь"
    if code == "rpc_error":
        return f"Telegram отклонил отправку ({detail})"
    if code == "internal_error":
        return f"внутренняя ошибка до отправки ({detail}) — сообщение не отправлено"
    return reason


def _clip(text: str | None, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def sendability_label(value: str) -> str:
    return _SENDABILITY_LABELS.get(value, f"{value} — получатель не определён")


def format_menu(counts: dict[str, int]) -> str:
    lines = [f"{QUEUE_LABELS[q]}: {counts.get(q, 0)}" for q in cb.QUEUES]
    return f"{DRAFTS_LABEL}\n\n" + "\n".join(lines) + f"\n\n{MANUAL_MODE_NOTE}"


def format_queue(queue: str, items: list[DmOutreach], total: int) -> str:
    header = f"{DRAFTS_LABEL} — {QUEUE_LABELS[queue]} ({total})"
    if not items:
        return f"{header}\n\nПусто."
    shown = f"\n\nПоказаны последние {len(items)}." if total > len(items) else ""
    return f"{header}\n\nВыберите черновик:{shown}"


def queue_item_label(item: DmOutreach, titles: dict[int, str]) -> str:
    title = titles.get(item.campaign_id, f"кампания #{item.campaign_id}")
    return f"#{item.id} {title} · {_clip(item.source_text, 40)}"


def format_card(item: DmOutreach, *, campaign_title: str, sender_label: str | None = None,
                notice: str | None = None) -> str:
    group = item.source_chat_title or item.source_chat_identifier or "группа"
    lines = []
    if item.status == STATUS_SENT:
        lines += [SENT_TEXT, f"Отправитель: {sender_label or '—'}"]
        if item.sent_at is not None:
            lines.append(f"Время: {format_tbilisi(item.sent_at)}")
        lines.append("")
    elif item.status in (STATUS_BLOCKED, STATUS_SEND_FAILED):
        lines += [NOT_SENT_TEXT, f"Причина: {reason_text(item.send_error)}"]
        if sender_label:
            lines.append(f"Отправитель: {sender_label}")
        lines.append("")
    lines += [
        f"📨 Черновик #{item.id} — {_STATUS_LABELS.get(item.status, item.status)}",
        "",
        f"Кампания: {campaign_title}",
        f"Группа: {group}",
    ]
    if item.source_link:
        lines.append(f"Ссылка: {item.source_link}")
    lines += ["", "Исходное сообщение:", _clip(item.source_text, 1000), "",
              f"Sendability: {sendability_label(item.sendability)}"]
    if item.status == STATUS_FAILED:
        lines += ["", f"Ошибка генерации: {item.error_kind or 'неизвестно'}"]
    else:
        lines += ["", "Черновик:", item.primary_text or "—"]
    if item.edited_at is not None:
        lines += ["", f"✏️ Отредактировано оператором: {format_tbilisi(item.edited_at)}"]
    if item.status == STATUS_SKIPPED and item.reviewed_at is not None:
        lines.append(f"Пропущено: {format_tbilisi(item.reviewed_at)}")
    if item.status == STATUS_DRAFT and item.send_error:
        lines += ["", f"Прошлая попытка отправки: {reason_text(item.send_error)}"]
    if item.status == STATUS_APPROVED:
        lines += ["", LEGACY_APPROVED_NOTE]
    text = "\n".join(lines)
    return f"{notice}\n\n{text}" if notice else text


def format_confirm(item: DmOutreach, *, campaign_title: str, sender_label: str) -> str:
    group = item.source_chat_title or item.source_chat_identifier or "группа"
    return "\n".join([
        f"📤 Отправить ЛС по черновику #{item.id}?",
        "",
        f"Кампания: {campaign_title}",
        f"Группа: {group}",
        f"Отправитель: {sender_label}",
        "",
        "Текст сообщения:",
        item.primary_text or "—",
        "",
        "⚠️ Сообщение будет реально отправлено в Telegram от имени отправителя.",
    ])


def format_edit_prompt(item: DmOutreach) -> str:
    return (
        f"{EDIT_LABEL} — черновик #{item.id}\n\n"
        f"Сейчас:\n{item.primary_text or '—'}\n\n"
        "Пришлите новый текст сообщения одним сообщением — он заменит текст "
        "черновика (исходный AI-вариант сохранится).\n"
        "Для отмены нажмите «Отмена»."
    )
