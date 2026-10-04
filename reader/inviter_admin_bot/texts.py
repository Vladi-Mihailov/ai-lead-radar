"""Тексты/форматирование сообщений reader/inviter_admin_bot/ — та же схема
разделения, что и у reader/public_bot/texts.py: только форматирование уже
готовых значений, никакой Telegram/БД/бизнес-логики здесь нет."""

from reader.inviter_admin_bot.models import (
    AccountCard,
    AccountStatusEntry,
    CampaignListEntry,
    CampaignStats,
    LeadRefreshOutcome,
    StatusSnapshot,
    SyncSummary,
)
from reader.time_display import format_tbilisi

MAIN_MENU_TEXT = "🤖 Управление инвайтером"
ACCESS_DENIED_TEXT = "⛔ Нет доступа"

ACCOUNTS_LABEL = "👤 Аккаунты"
ADD_ACCOUNT_LABEL = "➕ Добавить аккаунт"
START_LABEL = "▶️ Запустить"
PAUSE_LABEL = "⏸ Приостановить"
STATUS_LABEL = "📊 Статус"
SYNC_LABEL = "🔄 Синхронизировать"
HELP_LABEL = "ℹ️ Справка"
CAMPAIGNS_LABEL = "📣 Кампании"

CAMPAIGNS_HEADER = "📣 Кампании приглашений"
NO_CAMPAIGNS_TEXT = "Кампаний пока нет."
CAMPAIGN_ENABLE_LABEL = "▶️ Включить приглашения"
CAMPAIGN_DISABLE_LABEL = "⏸ Выключить приглашения"
CAMPAIGN_REFRESH_LABEL = "🔄 Обновить лиды"
CAMPAIGN_STATS_LABEL = "📊 Статистика"
CAMPAIGN_NOT_FOUND_TEXT = "⚠️ Кампания не найдена — откройте список заново через «📣 Кампании»."

TURN_OFF_ACCOUNT_LABEL = "⏸ Отключить"
TURN_ON_ACCOUNT_LABEL = "▶️ Включить"
CHANGE_LIMIT_LABEL = "⚙️ Изменить лимит"
CHECK_SYNC_LABEL = "🔄 Проверить / синхронизировать"
REAUTHORIZE_LABEL = "🔐 Переавторизовать"
BACK_BUTTON_LABEL = "⬅️ Назад"
CANCEL_BUTTON_LABEL = "Отмена"
MANUAL_LIMIT_LABEL = "Ввести вручную"

ACCOUNTS_HEADER = "👤 Аккаунты"
STATUS_ACCOUNTS_HEADER = "📊 Статус аккаунтов"
NO_ACCOUNTS_TEXT = "Аккаунтов пока нет. Добавьте первый через «➕ Добавить аккаунт»."

PHONE_PROMPT = "Введите номер телефона:\n\nпример:\n+995571024864"
INVALID_PHONE_TEXT = "❌ Похоже, это не номер телефона. Попробуйте ещё раз:\n\n" + PHONE_PROMPT
REQUESTING_CODE_TEXT = "⏳ Подключаемся и запрашиваем код..."
CODE_SENT_TEXT = "📩 Код отправлен.\nВведите код:"
PASSWORD_PROMPT_TEXT = "🔐 Введите пароль двухэтапной аутентификации:"
INVALID_CODE_RETRY_TEXT = "❌ Код неверный или устарел. Введите код ещё раз:"
INVALID_PASSWORD_RETRY_TEXT = "❌ Неверный пароль. Попробуйте ещё раз:"
STALE_DIALOG_TEXT = "⚠️ Диалог устарел, начните заново через «➕ Добавить аккаунт»."

LIMIT_PROMPT_CHOICES = (5, 10, 15, 20, 25)
LIMIT_MANUAL_PROMPT_TEXT = "Введите дневной лимит числом:"
INVALID_LIMIT_TEXT = "❌ Лимит должен быть положительным целым числом. Попробуйте ещё раз."

SYNC_RUNNING_TEXT = "🔄 Синхронизация запущена..."
GLOBAL_ENABLED_TEXT = "▶️ Автоприглашения включены."
GLOBAL_PAUSED_TEXT = "⏸ Автоприглашения приостановлены."

ACTION_FAILED_TEXT = "⚠️ Не удалось выполнить действие — откройте список заново через «👤 Аккаунты»."
OLD_ACCOUNT_TOGGLE_BLOCKED_TEXT = (
    "⚠️ Архивный аккаунт нельзя включить.\n"
    "Используйте актуальную запись этого Telegram-аккаунта."
)

