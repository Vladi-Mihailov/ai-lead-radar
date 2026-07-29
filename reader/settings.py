import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


class ConfigError(Exception):
    """Ошибка загрузки или валидации конфигурации."""


_LEGACY_SESSION_NAME = "reader_session"


def _session_file(session_path: Path) -> Path:
    """Реальное имя файла на диске — Telethon сам добавляет .session."""
    return session_path.parent / f"{session_path.name}.session"


def _migrate_legacy_session(session_path_live: Path) -> None:
    """Одноразовая миграция: до разделения на live/sync была одна сессия
    reader_session.session. Если reader_live.session ещё не существует, а
    старая сессия есть — переименовываем её, чтобы не заставлять
    пользователя заново авторизовывать main.py.

    Для reader_sync ничего не переносим: клонирование того же auth_key в
    два файла означало бы, что оба процесса работают под ОДНОЙ и той же
    авторизованной Telegram-сессией, а не двумя независимыми — именно то,
    ради чего их разделили. sync_users.py один раз пройдёт авторизацию
    самостоятельно.
    """
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


class AppSettings(BaseModel):
    log_level: str = "INFO"
    groups_file: Path
    scenarios_file: Path
    leads_output_file: Path
    users_db_file: Path
    lead_forward_to: list[int | str] = Field(default_factory=list)


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

    # ---------- Диагностика ----------
    session_path_live = (
        project_root
        / "data"
        / "sessions"
        / telegram_raw["session_name_live"]
    )
    session_path_sync = (
        project_root
        / "data"
        / "sessions"
        / telegram_raw["session_name_sync"]
    )

    print("=" * 80)
    print("PROJECT ROOT      :", project_root)
    print("SESSION PATH LIVE :", session_path_live)
    print("SESSION PATH SYNC :", session_path_sync)
    print("PARENT EXISTS     :", session_path_live.parent.exists())
    print("=" * 80)

    # Создаем каталог автоматически
    session_path_live.parent.mkdir(parents=True, exist_ok=True)
    session_path_sync.parent.mkdir(parents=True, exist_ok=True)
    # -------------------------------

    _migrate_legacy_session(session_path_live)

    try:
        return Settings(
            telegram=TelegramSettings(
                session_path_live=session_path_live,
                session_path_sync=session_path_sync,
                api_id=int(api_id),
                api_hash=api_hash,
                phone=phone,
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
            ),
        )
    except (KeyError, ValueError) as exc:
        raise ConfigError(
            f"Некорректная структура config.yaml: {exc}"
        ) from exc


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