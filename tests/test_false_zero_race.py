"""Регрессия production P099XO36 (2026-10-02): гонка между первой проверкой
новой задачи и ручным "🔎 Проверить сейчас".

Что произошло: Add Car получил от police.ge 12 штрафов (750 ₾), затем
несколько минут переводил/создавал detected_fines, а
last_successful_total_amount сохранялся только В КОНЦЕ _finalize(). Две
ручные проверки в это время получили транзиентный success:true с пустым
results; false-zero guard смотрел на task.last_successful_total_amount,
который всё ещё был NULL -> оба нуля приняты -> "штрафов не найдено", хотя
в detected_fines уже лежали 7-8 штрафов этой же задачи.

Номер — синтетический (тот же стиль, что и tests/test_false_zero_confirmation.py).
Репозитории настоящие (SQLite/tmp_path), provider — scripted-фейк, sleep —
мгновенный записывающий фейк, переводчик — фейк, который можно "задержать"."""

import asyncio
import json
import logging
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from test_false_zero_confirmation import _ScriptedProvider, _SleepLog

from reader.fines.check_service import FineCheckService
from reader.fines.detected_fine_repository import DetectedFineRepository
from reader.fines.models import ParsedFineRecord
from reader.fines.provider import FineProviderError
from reader.fines.task_repository import FineMonitoringTaskRepository
from reader.fines.translation import TranslatedFineText
from reader.public_bot import texts
from reader.public_bot.subscription_service import CheckNowOutcome

_PLATE = "ZZ123ZZ"
_GEORGIAN_PLACE = "ქუთაისი"  # грузинский текст -> переводчик действительно вызывается


def _fine(index: int, amount: float = 50.0, *, plate: str = _PLATE) -> ParsedFineRecord:
    fp = f"fp-{plate}-{index}"
    return ParsedFineRecord(
        car_number=plate, external_fine_id=f"KV{index:09d}",
        penalty_date=date(2026, 9, 28), due_date=date(2026, 11, 27),
        delivered_status="Не вручено", fingerprint=fp, raw_data={"protocolNo": fp},
        amount=amount, place=_GEORGIAN_PLACE,
    )


def _twelve_fines(*, plate: str = _PLATE) -> list[ParsedFineRecord]:
    """12 штрафов на 750 ₾ — как у P099XO36 (3 x 100 + 9 x 50)."""
    return [_fine(i, 100.0 if i < 3 else 50.0, plate=plate) for i in range(12)]


class _GateTranslator:
    """Переводчик, который на ПЕРВОМ вызове ждёт, пока тест его отпустит —
    моделирует медленную обработку штрафов (OpenAI) первой проверки."""

    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def translate(self, *, place, violation_description):
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            await self.release.wait()
        return TranslatedFineText(place_ru="Кутаиси", violation_description_ru=None)


class _Fixture:
    def __init__(self, tmp_path, *, translator=None):
        self.db_path = tmp_path / "users.db"
        self.tasks = FineMonitoringTaskRepository(self.db_path)
        self.fines = DetectedFineRepository(self.db_path)
        self.provider = _ScriptedProvider()
        self.sleep_log = _SleepLog()
        self.service = FineCheckService(
            self.provider, self.tasks, self.fines, translator, sleep=self.sleep_log,
        )

    def make_task(self, plate: str = _PLATE):
        return self.tasks.create(
            car_number=plate, label=None, start_date=date(2026, 10, 2), end_date=date(2026, 11, 1),
            telegram_chat_id=-1, created_by_user_id=1,
        )

    def snapshot(self, task):
        fresh = self.tasks.get(task.id)
        return fresh.last_successful_total_amount, fresh.last_check_status

    def add_detected_fine(self, task, index: int = 99, amount: float = 50.0):
        return self.fines.create(
            monitoring_task_id=task.id, car_number=task.car_number, external_fine_id=f"KV{index}",
            fingerprint=f"fp-history-{index}", penalty_date=date(2026, 9, 1), due_date=date(2026, 10, 1),
            delivered_status="Не вручено", raw_data=json.dumps({}), amount=amount,
        )


# ---- the exact incident ----


