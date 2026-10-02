"""Сосуществование двух независимых защит Georgia fine output:

- false-zero guard (3edc1fb, см. tests/test_false_zero_race.py) — ранний
  snapshot, свежее состояние для решения о подтверждающем запросе,
  detected_fines как свидетельство при NULL snapshot;
- suppression (reader/fines/suppression.py) — denylisted номер НИКОГДА не
  показывает штрафы никому, включая trusted-оператора.

Suppression проверяется в FineCheckService ПЕРВЫМ, до провайдера и до
false-zero guard, поэтому ни один шаг false-zero (запрос, подтверждение,
snapshot, detected_fines) для denylisted номера не выполняется, а для
остальных номеров false-zero поведение не меняется.

Репозитории — настоящие (SQLite/tmp_path), provider — scripted-фейк с
"канареечной" суммой 98765: если она появится в любом выводе — утечка."""

import json
import sys
from datetime import date
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from test_false_zero_confirmation import _ScriptedProvider, _SleepLog

from reader.fines.check_service import FineCheckService
from reader.fines.detected_fine_repository import DetectedFineRepository
from reader.fines.models import ParsedFineRecord
from reader.fines.suppression import is_fine_output_suppressed
from reader.fines.task_repository import FineMonitoringTaskRepository
from reader.public_bot import texts
from reader.public_bot.subscription_repository import FineSubscriptionRepository
from reader.public_bot.subscription_service import SubscriptionService
from reader.users.repository import UserRepository

_SUPPRESSED = "O687KE761"
_NORMAL = "AA111AA"
_CANARY = 98765.0


def _fine(plate: str, index: int, amount: float) -> ParsedFineRecord:
    fp = f"fp-{plate}-{index}"
    return ParsedFineRecord(
        car_number=plate, external_fine_id=f"KV{index:09d}",
        penalty_date=date(2026, 9, 28), due_date=date(2026, 11, 27),
        delivered_status="Не вручено", fingerprint=fp, raw_data={"protocolNo": fp}, amount=amount,
    )


class _Fixture:
    def __init__(self, tmp_path):
        db_path = tmp_path / "users.db"
        self.tasks = FineMonitoringTaskRepository(db_path)
        self.fines = DetectedFineRepository(db_path)
        self.subscriptions = FineSubscriptionRepository(db_path)
        self.users = UserRepository(db_path)
        self.provider = _ScriptedProvider()
        self.sleep_log = _SleepLog()
        self.check_service = FineCheckService(self.provider, self.tasks, self.fines, sleep=self.sleep_log)
        self.service = SubscriptionService(self.tasks, self.subscriptions, self.users, self.check_service)

    def make_task(self, plate: str, *, user_id: int = 1):
        return self.tasks.create(
            car_number=plate, label=None, start_date=date(2026, 10, 1), end_date=date(2026, 10, 31),
            telegram_chat_id=user_id, created_by_user_id=user_id,
        )

    def seed_snapshot(self, task, amount: float):
        self.tasks.record_check_result(task.id, last_check_status="ok", last_error=None)
        self.tasks.record_successful_check(task.id, total_amount=amount)

    def add_history(self, task, amount: float = _CANARY):
        self.fines.create(
            monitoring_task_id=task.id, car_number=task.car_number, external_fine_id="KV-HISTORY",
            fingerprint=f"fp-history-{task.id}", penalty_date=date(2026, 9, 1), due_date=date(2026, 10, 1),
            delivered_status="Не вручено", raw_data=json.dumps({}), amount=amount,
        )

    def close(self):
        for repo in (self.tasks, self.fines, self.subscriptions, self.users):
            repo.close()


@pytest.fixture
def fx(tmp_path):
    fixture = _Fixture(tmp_path)
    yield fixture
    fixture.close()


async def _check_now_text(fx, plate: str) -> str:
    outcome = await fx.service.check_now_task_by_car_number(plate)
    assert outcome is not None
    return texts.format_check_now_result(outcome)


# 1. normal car keeps the false-zero protection


async def test_unsuppressed_transient_zero_with_previous_positive_is_confirmed(fx):
    task = fx.make_task(_NORMAL)
    fx.seed_snapshot(task, 150.0)
    fx.provider.script(_NORMAL, [[], [_fine(_NORMAL, 0, 100.0), _fine(_NORMAL, 1, 50.0)]])

    text = await _check_now_text(fx, _NORMAL)

    assert fx.provider.requested_plates == [_NORMAL, _NORMAL]  # подтверждающий запрос
    assert "штрафов не найдено" not in text
    assert fx.tasks.get(task.id).last_successful_total_amount == 150.0


# 2. suppressed plate: nothing escapes, whatever the provider would return