HELP_TEXT = (
    "ℹ️ Inviter Admin Bot\n"
    "\n"
    "📣 Кампании\n"
    "Список кампаний приглашений. Каждая кампания включается и "
    "выключается отдельно — выключение одной не останавливает другие. "
    "Аккаунты и их лимиты общие для всех кампаний.\n"
    "\n"
    "✉️ ЛС-кампании\n"
    "Настройка будущих персональных ЛС (бензин, очереди на границе, "
    "страховка): AI-инструкция, ресурсы, источники, разрешённые аккаунты, "
    "follow-up. Пока это только настройки — сообщения не отправляются.\n"
    "\n"
    "👤 Аккаунты\n"
    "Управление отдельными Telegram-аккаунтами. У каждого аккаунта три "
    "кнопки: имя (карточка), 🟢/⚪ (включён/выключен), USED / LIMIT "
    "(изменить дневной лимит).\n"
    "\n"
    "🟢 — аккаунт разрешён для приглашений.\n"
    "⚪ — аккаунт выключен.\n"
    "🔴 — аккаунт временно заблокирован.\n"
    "\n"
    "USED / LIMIT — отправлено сегодня / дневной лимит.\n"
    "Например: 3 / 15\n"
    "Это означает: сегодня использовано 3 из лимита 15.\n"
    "\n"
    "🔄 Синхронизировать\n"
    "Проверяет session/account через Telegram, обновляет текущий "
    "username и состояние авторизации.\n"
    "\n"
    "📊 Статус\n"
    "Показывает состояние inviter и всех аккаунтов.\n"
    "\n"
    "Чтобы аккаунт реально использовался для приглашений, одновременно "
    "должны выполняться условия:\n"
    "1. Кампания включена 🟢.\n"
    "2. Аккаунт включён 🟢.\n"
    "3. Нет активной блокировки.\n"
    "4. Дневной/часовой лимит не исчерпан."
)


def format_toggle_label(*, enabled: bool) -> str:
    return "🟢" if enabled else "⚪"


def _dash_if_none(value) -> str:
    return str(value) if value is not None else "—"


def _fmt_dt(value) -> str:
    return format_tbilisi(value) if value is not None else "—"


def _fmt_dt_short(value) -> str:
    """"DD.MM HH:MM" без суффикса "по Тбилиси" — компактнее, чем _fmt_dt,
    для построчного списка "📊 Статус" (см. format_account_statuses)."""
    return format_tbilisi(value, fmt="%d.%m %H:%M", suffix=None) if value is not None else "—"


def format_account_card(card: AccountCard) -> str:
    """Карточка аккаунта (см. design "Карточка аккаунта") — "Session"/
    "Авторизация" — ДВА разных, независимых индикатора: Session — .session
    файл существует на диске (см. service.py::_session_file_path);
    Авторизация — identity этого аккаунта хотя бы раз подтверждена живой
    сессией (account.telegram_user_id задан, см.
    reader/inviter/identity.py) — оба читаются из уже сохранённых данных,
    БЕЗ реального подключения к Telegram (карточка — read-only экран, живая
    проверка — только явная "🔄 Проверить / синхронизировать")."""
    account = card.account
    session_icon = "✅" if card.session_exists else "❌"
    auth_icon = "✅" if account.telegram_user_id is not None else "⚠️"
    active_icon = "🟢" if account.enabled else "⚪"

    lines = [
        f"👤 {card.display_name}",
        "",
        f"Telegram ID: {_dash_if_none(account.telegram_user_id)}",
        f"Телефон: {_dash_if_none(account.phone) if account.phone else '—'}",
        f"Session: {session_icon}",
        f"Авторизация: {auth_icon}",
        f"Активен: {active_icon}",
        "",
        f"Сегодня: {card.usage.sent_today} / {card.usage.daily_limit}",
        f"Осталось: {card.usage.remaining}",
        "",
        f"Blocked until: {_fmt_dt(account.blocked_until)}",
        f"Причина: {_dash_if_none(account.blocked_reason)}",
        "",
        f"Последнее использование: {_fmt_dt(account.last_used_at)}",
        f"Последняя синхронизация: {_fmt_dt(account.last_synced_at)}",
    ]
    return "\n".join(lines)


def format_limit_prompt(display_name: str, sent_today: int, daily_limit: int) -> str:
    remaining = max(0, daily_limit - sent_today)
    return f"{display_name}\nСегодня: {sent_today} / {daily_limit}\nОсталось: {remaining}"


def format_limit_updated(display_name: str, new_limit: int) -> str:
    return f"✅ Лимит {display_name} изменён на {new_limit}."


def format_invalid_manual_limit_after_prompt() -> str:
    return f"{INVALID_LIMIT_TEXT}\n\n{LIMIT_MANUAL_PROMPT_TEXT}"


def format_auth_success(display_name: str) -> str:
    return f"✅ Аккаунт {display_name} авторизован и сохранён."


