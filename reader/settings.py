import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


class ConfigError(Exception):
    """Ошибка загрузки или валидации конфигурации."""


_LEGACY_SESSION_NAME = "reader_session"
_DEFAULT_SESSION_NAME_LIVE = "reader_live"


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
    api_id: int
    api_hash: str
    phone: str
    ignored_sender_ids: list[int] = Field(default_factory=list)
    ignored_usernames: list[str] = Field(default_factory=list)


class AppSettings(BaseModel):
    log_level: str = "INFO"
    groups_file: Path
    scenarios_file: Path
    leads_output_file: Path
    users_db_file: Path
    lead_forward_to: list[int | str] = Field(default_factory=list)
    debug_telegram_events: bool = False


class Settings(BaseModel):
    telegram: TelegramSettings
    app: AppSettings


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
    # -------------------------------

    _migrate_legacy_session(session_name_live, session_path_live)

    try:
        return Settings(
            telegram=TelegramSettings(
                session_path_live=session_path_live,
                session_path_sync=session_path_sync,
                api_id=int(api_id),
                api_hash=api_hash,
                phone=phone,
                ignored_sender_ids=telegram_raw.get("ignored_sender_ids", []),
                ignored_usernames=telegram_raw.get("ignored_usernames", []),
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