async def test_p099xo36_race_manual_check_during_slow_first_check_keeps_positive_result(tmp_path):
    translator = _GateTranslator()
    fx = _Fixture(tmp_path, translator=translator)
    task = fx.make_task()  # объект в памяти: last_successful_total_amount = None
    # 1) первая проверка: 12 штрафов; 2) ручная: транзиентный [] ; 3) подтверждение: 12 штрафов
    fx.provider.script(_PLATE, [_twelve_fines(), [], _twelve_fines()])

    first = asyncio.create_task(fx.service.check_task(task))
    await translator.entered.wait()  # первая проверка застряла в медленной обработке штрафов

    # H: snapshot принятого результата УЖЕ сохранён, до окончания обработки штрафов
    assert fx.snapshot(task) == (750.0, "ok")

    # Ручная "Проверить сейчас" — с тем же УСТАРЕВШИМ объектом task (NULL в памяти).
    manual = await fx.service.check_task(task)
    outcome = CheckNowOutcome(
        car_number=task.car_number, check_ok=manual.status == "ok",
        fines=manual.current_fines if manual.status == "ok" else [], is_owner=False,
    )

    assert len(fx.provider.requested_plates) == 3          # guard сделал подтверждающий запрос
    assert fx.sleep_log.calls == [fx.service._zero_confirmation_delay_seconds]
    assert manual.status == "ok" and len(manual.current_fines) == 12
    assert "штрафов не найдено" not in texts.format_check_now_result(outcome)
    assert fx.snapshot(task) == (750.0, "ok")

    translator.release.set()
    result = await first
    assert result.status == "ok" and len(result.current_fines) == 12
    assert fx.snapshot(task) == (750.0, "ok")                # положительный результат пережил всё
    assert len(fx.fines.list_by_car_number(_PLATE)) == 12     # без дублей detected_fines


async def test_old_ordering_would_have_accepted_the_zero(tmp_path):
    """Контроль: без early snapshot и без detected_fines — тот же старый
    сценарий "первой проверки ещё не было" принимает ноль (A)."""
    fx = _Fixture(tmp_path)
    task = fx.make_task()
    fx.provider.script(_PLATE, [[]])
    result = await fx.service.check_task(task)
    assert result.status == "ok" and result.current_fines == []
    assert len(fx.provider.requested_plates) == 1 and fx.sleep_log.calls == []
    assert fx.snapshot(task) == (0.0, "ok")


# ---- B, C, D: previous positive snapshot ----


async def _seed(fx, task, amount=750.0):
    fx.provider.script(_PLATE, [[_fine(0, amount)]])
    await fx.service.check_task(task)
    fx.provider.requested_plates.clear()
    fx.sleep_log.calls.clear()


async def test_b_previous_positive_double_zero_is_accepted(tmp_path):
    fx = _Fixture(tmp_path)
    task = fx.make_task()
    await _seed(fx, task)
    fx.provider.script(_PLATE, [[], []])
    result = await fx.service.check_task(task)
    assert len(fx.provider.requested_plates) == 2
    assert result.status == "ok" and result.current_fines == []
    assert fx.snapshot(task) == (0.0, "ok")


async def test_c_previous_positive_then_zero_then_positive_is_accepted(tmp_path):
    fx = _Fixture(tmp_path)
    task = fx.make_task()
    await _seed(fx, task)
    fx.provider.script(_PLATE, [[], _twelve_fines()])
    result = await fx.service.check_task(task)
    assert len(result.current_fines) == 12
    assert fx.snapshot(task) == (750.0, "ok")


async def test_d_previous_positive_then_zero_then_error_preserves_reliable_state(tmp_path):
    fx = _Fixture(tmp_path)
    task = fx.make_task()
    await _seed(fx, task, amount=200.0)
    fx.provider.script(_PLATE, [[], FineProviderError("police.ge timeout")])
    result = await fx.service.check_task(task)
    assert result.status == "error"
    fresh = fx.tasks.get(task.id)
    assert fresh.last_successful_total_amount == 200.0 and fresh.last_check_status == "error"


async def test_fresh_state_guard_uses_db_not_stale_task_object(tmp_path):
    """Объект task загружен ДО того, как другая проверка сохранила
    положительный snapshot — guard читает свежее состояние."""
    fx = _Fixture(tmp_path)
    stale = fx.make_task()
    fx.tasks.record_check_result(stale.id, last_check_status="ok", last_error=None)
    fx.tasks.record_successful_check(stale.id, total_amount=750.0)  # "параллельная" проверка
    assert stale.last_successful_total_amount is None
    fx.provider.script(_PLATE, [[], _twelve_fines()])
    result = await fx.service.check_task(stale)
    assert len(fx.provider.requested_plates) == 2 and len(result.current_fines) == 12


# ---- E, F: detected_fines safety net ----


async def test_e_null_snapshot_with_detected_fines_triggers_confirmation(tmp_path):
    fx = _Fixture(tmp_path)
    task = fx.make_task()
    fx.add_detected_fine(task)
    fx.provider.script(_PLATE, [[], _twelve_fines()])
    result = await fx.service.check_task(task)
    assert len(fx.provider.requested_plates) == 2
    assert len(result.current_fines) == 12


async def test_f_detected_fines_never_used_as_amount(tmp_path):
    """Подтверждённый ноль при наличии detected_fines — сохраняется 0, а не
    сумма истории; положительный — ровно сумма ответа provider'а."""
    fx = _Fixture(tmp_path)
    task = fx.make_task()
    fx.add_detected_fine(task, index=1, amount=500.0)
    fx.add_detected_fine(task, index=2, amount=300.0)
    fx.provider.script(_PLATE, [[], []])
    result = await fx.service.check_task(task)
    assert len(fx.provider.requested_plates) == 2
    assert result.current_fines == [] and fx.snapshot(task) == (0.0, "ok")

    fx.provider.requested_plates.clear()
    fx.provider.script(_PLATE, [[_fine(0, 50.0)]])
    await fx.service.check_task(task)
    assert fx.snapshot(task) == (50.0, "ok")  # не 850 и не 50+500+300


