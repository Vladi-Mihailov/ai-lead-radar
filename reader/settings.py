import os
from datetime import time
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


class ConfigError(Exception):
    """Ошибка загрузки или валидации конфигурации."""


_LEGACY_SESSION_NAME = "reader_session"
_DEFAULT_SESSION_NAME_LIVE = "reader_live"
_DEFAULT_SESSION_NAME_NOTIFIER = "reader_notifier"


def _session_file(session_path: Path) -> Path:
    """Реальное имя файла на диске — Telethon сам добавляет .session."""
    return session_path.parent / f"{session_path.name}.session"


def _migrate_legacy_session(session_name_live: str, session_path_live: Path) -> None:
    """Одноразовая миграция: до разделения на live/sync была одна сессия
    reader_session.session. Если reader_live.session ещё не существует, а
    старая сессия есть — переименовываем её, чтобы не заставлять
    пользователя заново авторизовывать main.py.

    Срабатывает только для имени сессии по умолчанию (reader_live).
    Если TELEGRAM_SESSION_NAME задаёт кастомное имя (например, reader_dev на
    локальной машине) — миграция не выполняется: старая prod-сессия не
    должна молча переименовываться в произвольное имя, случайно оказавшееся
    рядом на диске (например, при копировании data/ из бэкапа).

    Для reader_sync ничего не переносим: клонирование того же auth_key в
    два файла означало бы, что оба процесса работают под ОДНОЙ и той же
    авторизованной Telegram-сессией, а не двумя независимыми — именно то,
    ради чего их разделили. sync_users.py один раз пройдёт авторизацию
    самостоятельно.
    """
    if session_name_live != _DEFAULT_SESSION_NAME_LIVE:
        return

    live_file = _session_file(session_path_live)
    if live_file.exists():
        return  # уже мигрировали, либо это не первый запуск после обновления

    legacy_file = session_path_live.parent / f"{_LEGACY_SESSION_NAME}.session"
    if not legacy_file.exists():
        return  # старой сессии нет — свежая установка, переносить нечего

    try:
        legacy_file.rename(live_file)
        print(
            f"✔ Обнаружена сессия старого формата — перенесена "
            f"{legacy_file.name} -> {live_file.name}, повторная "
            f"авторизация main.py не потребуется"
        )

        legacy_journal = legacy_file.parent / f"{_LEGACY_SESSION_NAME}.session-journal"
        if legacy_journal.exists():
            legacy_journal.rename(live_file.parent / f"{live_file.name}-journal")
    except OSError as exc:
        print(
            f"⚠ Не удалось перенести старую сессию {legacy_file.name} "
            f"в {live_file.name} ({exc}) — потребуется повторная "
            f"авторизация main.py"
        )


class TelegramSettings(BaseModel):
    session_path_live: Path
    session_path_sync: Path
    # Опционален (в отличие от live/sync) — используется только
    # reader/inviter (уведомления оператору о ходе приглашений, см.
    # OperatorNotifier); прямое построение TelegramSettings(...) в
    # существующих тестах/коде не обязано его задавать.
    session_path_notifier: Path | None = None
    api_id: int
    api_hash: str
    phone: str
    ignored_sender_ids: list[int] = Field(default_factory=list)
    ignored_usernames: list[str] = Field(default_factory=list)
    ignored_display_names: list[str] = Field(default_factory=list)


class AppSettings(BaseModel):
    log_level: str = "INFO"
    groups_file: Path
    scenarios_file: Path
    leads_output_file: Path
    users_db_file: Path
    lead_forward_to: list[int | str] = Field(default_factory=list)
    debug_telegram_events: bool = False


class FineMonitorSettings(BaseModel):
    enabled: bool = False
    timezone: str = "Asia/Tbilisi"
    check_times: list[time] = Field(default_factory=lambda: [time(9, 0), time(15, 0), time(21, 0)])
    source_url: str = "https://police.ge/protocol/index.php?lang=en"
    request_timeout: float = 30.0
    notification_chat_ids: list[int | str] = Field(default_factory=list)
    allowed_user_ids: list[int] = Field(default_factory=list)

    # Архивный режим (см. reader/jobs/archive_fine_job.py) — автомобили,
    # у которых закончился обычный период (check_times, несколько раз в
    # сутки), продолжают изредка проверяться, а не выпадают из мониторинга
    # совсем. archive_check_enabled — независимый от `enabled` выключатель:
    # можно держать обычный мониторинг включённым, а архивный — нет (и
    # наоборот было бы бессмысленно, но проверка этого — забота
    # validate_fine_monitor_config, а не самой модели настроек).
    archive_check_enabled: bool = True
    archive_check_hour: int = 4
    archive_interval_days: int = 30
    # Safety limit на один запуск ArchiveFineJob (см. её докстрок про
    # downtime/backlog) — с запасом относительно текущего объёма (~1000
    # машин/30 дней ≈ 34 проверки в день), чтобы после нескольких дней
    # простоя backlog разбирался за разумное число запусков, а не за один
    # неограниченный проход.
    archive_daily_limit: int = 200


