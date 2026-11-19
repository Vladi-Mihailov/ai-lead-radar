"""Тесты CollectorHealthMonitor (reader/public_bot/collector_health.py) —
предупреждения менеджерам о простое и отставании REDNET-коллектора: что и
когда ставится в очередь по отчётам коллектора и сторожем тишины, как
очередь доставляется (только после того, как Telegram принял сообщение, с
повторами) и что состояние переживает рестарт бота. Только синтетические
данные."""

import asyncio
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.public_bot.collector_health import (
    RETRY_MAX_DELAY,
    CollectorHealthMonitor,
    CollectorHealthStore,
    format_problem_alert,
    retry_delay,
)

_START = datetime(2026, 11, 20, 5, 3, tzinfo=timezone.utc)
_TITLE = "Сборщик штрафов REDNET (общий список police.ge)"


class _Clock:
    def __init__(self, now: datetime = _START):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


class _Telegram:
    """send(text) для deliver(): failing=True — Telegram сообщения не
    принимает; fail_next=N — не примет только N следующих."""

    def __init__(self):
        self.sent: list[str] = []
        self.failing = False
        self.fail_next = 0
        self.attempts = 0

    async def send(self, text: str) -> None:
        self.attempts += 1
        if self.fail_next > 0:
            self.fail_next -= 1
            raise ConnectionError("telegram unavailable")
        if self.failing:
            raise ConnectionError("telegram unavailable")
        self.sent.append(text)


def _deliver(monitor: CollectorHealthMonitor, telegram: _Telegram) -> list[str]:
    before = len(telegram.sent)
    asyncio.run(monitor.deliver(telegram.send))
    return telegram.sent[before:]


def _recovering(**overrides) -> dict:
    payload = {
        "status": "recovering",
        "detail": "recovery reached the page cap of 300 pages before rows covered by the last successful cycle",
        "mode": "recovery",
        "pages": 300,
        "last_success_at": "2026-11-18T19:55:00+00:00",
        "regions": 1,
        "backlog_rows_read": 9000,
        "oldest_publish_date": "2026-11-19",
        "horizon_date": "2026-11-18",
    }
    payload.update(overrides)
    return payload


def _error(detail: str = "ConnectTimeout", **overrides) -> dict:
    payload = {"status": "error", "detail": detail, "mode": "recovery", "pages": 3,
               "last_success_at": "2026-11-18T19:55:00+00:00"}
    payload.update(overrides)
    return payload


def _monitor(clock: _Clock, db_path=None) -> CollectorHealthMonitor:
    return CollectorHealthMonitor(CollectorHealthStore(db_path) if db_path else None, clock=clock)


# ==== отчёты коллектора: когда предупреждать ====


def test_ok_reports_never_alert():
    clock, telegram = _Clock(), _Telegram()
    monitor = _monitor(clock)
    monitor.observe({"status": "ok", "detail": "feed poll finished: pages=12, mode=fast"})
    monitor.observe({"status": "ok"})
    assert _deliver(monitor, telegram) == []


def test_a_single_transient_error_alerts_nobody_and_the_second_in_a_row_does():
    clock, telegram = _Clock(), _Telegram()
    monitor = _monitor(clock)
    monitor.observe({"status": "ok"})
    clock.advance(hours=6)
    monitor.observe(_error())
    assert _deliver(monitor, telegram) == []
    clock.advance(hours=6)
    monitor.observe({"status": "ok"})
    assert _deliver(monitor, telegram) == []

    clock.advance(hours=6)
    monitor.observe(_error())
    clock.advance(hours=6)
    monitor.observe(_error("police.ge HTTP 502"))
    [alert] = _deliver(monitor, telegram)
    assert alert.startswith(f"⚠️ {_TITLE}: цикл обхода не завершён.")
    assert "Последний полный обход: 2026-11-18 23:55 по Тбилиси." in alert
    assert "Неудачных циклов подряд: 2." in alert
    assert "Техническая причина: police.ge HTTP 502" in alert


