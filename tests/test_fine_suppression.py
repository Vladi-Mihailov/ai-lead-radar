"""Тесты центрального denylist'а Georgia fine output (reader/fines/
suppression.py) — ОДИН номер (O687KE761), штрафы которого ни при каких
условиях не должны показываться НИКОМУ, включая trusted/admin (см. задачу
про приватность конкретного автомобиля).

Unit-тесты is_fine_output_suppressed() + сквозной "canary"-тест: провайдер
намеренно "находит" штраф с заметной суммой 98765 для denylisted номера —
тест доказывает, что provider вообще не вызывается (а значит эта сумма
структурно не может попасть ни в один BotReply/уведомление), и дополнительно
проверяет несколько готовых текстов форматирования.
"""

import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.fines.check_service import FineCheckService
from reader.fines.detected_fine_repository import DetectedFineRepository
from reader.fines.models import ParsedFineRecord
from reader.fines.provider import FineProvider
from reader.fines.suppression import is_fine_output_suppressed
from reader.fines.task_repository import FineMonitoringTaskRepository
from reader.public_bot import texts
from reader.public_bot.delivery_texts import (
    format_check_now_fines_message,
)

_SUPPRESSED_PLATE = "O687KE761"
_UNRELATED_PLATE = "P004XC163"
_CHAT_ID = -100999
_USER_ID = 111
_CANARY_AMOUNT = 98765.0


# ---- 1/2/3: нормализация ----


def test_exact_suppressed_plate_is_suppressed():
    assert is_fine_output_suppressed(_SUPPRESSED_PLATE) is True


def test_lowercase_is_suppressed():
    assert is_fine_output_suppressed("o687ke761") is True


def test_mixed_case_is_suppressed():
    assert is_fine_output_suppressed("O687ke761") is True


def test_whitespace_variants_are_suppressed():
    assert is_fine_output_suppressed("O687 KE761") is True
    assert is_fine_output_suppressed(" O687KE761 ") is True
    assert is_fine_output_suppressed("O 6 8 7 K E 7 6 1") is True


def test_dash_variants_are_suppressed():
    assert is_fine_output_suppressed("O687-KE761") is True
    assert is_fine_output_suppressed("O-6-8-7-K-E-7-6-1") is True


def test_combined_case_whitespace_dash_variant_is_suppressed():
    assert is_fine_output_suppressed(" o687-ke 761 ") is True


def test_unrelated_plate_is_not_suppressed():
    assert is_fine_output_suppressed(_UNRELATED_PLATE) is False


def test_invalid_car_number_is_not_suppressed():
    """Невалидный номер — не забота этой функции (вызывающий код сам
    обрабатывает FineValidationError там, где она возникает)."""
    assert is_fine_output_suppressed("") is False
    assert is_fine_output_suppressed("   ") is False


# ---- сквозной canary-тест: provider вообще не вызывается ----


class _CanaryProvider(FineProvider):
    """"Находит" штраф с заметной суммой ДЛЯ ЛЮБОГО номера, который
    спросят — если denylist работает корректно, этот provider никогда не
    будет вызван для _SUPPRESSED_PLATE, и сумма 98765 физически не может
    попасть ни в один result/CheckResult/BotReply."""

    def __init__(self):
        self.requested_plates: list[str] = []

    async def search_by_plate(self, plate: str) -> list[ParsedFineRecord]:
        self.requested_plates.append(plate)
        return [
            ParsedFineRecord(
                car_number=plate,
                external_fine_id="CANARY-1",
                penalty_date=date(2026, 8, 6),
                due_date=date(2026, 8, 20),
                delivered_status="Не вручено",
                fingerprint=f"fp-canary-{plate}",
                raw_data={"protocolNo": "CANARY-1"},
                amount=_CANARY_AMOUNT,
                place="Тбилиси",
                violation_description="Превышение скорости",
            )
        ]