class InviterWorkerSettings(BaseModel):
    """Постоянный фоновый режим инвайтера (см. reader/inviter/worker.py,
    python -m reader.inviter.main --worker) — равномерно распределяет
    приглашения во времени вместо разовой дневной пачки.

    invitations_per_account_per_hour — ДОПОЛНИТЕЛЬНОЕ ограничение поверх
    account.daily_limit (столбец telegram_accounts.daily_limit в БД, не
    здесь), а не вместо него: обе проверки применяются независимо, самая
    строгая побеждает (см. InviterService.run_one_worker_attempt).

    poll_interval_seconds — как часто worker проверяет ОДИН очередной
    аккаунт по кругу (round-robin, см. InviterWorker) — не интервал между
    приглашениями одного аккаунта. Поскольку за один тик обрабатывается
    не более одной пары (кампания, аккаунт), реальный интервал между
    приглашениями РАЗНЫХ аккаунтов не может быть меньше этого значения —
    так они не приглашают одновременно."""

    invitations_per_account_per_hour: int = 2
    poll_interval_seconds: int = 600


class InviterSettings(BaseModel):
    worker: InviterWorkerSettings = Field(default_factory=InviterWorkerSettings)


class Settings(BaseModel):
    telegram: TelegramSettings
    app: AppSettings
    fine_monitor: FineMonitorSettings
    inviter: InviterSettings = Field(default_factory=InviterSettings)


