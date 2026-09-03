"""Синхронизация клиента @GEShtrafbot с уже существующим UserRepository —
чтобы операторский Fine Monitor (fine list/fine check/уведомления, см.
reader/fines/notification_coordinator.py::format_car_owner_display)
продолжал показывать владельца машины и для авто, добавленных через
клиентского бота, без единой правки на стороне оператора.
"""

from reader.users.models import TelegramUserInfo
from reader.users.repository import UserRepository


def sync_user_and_car(
    user_repository: UserRepository,
    *,
    telegram_user_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
    car_number: str,
) -> None:
    """upsert() по user_id — ТОТ ЖЕ numeric id, что и identity подписки
    (см. design: numeric Telegram user_id — единственный стабильный
    идентификатор) — плюс add_car_numbers(), который ДОБАВЛЯЕТ car_number
    к уже сохранённым за этим пользователем номерам, не удаляя остальные
    (тот же путь, что и "fine add NUMBER @username" в
    reader/commands/fine.py — никакой второй реализации маппинга/связывания
    здесь нет)."""
    user_repository.upsert(
        TelegramUserInfo(
            user_id=telegram_user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            is_bot=False,
        )
    )
    user_repository.add_car_numbers(telegram_user_id, [car_number])
