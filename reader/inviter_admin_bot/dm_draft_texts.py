"""Тексты раздела "📨 Черновики" — только форматирование. Карточка НЕ
содержит user_id/username получателя, access_hash, ref-меток контекста
(used_context_refs) и данных сессий/авторизации."""

from reader.dm_campaigns.outreach_repository import (
    STATUS_APPROVED,
    STATUS_DRAFT,
    STATUS_FAILED,
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
EDIT_LABEL = "✏️ Изменить"
SKIP_LABEL = "⏭ Пропустить"
BACK_LABEL = "⬅️ Назад"

QUEUE_LABELS = {
    cb.QUEUE_NEW: "🆕 Новые",
    cb.QUEUE_APPROVED: "✅ Одобренные",
    cb.QUEUE_SKIPPED: "⏭ Пропущенные",
    cb.QUEUE_FAILED: "❌ Ошибки",
}
QUEUE_STATUS = {
    cb.QUEUE_NEW: STATUS_DRAFT,
    cb.QUEUE_APPROVED: STATUS_APPROVED,
    cb.QUEUE_SKIPPED: STATUS_SKIPPED,
    cb.QUEUE_FAILED: STATUS_FAILED,
}

MANUAL_MODE_NOTE = (
    "ℹ️ Ручной режим: каждый черновик проверяет оператор. "
    "Автоматической отправки нет."
)
NOT_SENT_NOTE = (
    "ℹ️ Реальная отправка ЛС появится после реализации выбора отправителя — "
    "сейчас ничего не отправлено."
)
ALREADY_PROCESSED_TEXT = "⚠️ Этот черновик уже обработан."
DRAFT_NOT_FOUND_TEXT = "⚠️ Черновик не найден."
STALE_BUTTON_TEXT = "⚠️ Кнопка устарела — откройте «📨 Черновики» заново."
APPROVED_TEXT = "✅ Одобрено."
SKIPPED_TEXT = "⏭ Пропущено."
EDITED_TEXT = "✏️ Текст черновика обновлён."

_SENDABILITY_LABELS = {
    SENDABILITY_USERNAME: "username — можно написать по @username",
    SENDABILITY_SOURCE_MESSAGE: "source_message — через исходное сообщение",
    SENDABILITY_PREMIUM_REQUIRED: "premium_required — нужен Premium-отправитель",
}
_STATUS_LABELS = {
    STATUS_DRAFT: "🆕 ждёт решения",
    STATUS_APPROVED: "✅ одобрен",
    STATUS_SKIPPED: "⏭ пропущен",
    STATUS_FAILED: "❌ ошибка",
}

_SOURCE_PREVIEW = 1000
_LIST_PREVIEW = 40


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
    return f"#{item.id} {title} · {_clip(item.source_text, _LIST_PREVIEW)}"


def format_card(item: DmOutreach, *, campaign_title: str, notice: str | None = None) -> str:
    group = item.source_chat_title or item.source_chat_identifier or "группа"
    lines = [
        f"📨 Черновик #{item.id} — {_STATUS_LABELS.get(item.status, item.status)}",
        "",
        f"Кампания: {campaign_title}",
        f"Группа: {group}",
    ]
    if item.source_link:
        lines.append(f"Ссылка: {item.source_link}")
    lines += [
        "",
        "Исходное сообщение:",
        _clip(item.source_text, _SOURCE_PREVIEW),
        "",
        f"Sendability: {sendability_label(item.sendability)}",
    ]
    if item.status == STATUS_FAILED:
        lines += ["", f"Ошибка генерации: {item.error_kind or 'неизвестно'}"]
    else:
        lines += ["", "Черновик:", item.primary_text or "—"]
        if item.follow_up_text:
            lines += ["", "Follow-up:", item.follow_up_text]
    if item.edited_at is not None:
        lines += ["", f"✏️ Отредактировано оператором: {format_tbilisi(item.edited_at)}"]
    if item.reviewed_at is not None and item.status in (STATUS_APPROVED, STATUS_SKIPPED):
        verb = "Одобрено" if item.status == STATUS_APPROVED else "Пропущено"
        lines.append(f"{verb}: {format_tbilisi(item.reviewed_at)}")
    if item.status == STATUS_APPROVED:
        lines += ["", NOT_SENT_NOTE]
    text = "\n".join(lines)
    return f"{notice}\n\n{text}" if notice else text


def format_edit_prompt(item: DmOutreach) -> str:
    return (
        f"{EDIT_LABEL} — черновик #{item.id}\n\n"
        f"Сейчас:\n{item.primary_text or '—'}\n\n"
        "Пришлите новый текст сообщения одним сообщением — он заменит текст "
        "черновика (исходный AI-вариант сохранится).\n"
        "Для отмены нажмите «Отмена»."
    )
