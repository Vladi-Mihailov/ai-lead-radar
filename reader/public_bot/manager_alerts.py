"""Проактивные уведомления группе менеджеров @GEShtrafbot (settings.
public_bot.support_chat_id — та же "внутренняя группа менеджеров", что и у
живого диалога «👨‍💼 Оператор», см. reader/protocol_support/) о новых
пользователях.

Ключевая компенсация emergency mode (см. задачу "back off when police.ge
is unavailable" + "NEW USER EMERGENCY WORKFLOW", production incident
2026-10-21): пока circuit breaker открыт, self-service Add Car больше не
может сделать реальную первичную проверку (см. reader/fines/
check_service.py::check_task_or_read_cached) — менеджер должен узнать о
новом клиенте и его автомобиле СРАЗУ, чтобы проверить штрафы вручную в
браузере, не дожидаясь восстановления police.ge. Эти уведомления
ВСЕГДА включены (не завязаны на состояние circuit breaker) — так
функция уже проверена в обычном режиме к моменту, когда она реально
понадобится при следующем сбое.

Только форматирование текста — ничего не знает про TelegramClient/
chat_id (та же граница ответственности, что и reader/public_bot/texts.py);
отправка — исключительно reader/public_bot/handlers.py (единственное
место с доступом к живому TelegramClient)."""

from reader.plate_display import format_plate_for_telegram


def _display_name(*, first_name: str | None, last_name: str | None) -> str:
    parts = [p for p in (first_name, last_name) if p]
    return " ".join(parts) if parts else "—"


def _username_line(username: str | None) -> str:
    return f"@{username}" if username else "не указан"


def format_new_user_alert(
    *, telegram_user_id: int, username: str | None, first_name: str | None, last_name: str | None,
) -> str:
    """Первое появление numeric telegram_user_id в bot_known_users (см.
    reader/public_bot/handlers.py::register — is_known() проверяется ДО
    record_seen()) — номер автомобиля здесь ЕЩЁ неизвестен (см. design:
    "отправить initial new-user notification один раз, а когда... добавит
    автомобиль — отдельное дополнение", см. format_car_added_alert)."""
    return "\n".join([
        "👤 Новый пользователь",
        "",
        f"Имя: {_display_name(first_name=first_name, last_name=last_name)}",
        f"Username: {_username_line(username)}",
        f"Telegram ID: {telegram_user_id}",
    ])


def format_car_added_alert(
    *, telegram_user_id: int, username: str | None, first_name: str | None, last_name: str | None,
    car_number: str, bootstrap_pending: bool = False,
) -> str:
    """Этот же telegram_user_id впервые за всё время добавил автомобиль
    (см. SubscriptionService.add_car::is_first_car_for_user — ПЕРВАЯ
    когда-либо созданная fine_monitoring_subscriptions строка для него, а
    не "первая за сегодня"/"первая активная") — короткое дополнение к уже
    отправленному format_new_user_alert, НЕ дублирует имя/username/id
    целиком новым длинным сообщением.

    bootstrap_pending (см. задачу "ВРЕМЕННЫЙ manager workflow для Georgia
    fines bot") — True, ТОЛЬКО когда этот физический номер действительно
    ожидает ручной первичной проверки (georgia_plate_bootstrap_state.
    bootstrap_status == 'pending', см. reader/public_bot/
    manual_bootstrap_service.py) — call site решает это, не эта функция
    (чистое форматирование). False (по умолчанию) сохраняет прежний текст
    БИТ В БИТ — существующие вызовы/тесты без этого параметра не меняются."""
    lines = [
        "🚗 Пользователь добавил автомобиль",
        "",
        f"{_display_name(first_name=first_name, last_name=last_name)} ({_username_line(username)})",
        f"Telegram ID: {telegram_user_id}",
        f"Авто: {format_plate_for_telegram(car_number)}",
    ]
    if bootstrap_pending:
        lines += ["", "Статус первичной проверки:", "⏳ Ожидает проверки"]
    return "\n".join(lines)
