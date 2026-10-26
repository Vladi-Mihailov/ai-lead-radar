"""REDNET's inbound half of the Check Now fix (see "Fix the Georgia public
bot Check Now flow"). Until this module, the collector was outbound-only —
it polled/pushed to production (collector_job.py via production_client.py);
nothing on REDNET accepted a connection. This adds the ONE thing production
needs synchronously: "look up this plate on police.ge right now and tell me
what you found" — for the public bot's 🔎 Проверить сейчас button, which
cannot wait for the next scheduled collector cycle.

POST /internal/georgia/check-now, body {"plate": "<plate>"} ->
{"records": [<collector_payload.record_to_payload() dicts>]} on success.
Uses the SAME PoliceGeSession/PoliceGeCircuitBreaker instance the polling
job already holds (see reader/georgia_collector/__main__.py) — one breaker
per process, shared across both the scheduled polling and these on-demand
lookups, so a string of on-demand failures counts toward the same breaker
the scheduled job already respects, and vice versa.

Same HMAC auth as the rest of this architecture (reader/fines/
collector_http_auth.py, reader/fines/collector_auth.py) — production signs
the request the same way this collector signs ITS requests to production
(see production_client.py). Deliberately does NOT import anything from
reader/public_bot/ or reader/jobs/ (see collector_job.py's "no Telegram/
production-DB imports" invariant, enforced by test) — this module only
calls police.ge and formats a response; all persistence/dedup/notification
stays on production."""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from reader.fines.collector_http_auth import build_hmac_middleware
from reader.fines.collector_payload import record_to_payload
from reader.fines.parser import FineParseError, parse_search_response
from reader.fines.police_ge_session import PoliceGeSession
from reader.fines.provider import FineProviderError, PoliceGeUnavailableError
from reader.fines.validation import FineValidationError, normalize_car_number

logger = logging.getLogger(__name__)

_CHECK_NOW_PATH = "/internal/georgia/check-now"

# Same reasoning as collector_api.py's _MAX_REQUEST_BODY_SIZE — a
# {"plate": "..."} body is a few dozen bytes; generous headroom, tight cap.
_MAX_REQUEST_BODY_SIZE = 64 * 1024


def build_check_now_app(session: PoliceGeSession, *, hmac_secret: str) -> web.Application:
    app = web.Application(
        middlewares=[build_hmac_middleware(hmac_secret)], client_max_size=_MAX_REQUEST_BODY_SIZE,
    )

    async def post_check_now(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
            plate = normalize_car_number(payload["plate"])
        except (ValueError, KeyError, TypeError):
            return web.json_response({"error": "malformed payload"}, status=400)
        except FineValidationError:
            return web.json_response({"error": "invalid plate"}, status=400)

        try:
            raw = await session.search_by_plate(plate)
            records = parse_search_response(raw, car_number=plate)
        except PoliceGeUnavailableError:
            # Circuit breaker open — distinguishable from other failures so
            # production's RednetCheckNowProvider can raise the SAME
            # PoliceGeUnavailableError its caller (FineCheckService) already
            # knows how to turn into the existing "service temporarily
            # unavailable" message (see reader/fines/provider.py).
            logger.info("check-now: circuit open, rejecting without a police.ge call")
            return web.json_response({"error": "circuit_open"}, status=503)
        except FineParseError as exc:
            logger.warning("check-now: police.ge response did not parse (%s)", type(exc).__name__)
            return web.json_response({"error": "parse_error"}, status=502)
        except FineProviderError as exc:
            logger.warning("check-now: provider error (%s)", type(exc).__name__)
            return web.json_response({"error": "provider_error"}, status=502)

        return web.json_response({"records": [record_to_payload(r) for r in records]})

    app.router.add_post(_CHECK_NOW_PATH, post_check_now)
    return app


async def run_check_now_server_forever(session: PoliceGeSession, *, hmac_secret: str, host: str, port: int) -> None:
    """Same pattern as reader/public_bot/collector_api.py's
    run_collector_api_forever() — one of __main__.py's concurrently-awaited
    coroutines, alongside the existing polling loop."""
    app = build_check_now_app(session, hmac_secret=hmac_secret)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("Georgia collector check-now server listening on %s:%d", host, port)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
