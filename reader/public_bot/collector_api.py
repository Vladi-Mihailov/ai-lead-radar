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

/health: the collector reports every cycle's outcome there (counts and dates
only, see reader/georgia_collector/collector_job.py::FeedPollOutcome).
reader/public_bot/collector_health.py turns problem reports and a silent
collector into short alerts for the managers' group
(settings.public_bot.support_chat_id, passed in as alert_chat_id; None —
alerts are only logged). An alert that cannot be sent is logged and never
fails the request.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web
from telethon import TelegramClient

from reader.fines.collector_http_auth import build_hmac_middleware
from reader.fines.collector_payload import payload_to_record
from reader.public_bot.collector_health import CollectorHealthMonitor
from reader.public_bot.collector_ingest_service import (
    BOOTSTRAP_MODE,
    VALID_INGEST_MODES,
    CollectorIngestService,
)

logger = logging.getLogger(__name__)

# Explicit, intentional cap — NOT relying on aiohttp's own 1 MiB library
# default (undocumented/untested in this codebase). A single ParsedFineRecord
# is well under 1 KB; even a generously-sized bootstrap-result (dozens of
# fines for one plate) stays under a few tens of KB — 64 KiB leaves ample
# headroom while keeping the ceiling tight. aiohttp enforces this while
# streaming the body (raises HTTPRequestEntityTooLarge from within
# request.read() itself), so an oversized body is rejected with 413 BEFORE
# the auth middleware's body = await request.read() call below can return —
# the HMAC verification's own sha256(body) never runs on an oversized
# payload, and no handler/business logic ever sees it either.
_MAX_REQUEST_BODY_SIZE = 64 * 1024

# Как часто сторож проверяет, не замолчал ли коллектор (см.
# CollectorHealthMonitor.check_silence) — порог там 13 ч, точнее не нужно.
_SILENCE_CHECK_INTERVAL_SECONDS = 600


async def send_collector_alert(telegram_client: TelegramClient, chat_id: int | None, text: str) -> None:
    """fail-open: предупреждение, которое не удалось отправить, только
    пишется в журнал (тот же приём, что и у "штрафов не найдено" ниже)."""
    if chat_id is None:
        logger.warning("Georgia collector alert (support_chat_id not configured): %s", text.splitlines()[0])
        return
    try:
        await telegram_client.send_message(chat_id, text)
    except Exception:
        logger.exception("Georgia collector API: не удалось отправить предупреждение о коллекторе менеджерам")


async def watch_collector_silence(
    monitor: CollectorHealthMonitor, send, *, sleep=asyncio.sleep,
    interval_seconds: float = _SILENCE_CHECK_INTERVAL_SECONDS,
) -> None:
    """Сторож (см. CollectorHealthMonitor.check_silence): send — async
    callable(text). Работает, пока его не отменят."""
    while True:
        await sleep(interval_seconds)
        text = monitor.check_silence()
        if text:
            await send(text)


def build_collector_api_app(
    ingest_service: CollectorIngestService, *, hmac_secret: str, telegram_client: TelegramClient,
    health_monitor: CollectorHealthMonitor | None = None, alert_chat_id: int | None = None,
) -> web.Application:
    monitor = health_monitor or CollectorHealthMonitor()
    app = web.Application(
        middlewares=[build_hmac_middleware(hmac_secret)], client_max_size=_MAX_REQUEST_BODY_SIZE,
    )

    async def get_plates(request: web.Request) -> web.Response:
        return web.json_response({"plates": ingest_service.active_plates()})

    async def get_pending_bootstrap(request: web.Request) -> web.Response:
        return web.json_response({"plates": ingest_service.pending_bootstrap_plates()})

    async def post_feed_match(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
            car_number = payload["car_number"]
            record = payload_to_record(payload)
        except (ValueError, KeyError, TypeError) as exc:
            return web.json_response({"error": f"malformed payload: {exc}"}, status=400)

        result = await ingest_service.ingest_feed_match(car_number, record)
        return web.json_response({"status": "ok", "tasks_matched": result.tasks_matched})

    async def post_bootstrap_result(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
            car_number = payload["car_number"]
            records = [
                payload_to_record({**entry, "car_number": car_number})
                for entry in payload.get("fines", [])
            ]
            # "bootstrap" (default, absent field) — identical to every call
            # made before this field existed (см. задачу "fix the remaining
            # issues discovered during live testing"). "audit" — see
            # collector_ingest_service.py module docstring.
            mode = payload.get("mode", BOOTSTRAP_MODE)
        except (ValueError, KeyError, TypeError) as exc:
            return web.json_response({"error": f"malformed payload: {exc}"}, status=400)

        if mode not in VALID_INGEST_MODES:
            return web.json_response({"error": f"invalid mode: {mode!r}"}, status=400)

        outcome = await ingest_service.ingest_bootstrap_result(car_number, records, mode=mode)

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
        if not isinstance(payload, dict):
            payload = {}
        status = payload.get("status", "unknown")
        if status == "ok":
            logger.info("Georgia collector health: ok (%s)", payload.get("detail", ""))
        else:
            logger.warning("Georgia collector health: %s (%s)", status, payload.get("detail", ""))
        alert = monitor.observe(payload)
        if alert:
            await send_collector_alert(telegram_client, alert_chat_id, alert)
        return web.json_response({"status": "ack"})

    app.router.add_get("/internal/georgia/plates", get_plates)
    app.router.add_get("/internal/georgia/bootstrap/pending", get_pending_bootstrap)
    app.router.add_post("/internal/georgia/feed-match", post_feed_match)
    app.router.add_post("/internal/georgia/bootstrap-result", post_bootstrap_result)
    app.router.add_post("/internal/georgia/health", post_health)

    return app


async def run_collector_api_forever(
    ingest_service: CollectorIngestService, *, hmac_secret: str, telegram_client: TelegramClient,
    host: str, port: int, alert_chat_id: int | None = None,
) -> None:
    """Одна из корутин reader/public_bot/main.py's _run_concurrently([...])
    (ТОТ ЖЕ приём, что и client.run_until_disconnected()/
    _run_delivery_poller() там же) — awaited forever, как и
    scheduler.run_forever() в reader/main.py."""
    monitor = CollectorHealthMonitor()
    app = build_collector_api_app(
        ingest_service, hmac_secret=hmac_secret, telegram_client=telegram_client,
        health_monitor=monitor, alert_chat_id=alert_chat_id,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("Georgia collector API listening on %s:%d", host, port)
    watchdog = asyncio.create_task(watch_collector_silence(
        monitor, lambda text: send_collector_alert(telegram_client, alert_chat_id, text),
    ))
    try:
        await asyncio.Event().wait()
    finally:
        watchdog.cancel()
        await runner.cleanup()