def _make_task(task_repo: FineMonitoringTaskRepository, *, car_number: str):
    return task_repo.create(
        car_number=car_number, label=None,
        start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
        telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
    )


async def test_canary_suppressed_plate_fine_never_reaches_provider_or_persistence(tmp_path):
    """ГЛАВНЫЙ regression-тест: даже когда provider технически "нашёл бы"
    штраф на 98765 ₾ для denylisted номера, check_task() никогда не
    вызывает provider вовсе (короткое замыкание ДО _fetch()) — ни
    CheckResult, ни detected_fines, ни last_successful_total_amount не
    могут содержать эту сумму, потому что она никогда не запрашивалась."""
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    fine_repo = DetectedFineRepository(db_path)
    try:
        task = _make_task(task_repo, car_number=_SUPPRESSED_PLATE)
        provider = _CanaryProvider()
        service = FineCheckService(provider, task_repo, fine_repo)

        result = await service.check_task(task)

        # Провайдер вообще не вызван — 98765 структурно не может никуда попасть.
        assert provider.requested_plates == []
        assert result.status == "suppressed"
        assert result.new_fines == []
        assert result.current_fines == []
        assert result.total_fines_found == 0

        # Persistence не тронут вовсе — ни detected_fines, ни last_successful_*.
        assert fine_repo.list_by_car_number(_SUPPRESSED_PLATE) == []
        reloaded = task_repo.get(task.id)
        assert reloaded.last_check_status is None
        assert reloaded.last_successful_total_amount is None
        assert reloaded.last_successful_checked_at is None
    finally:
        task_repo.close()
        fine_repo.close()


async def test_canary_check_plate_for_tasks_also_never_calls_provider(tmp_path):
    """Тот же canary, но для группового пути (DebtRefreshService.refresh,
    см. FineCheckService.check_plate_for_tasks)."""
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    fine_repo = DetectedFineRepository(db_path)
    try:
        task_a = _make_task(task_repo, car_number=_SUPPRESSED_PLATE)
        task_b = task_repo.create(
            car_number=_SUPPRESSED_PLATE, label="second task on same plate",
            start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )
        provider = _CanaryProvider()
        service = FineCheckService(provider, task_repo, fine_repo)

        results = await service.check_plate_for_tasks(_SUPPRESSED_PLATE, [task_a, task_b])

        assert provider.requested_plates == []
        assert results[task_a.id].status == "suppressed"
        assert results[task_b.id].status == "suppressed"
        assert fine_repo.list_by_car_number(_SUPPRESSED_PLATE) == []
    finally:
        task_repo.close()
        fine_repo.close()


async def test_canary_unrelated_plate_is_unaffected_by_denylist(tmp_path):
    """Regression (см. задачу п.12): машина, НЕ попадающая в denylist,
    проверяется ровно как раньше — provider вызывается, штраф находится и
    персистится."""
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    fine_repo = DetectedFineRepository(db_path)
    try:
        task = _make_task(task_repo, car_number=_UNRELATED_PLATE)
        provider = _CanaryProvider()
        service = FineCheckService(provider, task_repo, fine_repo)

        result = await service.check_task(task)

        assert provider.requested_plates == [_UNRELATED_PLATE]
        assert result.status == "ok"
        assert len(result.new_fines) == 1
        assert result.new_fines[0].amount == _CANARY_AMOUNT
        reloaded = task_repo.get(task.id)
        assert reloaded.last_successful_total_amount == _CANARY_AMOUNT
    finally:
        task_repo.close()
        fine_repo.close()


def test_canary_amount_never_appears_in_check_now_result_text():
    """format_check_now_result() для check_ok=False (см. CheckNowOutcome,
    куда суппрессия естественно транслируется через status != 'ok') —
    текст нейтральный, без суммы, без слова "штраф" с цифрами."""

    class _FakeOutcome:
        car_number = _SUPPRESSED_PLATE
        check_ok = False
        fines: tuple = ()

    text = texts.format_check_now_result(_FakeOutcome())

    assert str(_CANARY_AMOUNT) not in text
    assert "98765" not in text
    # Не должно быть лжи "штрафов не найдено".
    assert "не найдено" not in text