def load_settings(config_path: Path) -> Settings:
    config_path = Path(config_path)
    if not config_path.exists():
        raise ConfigError(f"Файл конфигурации не найден: {config_path}")

    load_dotenv()

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Некорректный YAML в {config_path}: {exc}") from exc

    project_root = config_path.resolve().parent.parent

    api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    phone = os.getenv("TELEGRAM_PHONE")

    missing = [
        name
        for name, value in [
            ("TELEGRAM_API_ID", api_id),
            ("TELEGRAM_API_HASH", api_hash),
            ("TELEGRAM_PHONE", phone),
        ]
        if not value
    ]

    if missing:
        raise ConfigError(
            "Не заданы переменные окружения: "
            + ", ".join(missing)
            + ". Заполните .env на основе .env.example"
        )

    telegram_raw = raw.get("telegram", {})
    app_raw = raw.get("app", {})
    fine_monitor_raw = raw.get("fine_monitor", {})
    inviter_raw = raw.get("inviter", {})
    inviter_worker_raw = inviter_raw.get("worker", {})

    # Имя live-сессии: TELEGRAM_SESSION_NAME (.env) имеет приоритет над
    # session_name_live (config.yaml), а если ничего не задано — reader_live.
    # Так на VPS и локально можно использовать один и тот же config.yaml,
    # различаясь только .env (например, TELEGRAM_SESSION_NAME=reader_dev).
    # session_name_sync — не затронуто, как и раньше, обязательный ключ.
    session_name_live = (
        os.getenv("TELEGRAM_SESSION_NAME")
        or telegram_raw.get("session_name_live")
        or _DEFAULT_SESSION_NAME_LIVE
    )

    # ---------- Диагностика ----------
    session_path_live = (
        project_root
        / "data"
        / "sessions"
        / session_name_live
    )
    session_path_sync = (
        project_root
        / "data"
        / "sessions"
        / telegram_raw["session_name_sync"]
    )
    # Опционален в config.yaml (в отличие от session_name_sync) — только
    # reader/inviter открывает эту сессию (уведомления оператору, см.
    # OperatorNotifier); её отсутствие не должно ломать существующие
    # config.yaml, у которых этого ключа никогда не было.
    session_name_notifier = (
        telegram_raw.get("session_name_notifier") or _DEFAULT_SESSION_NAME_NOTIFIER
    )
    session_path_notifier = (
        project_root
        / "data"
        / "sessions"
        / session_name_notifier
    )

    print("=" * 80)
    print("PROJECT ROOT      :", project_root)
    print("SESSION NAME LIVE :", session_name_live)
    print("SESSION PATH LIVE :", session_path_live)
    print("SESSION PATH SYNC :", session_path_sync)
    print("PARENT EXISTS     :", session_path_live.parent.exists())
    print("=" * 80)

    # Создаем каталог автоматически
    session_path_live.parent.mkdir(parents=True, exist_ok=True)
    session_path_sync.parent.mkdir(parents=True, exist_ok=True)
    session_path_notifier.parent.mkdir(parents=True, exist_ok=True)
    # -------------------------------

    _migrate_legacy_session(session_name_live, session_path_live)

    try:
        return Settings(
            telegram=TelegramSettings(
                session_path_live=session_path_live,
                session_path_sync=session_path_sync,
                session_path_notifier=session_path_notifier,
                api_id=int(api_id),
                api_hash=api_hash,
                phone=phone,
                ignored_sender_ids=telegram_raw.get("ignored_sender_ids", []),
                ignored_usernames=telegram_raw.get("ignored_usernames", []),
                ignored_display_names=telegram_raw.get("ignored_display_names", []),
            ),
            app=AppSettings(
                log_level=app_raw.get("log_level", "INFO"),
                groups_file=project_root / app_raw["groups_file"],
                scenarios_file=project_root / app_raw["scenarios_file"],
                leads_output_file=project_root / app_raw["leads_output_file"],
                users_db_file=project_root / app_raw.get("users_db_file", "data/users.db"),
                lead_forward_to=_parse_forward_targets(
                    os.getenv("LEAD_FORWARD_TO", "")
                ),
                debug_telegram_events=_parse_bool_env(
                    os.getenv("DEBUG_TELEGRAM_EVENTS")
                ),
            ),
            fine_monitor=FineMonitorSettings(
                enabled=bool(fine_monitor_raw.get("enabled", False)),
                timezone=fine_monitor_raw.get("timezone", "Asia/Tbilisi"),
                check_times=[
                    _parse_check_time(value)
                    for value in fine_monitor_raw.get(
                        "check_times", ["09:00", "15:00", "21:00"]
                    )
                ],
                source_url=fine_monitor_raw.get(
                    "source_url", "https://police.ge/protocol/index.php?lang=en"
                ),
                request_timeout=float(fine_monitor_raw.get("request_timeout", 30)),
                notification_chat_ids=[
                    _normalize_chat_id(value)
                    for value in fine_monitor_raw.get("notification_chat_ids", [])
                ],
                allowed_user_ids=list(fine_monitor_raw.get("allowed_user_ids", [])),
                archive_check_enabled=bool(fine_monitor_raw.get("archive_check_enabled", True)),
                archive_check_hour=int(fine_monitor_raw.get("archive_check_hour", 4)),
                archive_interval_days=int(fine_monitor_raw.get("archive_interval_days", 30)),
                archive_daily_limit=int(fine_monitor_raw.get("archive_daily_limit", 200)),
            ),
            inviter=InviterSettings(
                worker=InviterWorkerSettings(
                    invitations_per_account_per_hour=int(
                        inviter_worker_raw.get("invitations_per_account_per_hour", 2)
                    ),
                    poll_interval_seconds=int(
                        inviter_worker_raw.get("poll_interval_seconds", 600)
                    ),
                ),
            ),
        )
    except (KeyError, ValueError) as exc:
        raise ConfigError(
            f"Некорректная структура config.yaml: {exc}"
        ) from exc


def _parse_bool_env(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _parse_forward_targets(raw: str) -> list[int | str]:
    targets: list[int | str] = []

    for token in raw.split(","):
        token = token.strip().lstrip("@")

        if not token:
            continue

        try:
            targets.append(int(token))
        except ValueError:
            targets.append(token)

    return targets


def _parse_check_time(value: str) -> time:
    hour_str, _, minute_str = str(value).partition(":")
    return time(int(hour_str), int(minute_str))


def _normalize_chat_id(value: int | str) -> int | str:
    if isinstance(value, str):
        token = value.strip().lstrip("@")
        try:
            return int(token)
        except ValueError:
            return token

    return value