"""Telegram-клиент ЛС-отправителя (Phase 3C) поверх СУЩЕСТВУЮЩЕЙ сессии
аккаунта (telegram_accounts.session_path).

Auth key читается из .session-файла только на чтение (?mode=ro) в
StringSession в памяти: файл не пишется и не блокируется — даже если тот
же аккаунт параллельно открыт другим процессом. Кэш сущностей не
сохраняется (получатель резолвится заново при каждой отправке).

flood_sleep_threshold=0 — FloodWait любой длины сразу исключением (никакого
сна внутри callback оператора); request_retries=1 — Telethon не повторяет
запрос сам (иначе после обрыва связи возможен дубль send_message)."""

import sqlite3
from collections.abc import Callable
from pathlib import Path


def session_file(account, project_root: Path) -> Path:
    raw = Path(account.session_path)
    path = raw if raw.is_absolute() else project_root / raw
    return path if path.suffix == ".session" else Path(f"{path}.session")


def build_sender_client_factory(api_id: int, api_hash: str, project_root: Path) -> Callable[[object], object]:
    from telethon import TelegramClient
    from telethon.crypto import AuthKey
    from telethon.sessions import StringSession

    def factory(account):
        path = session_file(account, project_root)
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT dc_id, server_address, port, auth_key FROM sessions").fetchone()
        finally:
            conn.close()
        memory = StringSession()
        if row and row[3]:
            memory.set_dc(row[0], row[1], row[2])
            memory.auth_key = AuthKey(row[3])
        client = TelegramClient(memory, api_id, api_hash, receive_updates=False, request_retries=1)
        client.flood_sleep_threshold = 0
        return client

    return factory


def build_session_exists(project_root: Path) -> Callable[[object], bool]:
    return lambda account: session_file(account, project_root).exists()
