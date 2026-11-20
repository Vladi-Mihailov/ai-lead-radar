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
from reader.georgia_collector.check_now_server import run_check_now_server_forever
from reader.georgia_collector.collector_job import (
    FAST_LOOKAHEAD_PAGES,
    GeorgiaCollectorJob,
)
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


# Инбаунд check-now сервер (см. задачу "Fix the Georgia public bot Check Now
# flow") — ОТДЕЛЬНЫЙ порт от production_base_url (8765, исходящий клиент к
# production), чтобы не завязывать два независимых направления (REDNET->
# production push/poll, production->REDNET check-now pull) на один и тот же
# адрес/порт. 127.0.0.1 по умолчанию — ТОТ ЖЕ принцип, что и у production
# settings.fine_monitor.collector_api_host: не предназначен быть напрямую
# доступным из интернета, production достаёт его через WireGuard
# (production.env задаёт здесь REDNET'овский wg0-адрес, см.
# docs/georgia-remote-collector.md).
_DEFAULT_CHECKNOW_HOST = "127.0.0.1"
_DEFAULT_CHECKNOW_PORT = 8766

# Лимиты страниц общего списка за цикл (см. collector_job.py, DEEP-горизонт).
# 300 страниц = 9 000 строк ≈ полтора дня публикаций (~5–6,3 тыс. в сутки):
# самый загруженный цикл (21:00 Тбилиси, ~3–3,5 тыс. новых строк ≈ 100–117
# страниц) доходит до уже просмотренных строк, а первый запуск читает только
# сегодняшние публикации (≤ ~210 страниц). Прежние неявные 30/100 были
# рассчитаны на ~670 публикаций в сутки.
_DEFAULT_MAX_PAGES_PER_CYCLE = 300
_DEFAULT_FIRST_RUN_MAX_PAGES = 300


def load_page_limits(env: dict[str, str] | None = None) -> tuple[int, int]:
    """(max_pages_per_cycle, first_run_max_pages) из
    GEORGIA_COLLECTOR_MAX_PAGES_PER_CYCLE / GEORGIA_COLLECTOR_FIRST_RUN_MAX_PAGES
    (тот же приём, что и load_timeouts() выше). Не целое число или < 1 —
    ValueError при старте, а не молчаливый откат к умолчанию."""
    if env is None:
        env = os.environ
    max_pages = int(env.get("GEORGIA_COLLECTOR_MAX_PAGES_PER_CYCLE", str(_DEFAULT_MAX_PAGES_PER_CYCLE)))
    first_run = int(env.get("GEORGIA_COLLECTOR_FIRST_RUN_MAX_PAGES", str(_DEFAULT_FIRST_RUN_MAX_PAGES)))
    if max_pages < 1 or first_run < 1:
        raise ValueError("GEORGIA_COLLECTOR_*_MAX_PAGES must be >= 1")
    return max_pages, first_run


def load_fast_lookahead_pages(env: dict[str, str] | None = None) -> int:
    """GEORGIA_COLLECTOR_FAST_LOOKAHEAD_PAGES — сколько ещё тихих страниц FAST
    читает после первой страницы без новых строк, прежде чем остановиться
    (по умолчанию collector_job.FAST_LOOKAHEAD_PAGES, там же — данные за это
    значение; тот же K — у look-ahead DEEP-горизонта). Тот же приём, что и
    load_page_limits(): не целое число, < 1 или не меньше любого из лимитов
    страниц того же env (GEORGIA_COLLECTOR_MAX_PAGES_PER_CYCLE,
    GEORGIA_COLLECTOR_FIRST_RUN_MAX_PAGES: с таким K каждый цикл упирался бы
    в лимит) — ValueError при старте, а не молчаливый откат к умолчанию."""
    if env is None:
        env = os.environ
    pages = int(env.get("GEORGIA_COLLECTOR_FAST_LOOKAHEAD_PAGES", str(FAST_LOOKAHEAD_PAGES)))
    max_pages_per_cycle, first_run_max_pages = load_page_limits(env)
    if not 1 <= pages < min(max_pages_per_cycle, first_run_max_pages):
        raise ValueError(
            "GEORGIA_COLLECTOR_FAST_LOOKAHEAD_PAGES must be >= 1 and below "
            f"GEORGIA_COLLECTOR_MAX_PAGES_PER_CYCLE ({max_pages_per_cycle}) and "
            f"GEORGIA_COLLECTOR_FIRST_RUN_MAX_PAGES ({first_run_max_pages})"
        )
    return pages


def load_checknow_server_config(env: dict[str, str] | None = None) -> tuple[str, int]:
    """(host, port) для run_check_now_server_forever() — та же тестируемая-
    без-event-loop функция, что и load_timeouts() выше (см. её докстрок про
    __main__.py's отсутствие юнит-тестов до появления этого приёма)."""
    if env is None:
        env = os.environ
    host = env.get("GEORGIA_COLLECTOR_CHECKNOW_HOST", _DEFAULT_CHECKNOW_HOST)
    port = int(env.get("GEORGIA_COLLECTOR_CHECKNOW_PORT", str(_DEFAULT_CHECKNOW_PORT)))
    return host, port


async def _run_concurrently(coroutines: list) -> None:
    """Как asyncio.gather, но при ошибке в одной корутине отменяет
    остальные — ТОТ ЖЕ приём, что и reader/public_bot/main.py::
    _run_concurrently (не импортируется оттуда: отдельный, самостоятельный
    процесс/entrypoint, а не библиотека) — крах polling-loop'а или
    check-now-сервера не должен молча оставить другой висеть без логов."""
    tasks = [asyncio.create_task(coro) for coro in coroutines]
    try:
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    for task in done:
        exc = task.exception()
        if exc is not None:
            raise exc


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
    checknow_host, checknow_port = load_checknow_server_config()
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

    max_pages_per_cycle, first_run_max_pages = load_page_limits()
    fast_lookahead_pages = load_fast_lookahead_pages()
    job = GeorgiaCollectorJob(
        session, production_client, feed_state_repository, seen_repository, outbox_repository,
        run_times=run_times, tz=tz,
        max_pages_per_cycle=max_pages_per_cycle, first_run_max_pages=first_run_max_pages,
        fast_lookahead_pages=fast_lookahead_pages,
    )

    logger.info(
        "Georgia collector starting: production=%s, run_times=%s, tz=%s, db=%s, "
        "request_timeout=%ss, submission_timeout=%ss, checknow=%s:%d, max_pages=%d/%d, "
        "fast_lookahead_pages=%d",
        production_base_url, [t.strftime("%H:%M") for t in run_times], tz, db_path,
        request_timeout, submission_timeout, checknow_host, checknow_port,
        max_pages_per_cycle, first_run_max_pages, fast_lookahead_pages,
    )

    # Та же SAME session (= тот же circuit breaker), что и у polling job'а
    # выше — ОДИН breaker на процесс покрывает и запланированные, и
    # on-demand (Check Now) обращения к police.ge (см. check_now_server.py
    # модульный докстрок). Крах одной корутины не должен молча останавливать
    # другую незамеченной — ТОТ ЖЕ приём, что и в reader/public_bot/main.py
    # (_run_concurrently выше).
    try:
        await _run_concurrently([
            _run_forever(job),
            run_check_now_server_forever(
                session, hmac_secret=hmac_secret, host=checknow_host, port=checknow_port,
            ),
        ])
    finally:
        await police_http_client.aclose()
        await production_http_client.aclose()
        feed_state_repository.close()
        seen_repository.close()
        outbox_repository.close()


if __name__ == "__main__":
    asyncio.run(main())
