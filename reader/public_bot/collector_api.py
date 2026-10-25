"""HTTP/HMAC transport layer for the Georgia remote collector internal API
(see "Georgia remote collector architecture (REDNET)") — reader/public_bot/
collector_ingest_service.py has all the actual persistence logic; this
module is ONLY aiohttp routing, HMAC auth, and JSON (de)serialization.

Runs IN-PROCESS with reader/public_bot/main.py (one more coroutine in its
existing _run_concurrently([...]) list, alongside client.run_until_
disconnected()/_run_delivery_poller()) — NOT reader/main.py — specifically
so the zero-fines "no fines found" customer message can be sent via the SAME
Telethon client/bot identity the customer is already talking to. Sending it
from reader/main.py's separate client (a different Telegram identity
entirely) would appear to come from the wrong bot.

Bound to 127.0.0.1 by default (settings.fine_monitor.collector_api_host) —
deliberately NOT directly internet-facing; REDNET reaches this through a
reverse proxy terminating TLS, set up separately and manually at deploy time
(see docs/georgia-remote-collector.md) — this module never does TLS itself.

No new dedup/idempotency layer here on purpose (see collector_ingest_service.
py docstring) — persist_feed_match()'s fingerprint dedup and mark_completed()'s
idempotent re-check already make a retried/duplicate POST safe; adding a
second "have we seen this request" layer here would be exactly the parallel
dedupe system the architecture explicitly avoids.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date

from aiohttp import web
from telethon import TelegramClient

from reader.fines.collector_auth import SIGNATURE_HEADER, TIMESTAMP_HEADER, verify
from reader.fines.models import ParsedFineRecord
from reader.public_bot.collector_ingest_service import CollectorIngestService

logger = logging.getLogger(__name__)


def _parse_iso_date(value: object) -> date | None:
    if value in (None, ""):
        return None
    return date.fromisoformat(value)  # type: ignore[arg-type]


def _record_from_payload(payload: dict) -> ParsedFineRecord:
    """Builds a ParsedFineRecord from fields the COLLECTOR already parsed
    AND fingerprinted (it already called extract_published_protocols_
    results()/parse_published_protocol_entry()/parse_search_response() —
    the SAME reader/fines/parser.py functions an in-process job would use —
    on the raw police.ge JSON itself, see reader/georgia_collector/
    collector_job.py). This is not a second parse of raw police.ge data.

    CRITICAL: `fingerprint` is taken VERBATIM from the payload — it is NOT
    recomputed here. The collector already ran compute_fingerprint() on the
    true raw (pre-parse) protocolAmount value, which this API never sees at
    all (ParsedFineRecord doesn't carry it — only the already-hashed
    fingerprint). Recomputing it server-side from a separately-transmitted
    "amount" field would reintroduce exactly the representation-drift risk
    this design avoids (e.g. a JSON round-trip turning "50" into 50 and
    back into "50" is safe, but typing it as a float first and stringifying
    that is not, see FINGERPRINT CAVEAT in the PR description) — carrying
    the finished fingerprint string is a plain string round-trip through
    JSON, which has zero representation risk at all. Trusting the
    collector's own fingerprint value is safe because the collector is a
    trusted HMAC-authenticated party, not an adversarial input source."""
    external_fine_id = payload.get("external_fine_id")
    violation_date = _parse_iso_date(payload.get("violation_date"))

    return ParsedFineRecord(
        car_number=payload["car_number"],
        external_fine_id=external_fine_id,
        penalty_date=_parse_iso_date(payload.get("penalty_date")),
        due_date=_parse_iso_date(payload.get("due_date")),
        delivered_status=payload.get("delivered_status") or "Не вручено",
        fingerprint=payload["fingerprint"],
        raw_data=payload.get("raw_data") or {},
        violation_date=violation_date,
        amount=payload.get("amount"),
        place=payload.get("place"),
        violation_description=payload.get("violation_description"),
    )


def _auth_middleware(secret: str):
    @web.middleware
    async def middleware(request: web.Request, handler):
        timestamp = request.headers.get(TIMESTAMP_HEADER, "")
        signature = request.headers.get(SIGNATURE_HEADER, "")
        body = await request.read()

        is_valid, reason = verify(
            secret, method=request.method, path=request.path,
            timestamp=timestamp, body=body, signature=signature,
        )
        if not is_valid:
            # Только категория причины — никогда секрет/подпись/тело (см.
            # модульный докстрок reader/fines/collector_auth.py).
            logger.warning("Georgia collector API: auth rejected (%s)", reason)
            return web.json_response({"error": "unauthorized"}, status=401)

        return await handler(request)

    return middleware


def build_collector_api_app(
    ingest_service: CollectorIngestService, *, hmac_secret: str, telegram_client: TelegramClient,
) -> web.Application:
    app = web.Application(middlewares=[_auth_middleware(hmac_secret)])

    async def get_plates(request: web.Request) -> web.Response:
        return web.json_response({"plates": ingest_service.active_plates()})

    async def get_pending_bootstrap(request: web.Request) -> web.Response:
        return web.json_response({"plates": ingest_service.pending_bootstrap_plates()})

    async def post_feed_match(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
            car_number = payload["car_number"]
            record = _record_from_payload(payload)
        except (ValueError, KeyError, TypeError) as exc:
            return web.json_response({"error": f"malformed payload: {exc}"}, status=400)

        result = await ingest_service.ingest_feed_match(car_number, record)
        return web.json_response({"status": "ok", "tasks_matched": result.tasks_matched})

    async def post_bootstrap_result(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
            car_number = payload["car_number"]
            records = [
                _record_from_payload({**entry, "car_number": car_number})
                for entry in payload.get("fines", [])
            ]
        except (ValueError, KeyError, TypeError) as exc:
            return web.json_response({"error": f"malformed payload: {exc}"}, status=400)

        outcome = await ingest_service.ingest_bootstrap_result(car_number, records)

        for chat_id, text in outcome.customer_notify_messages:
            try:
                await telegram_client.send_message(chat_id, text)
            except Exception:
                # fail-open (тот же приём, что и handlers.py::
                # customer_notify_messages докстрок) — доставка клиенту не
                # должна откатывать уже успешно сохранённое
                # production-состояние (bootstrap уже completed).
                logger.exception(
                    "Georgia collector API: не удалось доставить "
                    "'штрафов не найдено' chat_id=%s", chat_id,
                )

        return web.json_response({
            "status": "ok",
            "tasks_affected": outcome.tasks_affected,
            "fines_persisted": outcome.fines_persisted,
            "bootstrap_completed": outcome.bootstrap_completed,
        })

    async def post_health(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        status = payload.get("status", "unknown")
        if status == "ok":
            logger.info("Georgia collector health: ok (%s)", payload.get("detail", ""))
        else:
            logger.warning("Georgia collector health: %s (%s)", status, payload.get("detail", ""))
        return web.json_response({"status": "ack"})

    app.router.add_get("/internal/georgia/plates", get_plates)
    app.router.add_get("/internal/georgia/bootstrap/pending", get_pending_bootstrap)
    app.router.add_post("/internal/georgia/feed-match", post_feed_match)
    app.router.add_post("/internal/georgia/bootstrap-result", post_bootstrap_result)
    app.router.add_post("/internal/georgia/health", post_health)

    return app


async def run_collector_api_forever(
    ingest_service: CollectorIngestService, *, hmac_secret: str, telegram_client: TelegramClient,
    host: str, port: int,
) -> None:
    """Одна из корутин reader/public_bot/main.py's _run_concurrently([...])
    (ТОТ ЖЕ приём, что и client.run_until_disconnected()/
    _run_delivery_poller() там же) — awaited forever, как и
    scheduler.run_forever() в reader/main.py."""
    app = build_collector_api_app(
        ingest_service, hmac_secret=hmac_secret, telegram_client=telegram_client,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("Georgia collector API listening on %s:%d", host, port)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
