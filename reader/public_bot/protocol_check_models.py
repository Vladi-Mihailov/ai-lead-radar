"""Typed-результат одного one-shot lookup'а "📸 Проверить протокол" против
videos.police.ge (см. задачу) — намеренно ОТДЕЛЬНО от reader/fines/* (это
НЕ FineCheckService/FineProvider — другой сайт, другая цель, никакого
отношения к мониторингу штрафов police.ge/protocol, см. задачу п.5/п.10:
"не смешивать с текущим FineCheckService").

UNKNOWN — технически успешный ответ (HTTP 200, сессия не истекла), но
positive/FOUND HTML-структура ещё ни разу не наблюдалась и намеренно НЕ
угадывается (см. задачу п.8: "не придумывай CSS selectors/поля для
FOUND") — архитектура расширяемая: когда появится реальный положительный
пример, _parse_result (см. protocol_check_provider.py) начнёт возвращать
FOUND вместо UNKNOWN для того же случая, ConversationController менять не
придётся (см. _format_protocol_check_result — уже обрабатывает все четыре
статуса)."""

from dataclasses import dataclass
from enum import Enum


class ProtocolCheckStatus(Enum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"
    ERROR = "error"


@dataclass(frozen=True)
class ProtocolCheckResult:
    status: ProtocolCheckStatus


class ProtocolCheckSessionError(Exception):
    """csrf_token/сессионная ошибка (см. protocol_check_session.py) —
    НИКОГДА не долетает до ConversationController напрямую:
    ProtocolCheckProvider ловит её и превращает в
    ProtocolCheckResult(status=ERROR) (см. задачу п.7: "не превращать
    session error/timeout/HTTP error в NOT_FOUND"). Сообщение исключения
    намеренно НЕ включает ни одно введённое пользователем значение (см.
    задачу п.4: "не включать в exception traceback/context")."""