def test_report_repeated_by_the_collector_client_is_not_a_second_failure(tmp_path):
    """Клиент коллектора повторяет POST после таймаута: тот же cycle_at
    уже учтён — одиночный сбой не превращается во "второй подряд"."""
    clock, telegram = _Clock(), _Telegram()
    monitor = _monitor(clock, tmp_path / "users.db")
    report = _error(cycle_at="2026-11-20T11:00:00+00:00")
    monitor.observe(report)
    clock.advance(seconds=35)
    monitor.observe(report)
    assert _deliver(monitor, telegram) == []
    assert monitor.store.load().non_ok_streak == 1

    clock.advance(hours=6)
    monitor.observe(_error(cycle_at="2026-11-20T17:00:00+00:00"))
    assert len(_deliver(monitor, telegram)) == 1


def test_recovering_alerts_on_the_first_report():
    clock, telegram = _Clock(), _Telegram()
    monitor = _monitor(clock)
    monitor.observe(_recovering())
    [alert] = _deliver(monitor, telegram)
    assert alert.startswith(f"⚠️ {_TITLE} догоняет список после перерыва.")
    assert "Последний полный обход: 2026-11-18 23:55 по Тбилиси." in alert
    assert "Этот цикл: 300 стр., дошёл до публикаций за 19.11.2026; догнать нужно до публикаций за 18.11.2026." in alert
    assert "Уже прочитано и сохранено строк списка: 9000." in alert
    assert "ничего не пропуская" in alert


def test_reminders_at_most_every_12_hours_after_delivery_then_recovery_message():
    clock, telegram = _Clock(), _Telegram()
    monitor = _monitor(clock)
    monitor.observe(_recovering())
    assert len(_deliver(monitor, telegram)) == 1

    clock.advance(hours=6)
    monitor.observe(_recovering())
    clock.advance(hours=5, minutes=59)
    monitor.observe(_error())
    assert _deliver(monitor, telegram) == []

    clock.advance(minutes=1)
    monitor.observe(_error())
    [reminder] = _deliver(monitor, telegram)
    assert reminder.startswith(f"⏰ Напоминание. ⚠️ {_TITLE}: цикл обхода не завершён.")
    assert "Неудачных циклов подряд: 4." in reminder

    clock.advance(hours=1)
    monitor.observe({"status": "ok", "last_success_at": "2026-11-21T06:03:00+00:00"})
    assert _deliver(monitor, telegram) == [
        f"✅ {_TITLE}: обход снова завершается успешно.\nПоследний полный обход: 2026-11-21 10:03 по Тбилиси."
    ]
    monitor.observe({"status": "ok"})
    assert _deliver(monitor, telegram) == []


def test_unknown_or_missing_status_counts_as_a_problem():
    clock, telegram = _Clock(), _Telegram()
    monitor = _monitor(clock)
    monitor.observe({})
    monitor.observe({})
    [alert] = _deliver(monitor, telegram)
    assert alert.startswith("⚠️ ")


def test_circuit_breaker_report_says_police_ge_is_unavailable_and_when_the_last_success_was():
    clock, telegram = _Clock(), _Telegram()
    monitor = _monitor(clock)
    report = {"status": "error", "detail": "police.ge circuit breaker open — cycle skipped", "mode": "skipped",
              "pages": 0, "last_success_at": "2026-11-18T19:55:00+00:00"}
    monitor.observe(report)
    monitor.observe(report)
    [alert] = _deliver(monitor, telegram)
    assert alert.startswith(f"⚠️ {_TITLE}: police.ge недоступен (сработала защита от сбоев), циклы пропускаются.")
    assert "Последний полный обход: 2026-11-18 23:55 по Тбилиси." in alert
    assert "circuit breaker" not in alert


# ==== доставка: только после того, как Telegram принял, с повторами ====


