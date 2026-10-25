"""HTTP client the REDNET collector uses to reach production's internal
collector API (reader/public_bot/collector_api.py) — see "Georgia remote
collector architecture (REDNET)". HMAC-signs every request via reader/fines/
collector_auth.py (shared, stdlib-only module, works unmodified on REDNET's
minimal install — see requirements-collector.txt).

Bounded in-call retry/backoff on transport failure ONLY — this client never
retries forever and never widens police.ge polling cadence when production
is down (see collector_job.py's local outbox for what happens once these
bounded retries are exhausted: the submission is queued locally and retried
on a LATER cycle, not blocked on here).

Two timeouts (см. задачу "fix the remaining issues discovered during live
testing", production-инцидент A500ET197: 7 genuinely new fines took ~2min
server-side, т.к. production синхронно делает OpenAI-перевод place/
violation_description ПЕРЕД тем, как ответить — короткий таймаут здесь
ложно засчитывал это как сбой, хотя persistence уже успешно завершился).
`client`'s own configured default (см. __main__.py, 30s) остаётся для
GET-эндпоинтов (plates/bootstrap-pending) и health — лёгкие, должны
отвечать быстро, долгий hang на НИХ — само по себе плохой знак, падать
быстро правильно. `submission_timeout` (дольше, см. __main__.py, 180s по
умолчанию) — ТОЛЬКО для submit_feed_match/submit_bootstrap_result, именно
того единственного пути, где синхронный OpenAI-перевод реально добавляет
время. Outbox/retry уже делает ложный таймаут безопасным (ничего не
теряется) — это не замена им, просто устраняет ШУМНЫЙ/ложный failure mode
на типичном объёме, а не инфинит-таймаут."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass

import httpx

from reader.fines.collector_auth import SIGNATURE_HEADER, TIMESTAMP_HEADER, sign

_PLATES_PATH = "/internal/georgia/plates"
_BOOTSTRAP_PENDING_PATH = "/internal/georgia/bootstrap/pending"
_FEED_MATCH_PATH = "/internal/georgia/feed-match"
_BOOTSTRAP_RESULT_PATH = "/internal/georgia/bootstrap-result"
_HEALTH_PATH = "/internal/georgia/health"


class ProductionApiError(Exception):
    """Raised once this client's own bounded in-call retries are exhausted —
    the caller (collector_job.py) is expected to queue the submission in its
    local outbox and retry on a later cycle, not to retry here itself."""


@dataclass
class ProductionApiClient:
    client: httpx.AsyncClient
    hmac_secret: str
    max_attempts: int = 3
    backoff_seconds: float = 5.0
    # ТОЛЬКО для submit_feed_match/submit_bootstrap_result — см. модульный
    # докстрок. None передавался бы как есть в httpx и означал бы "без
    # таймаута вовсе", поэтому __main__.py всегда передаёт явное число
    # (default 180.0 там же) — здесь дефолт только на случай прямого
    # конструирования в тестах без этого аргумента.
    submission_timeout: float = 180.0

    async def _request(
        self, method: str, path: str, *, json_body: dict | None = None,
        timeout: float | None = None,
    ) -> dict:
        body = json.dumps(json_body).encode("utf-8") if json_body is not None else b""

        last_exc: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            timestamp = str(time.time())
            signature = sign(
                self.hmac_secret, method=method, path=path, timestamp=timestamp, body=body,
            )
            headers = {TIMESTAMP_HEADER: timestamp, SIGNATURE_HEADER: signature}
            if json_body is not None:
                headers["Content-Type"] = "application/json"

            try:
                request_kwargs = {"content": body, "headers": headers}
                # timeout=None здесь -> не передаём kwarg вовсе -> httpx
                # использует timeout уже настроенный на самом self.client
                # (см. __main__.py, короткий 30s-дефолт) — явное число
                # (submission_timeout, см. submit_feed_match/
                # submit_bootstrap_result ниже) переопределяет ТОЛЬКО этот
                # один вызов, не трогая client-wide default.
                if timeout is not None:
                    request_kwargs["timeout"] = timeout
                response = await self.client.request(method, path, **request_kwargs)
                response.raise_for_status()
                return response.json()
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                if attempt < self.max_attempts:
                    await asyncio.sleep(self.backoff_seconds * attempt)

        raise ProductionApiError(
            f"{method} {path} failed after {self.max_attempts} attempts"
        ) from last_exc

    async def get_active_plates(self) -> list[str]:
        data = await self._request("GET", _PLATES_PATH)
        return list(data["plates"])

    async def get_pending_bootstrap_plates(self) -> list[str]:
        data = await self._request("GET", _BOOTSTRAP_PENDING_PATH)
        return list(data["plates"])

    async def submit_feed_match(self, payload: dict) -> dict:
        return await self._request(
            "POST", _FEED_MATCH_PATH, json_body=payload, timeout=self.submission_timeout,
        )

    async def submit_bootstrap_result(self, payload: dict) -> dict:
        return await self._request(
            "POST", _BOOTSTRAP_RESULT_PATH, json_body=payload, timeout=self.submission_timeout,
        )

    async def report_health(self, payload: dict) -> dict:
        return await self._request("POST", _HEALTH_PATH, json_body=payload)