def format_auth_failed(error_summary: str) -> str:
    return f"❌ {error_summary}"


def format_sync_summary(summary: SyncSummary) -> str:
    lines = [
        "🔄 Синхронизация завершена",
        "",
        f"Проверено: {summary.checked}",
        f"✅ Авторизовано: {summary.authorized}",
        f"⚠️ Требуют внимания: {summary.needs_attention}",
        f"🔄 Username обновлён: {summary.username_updated}",
    ]
    attention = [r for r in summary.results if r.status in ("connect_failed", "not_authorized", "identity_mismatch")]
    if attention:
        lines.append("")
        for r in attention:
            reason = {
                "connect_failed": "Session error",
                "not_authorized": "Требуется повторная авторизация",
                "identity_mismatch": "Сессия авторизована под другим аккаунтом",
            }.get(r.status, r.status)
            lines.append(f"⚠️ {r.display_name}\n{reason}")
    return "\n".join(lines)


def format_sync_one_result(result) -> str:
    if result.status in ("updated", "unchanged"):
        return f"✅ {result.display_name}\nСинхронизировано."
    reason = {
        "connect_failed": "Session error",
        "not_authorized": "Требуется повторная авторизация",
        "identity_mismatch": "Сессия авторизована под другим аккаунтом",
    }.get(result.status, result.status)
    return f"🔴 {result.display_name}\n{reason}"


def format_account_statuses(
    entries: list[AccountStatusEntry],
    *,
    inviter_enabled: bool,
    campaigns: list[CampaignListEntry] | None = None,
) -> str:
    """"📊 Статус" — статус КАЖДОГО аккаунта инвайтера (см. design:
    "прежде всего статус КАЖДОГО Telegram-аккаунта"). enabled и is_blocked
    показаны ОТДЕЛЬНЫМИ строками, никогда не смешиваются в одно значение
    (см. design "Это два разных состояния") — icon 🔴 только при is_blocked
    (даже если enabled=True, см. design пример "@wwww86w"), иначе 🟢/⚪ по
    enabled. is_blocked уже вычислен вызывающей стороной (см.
    service.py::list_account_statuses) — здесь никакого сравнения с
    datetime.now() нет.

    blocked_reason ИСТОРИЧЕСКИЙ (blocked_until уже прошёл) показывается
    ОТДЕЛЬНОЙ строкой "Последняя причина" — НЕ как активная блокировка (см.
    design "Не менять blocked state ради UI" — is_blocked остаётся
    единственным источником истины про АКТИВНОСТЬ блокировки, здесь только
    решается, какую строку про reason показать).

    inviter_enabled — ГЛОБАЛЬНЫЙ ▶️/⏸ переключатель автодобавления (см.
    InviterAdminService.is_global_enabled), отдельная короткая строка
    вверху экрана — НЕ путать с per-account enabled ниже (два независимых
    понятия: один "автоприглашение в целом", другой "этот конкретный
    аккаунт")."""
    auto_state = "▶️ включено" if inviter_enabled else "⏸ приостановлено"
    header = f"{STATUS_ACCOUNTS_HEADER}\n\nАвтоприглашение: {auto_state}"
    if campaigns is not None:
        # Мультикампании: состояние каждой кампании вместо одной глобальной
        # строки; глобальная пауза показывается, только если она реально
        # активна (см. runtime_state_repository.py — бот её больше не ставит).
        header = f"{STATUS_ACCOUNTS_HEADER}\n\n{format_campaigns_summary_line(campaigns)}"
        if not inviter_enabled:
            header += "\n⏸ Глобальная пауза инвайтера активна"

    if not entries:
        return f"{header}\n\n{NO_ACCOUNTS_TEXT}"

    blocks = []
    for entry in entries:
        icon = "🔴" if entry.is_blocked else ("🟢" if entry.enabled else "⚪")
        lines = [
            f"{icon} {entry.display_name}",
            f"Аккаунт: {'включён' if entry.enabled else 'выключен'}",
        ]
        if entry.is_blocked:
            lines.append(f"Блокировка: до {_fmt_dt_short(entry.blocked_until)}")
            lines.append(f"Причина: {entry.blocked_reason or '—'}")
        else:
            lines.append("Блокировка: нет")
            if entry.blocked_reason:
                lines.append(f"Последняя причина: {entry.blocked_reason}")
        lines.append(f"Сегодня: {entry.sent_today} / {entry.daily_limit}")
        blocks.append("\n".join(lines))

    return header + "\n\n" + "\n\n".join(blocks)