def test_alert_that_telegram_did_not_accept_is_retried_with_growing_pauses_and_counts_only_once_sent():
    clock, telegram = _Clock(), _Telegram()
    monitor = _monitor(clock)
    telegram.failing = True
    monitor.observe(_recovering())
    assert _deliver(monitor, telegram) == []
    assert telegram.attempts == 1
    assert monitor.store.load().problem_alerted_at is None
    assert len(monitor.store.queued()) == 1

    clock.advance(seconds=59)
    assert _deliver(monitor, telegram) == []
    assert telegram.attempts == 1  # пауза ещё не прошла
    clock.advance(seconds=1)
    assert _deliver(monitor, telegram) == []
    assert telegram.attempts == 2
    clock.advance(minutes=1, seconds=59)
    assert _deliver(monitor, telegram) == []
    assert telegram.attempts == 2
    clock.advance(seconds=1)
    _deliver(monitor, telegram)
    assert telegram.attempts == 3

    telegram.failing = False
    clock.advance(minutes=4)
    [alert] = _deliver(monitor, telegram)
    assert alert.startswith("⚠️ ")
    assert monitor.store.load().problem_alerted_at == clock.now
    assert monitor.store.queued() == []


def test_retry_pause_is_bounded():
    assert retry_delay(1) == timedelta(minutes=1)
    assert retry_delay(2) == timedelta(minutes=2)
    assert retry_delay(5) == timedelta(minutes=16)
    assert retry_delay(6) == RETRY_MAX_DELAY == timedelta(minutes=30)
    assert retry_delay(50) == RETRY_MAX_DELAY


def test_queued_alert_is_refreshed_by_the_next_report_instead_of_duplicated():
    clock, telegram = _Clock(), _Telegram()
    monitor = _monitor(clock)
    telegram.failing = True
    monitor.observe(_recovering(pages=100))
    _deliver(monitor, telegram)
    clock.advance(hours=6)
    monitor.observe(_recovering(pages=250))
    telegram.failing = False
    clock.advance(hours=1)
    [alert] = _deliver(monitor, telegram)
    assert "Этот цикл: 250 стр." in alert


def test_problem_over_before_its_alert_was_delivered_cancels_it_and_sends_no_recovery_message():
    clock, telegram = _Clock(), _Telegram()
    monitor = _monitor(clock)
    telegram.failing = True
    monitor.observe(_recovering())
    _deliver(monitor, telegram)
    clock.advance(hours=6)
    monitor.observe({"status": "ok"})
    telegram.failing = False
    clock.advance(hours=1)
    assert _deliver(monitor, telegram) == []
    assert monitor.store.queued() == []


def test_undelivered_reminder_is_dropped_when_the_problem_is_over_but_the_recovery_message_goes_out():
    clock, telegram = _Clock(), _Telegram()
    monitor = _monitor(clock)
    monitor.observe(_recovering())
    assert len(_deliver(monitor, telegram)) == 1
    clock.advance(hours=12)
    monitor.observe(_recovering())  # напоминание
    telegram.failing = True
    _deliver(monitor, telegram)
    clock.advance(hours=1)
    monitor.observe({"status": "ok"})  # первое предупреждение доставлено — "✅" нужен
    assert [alert.kind for alert in monitor.store.queued()] == ["problem_resolved"]
    telegram.failing = False
    clock.advance(hours=1)
    assert _deliver(monitor, telegram) == [f"✅ {_TITLE}: обход снова завершается успешно."]
    assert monitor.store.queued() == []


def test_undelivered_silence_alert_is_dropped_when_reports_resume():
    clock, telegram = _Clock(), _Telegram()
    monitor = _monitor(clock)
    monitor.observe({"status": "ok"})
    clock.advance(hours=20)
    monitor.check_silence()
    telegram.failing = True
    _deliver(monitor, telegram)
    monitor.observe(_recovering())  # молчание кончилось раньше, чем о нём узнали: без "✅"
    telegram.failing = False
    clock.advance(minutes=1)
    [alert] = _deliver(monitor, telegram)
    assert "догоняет список" in alert