def test_canary_amount_never_appears_in_search_money_line_when_check_failed():
    line = texts.format_search_money_line(check_ok=False, fines=[])
    assert "98765" not in line
    assert line == "💰 Штрафы: неизвестно"


def test_canary_amount_never_appears_in_debt_rows_when_car_excluded(tmp_path):
    """list_tasks_with_known_debt()/list_debt_car_groups() исключают
    denylisted car_number даже если last_successful_total_amount уже был
    бы (гипотетически) записан напрямую в БД — защита read-пути, независимая
    от того, кто и как записал значение."""
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    try:
        task = _make_task(task_repo, car_number=_SUPPRESSED_PLATE)
        task_repo.record_successful_check(task.id, total_amount=_CANARY_AMOUNT)

        rows = task_repo.list_tasks_with_known_debt()
        groups = task_repo.list_debt_car_groups()

        assert rows == []
        assert groups == []
    finally:
        task_repo.close()


def test_get_stats_by_car_excludes_suppressed_plate_even_with_preexisting_row(tmp_path):
    """fine stats читает УЖЕ persisted detected_fines напрямую — строка,
    созданная ДО появления denylist'а (здесь — вставленная напрямую, в
    обход FineCheckService), не должна всплыть в статистике."""
    db_path = tmp_path / "users.db"
    task_repo = FineMonitoringTaskRepository(db_path)
    fine_repo = DetectedFineRepository(db_path)
    try:
        task = _make_task(task_repo, car_number=_SUPPRESSED_PLATE)
        fine_repo.create(
            monitoring_task_id=task.id, car_number=_SUPPRESSED_PLATE,
            external_fine_id="A1", fingerprint="fp-historical",
            penalty_date=None, due_date=None, delivered_status="Не вручено",
            raw_data="{}",
        )
        unrelated_task = _make_task(task_repo, car_number=_UNRELATED_PLATE)
        fine_repo.create(
            monitoring_task_id=unrelated_task.id, car_number=_UNRELATED_PLATE,
            external_fine_id="A2", fingerprint="fp-real",
            penalty_date=None, due_date=None, delivered_status="Не вручено",
            raw_data="{}",
        )

        stats = fine_repo.get_stats_by_car()

        assert [row.car_number for row in stats] == [_UNRELATED_PLATE]
    finally:
        task_repo.close()
        fine_repo.close()


def test_format_check_now_fines_message_is_never_invoked_for_suppressed_outcome():
    """Защита от регрессии в самой текстовой функции: если бы кто-то
    по ошибке всё же вызвал её с canary-записью, сумма была бы видна — этот
    тест документирует, ПОЧЕМУ check_now никогда не должен вызывать её для
    suppressed CheckNowOutcome (см. texts.format_check_now_result: fines
    пуст для status != 'ok', эта функция вызывается только когда fines
    непусты)."""
    from reader.fines.models import NewFineEvent

    event = NewFineEvent(
        detected_fine_id=1, task_id=1, car_number=_SUPPRESSED_PLATE, label=None,
        external_fine_id="CANARY-1", penalty_date=None, due_date=None,
        delivered_status=None, amount=_CANARY_AMOUNT,
    )
    text = format_check_now_fines_message(car_number=_SUPPRESSED_PLATE, fines=[event])
    # Сама функция честно форматирует то, что ей передали (она не знает про
    # denylist — он живёт выше, см. check_service.py) — это ожидаемо.
    assert "98765" in text
    # Но CheckNowOutcome.fines для suppressed car ВСЕГДА [] (см.
    # test_canary_suppressed_plate_fine_never_reaches_provider_or_persistence),
    # поэтому format_check_now_result() никогда не передаёт сюда ничего для
    # этого номера — см. texts.py::format_check_now_result.
