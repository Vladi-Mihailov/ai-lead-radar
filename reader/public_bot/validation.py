"""Валидация Telegram-логина для клиентского Add Car flow (@GEShtrafbot).

Отдельно от reader/fines/validation.py (номер автомобиля, даты периода) —
формат Telegram-username не относится к домену штрафов, нужен только здесь,
на шаге, где Telegram не отдаёт username сам (см.
reader/public_bot/conversation.py)."""

import re

# Официальные ограничения Telegram: 5-32 символа, первый — буква, дальше
# буквы/цифры/подчёркивание.
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


class UsernameValidationError(Exception):
    """Ожидаемая ошибка валидации — сообщение показывается пользователю
    как есть (тот же приём, что и FineValidationError)."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def normalize_telegram_username(raw: str) -> str:
    """"@VeronaWarm"/"VeronaWarm" -> "VeronaWarm" (без ведущего "@")."""
    candidate = raw.strip().lstrip("@")

    if not _USERNAME_RE.match(candidate):
        raise UsernameValidationError(
            "Неверный формат Telegram-логина — используйте 5-32 символа "
            "(буквы, цифры, _), начиная с буквы. Например: VeronaWarm"
        )

    return candidate
