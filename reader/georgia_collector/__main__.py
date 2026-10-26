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


# Default production-submission timeout (см. задачу "fix the remaining
# issues discovered during live testing" — production-инцидент A500ET197:
# 7 genuinely new fines took ~2min server-side из-за синхронного OpenAI-
# перевода перед ответом). Вынесено в отдельную, напрямую тестируемую
# функцию (тот же приём, что и reader/public_bot/main.py::
# validate_collector_api_config() — __main__.py до этой задачи не имел ни
# одного юнит-теста вовсе, конструирование всего в async main() делало
# config-парсинг непроверяемым без реального event loop).
_DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
_DEFAULT_SUBMISSION_TIMEOUT_SECONDS = 180.0


def load_timeouts(env: dict[str, str] | None = None) -> tuple[float, float]:
    """(request_timeout, submission_timeout) — оба читаются из переданного
    env (по умолчанию os.environ): GEORGIA_COLLECTOR_REQUEST_TIMEOUT (30s по
    умолчанию) — police.ge И короткие production-эндпоинты (plates/
    bootstrap-pending/health); GEORGIA_COLLECTOR_SUBMISSION_TIMEOUT (180s по
    умолчанию) — ТОЛЬКО submit_feed_match/submit_bootstrap_result (см.
    reader/georgia_collector/production_client.py модульный докстрок)."""
    if env is None:
        env = os.environ
    request_timeout = float(
        env.get("GEORGIA_COLLECTOR_REQUEST_TIMEOUT", str(_DEFAULT_REQUEST_TIMEOUT_SECONDS))
    )
    submission_timeout = float(
        env.get("GEORGIA_COLLECTOR_SUBMISSION_TIMEOUT", str(_DEFAULT_SUBMISSION_TIMEOUT_SECONDS))
    )
    return request_timeout, submission_timeout


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
    request_timeout, submission_timeout = load_timeouts()
    tz = ZoneInfo(os.getenv("GEORGIA_COLLECTOR_TIMEZONE", "Asia/Tbilisi"))
    run_times = _parse_run_times(os.getenv("GEORGIA_COLLECTOR_RUN_TIMES"))

    feed_state_repository = GeorgiaPublicFeedStateRepository(db_path)
    seen_repository = GeorgiaPublicFeedSeenRepository(db_path)
    outbox_repository = CollectorOutboxRepository(db_path)

    police_http_client = httpx.AsyncClient(
        base_url="https://police.ge/protocol/", headers={"User-Agent": _POLICE_GE_USER_AGENT},
    )
    # client-wide default stays the SHORT request_timeout (plates/bootstrap-
    # pending/health) — submission_timeout is applied per-call, only for
    # submit_feed_match/submit_bootstrap_result (см. production_client.py).
    production_http_client = httpx.AsyncClient(base_url=production_base_url, timeout=request_timeout)

    circuit_breaker = PoliceGeCircuitBreaker()
    session = PoliceGeSession(
        police_http_client, page_url=source_url, request_timeout=request_timeout,
        circuit_breaker=circuit_breaker,
    )
    production_client = ProductionApiClient(
        production_http_client, hmac_secret=hmac_secret, submission_timeout=submission_timeout,
    )

    job = GeorgiaCollectorJob(
        session, production_client, feed_state_repository, seen_repository, outbox_repository,
        run_times=run_times, tz=tz,
    )

    logger.info(
        "Georgia collector starting: production=%s, run_times=%s, tz=%s, db=%s, "
        "request_timeout=%ss, submission_timeout=%ss",
        production_base_url, [t.strftime("%H:%M") for t in run_times], tz, db_path,
        request_timeout, submission_timeout,
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
