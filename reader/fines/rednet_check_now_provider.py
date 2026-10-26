"""Germany-side FineProvider implementations for the Check Now fix (see
"Fix the Georgia public bot Check Now flow") — police.ge is IP-blocked from
Germany's production IP (see "police.ge reachability"), so a live Check Now
lookup needs to reach police.ge through REDNET instead.

Two providers:

- RednetCheckNowProvider — calls REDNET's inbound check-now server (reader/
  georgia_collector/check_now_server.py) over the WireGuard tunnel, HMAC-
  signed the same way reader/georgia_collector/production_client.py signs
  ITS requests (the opposite direction of this same shared scheme).

- AutoFailoverCheckNowProvider — tries local police.ge first, falls back to
  REDNET only on failure. This is the production default (see
  reader/settings.py's fine_monitor.georgia_checknow_routing="auto") so that
  REDNET stays a fallback transport, not a mandatory dependency: if
  police.ge ever becomes reachable from Germany again, REDNET can be turned
  off without breaking Check Now, and local recovering doesn't require any
  config change either.

  Deliberately almost trivial — reader/fines/police_ge_session.py's
  PoliceGeSession.search_by_plate() already calls the circuit breaker's
  allow_request() at the very top and raises PoliceGeUnavailableError (a
  FineProviderError subclass) IMMEDIATELY when the breaker is open ("без
  ожидания request_timeout", see its own docstring) — so "skip local
  immediately once the breaker is open" and "don't wait out a full German
  timeout on every click" both fall out of the EXISTING breaker for free;
  catching FineProviderError and falling back to REDNET covers both a
  breaker-open rejection and a genuine mid-request transport failure with
  one branch. A legitimate zero-fines result is a normal RETURN (empty
  list) from search_by_plate(), never an exception — AUTO structurally
  never falls back on a real business result. "Periodically allow local to
  recover" needs no separate prober either: AUTO always tries local first
  on every call and never caches "local is dead" itself; the breaker's own
  cooldown/half-open logic (see police_ge_circuit_breaker.py) is what
  decides when local becomes attemptable again, and record_success() inside
  PoliceGeSession closes the breaker again once it does. If REDNET also
  fails after local failed, that exception is NOT caught here — it
  propagates to FineCheckService.check_task(), which already catches
  FineProviderError exactly once to produce exactly one CheckResult/
  error outcome (see reader/fines/check_service.py) — so a double local+
  REDNET failure still surfaces as exactly one user-facing message, with no
  second catch-and-retry layer needed here."""

from __future__ import annotations

import json
import logging
import time

import httpx

from reader.fines.collector_auth import SIGNATURE_HEADER, TIMESTAMP_HEADER, sign
from reader.fines.collector_payload import payload_to_record
from reader.fines.models import ParsedFineRecord
from reader.fines.provider import (
    FineProvider,
    FineProviderError,
    PoliceGeUnavailableError,
)

logger = logging.getLogger(__name__)

_CHECK_NOW_PATH = "/internal/georgia/check-now"

# Short, live-user-facing-wait timeout — this blocks a Telegram callback
# response while the user watches the ⏳ message, NOT the 180s
# submission_timeout used by the OPPOSITE (REDNET -> production)
# direction for feed-match/bootstrap-result (see production_client.py's
# module docstring) — that one tolerates a slow synchronous OpenAI
# translation on production's side; this one must stay short.
DEFAULT_CHECK_NOW_TIMEOUT_SECONDS = 35.0


class RednetCheckNowProvider(FineProvider):
    """Calls REDNET's /internal/georgia/check-now instead of police.ge
    directly. Single attempt, no in-call retries — see module docstring on
    why retry-shaped behavior belongs in AutoFailoverCheckNowProvider, not
    here."""

    def __init__(
        self, client: httpx.AsyncClient, *, hmac_secret: str,
        timeout: float = DEFAULT_CHECK_NOW_TIMEOUT_SECONDS,
    ):
        self._client = client
        self._hmac_secret = hmac_secret
        self._timeout = timeout

    async def search_by_plate(self, plate: str) -> list[ParsedFineRecord]:
        body = json.dumps({"plate": plate}).encode("utf-8")
        timestamp = str(time.time())
        signature = sign(
            self._hmac_secret, method="POST", path=_CHECK_NOW_PATH, timestamp=timestamp, body=body,
        )
        headers = {
            TIMESTAMP_HEADER: timestamp,
            SIGNATURE_HEADER: signature,
            "Content-Type": "application/json",
        }

        try:
            response = await self._client.post(
                _CHECK_NOW_PATH, content=body, headers=headers, timeout=self._timeout,
            )
        except httpx.TransportError as exc:
            raise FineProviderError(f"REDNET check-now unreachable: {exc}") from exc

        if response.status_code == 503:
            # See check_now_server.py's post_check_now() — REDNET's own
            # circuit breaker is open, distinguishable from any other
            # failure so this maps to the SAME PoliceGeUnavailableError the
            # local PoliceGeProvider would raise, and FineCheckService
            # already knows how to turn that into a clear, distinct
            # "service temporarily unavailable" message.
            raise PoliceGeUnavailableError("REDNET reports police.ge unavailable (circuit open)")
        if response.status_code != 200:
            raise FineProviderError(f"REDNET check-now returned HTTP {response.status_code}")

        try:
            data = response.json()
            return [payload_to_record(entry) for entry in data["records"]]
        except (ValueError, KeyError, TypeError) as exc:
            raise FineProviderError(f"REDNET check-now returned a malformed body: {exc}") from exc


class AutoFailoverCheckNowProvider(FineProvider):
    """local first, REDNET only on failure — see module docstring for why
    this needs no explicit prober/cache: the wrapped local provider's own
    circuit breaker already governs both "skip local instantly while it's
    known-down" and "try local again once its cooldown elapses"."""

    def __init__(self, local: FineProvider, rednet: FineProvider):
        self._local = local
        self._rednet = rednet

    def is_available(self) -> bool:
        # True if EITHER transport could serve a request — local being
        # momentarily down (breaker open) doesn't make Check Now itself
        # unavailable while REDNET can still answer.
        return self._local.is_available() or self._rednet.is_available()

    async def search_by_plate(self, plate: str) -> list[ParsedFineRecord]:
        try:
            return await self._local.search_by_plate(plate)
        except FineProviderError:
            logger.info(
                "Check Now: local police.ge provider failed, falling back to REDNET",
            )
            return await self._rednet.search_by_plate(plate)
