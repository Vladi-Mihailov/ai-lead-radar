"""Standalone, manually-invoked entrypoint for a background mass AUDIT
recheck (2026-10-25, "Extend REDNET ingestion for a background mass audit").

Run on the REDNET collector host (NOT on production), with the SAME
environment variables __main__.py uses (see .env.collector.example):

    python -m reader.georgia_collector.mass_audit --plates-file plates.txt

plates.txt: one already-normalized car_number per line (blank lines and
lines starting with "#" are ignored). Each plate is looked up once via
police.ge (PoliceGeSession.search_by_plate, the SAME call _run_bootstrap()
uses) and submitted to production's existing /internal/georgia/bootstrap-
result endpoint with mode="audit" (see reader/georgia_collector/
collector_job.py::GeorgiaCollectorJob.run_audit() and reader/public_bot/
collector_ingest_service.py's own module docstring for exactly what that
does and does not do: fines persist through the canonical path and new-fine
notifications still fire normally; the plate's bootstrap_status is never
read as a precondition and never changed, and no "штрафов не найдено"
message is ever sent in this mode, regardless of plate count or state).

ONE SHOT — unlike __main__.py's forever-loop, this runs run_audit() over the
given list exactly once and exits. Circuit breaker, outbox-on-failure, and
HMAC auth all behave identically to the normal scheduled path (see
run_audit()'s own docstring) — a production outage mid-run queues the
remaining submissions in the SAME local outbox, picked up by the collector's
own regular forever-loop process (__main__.py) on its next cycle's
_flush_outbox() call. This script does not need to be left running."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

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


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        print(f"Missing required environment variable: {name}", file=sys.stderr)
        sys.exit(1)
    return value


def _load_plates(path: Path) -> list[str]:
    if not path.is_file():
        print(f"Plates file not found: {path}", file=sys.stderr)
        sys.exit(1)
    plates = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        plates.append(stripped)
    return plates


async def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--plates-file", required=True, type=Path,
        help="Path to a text file with one normalized car_number per line.",
    )
    args = parser.parse_args(argv)

    load_dotenv()
    logging.basicConfig(
        level=os.getenv("GEORGIA_COLLECTOR_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    plates = _load_plates(args.plates_file)
    if not plates:
        print(f"No plates found in {args.plates_file} — nothing to do.", file=sys.stderr)
        sys.exit(1)

    production_base_url = _require_env("GEORGIA_COLLECTOR_PRODUCTION_BASE_URL")
    hmac_secret = _require_env("GEORGIA_COLLECTOR_HMAC_SECRET")

    db_path = Path(os.getenv("GEORGIA_COLLECTOR_DB_PATH", "./georgia_collector_state.db"))
    source_url = os.getenv(
        "GEORGIA_COLLECTOR_SOURCE_URL", "https://police.ge/protocol/index.php?lang=en",
    )
    request_timeout = float(os.getenv("GEORGIA_COLLECTOR_REQUEST_TIMEOUT", "30"))

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

    # run_times/tz are irrelevant here (should_run()/run() from __main__.py's
    # forever-loop are never called by this one-shot script) — defaults are
    # fine, this job instance is only ever used for its run_audit() method.
    job = GeorgiaCollectorJob(
        session, production_client, feed_state_repository, seen_repository, outbox_repository,
        run_times=[], tz=ZoneInfo("UTC"),
    )

    logger.info(
        "Georgia mass AUDIT starting: %d plate(s) from %s, production=%s",
        len(plates), args.plates_file, production_base_url,
    )
    try:
        await job.run_audit(plates)
    finally:
        await police_http_client.aclose()
        await production_http_client.aclose()
        feed_state_repository.close()
        seen_repository.close()
        outbox_repository.close()
    logger.info("Georgia mass AUDIT finished.")


if __name__ == "__main__":
    asyncio.run(main())
