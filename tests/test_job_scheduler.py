"""
Тесты Scheduler — универсального исполнителя Job. Никакого знания о
FineJob/штрафах здесь нет: используются лёгкие фейковые Job.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.jobs.base import Job  # noqa: E402
from reader.jobs.scheduler import Scheduler  # noqa: E402

_NOW = datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc)


class _FakeJob(Job):
    def __init__(
        self,
        name: str,
        *,
        should_run_result: bool = True,
        should_run_error: Exception | None = None,
        run_error: Exception | None = None,
    ):
        self.name = name
        self._should_run_result = should_run_result
        self._should_run_error = should_run_error
        self._run_error = run_error
        self.should_run_calls: list[datetime] = []
        self.run_calls = 0

    async def should_run(self, now: datetime) -> bool:
        self.should_run_calls.append(now)
        if self._should_run_error is not None:
            raise self._should_run_error
        return self._should_run_result

    async def run(self) -> None:
        self.run_calls += 1
        if self._run_error is not None:
            raise self._run_error


async def test_job_runs_only_when_should_run_returns_true():
    due_job = _FakeJob("due", should_run_result=True)
    not_due_job = _FakeJob("not_due", should_run_result=False)
    scheduler = Scheduler([due_job, not_due_job])

    await scheduler.tick(_NOW)

    assert due_job.run_calls == 1
    assert not_due_job.run_calls == 0
    assert due_job.should_run_calls == [_NOW]


async def test_scheduler_runs_multiple_jobs():
    job_a = _FakeJob("a")
    job_b = _FakeJob("b")
    scheduler = Scheduler([job_a, job_b])

    await scheduler.tick(_NOW)

    assert job_a.run_calls == 1
    assert job_b.run_calls == 1


async def test_exception_in_run_does_not_stop_other_jobs():
    failing = _FakeJob("failing", run_error=RuntimeError("boom in run"))
    healthy = _FakeJob("healthy")
    scheduler = Scheduler([failing, healthy])

    await scheduler.tick(_NOW)

    assert failing.run_calls == 1  # попытка выполнения была
    assert healthy.run_calls == 1  # и не помешала второй job


async def test_exception_in_should_run_does_not_stop_other_jobs():
    failing = _FakeJob("failing", should_run_error=RuntimeError("boom in should_run"))
    healthy = _FakeJob("healthy")
    scheduler = Scheduler([failing, healthy])

    await scheduler.tick(_NOW)

    assert failing.run_calls == 0  # до run() дело не дошло
    assert healthy.run_calls == 1
