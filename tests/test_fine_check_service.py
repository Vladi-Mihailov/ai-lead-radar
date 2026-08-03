"""
Тесты FineCheckService — бизнес-логика проверки одной задачи.
FineProvider подменяется лёгким фейком (без сети/HTTP), Repository —
настоящие (SQLite поверх tmp_path), чтобы честно проверить дедупликацию,
UNIQUE constraint и персистентность через реальную БД, как и в остальных
тестах проекта.
"""

import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.fines.check_service import FineCheckService  # noqa: E402
from reader.fines.detected_fine_repository import DetectedFineRepository  # noqa: E402
from reader.fines.models import ParsedFineRecord  # noqa: E402
from reader.fines.provider import FineProvider, FineProviderError  # noqa: E402
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402

_CHAT_ID = -100999
_USER_ID = 111


class _FakeProvider(FineProvider):
    def __init__(self, records: list[ParsedFineRecord] | None = None, error: Exception | None = None):
        self._records = records or []
        self._error = error
        self.requested_plates: list[str] = []

    async def search_by_plate(self, plate: str) -> list[ParsedFineRecord]:
        self.requested_plates.append(plate)
        if self._error is not None:
            raise self._error
        return self._records


class _AlwaysMissingDetectedFineRepository(DetectedFineRepository):
    """Обёртка для симуляции гонки: get_by_fingerprint() всегда говорит
    "не найдено", хотя запись с таким fingerprint для этой задачи уже
    реально существует в БД — ровно то, что происходило бы, если бы другой
    процесс успел вставить строку между чтением и записью."""

    def get_by_fingerprint(self, monitoring_task_id: int, fingerprint: str):
        return None


def _record(
    *,
    car_number="B957MA09",
    external_fine_id="AB123456",
    fingerprint="fp-1",
    penalty_date=date(2026, 8, 6),
    due_date=date(2026, 8, 20),
    delivered_status="Не вручено",
    raw_data=None,
) -> ParsedFineRecord:
    return ParsedFineRecord(
        car_number=car_number,
        external_fine_id=external_fine_id,
        penalty_date=penalty_date,
        due_date=due_date,
        delivered_status=delivered_status,
        fingerprint=fingerprint,
        raw_data=raw_data or {"protocolNo": external_fine_id},
    )


def _make_task(task_repo: FineMonitoringTaskRepository, *, car_number="B957MA09", label=None):
    return task_repo.create(
        car_number=car_number,
        label=label,
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 31),
        telegram_chat_id=_CHAT_ID,
        created_by_user_id=_USER_ID,
    )


async def test_first_call_creates_new_fines(tmp_path):
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    fine_repo = DetectedFineRepository(db_path)
    try:
        task = _make_task(task_repo, label="Toyota Camry")
        provider = _FakeProvider(records=[_record()])
        service = FineCheckService(provider, task_repo, fine_repo)

        result = await service.check_task(task)

        assert result.status == "ok"
        assert result.error_message is None
        assert len(result.new_fines) == 1
        assert result.total_fines_found == 1
        assert result.duration_ms >= 0

        event = result.new_fines[0]
        assert event.task_id == task.id
        assert event.car_number == "B957MA09"
        assert event.label == "Toyota Camry"
        assert event.external_fine_id == "AB123456"
        assert event.penalty_date == date(2026, 8, 6)
        assert event.due_date == date(2026, 8, 20)
        assert event.delivered_status == "Не вручено"
    finally:
        task_repo.close()
        fine_repo.close()


async def test_repeated_call_does_not_create_duplicates(tmp_path):
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    fine_repo = DetectedFineRepository(db_path)
    try:
        task = _make_task(task_repo)
        provider = _FakeProvider(records=[_record()])
        service = FineCheckService(provider, task_repo, fine_repo)

        first = await service.check_task(task)
        second = await service.check_task(task)

        assert len(first.new_fines) == 1
        assert len(second.new_fines) == 0
    finally:
        task_repo.close()
        fine_repo.close()


async def test_existing_fine_updates_last_seen_at(tmp_path):
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    fine_repo = DetectedFineRepository(db_path)
    try:
        task = _make_task(task_repo)
        provider = _FakeProvider(records=[_record(fingerprint="fp-seen")])
        service = FineCheckService(provider, task_repo, fine_repo)

        await service.check_task(task)
        first_seen = fine_repo.get_by_fingerprint(task.id, "fp-seen")

        await service.check_task(task)
        second_seen = fine_repo.get_by_fingerprint(task.id, "fp-seen")

        assert second_seen.last_seen_at >= first_seen.last_seen_at
        assert second_seen.first_detected_at == first_seen.first_detected_at
    finally:
        task_repo.close()
        fine_repo.close()


async def test_empty_provider_response_yields_no_new_fines(tmp_path):
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    fine_repo = DetectedFineRepository(db_path)
    try:
        task = _make_task(task_repo)
        provider = _FakeProvider(records=[])
        service = FineCheckService(provider, task_repo, fine_repo)

        result = await service.check_task(task)

        assert result.status == "ok"
        assert result.new_fines == []
    finally:
        task_repo.close()
        fine_repo.close()


async def test_multiple_new_fines_are_all_reported(tmp_path):
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    fine_repo = DetectedFineRepository(db_path)
    try:
        task = _make_task(task_repo)
        provider = _FakeProvider(
            records=[
                _record(fingerprint="fp-1", external_fine_id="A1"),
                _record(fingerprint="fp-2", external_fine_id="A2"),
                _record(fingerprint="fp-3", external_fine_id="A3"),
            ]
        )
        service = FineCheckService(provider, task_repo, fine_repo)

        result = await service.check_task(task)

        assert len(result.new_fines) == 3
        assert {e.external_fine_id for e in result.new_fines} == {"A1", "A2", "A3"}
    finally:
        task_repo.close()
        fine_repo.close()


