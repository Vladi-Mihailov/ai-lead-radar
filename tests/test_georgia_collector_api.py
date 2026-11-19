"""Tests for the Georgia remote collector's HTTP/HMAC transport layer
(reader/public_bot/collector_api.py) — see "Georgia remote collector
architecture (REDNET)". Persistence logic itself is covered by
tests/test_georgia_collector_ingest_service.py; this file exercises
routing, auth, and (de)serialization through a real aiohttp test client."""

import json
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from aiohttp.test_utils import TestClient, TestServer

from reader.fines.check_service import FineCheckService
from reader.fines.collector_auth import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    sign,
)
from reader.fines.detected_fine_repository import DetectedFineRepository
from reader.fines.parser import compute_fingerprint
from reader.fines.plate_bootstrap_repository import (
    GeorgiaPlateBootstrapRepository,
)
from reader.fines.provider import FineProvider
from reader.fines.task_repository import FineMonitoringTaskRepository
from reader.public_bot.collector_api import build_collector_api_app
from reader.public_bot.collector_ingest_service import (
    CollectorIngestService,
)
from reader.public_bot.manual_bootstrap_service import (
    ManualBootstrapService,
)
from reader.public_bot.texts import format_no_fines_found

pytestmark = pytest.mark.asyncio

_SECRET = "test-shared-secret"


class _AlwaysAvailableProvider(FineProvider):
    def is_available(self) -> bool:
        return True

    async def search_by_plate(self, plate: str):
        raise AssertionError("collector API path must never call search_by_plate()")


class _FakeTelegramClient:
    def __init__(self):
        self.sent_messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent_messages.append((chat_id, text))


def _make_ingest_service(tmp_path):
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    fine_repo = DetectedFineRepository(db_path)
    bootstrap_repo = GeorgiaPlateBootstrapRepository(db_path)
    check_service = FineCheckService(_AlwaysAvailableProvider(), task_repo, fine_repo)
    manual_bootstrap = ManualBootstrapService(bootstrap_repo, task_repo, fine_repo, check_service)
    ingest_service = CollectorIngestService(task_repo, fine_repo, check_service, bootstrap_repo, manual_bootstrap)
    return ingest_service, task_repo, fine_repo, bootstrap_repo


def _make_app(tmp_path, *, secret=_SECRET, telegram_client=None, **app_kwargs):
    ingest_service, task_repo, fine_repo, bootstrap_repo = _make_ingest_service(tmp_path)
    telegram_client = telegram_client or _FakeTelegramClient()

    app = build_collector_api_app(ingest_service, hmac_secret=secret, telegram_client=telegram_client, **app_kwargs)
    return app, task_repo, fine_repo, bootstrap_repo, telegram_client


def _create_task(task_repo, car_number, *, chat_id=-100111, created_by=1):
    return task_repo.create(
        car_number=car_number, label=None,
        start_date=date(2026, 10, 1), end_date=date(2026, 11, 1),
        telegram_chat_id=chat_id, created_by_user_id=created_by,
    )


def _signed_headers(method: str, path: str, body: bytes, *, secret=_SECRET, timestamp=None):
    ts = timestamp if timestamp is not None else str(time.time())
    signature = sign(secret, method=method, path=path, timestamp=ts, body=body)
    return {TIMESTAMP_HEADER: ts, SIGNATURE_HEADER: signature}


def _feed_match_payload(car_number="AA001AA", protocol_no="P-0001", amount=50, violation_date="2026-10-10"):
    fingerprint = compute_fingerprint(
        external_fine_id=protocol_no, violation_date=date.fromisoformat(violation_date), amount=amount,
    )
    return {
        "car_number": car_number,
        "external_fine_id": protocol_no,
        "penalty_date": violation_date,
        "due_date": None,
        "delivered_status": "Вручено",
        "fingerprint": fingerprint,
        "raw_data": {"protocolAmount": amount},
        "violation_date": violation_date,
        "amount": float(amount),
        "place": None,
        "violation_description": None,
    }


