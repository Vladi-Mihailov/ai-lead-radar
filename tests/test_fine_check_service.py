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
from reader.fines.translation import FineTranslationError, TranslatedFineText  # noqa: E402

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
    violation_date=None,
    amount=None,
    place=None,
    violation_description=None,
) -> ParsedFineRecord:
    return ParsedFineRecord(
        car_number=car_number,
        external_fine_id=external_fine_id,
        penalty_date=penalty_date,
        due_date=due_date,
        delivered_status=delivered_status,
        fingerprint=fingerprint,
        raw_data=raw_data or {"protocolNo": external_fine_id},
        violation_date=violation_date,
        amount=amount,
        place=place,
        violation_description=violation_description,
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


# ---- production incident regression: legacy detected_fines (созданные ДО
# появления violation_date/amount/place/violation_description) должны
# самостоятельно "самолечиться" при обычной повторной проверке, а не
# оставаться с этими полями NULL навсегда — см. диагностику реального
# случая (car E911EE95, штраф კვ000465186): protocolAmount/protocolPlace/
# protocolLawDescription/violationDate police.ge отдавал каждый раз, но
# check_task() при совпадении fingerprint просто вызывал mark_seen(id) без
# аргументов и никогда не обновлял уже существующую строку ----


async def test_existing_legacy_row_gets_extended_fields_backfilled_on_recheck(tmp_path):
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    fine_repo = DetectedFineRepository(db_path)
    try:
        task = _make_task(task_repo)

        # Шаг 1 — legacy-строка: создана ДО появления расширенных полей
        # (ровно как реальные produciton-строки, созданные до деплоя
        # 61db565) — violation_date/amount/place/violation_description не
        # переданы вовсе, то есть NULL.
        legacy = fine_repo.create(
            monitoring_task_id=task.id,
            car_number="E911EE95",
            external_fine_id="X000465186",
            fingerprint="fp-legacy-real-fine",
            penalty_date=date(2026, 8, 7),
            due_date=date(2026, 10, 6),
            delivered_status="Не вручено",
            raw_data="{}",
        )
        assert legacy.violation_date is None
        assert legacy.amount is None
        assert legacy.place is None
        assert legacy.violation_description is None

        # Шаг 2 — обычная повторная проверка того же штрафа (тот же
        # fingerprint — police.ge возвращает тот же протокол с ПОЛНЫМИ
        # данными, как и в реальном production raw_data).
        provider = _FakeProvider(
            records=[
                _record(
                    car_number="E911EE95",
                    external_fine_id="X000465186",
                    fingerprint="fp-legacy-real-fine",
                    penalty_date=date(2026, 8, 7),
                    due_date=date(2026, 10, 6),
                    delivered_status="Не вручено",
                    violation_date=date(2026, 8, 7),
                    amount=100.0,
                    place="სამტრედია-გრიგოლეთი 26კმ. ლანჩხუთი",
                    violation_description="ასკ 125-ე მუხლის პირველის პრიმა ნაწილი",
                )
            ]
        )
        service = FineCheckService(provider, task_repo, fine_repo)

        result = await service.check_task(task)

        # Уже известный штраф — НЕ новое обнаружение (не повторная рассылка).
        assert result.new_fines == []

        backfilled = fine_repo.get_by_fingerprint(task.id, "fp-legacy-real-fine")
        assert backfilled.id == legacy.id  # та же строка, не дубликат
        assert backfilled.violation_date == date(2026, 8, 7)
        assert backfilled.amount == 100.0
        assert backfilled.place == "სამტრედია-გრიგოლეთი 26კმ. ლანჩხუთი"
        assert backfilled.violation_description == "ასკ 125-ე მუხლის პირველის პრიმა ნაწილი"

        all_rows = fine_repo.list_by_car_number("E911EE95")
        assert len(all_rows) == 1  # ни одной лишней строки не создано
    finally:
        task_repo.close()
        fine_repo.close()


async def test_backfill_never_overwrites_existing_value_with_null(tmp_path):
    """COALESCE — если police.ge на КАКОЙ-ТО из проверок вдруг не отдаст
    protocolPlace/protocolLawDescription для уже известного штрафа, уже
    сохранённое значение не должно затираться NULL."""
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    fine_repo = DetectedFineRepository(db_path)
    try:
        task = _make_task(task_repo)
        already_backfilled = fine_repo.create(
            monitoring_task_id=task.id,
            car_number="B957MA09",
            external_fine_id="AB123456",
            fingerprint="fp-already-full",
            penalty_date=date(2026, 8, 6),
            due_date=date(2026, 8, 20),
            delivered_status="Не вручено",
            raw_data="{}",
            violation_date=date(2026, 8, 5),
            amount=100.0,
            place="Test place",
            violation_description="Test violation",
        )

        provider = _FakeProvider(
            records=[_record(fingerprint="fp-already-full")]  # place/violation_description = None
        )
        service = FineCheckService(provider, task_repo, fine_repo)

        await service.check_task(task)

        unchanged = fine_repo.get_by_fingerprint(task.id, "fp-already-full")
        assert unchanged.id == already_backfilled.id
        assert unchanged.place == "Test place"
        assert unchanged.violation_description == "Test violation"
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
        # current_fines — ВСЕ штрафы этой проверки (новые И уже известные,
        # см. задачу про UX manual "Проверить сейчас") — genuinely new
        # штраф по-прежнему корректно попадает в normal new-fine pipeline
        # (new_fines) РОВНО один раз, current_fines его не заменяет и не
        # дублирует.
        assert len(result.current_fines) == 3
        assert {e.external_fine_id for e in result.current_fines} == {"A1", "A2", "A3"}
    finally:
        task_repo.close()
        fine_repo.close()


# ---- production UX задача: manual "🔎 Проверить сейчас" должен показывать
# ТЕКУЩЕЕ состояние машины (current_fines), а фоновый мониторинг —
# по-прежнему ТОЛЬКО новые штрафы (new_fines). Оба списка строит один и
# тот же check_task(), без отдельной реализации/костыля. ----


async def test_manual_check_shows_all_three_existing_fines(tmp_path):
    """"Если police.ge сейчас возвращает 4 штрафа → показать все 4" —
    здесь 3, но тот же принцип: все уже известные штрафы попадают в
    current_fines, не только новые (которых в этом проходе ноль)."""
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    fine_repo = DetectedFineRepository(db_path)
    try:
        task = _make_task(task_repo)
        records = [
            _record(fingerprint="fp-1", external_fine_id="A1"),
            _record(fingerprint="fp-2", external_fine_id="A2"),
            _record(fingerprint="fp-3", external_fine_id="A3"),
        ]

        await FineCheckService(_FakeProvider(records=records), task_repo, fine_repo).check_task(task)
        # Повторная (manual) проверка — те же 3 штрафа, уже все известны.
        result = await FineCheckService(_FakeProvider(records=records), task_repo, fine_repo).check_task(task)

        assert result.new_fines == []  # ни одного НОВОГО обнаружения
        assert len(result.current_fines) == 3  # но клиент видит все 3
        assert {e.external_fine_id for e in result.current_fines} == {"A1", "A2", "A3"}
    finally:
        task_repo.close()
        fine_repo.close()


async def test_manual_check_of_existing_fines_creates_no_duplicate_rows(tmp_path):
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    fine_repo = DetectedFineRepository(db_path)
    try:
        task = _make_task(task_repo)
        records = [_record(fingerprint="fp-1", external_fine_id="A1")]
        service = FineCheckService(_FakeProvider(records=records), task_repo, fine_repo)

        await service.check_task(task)
        await service.check_task(task)  # "manual" повтор
        await service.check_task(task)

        assert len(fine_repo.list_by_car_number("B957MA09")) == 1
    finally:
        task_repo.close()
        fine_repo.close()


async def test_manual_check_of_existing_fine_does_not_reopen_operator_notification(tmp_path):
    """Ключевое требование задачи: показ штрафа в ответ на manual check
    НЕ должен считаться новым detection event и не должен приводить к
    повторной отправке оператору/trusted — notification_sent_at уже
    существующей строки не должен сбрасываться в NULL повторной
    проверкой (list_pending_notifications() — единственный источник
    решения "кого ещё нужно уведомить", см. FineNotificationCoordinator)."""
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    fine_repo = DetectedFineRepository(db_path)
    try:
        task = _make_task(task_repo)
        records = [_record(fingerprint="fp-1", external_fine_id="A1")]
        service = FineCheckService(_FakeProvider(records=records), task_repo, fine_repo)

        await service.check_task(task)  # создаёт штраф, notification_sent_at = NULL
        [fine] = fine_repo.list_by_car_number("B957MA09")
        fine_repo.mark_notification_sent(fine.id)  # оператор уже уведомлён (как в реальном flush job)
        assert fine_repo.list_pending_notifications() == []

        # "Manual" повторная проверка того же штрафа.
        result = await service.check_task(task)

        assert result.new_fines == []
        assert len(result.current_fines) == 1  # клиент всё равно видит штраф
        assert fine_repo.list_pending_notifications() == []  # оператор НЕ уведомляется повторно
    finally:
        task_repo.close()
        fine_repo.close()


async def test_manual_check_extended_fields_backfill_reflected_in_current_fines(tmp_path):
    """Продолжение production-инцидента (см. mark_seen backfill) —
    current_fines для manual check должен показывать УЖЕ backfilled
    (актуальные) значения, а не устаревшие NULL, даже для legacy-строки,
    созданной до появления этих колонок."""
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    fine_repo = DetectedFineRepository(db_path)
    try:
        task = _make_task(task_repo)
        legacy = fine_repo.create(
            monitoring_task_id=task.id, car_number="B957MA09",
            external_fine_id="A1", fingerprint="fp-legacy",
            penalty_date=date(2026, 8, 6), due_date=date(2026, 8, 20),
            delivered_status="Не вручено", raw_data="{}",
        )
        assert legacy.violation_date is None

        service = FineCheckService(
            _FakeProvider(records=[
                _record(
                    fingerprint="fp-legacy", external_fine_id="A1",
                    violation_date=date(2026, 8, 5), amount=100.0,
                    place="Test place", violation_description="Test violation",
                )
            ]),
            task_repo, fine_repo,
        )

        result = await service.check_task(task)

        assert result.new_fines == []
        [current] = result.current_fines
        assert current.violation_date == date(2026, 8, 5)
        assert current.amount == 100.0
        assert current.place == "Test place"
        assert current.violation_description == "Test violation"
    finally:
        task_repo.close()
        fine_repo.close()


async def test_scheduled_check_semantics_unaffected_only_new_fines_reported(tmp_path):
    """Явное требование задачи: background monitoring (то, что реально
    потребляет FineJob/NotificationFlushJob) должен продолжать видеть
    ТОЛЬКО новые штрафы — current_fines существует ИСКЛЮЧИТЕЛЬНО для
    manual check и не подменяет/не расширяет new_fines."""
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    fine_repo = DetectedFineRepository(db_path)
    try:
        task = _make_task(task_repo)
        records = [
            _record(fingerprint="fp-1", external_fine_id="A1"),
            _record(fingerprint="fp-2", external_fine_id="A2"),
        ]
        service = FineCheckService(_FakeProvider(records=records), task_repo, fine_repo)
        await service.check_task(task)  # оба становятся known, notification_sent_at ещё NULL
        # Симулируем реальный flush job (NotificationFlushJob), который
        # выставляет notification_sent_at для реально доставленных штрафов —
        # без этого шага pending и так был бы пуст только потому, что
        # никто их ни разу не "отправлял", а не потому, что check_task()
        # корректно себя ведёт при повторной проверке.
        for fine in fine_repo.list_by_car_number("B957MA09"):
            fine_repo.mark_notification_sent(fine.id)
        assert fine_repo.list_pending_notifications() == []

        # "Фоновая" (scheduled) проверка — те же штрафы, ничего нового.
        scheduled_result = await service.check_task(task)

        assert scheduled_result.new_fines == []
        # Фоновый пайплайн (FineJob) не читает current_fines вовсе — сам
        # факт его присутствия не должен создавать новых уведомлений:
        # единственный источник истины для оператора — notification_sent_at
        # (список ожидающих отправки штрафов), который здесь по-прежнему пуст.
        assert fine_repo.list_pending_notifications() == []
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


# ---- перевод грузинского place/violation_description на русский (см.
# задачу про перевод текста штрафов) — единственная точка (check_task()),
# переиспользуемая background/manual check/Add Car flow одинаково.
# translate() здесь ВСЕГДА mocked (Protocol FineTranslatorLike) — ни
# один тест не обращается к настоящему OpenAI API. ----

_GEORGIAN_PLACE = "სამტრედია-გრიგოლეთი 26კმ"
_GEORGIAN_DESCRIPTION = "ასკ 125-ე მუხლის პირველის პრიმა ნაწილი"
_RUSSIAN_PLACE = "Самтредиа-Григолети 26км"
_RUSSIAN_DESCRIPTION = "Статья 125-1-1, часть первая прима"


class _FakeTranslator:
    def __init__(self, *, result: TranslatedFineText | None = None, error: Exception | None = None):
        self._result = result or TranslatedFineText(
            place_ru=_RUSSIAN_PLACE, violation_description_ru=_RUSSIAN_DESCRIPTION,
        )
        self._error = error
        self.calls: list[dict] = []

    async def translate(self, *, place, violation_description):
        self.calls.append({"place": place, "violation_description": violation_description})
        if self._error is not None:
            raise self._error
        return self._result


async def test_georgian_place_is_translated_and_stored(tmp_path):
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    fine_repo = DetectedFineRepository(db_path)
    try:
        task = _make_task(task_repo)
        translator = _FakeTranslator(result=TranslatedFineText(place_ru=_RUSSIAN_PLACE))
        provider = _FakeProvider(records=[_record(fingerprint="fp-1", place=_GEORGIAN_PLACE)])
        service = FineCheckService(provider, task_repo, fine_repo, translator)

        result = await service.check_task(task)

        assert result.new_fines[0].place_ru == _RUSSIAN_PLACE
        stored = fine_repo.get_by_fingerprint(task.id, "fp-1")
        assert stored.place_ru == _RUSSIAN_PLACE
        assert stored.place == _GEORGIAN_PLACE  # оригинал не тронут
    finally:
        task_repo.close()
        fine_repo.close()


async def test_georgian_violation_description_is_translated_and_stored(tmp_path):
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    fine_repo = DetectedFineRepository(db_path)
    try:
        task = _make_task(task_repo)
        translator = _FakeTranslator(
            result=TranslatedFineText(violation_description_ru=_RUSSIAN_DESCRIPTION)
        )
        provider = _FakeProvider(
            records=[_record(fingerprint="fp-1", violation_description=_GEORGIAN_DESCRIPTION)]
        )
        service = FineCheckService(provider, task_repo, fine_repo, translator)

        result = await service.check_task(task)

        assert result.new_fines[0].violation_description_ru == _RUSSIAN_DESCRIPTION
        stored = fine_repo.get_by_fingerprint(task.id, "fp-1")
        assert stored.violation_description_ru == _RUSSIAN_DESCRIPTION
        assert stored.violation_description == _GEORGIAN_DESCRIPTION  # оригинал не тронут
    finally:
        task_repo.close()
        fine_repo.close()


async def test_both_georgian_fields_translated_in_one_call(tmp_path):
    """"Для одного штрафа желательно переводить place + violation_description
    одним запросом" — ровно один вызов translate(), несущий оба поля."""
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    fine_repo = DetectedFineRepository(db_path)
    try:
        task = _make_task(task_repo)
        translator = _FakeTranslator()
        provider = _FakeProvider(
            records=[
                _record(
                    fingerprint="fp-1", place=_GEORGIAN_PLACE, violation_description=_GEORGIAN_DESCRIPTION,
                )
            ]
        )
        service = FineCheckService(provider, task_repo, fine_repo, translator)

        result = await service.check_task(task)

        assert len(translator.calls) == 1
        assert translator.calls[0] == {
            "place": _GEORGIAN_PLACE, "violation_description": _GEORGIAN_DESCRIPTION,
        }
        assert result.new_fines[0].place_ru == _RUSSIAN_PLACE
        assert result.new_fines[0].violation_description_ru == _RUSSIAN_DESCRIPTION
    finally:
        task_repo.close()
        fine_repo.close()


async def test_already_russian_place_is_not_sent_for_translation(tmp_path):
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    fine_repo = DetectedFineRepository(db_path)
    try:
        task = _make_task(task_repo)
        translator = _FakeTranslator()
        provider = _FakeProvider(
            records=[_record(fingerprint="fp-1", place="Тбилиси, проспект Руставели")]
        )
        service = FineCheckService(provider, task_repo, fine_repo, translator)

        result = await service.check_task(task)

        assert translator.calls == []  # ни одного вызова API
        assert result.new_fines[0].place_ru is None
        assert result.new_fines[0].place == "Тбилиси, проспект Руставели"
    finally:
        task_repo.close()
        fine_repo.close()


async def test_empty_place_and_description_make_no_translation_call(tmp_path):
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    fine_repo = DetectedFineRepository(db_path)
    try:
        task = _make_task(task_repo)
        translator = _FakeTranslator()
        provider = _FakeProvider(
            records=[_record(fingerprint="fp-1", place=None, violation_description=None)]
        )
        service = FineCheckService(provider, task_repo, fine_repo, translator)

        result = await service.check_task(task)

        assert translator.calls == []
        assert result.new_fines[0].place_ru is None
        assert result.new_fines[0].violation_description_ru is None
    finally:
        task_repo.close()
        fine_repo.close()


async def test_no_translator_configured_leaves_ru_fields_none(tmp_path):
    """translator=None (см. задачу: config gap, а не ошибка запуска) —
    detection/сохранение/уведомление продолжаются как обычно, просто без
    перевода — format_fine_block() покажет оригинальный грузинский текст."""
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    fine_repo = DetectedFineRepository(db_path)
    try:
        task = _make_task(task_repo)
        provider = _FakeProvider(records=[_record(fingerprint="fp-1", place=_GEORGIAN_PLACE)])
        service = FineCheckService(provider, task_repo, fine_repo, translator=None)

        result = await service.check_task(task)

        assert result.status == "ok"
        assert result.new_fines[0].place_ru is None
        assert result.new_fines[0].place == _GEORGIAN_PLACE
    finally:
        task_repo.close()
        fine_repo.close()


async def test_repeated_scheduled_check_does_not_translate_again(tmp_path):
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    fine_repo = DetectedFineRepository(db_path)
    try:
        task = _make_task(task_repo)
        translator = _FakeTranslator()
        provider = _FakeProvider(records=[_record(fingerprint="fp-1", place=_GEORGIAN_PLACE)])
        service = FineCheckService(provider, task_repo, fine_repo, translator)

        await service.check_task(task)
        assert len(translator.calls) == 1

        # Тот же штраф, тот же провайдер (police.ge на каждой проверке
        # снова отдаёт полные данные) — повторная (background) проверка
        # не должна переводить его снова.
        second = await service.check_task(task)

        assert len(translator.calls) == 1  # без изменений
        assert len(second.current_fines) == 1
        assert second.current_fines[0].place_ru == _RUSSIAN_PLACE
    finally:
        task_repo.close()
        fine_repo.close()


async def test_repeated_manual_check_does_not_translate_again(tmp_path):
    """Manual "Проверить сейчас" использует ТОТ ЖЕ check_task() — тот же
    кеш применяется независимо от того, кто инициировал проверку."""
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    fine_repo = DetectedFineRepository(db_path)
    try:
        task = _make_task(task_repo)
        translator = _FakeTranslator()
        provider = _FakeProvider(records=[_record(fingerprint="fp-1", place=_GEORGIAN_PLACE)])
        service = FineCheckService(provider, task_repo, fine_repo, translator)

        await service.check_task(task)  # "фоновая" проверка нашла и перевела штраф
        assert len(translator.calls) == 1

        manual_result = await service.check_task(task)  # "Проверить сейчас"

        assert len(translator.calls) == 1
        assert manual_result.current_fines[0].place_ru == _RUSSIAN_PLACE
    finally:
        task_repo.close()
        fine_repo.close()


async def test_legacy_row_without_translation_receives_it_on_recheck(tmp_path):
    """Self-heal (см. задачу): legacy-штраф, у которого place_ru/
    violation_description_ru ещё NULL, получает перевод при следующей
    обычной проверке (тот же принцип, что и backfill violation_date/
    amount/place/violation_description из предыдущей задачи)."""
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    fine_repo = DetectedFineRepository(db_path)
    try:
        task = _make_task(task_repo)
        legacy = fine_repo.create(
            monitoring_task_id=task.id, car_number="B957MA09",
            external_fine_id="A1", fingerprint="fp-legacy",
            penalty_date=date(2026, 8, 6), due_date=date(2026, 8, 20),
            delivered_status="Не вручено", raw_data="{}",
            place=_GEORGIAN_PLACE, violation_description=_GEORGIAN_DESCRIPTION,
        )
        assert legacy.place_ru is None

        translator = _FakeTranslator()
        provider = _FakeProvider(
            records=[
                _record(
                    fingerprint="fp-legacy", external_fine_id="A1",
                    place=_GEORGIAN_PLACE, violation_description=_GEORGIAN_DESCRIPTION,
                )
            ]
        )
        service = FineCheckService(provider, task_repo, fine_repo, translator)

        result = await service.check_task(task)

        assert result.new_fines == []  # не новое обнаружение
        assert len(translator.calls) == 1
        healed = fine_repo.get_by_fingerprint(task.id, "fp-legacy")
        assert healed.place_ru == _RUSSIAN_PLACE
        assert healed.violation_description_ru == _RUSSIAN_DESCRIPTION
        assert healed.id == legacy.id  # та же строка, не дубликат
    finally:
        task_repo.close()
        fine_repo.close()


async def test_translation_api_failure_does_not_break_fine_detection(tmp_path):
    """Ключевое требование: сбой перевода никогда не должен ронять
    check_task() — detection/сохранение/уведомление продолжаются, просто
    place_ru/violation_description_ru остаются None (safe fallback —
    оригинальный грузинский текст в format_fine_block)."""
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    fine_repo = DetectedFineRepository(db_path)
    try:
        task = _make_task(task_repo)
        translator = _FakeTranslator(error=FineTranslationError("provider error"))
        provider = _FakeProvider(
            records=[
                _record(
                    fingerprint="fp-1", external_fine_id="A1",
                    place=_GEORGIAN_PLACE, violation_description=_GEORGIAN_DESCRIPTION,
                )
            ]
        )
        service = FineCheckService(provider, task_repo, fine_repo, translator)

        result = await service.check_task(task)

        assert result.status == "ok"
        assert len(result.new_fines) == 1
        assert result.new_fines[0].place_ru is None
        assert result.new_fines[0].place == _GEORGIAN_PLACE  # оригинал доступен как fallback
        stored = fine_repo.get_by_fingerprint(task.id, "fp-1")
        assert stored is not None
        assert stored.place_ru is None
        assert stored.place == _GEORGIAN_PLACE
    finally:
        task_repo.close()
        fine_repo.close()
