"""Тесты CollectorHealthMonitor (reader/public_bot/collector_health.py) —
предупреждения менеджерам о простое и отставании REDNET-коллектора: что и
когда отправляется по отчётам коллектора и сторожем тишины. Только
синтетические данные."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.public_bot.collector_health import (
    CollectorHealthMonitor,
    format_problem_alert,
)

_START = datetime(2026, 11, 20, 5, 3, tzinfo=timezone.utc)


class _Clock:
    def __init__(self, now: datetime = _START):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


def _recovering(**overrides) -> dict:
    payload = {
        "status": "recovering",
        "detail": "recovery reached the page cap of 300 pages before rows covered by the last successful cycle",
        "mode": "recovery",
        "pages": 300,
        "last_success_at": "2026-11-18T19:55:00+00:00",
        "resume_offset": 9000,
        "oldest_publish_date": "2026-11-19",
        "horizon_date": "2026-11-18",
    }
    payload.update(overrides)
    return payload


def _monitor(clock: _Clock) -> CollectorHealthMonitor:
    return CollectorHealthMonitor(clock=clock)


def test_ok_reports_never_alert():
    monitor = _monitor(_Clock())
    assert monitor.observe({"status": "ok", "detail": "feed poll finished: pages=12, mode=fast"}) is None
    assert monitor.observe({"status": "ok"}) is None


def test_first_problem_alerts_then_reminders_at_most_every_12_hours_then_recovery():
    clock = _Clock()
    monitor = _monitor(clock)

    first = monitor.observe(_recovering())
    assert first.startswith("⚠️ Сборщик штрафов REDNET (общий список police.ge) догоняет список после перерыва.")
    assert "Последний полный обход: 2026-11-18 23:55 по Тбилиси." in first
    assert "Этот цикл: 300 стр., дошёл до публикаций за 19.11.2026; догнать нужно до публикаций за 18.11.2026." in first
    assert "ничего не пропуская" in first

    clock.advance(hours=6)
    assert monitor.observe(_recovering()) is None
    clock.advance(hours=5, minutes=59)
    assert monitor.observe({"status": "error", "detail": "ConnectTimeout"}) is None

    clock.advance(minutes=1)
    reminder = monitor.observe({"status": "error", "detail": "ConnectTimeout"})
    assert reminder.startswith("⏰ Напоминание. ⚠️ Сборщик штрафов REDNET (общий список police.ge): цикл обхода не завершён.")
    assert "Подробности: ConnectTimeout" in reminder

    clock.advance(hours=1)
    assert monitor.observe({"status": "ok"}) == (
        "✅ Сборщик штрафов REDNET (общий список police.ge): обход снова завершается успешно."
    )
    assert monitor.observe({"status": "ok"}) is None

    clock.advance(hours=1)
    assert monitor.observe({"status": "error", "detail": "x"}).startswith("⚠️ ")


def test_unknown_or_missing_status_counts_as_a_problem():
    monitor = _monitor(_Clock())
    assert monitor.observe({}).startswith("⚠️ ")


def test_silence_is_not_watched_before_the_first_report():
    clock = _Clock()
    monitor = _monitor(clock)
    clock.advance(days=3)
    assert monitor.check_silence() is None


def test_silence_over_13_hours_alerts_once_and_the_next_report_says_it_is_back():
    clock = _Clock()
    monitor = _monitor(clock)
    monitor.observe({"status": "ok"})

    clock.advance(hours=13)
    assert monitor.check_silence() is None
    clock.advance(minutes=1)
    alert = monitor.check_silence()
    assert alert.startswith("⚠️ Сборщик штрафов REDNET (общий список police.ge): нет отчётов 13 ч")
    assert "последний — 2026-11-20 09:03 по Тбилиси" in alert
    assert "georgia-collector" in alert
    clock.advance(hours=10)
    assert monitor.check_silence() is None

    back = monitor.observe({"status": "ok"})
    assert back == "✅ Сборщик штрафов REDNET (общий список police.ge): снова на связи."
    clock.advance(hours=13, minutes=1)
    assert monitor.check_silence().startswith("⚠️ ")


def test_report_after_silence_with_a_problem_sends_both_lines_in_one_message():
    clock = _Clock()
    monitor = _monitor(clock)
    monitor.observe({"status": "ok"})
    clock.advance(hours=40)
    assert monitor.check_silence() is not None

    text = monitor.observe(_recovering())
    first, second = text.split("\n\n", 1)
    assert first == "✅ Сборщик штрафов REDNET (общий список police.ge): снова на связи."
    assert second.startswith("⚠️ Сборщик штрафов REDNET (общий список police.ge) догоняет список")


def test_alert_text_without_a_previous_success_and_with_a_long_plate_like_detail():
    text = format_problem_alert(
        {"status": "error", "last_success_at": None, "detail": "bad row AA-001-BB " + "x" * 400},
        reminder=False,
    )
    assert "Полного обхода ещё не было." in text
    detail_line = text.splitlines()[-1]
    assert "AA-001-BB" not in detail_line
    assert "<номер>" in detail_line
    assert len(detail_line) <= len("Подробности: ") + 300
    assert detail_line.endswith("…")


def test_recovering_alert_tolerates_missing_or_malformed_fields():
    text = format_problem_alert(
        {"status": "recovering", "last_success_at": "not a date", "oldest_publish_date": None},
        reminder=False,
    )
    assert "Полного обхода ещё не было." in text
    assert "Этот цикл" not in text
    assert "ничего не пропуская" in text