async def test_valid_authentication_succeeds(tmp_path):
    app, *_rest = _make_app(tmp_path)
    async with TestClient(TestServer(app)) as client:
        headers = _signed_headers("GET", "/internal/georgia/plates", b"")
        resp = await client.get("/internal/georgia/plates", headers=headers)
        assert resp.status == 200
        data = await resp.json()
        assert data == {"plates": []}


async def test_invalid_signature_rejected(tmp_path):
    app, *_rest = _make_app(tmp_path)
    async with TestClient(TestServer(app)) as client:
        ts = str(time.time())
        headers = {TIMESTAMP_HEADER: ts, SIGNATURE_HEADER: "0" * 64}
        resp = await client.get("/internal/georgia/plates", headers=headers)
        assert resp.status == 401


async def test_missing_signature_rejected(tmp_path):
    app, *_rest = _make_app(tmp_path)
    async with TestClient(TestServer(app)) as client:
        ts = str(time.time())
        resp = await client.get("/internal/georgia/plates", headers={TIMESTAMP_HEADER: ts})
        assert resp.status == 401


async def test_stale_timestamp_rejected(tmp_path):
    app, *_rest = _make_app(tmp_path)
    async with TestClient(TestServer(app)) as client:
        stale_ts = str(time.time() - 1000)
        headers = _signed_headers("GET", "/internal/georgia/plates", b"", timestamp=stale_ts)
        resp = await client.get("/internal/georgia/plates", headers=headers)
        assert resp.status == 401


async def test_wrong_secret_rejected_no_data_leaked(tmp_path):
    app, task_repo, *_rest = _make_app(tmp_path)
    _create_task(task_repo, "AA001AA")
    async with TestClient(TestServer(app)) as client:
        headers = _signed_headers("GET", "/internal/georgia/plates", b"", secret="wrong-secret")
        resp = await client.get("/internal/georgia/plates", headers=headers)
        assert resp.status == 401
        data = await resp.json()
        assert "plates" not in data


async def test_malformed_feed_match_payload_rejected(tmp_path):
    app, *_rest = _make_app(tmp_path)
    async with TestClient(TestServer(app)) as client:
        body = b"not json"
        headers = _signed_headers("POST", "/internal/georgia/feed-match", body)
        headers["Content-Type"] = "application/json"
        resp = await client.post("/internal/georgia/feed-match", data=body, headers=headers)
        assert resp.status == 400


async def test_active_plates_requires_auth_and_returns_normalized_set(tmp_path):
    app, task_repo, *_rest = _make_app(tmp_path)
    _create_task(task_repo, "AA001AA")
    _create_task(task_repo, "AA001AA")
    _create_task(task_repo, "BB002BB")

    async with TestClient(TestServer(app)) as client:
        resp_noauth = await client.get("/internal/georgia/plates")
        assert resp_noauth.status == 401

        headers = _signed_headers("GET", "/internal/georgia/plates", b"")
        resp = await client.get("/internal/georgia/plates", headers=headers)
        assert resp.status == 200
        data = await resp.json()
        assert data == {"plates": ["AA001AA", "BB002BB"]}


async def test_pending_bootstrap_endpoint(tmp_path):
    app, _task_repo, _fine_repo, bootstrap_repo, _tg = _make_app(tmp_path)
    bootstrap_repo.ensure_pending("AA001AA")

    async with TestClient(TestServer(app)) as client:
        headers = _signed_headers("GET", "/internal/georgia/bootstrap/pending", b"")
        resp = await client.get("/internal/georgia/bootstrap/pending", headers=headers)
        assert resp.status == 200
        data = await resp.json()
        assert data == {"plates": ["AA001AA"]}


async def test_feed_match_end_to_end_persists_and_dedupes(tmp_path):
    app, task_repo, fine_repo, _bootstrap, _tg = _make_app(tmp_path)
    task = _create_task(task_repo, "AA001AA")
    payload = _feed_match_payload()
    body = json.dumps(payload).encode("utf-8")

    async with TestClient(TestServer(app)) as client:
        headers = _signed_headers("POST", "/internal/georgia/feed-match", body)
        headers["Content-Type"] = "application/json"

        resp1 = await client.post("/internal/georgia/feed-match", data=body, headers=headers)
        assert resp1.status == 200
        data1 = await resp1.json()
        assert data1 == {"status": "ok", "tasks_matched": 1}

        # Повторная отправка ТОГО ЖЕ payload (см. задачу "Collector
        # retry/idempotency") — требует свежую подпись (новый timestamp),
        # но то же тело.
        headers2 = _signed_headers("POST", "/internal/georgia/feed-match", body)
        headers2["Content-Type"] = "application/json"
        resp2 = await client.post("/internal/georgia/feed-match", data=body, headers=headers2)
        assert resp2.status == 200

    rows = fine_repo.list_current_for_task(task.id, since=datetime(1970, 1, 1, tzinfo=timezone.utc))
    assert len(rows) == 1


async def test_bootstrap_result_zero_fines_sends_customer_message_via_telegram_client(tmp_path):
    app, task_repo, _fine_repo, bootstrap_repo, telegram_client = _make_app(tmp_path)
    _create_task(task_repo, "AA001AA", chat_id=-100111)
    bootstrap_repo.ensure_pending("AA001AA")

    payload = {"car_number": "AA001AA", "fines": []}
    body = json.dumps(payload).encode("utf-8")

    async with TestClient(TestServer(app)) as client:
        headers = _signed_headers("POST", "/internal/georgia/bootstrap-result", body)
        headers["Content-Type"] = "application/json"
        resp = await client.post("/internal/georgia/bootstrap-result", data=body, headers=headers)
        assert resp.status == 200
        data = await resp.json()
        assert data["bootstrap_completed"] is True

    assert telegram_client.sent_messages == [(-100111, format_no_fines_found("AA001AA"))]
    assert bootstrap_repo.is_completed("AA001AA")


async def test_bootstrap_result_audit_mode_zero_fines_sends_no_customer_message(tmp_path):
    """См. задачу "fix the remaining issues discovered during live testing"
    — production 515-plate audit needed an ad-hoc skip-list workaround
    precisely because this mode didn't exist at the HTTP layer yet."""
    app, task_repo, _fine_repo, bootstrap_repo, telegram_client = _make_app(tmp_path)
    _create_task(task_repo, "AA001AA", chat_id=-100111)
    bootstrap_repo.ensure_pending("AA001AA")

    payload = {"car_number": "AA001AA", "fines": [], "mode": "audit"}
    body = json.dumps(payload).encode("utf-8")

    async with TestClient(TestServer(app)) as client:
        headers = _signed_headers("POST", "/internal/georgia/bootstrap-result", body)
        headers["Content-Type"] = "application/json"
        resp = await client.post("/internal/georgia/bootstrap-result", data=body, headers=headers)
        assert resp.status == 200
        data = await resp.json()
        assert data["bootstrap_completed"] is False

    assert telegram_client.sent_messages == []
    # Still pending -- audit mode never touches bootstrap state.
    assert not bootstrap_repo.is_completed("AA001AA")


async def test_bootstrap_result_invalid_mode_rejected(tmp_path):
    app, task_repo, *_rest = _make_app(tmp_path)
    _create_task(task_repo, "AA001AA", chat_id=-100111)

    payload = {"car_number": "AA001AA", "fines": [], "mode": "not-a-real-mode"}
    body = json.dumps(payload).encode("utf-8")

    async with TestClient(TestServer(app)) as client:
        headers = _signed_headers("POST", "/internal/georgia/bootstrap-result", body)
        headers["Content-Type"] = "application/json"
        resp = await client.post("/internal/georgia/bootstrap-result", data=body, headers=headers)
        assert resp.status == 400


async def test_bootstrap_result_missing_mode_field_defaults_to_bootstrap_behavior(tmp_path):
    """Backward compatibility -- every call made before this field existed
    (including the currently-deployed collector's own _run_bootstrap(),
    which never sends it) must behave byte-for-byte as before."""
    app, task_repo, _fine_repo, bootstrap_repo, telegram_client = _make_app(tmp_path)
    _create_task(task_repo, "AA001AA", chat_id=-100111)
    bootstrap_repo.ensure_pending("AA001AA")

    payload = {"car_number": "AA001AA", "fines": []}  # no "mode" key at all
    body = json.dumps(payload).encode("utf-8")

    async with TestClient(TestServer(app)) as client:
        headers = _signed_headers("POST", "/internal/georgia/bootstrap-result", body)
        headers["Content-Type"] = "application/json"
        resp = await client.post("/internal/georgia/bootstrap-result", data=body, headers=headers)
        assert resp.status == 200
        data = await resp.json()
        assert data["bootstrap_completed"] is True

    assert telegram_client.sent_messages == [(-100111, format_no_fines_found("AA001AA"))]
    assert bootstrap_repo.is_completed("AA001AA")


async def test_bootstrap_result_missing_bootstrap_row_reports_not_completed(tmp_path):
    """Regression for the K333HY190 production incident -- a plate with an
    active task but no bootstrap row at all must report bootstrap_completed
    as False, not True (mark_completed() silently no-ops on a missing row)."""
    app, task_repo, _fine_repo, bootstrap_repo, telegram_client = _make_app(tmp_path)
    _create_task(task_repo, "AA001AA", chat_id=-100111)
    assert bootstrap_repo.get("AA001AA") is None

    payload = {"car_number": "AA001AA", "fines": []}
    body = json.dumps(payload).encode("utf-8")

    async with TestClient(TestServer(app)) as client:
        headers = _signed_headers("POST", "/internal/georgia/bootstrap-result", body)
        headers["Content-Type"] = "application/json"
        resp = await client.post("/internal/georgia/bootstrap-result", data=body, headers=headers)
        assert resp.status == 200
        data = await resp.json()
        assert data["bootstrap_completed"] is False  # fixed -- was incorrectly True before

    assert telegram_client.sent_messages == []  # no unwarranted "no fines found" message
    assert bootstrap_repo.get("AA001AA") is None


async def test_health_endpoint_requires_auth(tmp_path):
    """See security review: EVERY route, including /health, must go through
    the auth middleware — not just the data-returning ones."""
    app, *_rest = _make_app(tmp_path)
    payload = {"status": "ok"}
    body = json.dumps(payload).encode("utf-8")

    async with TestClient(TestServer(app)) as client:
        resp_noauth = await client.post(
            "/internal/georgia/health", data=body, headers={"Content-Type": "application/json"},
        )
        assert resp_noauth.status == 401

        resp_wrong_secret = await client.post(
            "/internal/georgia/health", data=body,
            headers={**_signed_headers("POST", "/internal/georgia/health", body, secret="wrong-secret"),
                     "Content-Type": "application/json"},
        )
        assert resp_wrong_secret.status == 401


async def test_oversized_request_body_rejected_before_auth_check(tmp_path):
    """See security review: an explicit client_max_size (64 KiB) must reject
    an oversized body with 413 from within aiohttp's own body-reading, i.e.
    BEFORE the auth middleware's `body = await request.read()` can return and
    hand that body to verify()'s sha256() call — an oversized payload must
    never reach HMAC verification or any handler/business logic at all."""
    app, *_rest = _make_app(tmp_path)
    oversized_body = json.dumps({
        "car_number": "AA001AA",
        "raw_data": {"padding": "x" * (128 * 1024)},  # well past the 64 KiB cap
    }).encode("utf-8")

    async with TestClient(TestServer(app)) as client:
        # Correctly signed for the oversized body — proves rejection happens
        # on size alone, not because the signature was wrong.
        headers = _signed_headers("POST", "/internal/georgia/feed-match", oversized_body)
        headers["Content-Type"] = "application/json"
        resp = await client.post(
            "/internal/georgia/feed-match", data=oversized_body, headers=headers,
        )
        assert resp.status == 413


async def test_health_endpoint_acknowledges(tmp_path):
    app, *_rest = _make_app(tmp_path)
    payload = {"status": "ok", "detail": "test"}
    body = json.dumps(payload).encode("utf-8")

    async with TestClient(TestServer(app)) as client:
        headers = _signed_headers("POST", "/internal/georgia/health", body)
        headers["Content-Type"] = "application/json"
        resp = await client.post("/internal/georgia/health", data=body, headers=headers)
        assert resp.status == 200
        data = await resp.json()
        assert data == {"status": "ack"}


# ==== /health -> alerts for the managers' group (reader/public_bot/
# collector_health.py) ====


async def _post_health(client, payload) -> int:
    body = json.dumps(payload).encode("utf-8")
    headers = _signed_headers("POST", "/internal/georgia/health", body)
    headers["Content-Type"] = "application/json"
    resp = await client.post("/internal/georgia/health", data=body, headers=headers)
    assert await resp.json() == {"status": "ack"}
    return resp.status


async def test_health_problem_alerts_the_support_chat_once_and_says_when_it_is_over(tmp_path):
    app, *_rest, telegram_client = _make_app(tmp_path, alert_chat_id=-100555)
    async with TestClient(TestServer(app)) as client:
        assert await _post_health(client, {"status": "ok"}) == 200
        assert await _post_health(client, {"status": "recovering", "detail": "cap", "pages": 300}) == 200
        assert await _post_health(client, {"status": "recovering", "detail": "cap", "pages": 300}) == 200
        assert await _post_health(client, {"status": "ok"}) == 200

    assert [chat for chat, _text in telegram_client.sent_messages] == [-100555, -100555]
    assert telegram_client.sent_messages[0][1].startswith("⚠️ ")
    assert telegram_client.sent_messages[1][1].startswith("✅ ")


async def test_health_single_error_is_not_alerted_the_second_in_a_row_is(tmp_path):
    app, *_rest, telegram_client = _make_app(tmp_path, alert_chat_id=-100555)
    async with TestClient(TestServer(app)) as client:
        assert await _post_health(client, {"status": "error", "detail": "ConnectTimeout"}) == 200
        assert telegram_client.sent_messages == []
        assert await _post_health(client, {"status": "error", "detail": "ConnectTimeout"}) == 200

    assert len(telegram_client.sent_messages) == 1
    assert telegram_client.sent_messages[0][1].startswith("⚠️ ")


async def test_health_alert_without_a_support_chat_is_only_logged(tmp_path, caplog):
    import logging

    app, *_rest, telegram_client = _make_app(tmp_path)
    with caplog.at_level(logging.WARNING, logger="reader.public_bot.collector_api"):
        async with TestClient(TestServer(app)) as client:
            assert await _post_health(client, {"status": "error", "detail": "ConnectTimeout"}) == 200
            assert await _post_health(client, {"status": "error", "detail": "ConnectTimeout"}) == 200

    assert telegram_client.sent_messages == []
    assert any("support_chat_id not configured" in r.getMessage() for r in caplog.records)


async def test_health_alert_that_cannot_be_sent_still_acknowledges_and_stays_queued(tmp_path):
    from reader.public_bot.collector_health import CollectorHealthMonitor, CollectorHealthStore

    class _FlakyTelegramClient(_FakeTelegramClient):
        failing = True

        async def send_message(self, chat_id: int, text: str) -> None:
            if self.failing:
                raise RuntimeError("telegram unavailable")
            await super().send_message(chat_id, text)

    telegram_client = _FlakyTelegramClient()
    monitor = CollectorHealthMonitor(CollectorHealthStore(tmp_path / "users.db"))
    app, *_rest = _make_app(tmp_path, telegram_client=telegram_client, alert_chat_id=-100555,
                            health_monitor=monitor)
    async with TestClient(TestServer(app)) as client:
        assert await _post_health(client, {"status": "recovering", "detail": "cap", "pages": 300}) == 200

    assert telegram_client.sent_messages == []
    [queued] = monitor.store.queued()
    assert queued.attempts == 1
    assert monitor.store.load().problem_alerted_at is None


async def test_health_answers_without_waiting_for_a_slow_telegram_and_delivers_in_the_background(
    tmp_path, monkeypatch,
):
    """A hanging Telegram must not hold the answer past the collector
    client's timeout (it would re-send the report): the handler waits a few
    seconds at most and the delivery finishes in the background."""
    import asyncio

    from reader.public_bot import collector_api

    monkeypatch.setattr(collector_api, "_HEALTH_DELIVERY_WAIT_SECONDS", 0.05)
    release = asyncio.Event()

    class _SlowTelegramClient(_FakeTelegramClient):
        async def send_message(self, chat_id: int, text: str) -> None:
            await release.wait()
            await super().send_message(chat_id, text)

    telegram_client = _SlowTelegramClient()
    app, *_rest = _make_app(tmp_path, telegram_client=telegram_client, alert_chat_id=-100555)
    async with TestClient(TestServer(app)) as client:
        assert await _post_health(client, {"status": "recovering", "detail": "cap", "pages": 300}) == 200
        assert telegram_client.sent_messages == []
        release.set()
        for _ in range(100):
            if telegram_client.sent_messages:
                break
            await asyncio.sleep(0.01)

    assert len(telegram_client.sent_messages) == 1
    assert telegram_client.sent_messages[0][1].startswith("⚠️ ")


async def test_health_with_a_non_object_json_body_is_acknowledged(tmp_path):
    app, *_rest, telegram_client = _make_app(tmp_path, alert_chat_id=-100555)
    async with TestClient(TestServer(app)) as client:
        assert await _post_health(client, ["not", "an", "object"]) == 200
        assert await _post_health(client, ["not", "an", "object"]) == 200

    assert len(telegram_client.sent_messages) == 1
    assert telegram_client.sent_messages[0][1].startswith("⚠️ ")


async def test_watchdog_alerts_after_13_silent_hours_and_retries_undelivered_alerts():
    import asyncio
    from datetime import timedelta

    from reader.public_bot.collector_api import watch_collector_health
    from reader.public_bot.collector_health import CollectorHealthMonitor

    now = [datetime(2026, 11, 20, 5, 0, tzinfo=timezone.utc)]
    monitor = CollectorHealthMonitor(clock=lambda: now[0])
    monitor.observe({"status": "ok"})
    sent: list[str] = []
    failures = [1]
    sleeps: list[float] = []

    async def send(text: str) -> None:
        if failures[0]:
            failures[0] -= 1
            raise ConnectionError("telegram unavailable")
        sent.append(text)

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) > 4:
            raise asyncio.CancelledError
        now[0] += timedelta(hours=7) if len(sleeps) <= 2 else timedelta(minutes=1)

    with pytest.raises(asyncio.CancelledError):
        await watch_collector_health(monitor, send, sleep=fake_sleep)

    # 12:00 — тихо; 19:00 — 14 ч молчания, Telegram не принял; 19:01 — повтор доставлен.
    assert len(sent) == 1
    assert "нет отчётов 14 ч" in sent[0]
    assert sleeps == [60, 60, 60, 60, 60]


async def test_watchdog_survives_an_unexpected_error():
    import asyncio

    from reader.public_bot.collector_api import watch_collector_health

    class _BrokenMonitor:
        calls = 0

        def check_silence(self):
            self.calls += 1
            raise RuntimeError("db locked")

    monitor = _BrokenMonitor()
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) > 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await watch_collector_health(monitor, None, sleep=fake_sleep)
    assert monitor.calls == 2


async def test_run_collector_api_forever_keeps_alert_state_in_the_production_db_and_stops_the_watchdog(
    tmp_path, monkeypatch,
):
    import asyncio
    import sqlite3

    from reader.public_bot import collector_api
    from reader.public_bot.collector_health import CollectorHealthMonitor

    ingest_service, *_repos = _make_ingest_service(tmp_path)
    captured: dict = {}
    real_build = collector_api.build_collector_api_app

    def spy_build(*args, **kwargs):
        captured.update(kwargs)
        return real_build(*args, **kwargs)

    monkeypatch.setattr(collector_api, "build_collector_api_app", spy_build)
    task = asyncio.create_task(collector_api.run_collector_api_forever(
        ingest_service, hmac_secret=_SECRET, telegram_client=_FakeTelegramClient(),
        host="127.0.0.1", port=0, alert_chat_id=-100777, health_db_path=tmp_path / "users.db",
    ))
    for _ in range(50):
        if captured:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert captured["alert_chat_id"] == -100777
    assert isinstance(captured["health_monitor"], CollectorHealthMonitor)
    tables = {row[0] for row in sqlite3.connect(tmp_path / "users.db").execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"georgia_collector_health", "georgia_collector_alert_outbox"} <= tables
