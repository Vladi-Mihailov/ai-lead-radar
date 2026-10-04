"""Тексты раздела "✉️ ЛС-кампании" (Phase 1 — только настройка). Только
форматирование уже готовых значений, как и texts.py."""

from reader.dm_campaigns.models import DAILY_LIMIT_MAX, DAILY_LIMIT_MIN, DmCampaign

DM_CAMPAIGNS_LABEL = "✉️ ЛС-кампании"

LIST_HEADER = "✉️ ЛС-кампании"
NOT_CONNECTED_NOTE = (
    "ℹ️ Режим: генерация черновиков. ЛС автоматически не отправляются."
)
NO_CAMPAIGNS_TEXT = "ЛС-кампаний нет."
CAMPAIGN_NOT_FOUND_TEXT = "⚠️ ЛС-кампания не найдена — откройте «✉️ ЛС-кампании» заново."
STALE_BUTTON_TEXT = "⚠️ Кнопка устарела — откройте «✉️ ЛС-кампании» заново."
SAVED_TEXT = "✅ Сохранено"

ENABLE_LABEL = "▶️ Включить"
DISABLE_LABEL = "⏸ Выключить"
GUIDELINE_LABEL = "📝 AI-инструкция"
RESOURCES_LABEL = "🔗 Ресурсы"
SOURCES_LABEL = "📍 Источники"
ACCOUNTS_LABEL = "👤 Аккаунты"
FOLLOW_UP_ENABLE_LABEL = "▶️ Follow-up"
FOLLOW_UP_DISABLE_LABEL = "⏸ Follow-up"
FOLLOW_UP_TEXT_LABEL = "📝 Follow-up инструкция"
BACK_LABEL = "⬅️ Назад"
DONE_LABEL = "✅ Готово"
ALL_GROUPS_LABEL = "🌐 Все группы"

ALL_GROUPS_TEXT = "все отслеживаемые группы"
CLEAR_MARKER = "-"

_PREVIEW_LENGTH = 700


def state_icon(enabled: bool) -> str:
    return "🟢" if enabled else "🔴"


def format_list_screen(campaigns: list[DmCampaign]) -> str:
    if not campaigns:
        return f"{LIST_HEADER}\n\n{NO_CAMPAIGNS_TEXT}"
    lines = [f"{state_icon(c.enabled)} {c.title}" for c in campaigns]
    return f"{LIST_HEADER}\n\n" + "\n".join(lines) + f"\n\n{NOT_CONNECTED_NOTE}"


def _preview(text: str | None, empty: str) -> str:
    if not text:
        return empty
    if len(text) > _PREVIEW_LENGTH:
        return text[:_PREVIEW_LENGTH] + "…"
    return text


def format_sources(source_labels: list[str]) -> str:
    return "\n".join(source_labels) if source_labels else ALL_GROUPS_TEXT


def format_campaign_card(campaign: DmCampaign, *, source_labels: list[str], accounts_selected: int) -> str:
    status = "🟢 включена" if campaign.enabled else "🔴 выключена"
    follow_up = "🟢 включён" if campaign.follow_up_enabled else "🔴 выключен"
    resources = "\n".join(campaign.resources) if campaign.resources else "не заданы"
    accounts = f"{accounts_selected} выбрано" if accounts_selected else "не выбраны"
    return "\n".join([
        campaign.title,
        "",
        f"Статус: {status}",
        NOT_CONNECTED_NOTE,
        "",
        "AI-инструкция:",
        _preview(campaign.ai_guideline, "не задана"),
        "",
        "Ресурсы:",
        resources,
        "",
        "Источники:",
        format_sources(source_labels),
        "",
        "Аккаунты:",
        accounts,
        "",
        f"Follow-up: {follow_up}",
        "Инструкция follow-up:",
        _preview(campaign.follow_up_guideline, "не задана"),
    ])


def _current_block(current: str | None) -> str:
    return f"Сейчас:\n{current}" if current else "Сейчас: не задано"


def format_guideline_prompt(campaign: DmCampaign) -> str:
    return (
        f"{GUIDELINE_LABEL} — {campaign.title}\n\n"
        f"{_current_block(_preview(campaign.ai_guideline, ''))}\n\n"
        "Пришлите новую инструкцию одним сообщением. Это указание для AI, "
        "а не готовый текст ЛС.\n"
        f"Чтобы очистить — отправьте «{CLEAR_MARKER}».\n"
        "Для отмены нажмите «Отмена»."
    )


def format_follow_up_prompt(campaign: DmCampaign) -> str:
    return (
        f"{FOLLOW_UP_TEXT_LABEL} — {campaign.title}\n\n"
        f"{_current_block(_preview(campaign.follow_up_guideline, ''))}\n\n"
        "Пришлите инструкцию для возможного второго сообщения одним сообщением.\n"
        f"Чтобы очистить — отправьте «{CLEAR_MARKER}».\n"
        "Для отмены нажмите «Отмена»."
    )


def format_resources_prompt(campaign: DmCampaign) -> str:
    current = "\n".join(campaign.resources) if campaign.resources else None
    return (
        f"{RESOURCES_LABEL} — {campaign.title}\n\n"
        f"{_current_block(current)}\n\n"
        "Введите ресурсы через запятую или каждый с новой строки, например:\n\n"
        "@tplgee\n@ProtocolGEbot\n\n"
        f"Чтобы очистить — отправьте «{CLEAR_MARKER}».\n"
        "Для отмены нажмите «Отмена»."
    )


def format_sources_screen(campaign: DmCampaign, *, selected_count: int, notice: str | None = None) -> str:
    current = f"выбрано групп: {selected_count}" if selected_count else ALL_GROUPS_TEXT
    text = (
        f"{SOURCES_LABEL} — {campaign.title}\n\n"
        f"Сейчас: {current}\n\n"
        "☑ — группа выбрана. Если не выбрана ни одна, кампания относится "
        "ко всем отслеживаемым группам (config/groups.yaml)."
    )
    return f"{notice}\n\n{text}" if notice else text


def format_accounts_screen(campaign: DmCampaign, lines: list[str], *, notice: str | None = None) -> str:
    body = "\n".join(lines) if lines else "Аккаунтов пока нет."
    text = (
        f"{ACCOUNTS_LABEL} — {campaign.title}\n\n"
        f"{body}\n\n"
        "☑ — кампании разрешено использовать аккаунт. Это настройка, а не "
        "проверка доступности: состояние аккаунта (отключён/заблокирован) "
        "будет проверяться в момент отправки, когда она появится.\n"
        "🔢 — дневной лимит ЛС этого аккаунта в этой кампании."
    )
    return f"{notice}\n\n{text}" if notice else text


def format_limit_value(daily_limit: int | None) -> str:
    return str(daily_limit) if daily_limit is not None else "не задан"


def format_limit_prompt(campaign: DmCampaign, account_label: str, current: int | None) -> str:
    return (
        f"🔢 Дневной лимит ЛС — {account_label}, {campaign.title}\n\n"
        f"Сейчас: {format_limit_value(current)}\n\n"
        f"Введите число от {DAILY_LIMIT_MIN} до {DAILY_LIMIT_MAX}.\n"
        f"Чтобы сбросить (не задан) — отправьте «{CLEAR_MARKER}».\n"
        "Для отмены нажмите «Отмена»."
    )


def format_invalid_input(error: str, prompt: str) -> str:
    return f"❌ {error}\n\n{prompt}"
