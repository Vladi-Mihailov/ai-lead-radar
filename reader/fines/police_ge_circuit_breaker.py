"""Circuit breaker для доступности police.ge (см. production incident
2026-10-21: источник стал недоступен на TCP-уровне на ~4+ часа — см.
confirmed network diagnostic report — каждая проверка ждала полный
request_timeout=30s прежде чем провалиться, и scheduled FineJob/manual
Check Now продолжали это делать для СОТЕН задач подряд). Один инстанс на
процесс (GE-бот-процесс и base reader — РАЗНЫЕ процессы, у каждого своя
копия — см. design report "align Georgia user check-now flow" про то же
разделение PoliceGeSession/FineCheckService; этого достаточно, т.к. КАЖДЫЙ
процесс обнаруживает сбой самостоятельно в течение первых N попыток, см.
финальный отчёт про обоснование).

Три состояния (CLOSED подразумевается, пока _opened_at is None):
- CLOSED: обычная работа, allow_request() всегда True.
- OPEN: после failure_threshold подряд идущих TRANSPORT-сбоев —
  allow_request() False до истечения cooldown, НИКАКИХ новых HTTP-запросов
  к police.ge не делается вовсе (см. PoliceGeSession.search_by_plate).
- HALF-OPEN (неявное): cooldown истёк — РОВНО ОДИН запрос допускается
  "на пробу" (allow_request() снова True); если он успешен — CLOSED,
  cooldown сбрасывается на initial; если он тоже проваливается как
  transport failure — снова OPEN, cooldown удваивается (капается на
  max_cooldown)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


class PoliceGeCircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        initial_cooldown: timedelta = timedelta(minutes=15),
        max_cooldown: timedelta = timedelta(minutes=60),
    ):
        self._failure_threshold = failure_threshold
        self._initial_cooldown = initial_cooldown
        self._max_cooldown = max_cooldown

        self._consecutive_failures = 0
        self._current_cooldown = initial_cooldown
        self._opened_until: datetime | None = None
        self._half_open_probe_issued = False
        self._last_success_at: datetime | None = None

    def allow_request(self, *, now: datetime | None = None) -> bool:
        """True — можно делать реальный HTTP-запрос к police.ge прямо
        сейчас. CLOSED — всегда True. OPEN и cooldown ещё не истёк —
        False. Cooldown истёк — ОДИН пробный запрос допускается (см.
        задачу: "Do not retry multiple times inside one user request
        merely to test availability") — half_open_probe_issued не даёт
        выдать больше одного "билета" за раз, пока этот пробный запрос не
        завершится (см. record_success/record_transport_failure)."""
        if self._opened_until is None:
            return True

        current_time = now if now is not None else datetime.now(timezone.utc)
        if current_time < self._opened_until:
            return False

        if self._half_open_probe_issued:
            # Пробный запрос уже выдан кому-то другому и ещё не завершился
            # (см. докстрок класса про допустимую небольшую "гонку" —
            # сознательно не используем lock/mutex ради простоты, см.
            # финальный отчёт).
            return False

        self._half_open_probe_issued = True
        logger.info("police_ge circuit half-open probe")
        return True

    def record_success(self, *, now: datetime | None = None) -> None:
        """Реальный HTTP round-trip до police.ge завершился успешно (ЛЮБОЙ
        ответ сервера — не обязательно "штрафы найдены", см. задачу п.2:
        "Only network/connectivity failures should affect circuit state")
        — полный сброс в CLOSED, cooldown возвращается к initial."""
        was_open = self._opened_until is not None
        self._consecutive_failures = 0
        self._current_cooldown = self._initial_cooldown
        self._opened_until = None
        self._half_open_probe_issued = False
        self._last_success_at = now if now is not None else datetime.now(timezone.utc)
        if was_open:
            logger.info("police_ge circuit recovered")

    def record_transport_failure(self, *, now: datetime | None = None) -> None:
        """Именно TRANSPORT-level сбой (ConnectTimeout/ConnectError/
        NetworkError-класс — см. PoliceGeSession, вызывающий код уже
        отличил это от parser/business-ошибок, см. задачу п.2: "Do NOT
        classify parser/business errors as connectivity failures" — эта
        функция ничего не проверяет сама, доверяет вызывающему коду)."""
        current_time = now if now is not None else datetime.now(timezone.utc)
        was_half_open_probe = self._half_open_probe_issued
        self._half_open_probe_issued = False
        self._consecutive_failures += 1

        if self._consecutive_failures < self._failure_threshold:
            return

        if was_half_open_probe:
            # Пробный запрос после cooldown ТОЖЕ провалился — эскалируем
            # (см. задачу п.3: "15 min -> 30 min -> 60 min, cap at 60").
            self._current_cooldown = min(self._current_cooldown * 2, self._max_cooldown)

        self._opened_until = current_time + self._current_cooldown
        logger.warning(
            "police_ge circuit opened consecutive_failures=%d cooldown_until=%s",
            self._consecutive_failures, self._opened_until.isoformat(),
        )

    @property
    def is_open(self) -> bool:
        """Только для наблюдаемости/логов — НЕ использовать для решения
        "можно ли сделать запрос" (см. allow_request() — у него другая
        семантика: cooldown истёк -> True, даже если is_open тоже True)."""
        return self._opened_until is not None

    @property
    def opened_until(self) -> datetime | None:
        return self._opened_until

    @property
    def last_success_at(self) -> datetime | None:
        return self._last_success_at

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures
