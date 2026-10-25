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


def _make_app(tmp_path, *, secret=_SECRET):
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    fine_repo = DetectedFineRepository(db_path)
    bootstrap_repo = GeorgiaPlateBootstrapRepository(db_path)
    check_service = FineCheckService(_AlwaysAvailableProvider(), task_repo, fine_repo)
    manual_bootstrap = ManualBootstrapService(bootstrap_repo, task_repo, fine_repo, check_service)
    ingest_service = CollectorIngestService(task_repo, fine_repo, check_service, bootstrap_repo, manual_bootstrap)
    telegram_client = _FakeTelegramClient()

    app = build_collector_api_app(ingest_service, hmac_secret=secret, telegram_client=telegram_client)
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
