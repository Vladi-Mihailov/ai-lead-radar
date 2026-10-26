"""Shared aiohttp HMAC auth middleware for the Georgia remote collector's two
internal HTTP APIs — production's internal collector API (reader/public_bot/
collector_api.py, REDNET -> production) and REDNET's internal check-now
server (reader/georgia_collector/check_now_server.py, production -> REDNET,
see "Fix the Georgia public bot Check Now flow"). Extracted from
collector_api.py's own former private _auth_middleware so both sides verify
requests identically instead of maintaining two copies of the same logic.

Deliberately NOT in reader/fines/collector_auth.py — that module is
stdlib-only so REDNET's minimal install (requirements-collector.txt) can
import it without aiohttp. This module depends on aiohttp and is itself an
addition to REDNET's minimal dependency set (see requirements-collector.txt's
comment for why aiohttp is now needed there too)."""

from __future__ import annotations

import logging

from aiohttp import web

from reader.fines.collector_auth import SIGNATURE_HEADER, TIMESTAMP_HEADER, verify

logger = logging.getLogger(__name__)


def build_hmac_middleware(secret: str):
    """aiohttp middleware factory — rejects any request whose
    X-Collector-Timestamp/X-Collector-Signature headers don't verify against
    `secret` (see collector_auth.verify()). Never logs the secret, signature,
    or body — only the short rejection-reason category."""

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
            logger.warning("Georgia internal API: auth rejected (%s)", reason)
            return web.json_response({"error": "unauthorized"}, status=401)

        return await handler(request)

    return middleware
