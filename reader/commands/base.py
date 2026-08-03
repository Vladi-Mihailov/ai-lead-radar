from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CommandContext:
    chat_id: int
    user_id: int | None
    args: list[str]
    raw_text: str
    event: Any


class CommandError(Exception):
    """Ожидаемая ошибка команды (неверный формат, невалидные данные и т.п.) —
    её сообщение показывается оператору как есть, без трейсбека в логах."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class CommandResult:
    """Результат успешной обработки команды.

    Пока единственное поле — text, но это отдельный тип, а не голая строка,
    именно для того, чтобы позже добавить файлы/изображения/кнопки и другие
    варианты ответа без изменения сигнатуры Command.handle().
    """

    text: str


class Command(ABC):
    name: str

    @abstractmethod
    async def handle(self, ctx: CommandContext) -> CommandResult:
        """Обработать команду и вернуть результат для ответа оператору."""