def test_delivery_keeps_queue_order_when_the_head_fails():
    clock, telegram = _Clock(), _Telegram()
    monitor = _monitor(clock)
    monitor.observe({"status": "ok"})
    clock.advance(hours=20)
    monitor.check_silence()
    assert len(_deliver(monitor, telegram)) == 1
    monitor.observe(_recovering())  # "✅ снова на связи", затем предупреждение о проблеме
    telegram.fail_next = 1  # Telegram не принял только первое из двух
    assert _deliver(monitor, telegram) == []  # второе не обгоняет голову очереди
    assert telegram.attempts == 2
    clock.advance(seconds=30)
    assert _deliver(monitor, telegram) == []  # голова ждёт паузы повтора, вторая — за ней
    clock.advance(seconds=30)
    back, problem = _deliver(monitor, telegram)
    assert back == f"✅ {_TITLE}: снова на связи."
    assert "догоняет список" in problem


# ==== сторож тишины ====


def test_silence_is_not_watched_before_the_first_report():
    clock, telegram = _Clock(), _Telegram()
    monitor = _monitor(clock)
    clock.advance(days=3)
    monitor.check_silence()
    assert _deliver(monitor, telegram) == []


def test_silence_over_13_hours_alerts_reminds_every_12_hours_and_the_next_report_says_it_is_back():
    clock, telegram = _Clock(), _Telegram()
    monitor = _monitor(clock)
    monitor.observe({"status": "ok"})

    clock.advance(hours=13)
    monitor.check_silence()
    assert _deliver(monitor, telegram) == []
    clock.advance(minutes=1)
    monitor.check_silence()
    [alert] = _deliver(monitor, telegram)
    assert alert.startswith(f"⚠️ {_TITLE}: нет отчётов 13 ч")
    assert "последний — 2026-11-20 09:03 по Тбилиси" in alert
    assert "georgia-collector" in alert

    clock.advance(hours=11)
    monitor.check_silence()
    assert _deliver(monitor, telegram) == []
    clock.advance(hours=1)
    monitor.check_silence()
    [reminder] = _deliver(monitor, telegram)
    assert reminder.startswith(f"⏰ Напоминание. ⚠️ {_TITLE}: нет отчётов 25 ч")

    monitor.observe({"status": "ok"})
    assert _deliver(monitor, telegram) == [f"✅ {_TITLE}: снова на связи."]
    clock.advance(hours=13, minutes=1)
    monitor.check_silence()
    assert _deliver(monitor, telegram)[0].startswith("⚠️ ")


def test_report_after_silence_with_a_problem_queues_both_messages_in_order():
    clock, telegram = _Clock(), _Telegram()
    monitor = _monitor(clock)
    monitor.observe({"status": "ok"})
    clock.advance(hours=40)
    monitor.check_silence()
    assert len(_deliver(monitor, telegram)) == 1

    monitor.observe(_recovering())
    back, problem = _deliver(monitor, telegram)
    assert back == f"✅ {_TITLE}: снова на связи."
    assert problem.startswith(f"⚠️ {_TITLE} догоняет список")


# ==== рестарт бота: состояние в БД ====


def test_bot_restart_during_a_collector_outage_still_alerts(tmp_path):
    db_path = tmp_path / "users.db"
    clock, telegram = _Clock(), _Telegram()
    _monitor(clock, db_path).observe({"status": "ok"})
    clock.advance(hours=5)

    restarted = _monitor(clock, db_path)  # бот перезапущен, коллектор молчит
    sent = []
    for _ in range(14 * 24):  # две недели проверок сторожа раз в час
        clock.advance(hours=1)
        restarted.check_silence()
        sent += _deliver(restarted, telegram)
    assert sent[0].startswith(f"⚠️ {_TITLE}: нет отчётов 14 ч")
    assert all(text.startswith("⏰ Напоминание. ") for text in sent[1:])
    assert len(sent) == 1 + (14 * 24 - 9) // 12