async def test_confirmed_zero_snapshot_stays_authoritative_despite_detected_fines(tmp_path):
    """Сохранённый подтверждённый 0 (машина оплатила штрафы) — решает он:
    detected_fines-история не заставляет делать повторный запрос на КАЖДОЙ
    последующей проверке (safety net — только когда snapshot ещё нет)."""
    fx = _Fixture(tmp_path)
    task = fx.make_task()
    fx.add_detected_fine(task)
    fx.tasks.record_check_result(task.id, last_check_status="ok", last_error=None)
    fx.tasks.record_successful_check(task.id, total_amount=0.0)
    fx.provider.script(_PLATE, [[]])
    await fx.service.check_task(task)
    assert len(fx.provider.requested_plates) == 1 and fx.sleep_log.calls == []


# ---- G: grouped path ----


async def test_g_check_plate_for_tasks_reads_fresh_state_and_detected_fines(tmp_path):
    fx = _Fixture(tmp_path)
    t1, t2 = fx.make_task(), fx.make_task()
    # Свежий положительный snapshot только у t2, объекты в памяти — устаревшие.
    fx.tasks.record_check_result(t2.id, last_check_status="ok", last_error=None)
    fx.tasks.record_successful_check(t2.id, total_amount=750.0)
    fx.provider.script(_PLATE, [[], _twelve_fines()])
    results = await fx.service.check_plate_for_tasks(_PLATE, [t1, t2])
    assert len(fx.provider.requested_plates) == 2
    assert all(len(r.current_fines) == 12 for r in results.values())
    assert fx.snapshot(t1) == (750.0, "ok") and fx.snapshot(t2) == (750.0, "ok")

    fx2 = _Fixture(tmp_path / "g2")
    a, b = fx2.make_task(), fx2.make_task()
    fx2.add_detected_fine(b)  # snapshot нет ни у кого, но у b есть история
    fx2.provider.script(_PLATE, [[], _twelve_fines()])
    await fx2.service.check_plate_for_tasks(_PLATE, [a, b])
    assert len(fx2.provider.requested_plates) == 2


async def test_g_group_snapshot_persisted_for_all_tasks_before_slow_enrichment(tmp_path):
    translator = _GateTranslator()
    fx = _Fixture(tmp_path, translator=translator)
    t1, t2 = fx.make_task(), fx.make_task()
    fx.provider.script(_PLATE, [_twelve_fines()])
    run = asyncio.create_task(fx.service.check_plate_for_tasks(_PLATE, [t1, t2]))
    await translator.entered.wait()  # первая задача группы ещё в обработке
    assert fx.snapshot(t1) == (750.0, "ok") and fx.snapshot(t2) == (750.0, "ok")
    translator.release.set()
    await run


# ---- ordering / failure safety ----


async def test_snapshot_survives_failure_during_enrichment(tmp_path):
    """Snapshot = ответ provider'а; сбой последующей обработки штрафов (здесь —
    неожиданное исключение переводчика) его не откатывает."""

    class _BrokenTranslator:
        async def translate(self, *, place, violation_description):
            raise RuntimeError("enrichment crashed")

    fx = _Fixture(tmp_path, translator=_BrokenTranslator())
    task = fx.make_task()
    fx.provider.script(_PLATE, [_twelve_fines()])
    try:
        await fx.service.check_task(task)
    except RuntimeError:
        pass
    assert fx.snapshot(task) == (750.0, "ok")


async def test_provider_error_persists_no_snapshot(tmp_path):
    fx = _Fixture(tmp_path)
    task = fx.make_task()
    fx.provider.script(_PLATE, [FineProviderError("parse failure")])
    result = await fx.service.check_task(task)
    fresh = fx.tasks.get(task.id)
    assert result.status == "error"
    assert fresh.last_successful_total_amount is None and fresh.last_successful_checked_at is None


# ---- logging ----


async def test_logs_safe_operational_fields_only(tmp_path, caplog):
    fx = _Fixture(tmp_path)
    task = fx.make_task()
    fx.add_detected_fine(task)
    fx.provider.script(_PLATE, [[], _twelve_fines()])
    with caplog.at_level(logging.INFO, logger="reader.fines.check_service"):
        await fx.service.check_task(task)
    messages = [r.getMessage() for r in caplog.records]
    assert f"Georgia zero confirmation plate={_PLATE} first_count=0 second_count=12 second_total=750" in messages
    assert f"Georgia fine check plate={_PLATE} result_count=12 total=750 zero_confirmation=True" in messages
    joined = " ".join(messages).lower()
    assert "csrf" not in joined and "cookie" not in joined and "protocolno" not in joined
