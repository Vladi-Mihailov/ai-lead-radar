"""
Тесты PoliceGeCircuitBreaker (см. задачу "back off when police.ge is
unavailable", production incident 2026-10-21: police.ge стал недоступен на
TCP-уровне на ~4+ часа, и каждая проверка ждала полный request_timeout=30s,
прежде чем провалиться — scheduled FineJob/manual Check Now продолжали
делать это для сотен задач подряд). Чистые unit-тесты самого breaker'а, без
HTTP/DB — now передаётся явно, чтобы не зависеть от реального времени.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.fines.police_ge_circuit_breaker import PoliceGeCircuitBreaker  # noqa: E402

_T0 = datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc)


def _make_breaker(**kwargs) -> PoliceGeCircuitBreaker:
    return PoliceGeCircuitBreaker(
        failure_threshold=3,
        initial_cooldown=timedelta(minutes=15),
        max_cooldown=timedelta(minutes=60),
        **kwargs,
    )


# ---- CLOSED -> OPEN после N подряд идущих transport-сбоев ----


def test_closed_by_default_allows_requests():
    breaker = _make_breaker()
    assert breaker.allow_request(now=_T0) is True
    assert breaker.is_open is False


def test_two_consecutive_failures_do_not_open_circuit():
    breaker = _make_breaker()
    breaker.record_transport_failure(now=_T0)
    breaker.record_transport_failure(now=_T0)

    assert breaker.is_open is False
    assert breaker.allow_request(now=_T0) is True


def test_three_consecutive_failures_open_the_circuit():
    """Главный regression-критерий задачи (п.9.1): "3 connect failures ->
    circuit OPEN"."""
    breaker = _make_breaker()
    breaker.record_transport_failure(now=_T0)
    breaker.record_transport_failure(now=_T0)
    breaker.record_transport_failure(now=_T0)

    assert breaker.is_open is True
    assert breaker.consecutive_failures == 3


def test_while_open_allow_request_is_false_before_cooldown_expires():
    """П.9.2: "while OPEN -> provider not called" — allow_request()
    решает это на уровне PoliceGeSession.search_by_plate() (см.
    test_police_ge_session.py), здесь — сам сигнал."""
    breaker = _make_breaker()
    for _ in range(3):
        breaker.record_transport_failure(now=_T0)

    just_before_cooldown_ends = _T0 + timedelta(minutes=14, seconds=59)
    assert breaker.allow_request(now=just_before_cooldown_ends) is False


def test_opened_until_is_now_plus_initial_cooldown():
    breaker = _make_breaker()
    for _ in range(3):
        breaker.record_transport_failure(now=_T0)

    assert breaker.opened_until == _T0 + timedelta(minutes=15)


# ---- Успешный пробный запрос после cooldown -> CLOSED ----


def test_cooldown_expiry_allows_exactly_one_half_open_probe():
    """П.3 задачи: "Do not retry multiple times inside one user request
    merely to test availability" — ровно один allow_request()=True за раз
    после истечения cooldown, пока этот пробный запрос не завершится."""
    breaker = _make_breaker()
    for _ in range(3):
        breaker.record_transport_failure(now=_T0)

    after_cooldown = _T0 + timedelta(minutes=15, seconds=1)
    assert breaker.allow_request(now=after_cooldown) is True
    # Второй "запрос" в это же мгновение не получает отдельный билет —
    # breaker всё ещё технически open, пока пробный не завершился.
    assert breaker.allow_request(now=after_cooldown) is False


def test_successful_probe_closes_circuit_and_resets_cooldown():
    """П.9.6: "successful probe closes circuit"."""
    breaker = _make_breaker()
    for _ in range(3):
        breaker.record_transport_failure(now=_T0)

    after_cooldown = _T0 + timedelta(minutes=15, seconds=1)
    assert breaker.allow_request(now=after_cooldown) is True

    breaker.record_success(now=after_cooldown)

    assert breaker.is_open is False
    assert breaker.consecutive_failures == 0
    assert breaker.allow_request(now=after_cooldown) is True


def test_record_success_resets_consecutive_failures_even_before_opening():
    """П.9.9: "normal HTTP success resets failure counter" — даже когда
    circuit ещё НЕ открыт (всего 1-2 подряд идущих сбоя)."""
    breaker = _make_breaker()
    breaker.record_transport_failure(now=_T0)
    breaker.record_transport_failure(now=_T0)
    breaker.record_success(now=_T0)

    assert breaker.consecutive_failures == 0

    # Следующие 2 сбоя снова не должны открыть circuit — счётчик
    # действительно сброшен, а не просто "накопился" до 4.
    breaker.record_transport_failure(now=_T0)
    breaker.record_transport_failure(now=_T0)
    assert breaker.is_open is False


# ---- Эскалация cooldown 15 -> 30 -> 60 ----


def test_cooldown_escalates_when_half_open_probe_also_fails():
    """Главный regression-критерий п.9.7: "cooldown escalation 15->30->60"
    (см. задачу п.3: "If the first cooldown expires and connection still
    fails: increase cooldown: 15 -> 30 -> 60")."""
    breaker = _make_breaker()
    for _ in range(3):
        breaker.record_transport_failure(now=_T0)
    assert breaker.opened_until == _T0 + timedelta(minutes=15)

    probe_time = _T0 + timedelta(minutes=15, seconds=1)
    assert breaker.allow_request(now=probe_time) is True
    breaker.record_transport_failure(now=probe_time)  # пробный запрос тоже провалился

    assert breaker.opened_until == probe_time + timedelta(minutes=30)

    second_probe_time = probe_time + timedelta(minutes=30, seconds=1)
    assert breaker.allow_request(now=second_probe_time) is True
    breaker.record_transport_failure(now=second_probe_time)

    assert breaker.opened_until == second_probe_time + timedelta(minutes=60)


def test_cooldown_caps_at_max_cooldown():
    breaker = _make_breaker()
    for _ in range(3):
        breaker.record_transport_failure(now=_T0)

    current_time = _T0
    cooldowns_seen = []
    for _ in range(5):
        probe_time = breaker.opened_until + timedelta(seconds=1)
        assert breaker.allow_request(now=probe_time) is True
        breaker.record_transport_failure(now=probe_time)
        current_time = probe_time
        cooldowns_seen.append(breaker.opened_until - current_time)

    assert cooldowns_seen[-1] == timedelta(minutes=60)
    assert all(c <= timedelta(minutes=60) for c in cooldowns_seen)


def test_successful_probe_resets_cooldown_back_to_initial():
    """После эскалации до 30 минут успешный пробный запрос возвращает
    cooldown к initial (15 минут) для СЛЕДУЮЩЕГО открытия, а не оставляет
    его эскалированным."""
    breaker = _make_breaker()
    for _ in range(3):
        breaker.record_transport_failure(now=_T0)
    probe_time = _T0 + timedelta(minutes=15, seconds=1)
    assert breaker.allow_request(now=probe_time) is True
    breaker.record_transport_failure(now=probe_time)  # пробный запрос провалился -> эскалация до 30 мин
    assert breaker.opened_until == probe_time + timedelta(minutes=30)

    second_probe_time = probe_time + timedelta(minutes=30, seconds=1)
    breaker.record_success(now=second_probe_time)

    # Новое открытие (следующий сбойный период) снова начинается с initial.
    for _ in range(3):
        breaker.record_transport_failure(now=second_probe_time)
    assert breaker.opened_until == second_probe_time + timedelta(minutes=15)