def test_streak_delivery_and_queue_survive_a_restart(tmp_path):
    db_path = tmp_path / "users.db"
    clock, telegram = _Clock(), _Telegram()
    first = _monitor(clock, db_path)
    first.observe(_error())
    clock.advance(hours=6)

    second = _monitor(clock, db_path)
    second.observe(_error())  # второй подряд — уже после рестарта
    telegram.failing = True
    _deliver(second, telegram)

    third = _monitor(clock, db_path)
    telegram.failing = False
    clock.advance(minutes=1)
    [alert] = _deliver(third, telegram)
    assert "Неудачных циклов подряд: 2." in alert

    fourth = _monitor(clock, db_path)
    clock.advance(hours=6)
    fourth.observe({"status": "ok"})
    assert _deliver(fourth, telegram)[0].startswith("✅ ")


def test_store_tables_are_created_next_to_existing_data(tmp_path):
    db_path = tmp_path / "users.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE other (x INTEGER)")
    conn.execute("INSERT INTO other VALUES (1)")
    conn.commit()
    conn.close()

    CollectorHealthStore(db_path).close()
    CollectorHealthStore(db_path).close()

    conn = sqlite3.connect(db_path)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"other", "georgia_collector_health", "georgia_collector_alert_outbox"} <= tables
    assert conn.execute("SELECT x FROM other").fetchall() == [(1,)]
    conn.close()


# ==== тексты ====


def test_alert_text_without_a_previous_success_and_with_a_long_plate_like_detail():
    text = format_problem_alert(
        {"status": "error", "last_success_at": None, "detail": "bad row AA-001-BB protocol 0123456789 " + "x" * 400},
        reminder=False,
    )
    assert "Полного обхода ещё не было." in text
    detail_line = text.splitlines()[-1]
    assert detail_line.startswith("Техническая причина: ")
    assert "AA-001-BB" not in detail_line
    assert "0123456789" not in detail_line
    assert "<номер>" in detail_line
    assert len(detail_line) <= len("Техническая причина: ") + 300
    assert detail_line.endswith("…")


def test_alert_text_from_a_collector_that_does_not_send_the_last_success():
    text = format_problem_alert({"status": "error", "detail": "x"}, reminder=False)
    assert "обход" in text.splitlines()[0]
    assert "Полного обхода" not in text
    assert "Последний полный обход" not in text


def test_recovering_alert_tolerates_missing_or_malformed_fields():
    text = format_problem_alert(
        {"status": "recovering", "last_success_at": "not a date", "oldest_publish_date": None,
         "backlog_rows_read": "many"},
        reminder=False,
    )
    assert "Полного обхода ещё не было." in text
    assert "Этот цикл" not in text
    assert "Уже прочитано" not in text
    assert "ничего не пропуская" in text


def test_no_alert_text_carries_a_plate_or_a_protocol_number():
    clock, telegram = _Clock(), _Telegram()
    monitor = _monitor(clock)
    leaky = "row P0035688 of AA001AA failed (GE-123-AB)"
    monitor.observe(_error(leaky))
    monitor.observe(_error(leaky))
    sent = _deliver(monitor, telegram)
    clock.advance(hours=20)
    monitor.check_silence()
    sent += _deliver(monitor, telegram)
    monitor.observe(_recovering(detail=leaky))
    sent += _deliver(monitor, telegram)
    clock.advance(hours=13)
    monitor.observe({"status": "ok", "detail": leaky})
    sent += _deliver(monitor, telegram)
    # ошибка, молчание, "снова на связи", напоминание (догоняет), "обход снова завершается"
    assert len(sent) == 5
    for text in sent:
        assert not re.search(r"P\d{7}|AA001AA|GE-123-AB", text), text
