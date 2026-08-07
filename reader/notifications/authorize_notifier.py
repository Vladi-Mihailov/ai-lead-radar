"""
Разовая интерактивная авторизация Telegram-сессии уведомлений
(settings.telegram.session_path_notifier, см. reader/notifications/
operator_notifier.py) — по аналогии с reader/inviter/authorize.py, только
для этой отдельной сессии. Создаёт/авторизует .session-файл, которого
OperatorNotifier.start() ждёт при обычном client.connect(): без этого шага
сессия подключается, но не авторизована, и любая попытка резолвить
получателя (get_entity) ошибочно выглядит как "получатель не найден" (см.
задачу). Больше эта команда ничего не делает — ни резолва получателей, ни
отправки уведомлений.

Использование:
    python -m reader.notifications.authorize_notifier

Телефон, код Telegram и (если включена) пароль двухфакторной аутентификации
запрашиваются самим Telethon интерактивно (client.start() без phone=...) —
эта команда их не хранит и не подставляет заранее.
"""

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from telethon import TelegramClient  # noqa: E402

from reader.settings import ConfigError, load_settings  # noqa: E402

CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


async def run() -> None:
    settings = load_settings(CONFIG_PATH)
    session_path = settings.telegram.session_path_notifier

    print("Авторизация сессии уведомлений (session_path_notifier)")
    print(f"Session будет создана: {session_path}.session")
    print()

    client = TelegramClient(
        str(session_path), settings.telegram.api_id, settings.telegram.api_hash,
        receive_updates=False,
    )
    # Без phone=... — Telethon сам запросит номер телефона, код Telegram и
    # (если включена) пароль 2FA через stdin, точно как в
    # reader/inviter/authorize.py.
    await client.start()
    await client.disconnect()

    print()
    print(f"✔ Сессия создана: {session_path}.session")
    print("Теперь OperatorNotifier сможет резолвить получателей уведомлений.")


def main() -> None:
    try:
        asyncio.run(run())
    except ConfigError as exc:
        print(f"Ошибка запуска: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")
        sys.exit(0)


if __name__ == "__main__":
    main()