async def test_suppressed_plate_never_calls_provider_and_looks_like_no_fines(fx):
    fx.make_task(_SUPPRESSED)
    fx.provider.script(_SUPPRESSED, [[_fine(_SUPPRESSED, 0, _CANARY)]])

    text = await _check_now_text(fx, _SUPPRESSED)

    assert fx.provider.requested_plates == []
    assert text == f"🔎 {_SUPPRESSED}: штрафов не найдено"  # тот же стандартный текст, что и у любого номера
    assert "98765" not in text and "недоступн" not in text
    assert fx.fines.list_by_car_number(_SUPPRESSED) == []


# 3. suppressed + historical detected_fines (the false-zero NULL-snapshot evidence)


async def test_suppressed_plate_with_history_does_not_leak_and_history_is_preserved(fx):
    task = fx.make_task(_SUPPRESSED)
    fx.add_history(task)
    assert fx.fines.has_detected_fines(task.id) is True  # false-zero "evidence" exists...
    fx.provider.script(_SUPPRESSED, [[], [_fine(_SUPPRESSED, 0, _CANARY)]])

    text = await _check_now_text(fx, _SUPPRESSED)

    assert fx.provider.requested_plates == [] and fx.sleep_log.calls == []  # ...but no request/confirmation
    assert "98765" not in text and text == f"🔎 {_SUPPRESSED}: штрафов не найдено"
    assert all(s.car_number != _SUPPRESSED for s in fx.fines.get_stats_by_car())
    assert len(fx.fines.list_by_car_number(_SUPPRESSED)) == 1  # история НЕ удалена


# 4. suppressed + positive persisted snapshot


async def test_suppressed_plate_with_positive_snapshot_does_not_leak_and_snapshot_untouched(fx):
    task = fx.make_task(_SUPPRESSED)
    fx.seed_snapshot(task, _CANARY)
    before = fx.tasks.get(task.id)

    text = await _check_now_text(fx, _SUPPRESSED)
    result = await fx.check_service.check_task(task)

    assert result.status == "suppressed" and result.current_fines == [] and result.total_fines_found == 0
    assert "98765" not in text
    assert all(row.car_number != _SUPPRESSED for row in fx.tasks.list_tasks_with_known_debt())
    assert all(group.car_number != _SUPPRESSED for group in fx.tasks.list_debt_car_groups())
    after = fx.tasks.get(task.id)
    assert (after.last_successful_total_amount, after.last_successful_checked_at, after.last_check_status) == (
        before.last_successful_total_amount, before.last_successful_checked_at, before.last_check_status,
    )


# 5. suppressed plate in a grouped check (several owners/tasks, variant spelling)


async def test_suppressed_plate_in_grouped_check_does_not_leak_through_any_task(fx):
    owner_a = fx.make_task(_SUPPRESSED, user_id=1)
    owner_b = fx.make_task(_SUPPRESSED, user_id=2)
    fx.seed_snapshot(owner_b, _CANARY)
    fx.provider.script(_SUPPRESSED, [[_fine(_SUPPRESSED, 0, _CANARY)]])

    for spelling in (_SUPPRESSED, "o687ke761", " O687-KE 761 "):
        assert is_fine_output_suppressed(spelling) is True
        results = await fx.check_service.check_plate_for_tasks(spelling, [owner_a, owner_b])
        assert {r.status for r in results.values()} == {"suppressed"}
        assert all(r.current_fines == [] and r.new_fines == [] for r in results.values())

    assert fx.provider.requested_plates == []
    assert fx.tasks.get(owner_a.id).last_successful_total_amount is None  # ничего не записано


# 6. unsuppressed grouped checks keep the new false-zero protection


async def test_unsuppressed_grouped_check_uses_fresh_snapshot_for_confirmation(fx):
    t1, t2 = fx.make_task(_NORMAL, user_id=1), fx.make_task(_NORMAL, user_id=2)
    fx.seed_snapshot(t2, 150.0)  # объекты t1/t2 в памяти — устаревшие (NULL)
    fx.provider.script(_NORMAL, [[], [_fine(_NORMAL, 0, 150.0)]])

    results = await fx.check_service.check_plate_for_tasks(_NORMAL, [t1, t2])

    assert fx.provider.requested_plates == [_NORMAL, _NORMAL]
    assert all(len(r.current_fines) == 1 for r in results.values())
    assert fx.tasks.get(t1.id).last_successful_total_amount == 150.0


# 7. other plates' debt semantics unchanged