async def test_mix_of_new_and_known_fines(tmp_path):
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    fine_repo = DetectedFineRepository(db_path)
    try:
        task = _make_task(task_repo)

        # Первый проход: fp-1 и fp-2 становятся известными.
        first_provider = _FakeProvider(
            records=[
                _record(fingerprint="fp-1", external_fine_id="A1"),
                _record(fingerprint="fp-2", external_fine_id="A2"),
            ]
        )
        await FineCheckService(first_provider, task_repo, fine_repo).check_task(task)

        # Второй проход: fp-1 уже известен, fp-2 уже известен, fp-3 — новый.
        second_provider = _FakeProvider(
            records=[
                _record(fingerprint="fp-1", external_fine_id="A1"),
                _record(fingerprint="fp-2", external_fine_id="A2"),
                _record(fingerprint="fp-3", external_fine_id="A3"),
            ]
        )
        result = await FineCheckService(second_provider, task_repo, fine_repo).check_task(task)

        assert len(result.new_fines) == 1
        assert result.new_fines[0].external_fine_id == "A3"
        # total_fines_found — все найденные провайдером записи, а не только новые.
        assert result.total_fines_found == 3
    finally:
        task_repo.close()
        fine_repo.close()


async def test_fine_provider_error_sets_error_status_and_keeps_existing_data(tmp_path):
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    fine_repo = DetectedFineRepository(db_path)
    try:
        task = _make_task(task_repo)

        # Сначала успешная проверка создаёт штраф.
        ok_provider = _FakeProvider(records=[_record(fingerprint="fp-1")])
        await FineCheckService(ok_provider, task_repo, fine_repo).check_task(task)
        before = fine_repo.get_by_fingerprint(task.id, "fp-1")
        assert before is not None

        # Затем сайт недоступен.
        failing_provider = _FakeProvider(error=FineProviderError("сайт недоступен"))
        result = await FineCheckService(failing_provider, task_repo, fine_repo).check_task(task)

        assert result.status == "error"
        assert result.new_fines == []
        assert result.error_message == "сайт недоступен"
        assert result.total_fines_found == 0
        assert result.duration_ms >= 0

        # Ошибка источника — не "штрафов нет": ранее сохранённая запись цела.
        after = fine_repo.get_by_fingerprint(task.id, "fp-1")
        assert after == before

        updated_task = task_repo.get(task.id)
        assert updated_task.last_check_status == "error"
        assert updated_task.last_error == "сайт недоступен"
    finally:
        task_repo.close()
        fine_repo.close()


async def test_check_result_updates_task_last_checked_fields(tmp_path):
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    fine_repo = DetectedFineRepository(db_path)
    try:
        task = _make_task(task_repo)
        assert task.last_checked_at is None

        provider = _FakeProvider(records=[_record()])
        await FineCheckService(provider, task_repo, fine_repo).check_task(task)

        updated = task_repo.get(task.id)
        assert updated.last_checked_at is not None
        assert updated.last_check_status == "ok"
        assert updated.last_error is None
    finally:
        task_repo.close()
        fine_repo.close()


async def test_concurrent_unique_conflict_is_treated_as_existing_not_new(tmp_path):
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    real_fine_repo = DetectedFineRepository(db_path)
    try:
        task = _make_task(task_repo)

        # "Другой процесс" уже вставил запись с этим fingerprint для этой задачи.
        real_fine_repo.create(
            monitoring_task_id=task.id,
            car_number="B957MA09",
            external_fine_id="AB123456",
            fingerprint="fp-race",
            penalty_date=date(2026, 8, 6),
            due_date=date(2026, 8, 20),
            delivered_status="Не вручено",
            raw_data="{}",
        )

        # Репозиторий сервиса "не видит" эту запись через get_by_fingerprint
        # (имитация окна гонки) — INSERT неизбежно упрётся в UNIQUE constraint.
        racy_fine_repo = _AlwaysMissingDetectedFineRepository(db_path)
        provider = _FakeProvider(records=[_record(fingerprint="fp-race")])
        service = FineCheckService(provider, task_repo, racy_fine_repo)

        result = await service.check_task(task)

        assert result.status == "ok"
        assert result.new_fines == []  # не должно попасть в "новые"

        # И падения всей проверки не произошло.
        stored = real_fine_repo.get_by_fingerprint(task.id, "fp-race")
        assert stored is not None
    finally:
        task_repo.close()
        real_fine_repo.close()


async def test_fine_without_stable_fields_is_still_handled(tmp_path):
    # parser.compute_fingerprint поддерживает случай, когда external_fine_id/
    # violation_date/amount отсутствуют — вырожденный, но детерминированный
    # fingerprint. FineCheckService должен обработать такую запись как любую
    # другую, не падая и не считая её особым случаем.
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    fine_repo = DetectedFineRepository(db_path)
    try:
        task = _make_task(task_repo)
        provider = _FakeProvider(
            records=[
                _record(
                    external_fine_id=None,
                    fingerprint="fp-empty-stable-fields",
                    penalty_date=None,
                    due_date=None,
                    delivered_status="Не вручено",
                )
            ]
        )
        service = FineCheckService(provider, task_repo, fine_repo)

        first = await service.check_task(task)
        second = await service.check_task(task)

        assert len(first.new_fines) == 1
        assert first.new_fines[0].external_fine_id is None
        assert len(second.new_fines) == 0
    finally:
        task_repo.close()
        fine_repo.close()
