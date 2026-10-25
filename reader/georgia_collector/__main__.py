"""Entrypoint for the REDNET Georgia collector — run with:

    python -m reader.georgia_collector

See docs/georgia-remote-collector.md for deployment. Single long-running
process + systemd (deploy/georgia-collector.service), matching this
project's existing convention for background services — not a systemd
timer, so the SAME exact-wall-clock should_run()/run_times logic used
elsewhere in this project (reader/jobs/fine_job.py, reader/jobs/
georgia_public_feed_job.py) applies here too, independently reimplemented
in collector_job.py (see its "no reader/jobs import" requirement).

All configuration is environment variables ONLY (see .env.collector.example)
— this process has no config/config.yaml, no Telegram credentials, and
cannot read production's own .env (different machine entirely)."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from datetime import time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

from reader.fines.police_ge_circuit_breaker import PoliceGeCircuitBreaker
from reader.fines.police_ge_session import PoliceGeSession
from reader.fines.public_feed_seen_repository import GeorgiaPublicFeedSeenRepository
from reader.fines.public_feed_state_repository import GeorgiaPublicFeedStateRepository
from reader.georgia_collector.collector_job import GeorgiaCollectorJob
from reader.georgia_collector.outbox_repository import CollectorOutboxRepository
from reader.georgia_collector.production_client import ProductionApiClient

logger = logging.getLogger(__name__)

_POLICE_GE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
_DEFAULT_RUN_TIMES = [dt_time(9, 0), dt_time(15, 0), dt_time(21, 0), dt_time(23, 55)]
_SCHEDULER_POLL_INTERVAL_SECONDS = 30.0


def _parse_run_times(raw: str | None) -> list[dt_time]:
    if not raw:
        return list(_DEFAULT_RUN_TIMES)
    times = []
    for part in raw.split(","):
        hour_str, minute_str = part.strip().split(":")
        times.append(dt_time(int(hour_str), int(minute_str)))
    return times


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        print(f"Missing required environment variable: {name}", file=sys.stderr)
        sys.exit(1)
    return value


async def _run_forever(job: GeorgiaCollectorJob) -> None:
    while True:
        now = datetime.now(timezone.utc)
        try:
            if await job.should_run(now):
                await job.run(now)
        except Exception:
            logger.exception("GeorgiaCollectorJob: unhandled error in run()")
        await asyncio.sleep(_SCHEDULER_POLL_INTERVAL_SECONDS)


async def main() -> None:
    load_dotenv()
    logging.basicConfig(
        level=os.getenv("GEORGIA_COLLECTOR_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    production_base_url = _require_env("GEORGIA_COLLECTOR_PRODUCTION_BASE_URL")
    hmac_secret = _require_env("GEORGIA_COLLECTOR_HMAC_SECRET")

    db_path = Path(os.getenv("GEORGIA_COLLECTOR_DB_PATH", "./georgia_collector_state.db"))
    source_url = os.getenv(
        "GEORGIA_COLLECTOR_SOURCE_URL", "https://police.ge/protocol/index.php?lang=en",
    )
    request_timeout = float(os.getenv("GEORGIA_COLLECTOR_REQUEST_TIMEOUT", "30"))
    tz = ZoneInfo(os.getenv("GEORGIA_COLLECTOR_TIMEZONE", "Asia/Tbilisi"))
    run_times = _parse_run_times(os.getenv("GEORGIA_COLLECTOR_RUN_TIMES"))

    feed_state_repository = GeorgiaPublicFeedStateRepository(db_path)
    seen_repository = GeorgiaPublicFeedSeenRepository(db_path)
    outbox_repository = CollectorOutboxRepository(db_path)

    police_http_client = httpx.AsyncClient(
        base_url="https://police.ge/protocol/", headers={"User-Agent": _POLICE_GE_USER_AGENT},
    )
    production_http_client = httpx.AsyncClient(base_url=production_base_url, timeout=request_timeout)

    circuit_breaker = PoliceGeCircuitBreaker()
    session = PoliceGeSession(
        police_http_client, page_url=source_url, request_timeout=request_timeout,
        circuit_breaker=circuit_breaker,
    )
    production_client = ProductionApiClient(production_http_client, hmac_secret=hmac_secret)

    job = GeorgiaCollectorJob(
        session, production_client, feed_state_repository, seen_repository, outbox_repository,
        run_times=run_times, tz=tz,
    )

    logger.info(
        "Georgia collector starting: production=%s, run_times=%s, tz=%s, db=%s",
        production_base_url, [t.strftime("%H:%M") for t in run_times], tz, db_path,
    )

    try:
        await _run_forever(job)
    finally:
        await police_http_client.aclose()
        await production_http_client.aclose()
        feed_state_repository.close()
        seen_repository.close()
        outbox_repository.close()


if __name__ == "__main__":
    asyncio.run(main())