async def test_suppression_does_not_change_debt_rows_for_other_plates(fx):
    suppressed, normal = fx.make_task(_SUPPRESSED), fx.make_task(_NORMAL)
    fx.seed_snapshot(suppressed, _CANARY)
    fx.seed_snapshot(normal, 40.0)

    rows = fx.tasks.list_tasks_with_known_debt()

    assert [(r.car_number, r.total_amount) for r in rows] == [(_NORMAL, 40.0)]
    fx.provider.script(_NORMAL, [[_fine(_NORMAL, 0, 40.0)]])
    assert (await fx.check_service.check_task(normal)).status == "ok"
    assert fx.tasks.get(normal.id).last_successful_total_amount == 40.0


# 8. Turkey is untouched by Georgia suppression


def test_turkey_code_does_not_use_georgia_suppression():
    turkey_files = [
        path for package in ("turkey_bot", "turkey_bot_test")
        for path in (PROJECT_ROOT / "reader" / package).rglob("*.py")
    ]
    assert turkey_files
    assert not [p for p in turkey_files if "is_fine_output_suppressed" in p.read_text(encoding="utf-8")]


# ---- invisible suppression: identical to an ordinary car without fines ----


async def _add_car(fx, plate: str, user_id: int):
    return await fx.service.add_car(
        telegram_user_id=user_id, telegram_chat_id=user_id, username=None, first_name=None, last_name=None,
        car_number=plate, period_days=30, today=date(2026, 10, 2),
    )


def _summary(outcome) -> str:
    return texts.format_add_car_summary(
        car_number=outcome.subscription.car_number, start_date=outcome.subscription.start_date,
        end_date=outcome.subscription.end_date, check_ok=outcome.check_ok, new_fines_count=outcome.new_fines_count,
    )


async def test_owner_add_car_and_check_now_identical_to_car_without_fines(fx):
    fx.provider.script(_SUPPRESSED, [[_fine(_SUPPRESSED, 0, _CANARY)]])  # would leak if ever requested
    fx.provider.script(_NORMAL, [[]])                                      # genuinely no fines

    suppressed_add, normal_add = await _add_car(fx, _SUPPRESSED, 42), await _add_car(fx, _NORMAL, 43)
    suppressed_now = await fx.service.check_now(suppressed_add.subscription.id, telegram_user_id=42)
    normal_now = await fx.service.check_now(normal_add.subscription.id, telegram_user_id=43)

    assert fx.provider.requested_plates == [_NORMAL, _NORMAL]   # O687KE761 never requested
    assert _summary(suppressed_add) == _summary(normal_add).replace(_NORMAL, _SUPPRESSED)
    assert texts.format_check_now_result(suppressed_now) == f"🔎 {_SUPPRESSED}: штрафов не найдено"
    assert texts.format_check_now_result(normal_now) == f"🔎 {_NORMAL}: штрафов не найдено"
    for text in (_summary(suppressed_add), texts.format_check_now_result(suppressed_now)):
        assert "98765" not in text
        assert not any(word in text.lower() for word in ("недоступн", "исключ", "suppress", "не удалось"))


async def test_trusted_check_now_and_search_line_identical_to_car_without_fines(fx):
    suppressed_task = fx.make_task(_SUPPRESSED)
    fx.seed_snapshot(suppressed_task, _CANARY)  # historical positive debt must not surface
    fx.add_history(suppressed_task)
    fx.make_task(_NORMAL)
    fx.provider.script(_NORMAL, [[]])

    trusted = await fx.service.check_now_task_by_car_number(_SUPPRESSED)
    search = await fx.service.check_now_task_for_search(suppressed_task.id)
    normal_search = await fx.service.check_now_task_for_search(fx.tasks.get_active_by_car_number(_NORMAL)[0].id)

    assert texts.format_check_now_result(trusted) == f"🔎 {_SUPPRESSED}: штрафов не найдено"
    line = texts.format_search_money_line(check_ok=search.check_ok, fines=search.fines)
    assert line == texts.format_search_money_line(check_ok=normal_search.check_ok, fines=normal_search.fines)
    assert line == "💰 Штрафы: 0 ₾" and "98765" not in line
    assert fx.provider.requested_plates == [_NORMAL]
    assert len(fx.fines.list_by_car_number(_SUPPRESSED)) == 1  # history untouched


def test_no_special_suppression_wording_left_in_bot_texts():
    source = (PROJECT_ROOT / "reader" / "public_bot" / "texts.py").read_text(encoding="utf-8")
    assert "CHECK_NOW_UNAVAILABLE_TEXT" not in source
    assert "Проверка штрафов для этого автомобиля недоступна" not in source
    fine_source = (PROJECT_ROOT / "reader" / "commands" / "fine.py").read_text(encoding="utf-8")
    assert "Проверка штрафов для этого автомобиля недоступна" not in fine_source
    assert "проверка недоступна" not in fine_source