def format_status(snapshot: StatusSnapshot) -> str:
    service_icon = "🟢 работает" if snapshot.worker_alive else "🔴 не отвечает"
    auto_state = "▶️ включено" if snapshot.inviter_enabled else "⏸ приостановлено"

    lines = [
        "📊 Inviter",
        "",
        f"Сервис: {service_icon}",
        f"Автодобавление: {auto_state}",
        "",
        "Аккаунты:",
        f"🟢 Активных: {snapshot.active_count}",
        f"⚪ Выключено: {snapshot.disabled_count}",
        f"🔴 Заблокировано: {snapshot.blocked_count}",
        "",
        "Сегодня:",
        f"Отправлено: {snapshot.sent_today}",
        f"Pending: {snapshot.pending_today}",
        f"Failed: {snapshot.failed_today}",
    ]

    if snapshot.campaign_name:
        lines += ["", "Кампания:", snapshot.campaign_name, snapshot.campaign_target_chat or ""]

    lines += [
        "",
        f"Последний тик: {_fmt_dt(snapshot.last_tick_at)}",
        f"Следующий тик: {_fmt_dt(snapshot.next_tick_at)}",
    ]

    if snapshot.attention:
        lines += ["", "⚠️ Требуют внимания", ""]
        for item in snapshot.attention:
            lines.append(f"{item.display_name}\n{item.reason}")

    return "\n".join(lines)


def _campaign_state(enabled: bool) -> str:
    return "🟢 Включена" if enabled else "🔴 Выключена"


def format_campaigns_list(entries: list[CampaignListEntry]) -> str:
    if not entries:
        return f"{CAMPAIGNS_HEADER}\n\n{NO_CAMPAIGNS_TEXT}"
    blocks = [f"{entry.label}\n{_campaign_state(entry.enabled)}" for entry in entries]
    return CAMPAIGNS_HEADER + "\n\n" + "\n\n".join(blocks)


def _recency_line(stats: CampaignStats) -> list[str]:
    if not stats.lead_max_age_days:
        return []
    return [f"Давность лидов: последнее сообщение не старше {stats.lead_max_age_days} дн."]


def format_campaign_card(stats: CampaignStats) -> str:
    state = "🟢 Приглашения включены" if stats.enabled else "🔴 Приглашения выключены"
    return "\n".join([
        stats.label,
        "",
        f"Источник: {stats.source}",
        f"Фильтр: {stats.keyword}",
        f"Цель: {stats.target_chat}",
        *_recency_line(stats),
        "",
        f"Найдено лидов: {stats.leads_found}",
        f"Ожидают приглашения: {stats.awaiting}",
        f"Приглашено: {stats.joined}",
        f"Пропущено: {stats.skipped}",
        f"Ошибки: {stats.failed}",
        "",
        state,
    ])


def format_campaign_stats(stats: CampaignStats) -> str:
    lines = [
        f"📊 {stats.label}",
        "",
        f"Статус: {_campaign_state(stats.enabled)}",
        f"Источник: {stats.source}",
        f"Фильтр: {stats.keyword}",
        f"Цель: {stats.target_chat}",
        *_recency_line(stats),
        "",
        f"Найдено лидов: {stats.leads_found}",
        f"Ожидают приглашения: {stats.awaiting}",
        f"Отправлено, ждут вступления: {stats.sent_pending}",
        f"Приглашено (вступили): {stats.joined}",
        f"Пропущено: {stats.skipped}",
        f"  не вступили после приглашения: {stats.not_joined}",
        f"  невозможно пригласить (invalid): {stats.invalid}",
        f"  без username: {stats.without_username}",
    ]
    if stats.has_pool:
        lines += [
            f"  уже в группе: {stats.pool_already_member}",
            f"  боты: {stats.pool_bots}",
            f"  удалённые аккаунты: {stats.pool_deleted}",
        ]
    lines.append(f"Ошибки: {stats.failed}")
    if stats.has_pool:
        lines += ["", f"Последнее сканирование: {_fmt_dt(stats.last_scan_at)}"]
    return "\n".join(lines)


def format_lead_refresh(outcome: LeadRefreshOutcome) -> str:
    if not outcome.ok:
        return f"⚠️ Лиды не обновлены.\n{outcome.reason}"
    return (
        "🔄 Лиды обновлены\n\n"
        f"Новых сообщений: {outcome.messages_scanned}\n"
        f"Совпадений: {outcome.matching_messages}\n"
        f"Новых пользователей: {outcome.new_users}"
    )


def format_campaigns_summary_line(entries: list[CampaignListEntry]) -> str:
    """Строка "Кампании" вверху "📊 Статус" — вместо прежнего глобального
    "Автоприглашение: ..." (включение теперь по кампаниям)."""
    if not entries:
        return "Кампании: —"
    return "Кампании:\n" + "\n".join(
        f"{'🟢' if e.enabled else '🔴'} {e.label}" for e in entries
    )
