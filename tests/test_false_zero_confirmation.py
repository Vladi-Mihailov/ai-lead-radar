"""Тесты false-zero guard (задача "guard Georgia debt against false zero
results") — production incident: manager mass refresh ("🔄 Проверить авто
со штрафами") получил от police.ge технически валидный success:true с
пустым results для нескольких машин (включая P004XC163, реальная
задолженность 200 ₾), FineCheckService.check_task() принял это как
authoritative SUCCESS(0) и стёр реальную задолженность.

Repository — настоящие (SQLite/tmp_path), FineProvider — scripted-фейк
(возвращает РАЗНЫЕ ответы на последовательные вызовы одного и того же
plate — обычный _FakeProvider с records_by_car для этого не подходит).
sleep — записывающий фейк (см. _instant_sleep), НЕ реальные 5 секунд.
"""

import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.fines.check_service import FineCheckService
from reader.fines.detected_fine_repository import DetectedFineRepository
from reader.fines.models import ParsedFineRecord
from reader.fines.provider import FineProvider, FineProviderError
from reader.fines.task_repository import FineMonitoringTaskRepository
from reader.public_bot.debt_refresh_service import DebtRefreshService

_CHAT_ID = -100999
_USER_ID = 111


class _SleepLog:
    """Записывает КАЖДЫЙ await sleep(...) — не ждёт реально ни секунды (см.
    задачу: false-zero confirmation delay/inter-car delay инжектируются
    через конструктор именно для этого)."""

    def __init__(self):
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class _ScriptedProvider(FineProvider):
    """responses[plate] — список "ответов" на ПОСЛЕДОВАТЕЛЬНЫЕ вызовы
    ЭТОГО номера — каждый элемент либо list[ParsedFineRecord] (успех),
    либо Exception (обычно FineProviderError, эмулирует provider-уровня
    сбой, включая тот, что был бы получен из FineParseError). Если для
    plate ответы кончились — повторяет последний (для тестов, которым
    не важен третий вызов)."""

    def __init__(self, responses: dict[str, list] | None = None):
        self._responses: dict[str, list] = {k: list(v) for k, v in (responses or {}).items()}
        self.requested_plates: list[str] = []

    def script(self, plate: str, responses: list) -> None:
        self._responses[plate] = list(responses)

    async def search_by_plate(self, plate: str):
        self.requested_plates.append(plate)
        queue = self._responses.get(plate)
        if not queue:
            return []
        response = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(response, Exception):
            raise response
        return response


def _fine(*, car_number: str, fingerprint: str, amount: float) -> ParsedFineRecord:
    return ParsedFineRecord(
        car_number=car_number, external_fine_id=fingerprint,
        penalty_date=date(2026, 8, 6), due_date=date(2026, 8, 20),
        delivered_status="Не вручено", fingerprint=fingerprint,
        raw_data={"protocolNo": fingerprint}, amount=amount,
    )


class _Fixture:
    def __init__(self, tmp_path):
        self.db_path = tmp_path / "users.db"
        self.task_repository = FineMonitoringTaskRepository(self.db_path)
        self.detected_fine_repository = DetectedFineRepository(self.db_path)
        self.provider = _ScriptedProvider()
        self.sleep_log = _SleepLog()
        self.check_service = FineCheckService(
            self.provider, self.task_repository, self.detected_fine_repository,
            sleep=self.sleep_log,
        )
        self.debt_refresh_service = DebtRefreshService(
            self.task_repository, self.check_service, sleep=self.sleep_log,
        )

    def make_task(self, car_number: str):
        return self.task_repository.create(
            car_number=car_number, label=None,
            start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        )

    async def seed_positive_amount(self, task, *, amount: float, fingerprint: str = "fp-seed"):
        """Создаёт РЕАЛЬНОЕ предыдущее достоверное состояние (не просто
        пишет в БД напрямую) — ровно один provider-вызов, previous=None до
        этого, поэтому confirmation здесь не участвует."""
        self.provider.script(task.car_number, [[_fine(car_number=task.car_number, fingerprint=fingerprint, amount=amount)]])
        result = await self.check_service.check_task(task)
        assert result.status == "ok"
        self.provider.requested_plates.clear()
        self.sleep_log.calls.clear()
        return self.task_repository.get(task.id)


# ---- 1. previous=200, first=0, second=0 -> final=0 ----

async def test_confirmed_zero_replaces_previous_positive_amount(tmp_path):
    fx = _Fixture(tmp_path)
    task = fx.make_task("AA001AA")
    task = await fx.seed_positive_amount(task, amount=200)
    assert task.last_successful_total_amount == 200

    fx.provider.script(task.car_number, [[], []])
    result = await fx.check_service.check_task(task)

    assert result.status == "ok"
    updated = fx.task_repository.get(task.id)
    assert updated.last_successful_total_amount == 0
    assert fx.provider.requested_plates == ["AA001AA", "AA001AA"]
    assert fx.sleep_log.calls == [5.0]


# ---- 2. previous=200, first=0, second=200 -> final=200 ----

async def test_false_zero_corrected_back_to_same_amount(tmp_path):
    fx = _Fixture(tmp_path)
    task = fx.make_task("P004XC163")
    task = await fx.seed_positive_amount(task, amount=200, fingerprint="fp-original")

    fx.provider.script(
        task.car_number,
        [[], [_fine(car_number=task.car_number, fingerprint="fp-original", amount=200)]],
    )
    result = await fx.check_service.check_task(task)

    assert result.status == "ok"
    updated = fx.task_repository.get(task.id)
    assert updated.last_successful_total_amount == 200
    assert fx.provider.requested_plates == ["P004XC163", "P004XC163"]


# ---- 3. previous=200, first=0, second=150 -> final=150 ----

async def test_false_zero_corrected_to_different_positive_amount(tmp_path):
    fx = _Fixture(tmp_path)
    task = fx.make_task("BB002BB")
    task = await fx.seed_positive_amount(task, amount=200, fingerprint="fp-a")

    fx.provider.script(
        task.car_number,
        [[], [_fine(car_number=task.car_number, fingerprint="fp-b", amount=150)]],
    )
    result = await fx.check_service.check_task(task)

    assert result.status == "ok"
    updated = fx.task_repository.get(task.id)
    assert updated.last_successful_total_amount == 150


# ---- 4. previous=200, first=0, second=ERROR -> previous 200 preserved ----

async def test_confirmation_provider_error_preserves_previous_amount(tmp_path):
    fx = _Fixture(tmp_path)
    task = fx.make_task("CC003CC")
    task = await fx.seed_positive_amount(task, amount=200)
    previous_checked_at = fx.task_repository.get(task.id).last_successful_checked_at

    fx.provider.script(task.car_number, [[], FineProviderError("police.ge timeout")])
    result = await fx.check_service.check_task(task)

    assert result.status == "error"
    updated = fx.task_repository.get(task.id)
    assert updated.last_successful_total_amount == 200
    assert updated.last_successful_checked_at == previous_checked_at
    assert updated.last_check_status == "error"


# ---- 5. previous=200, first=0, second=parse error -> previous 200 preserved ----

async def test_confirmation_parse_error_preserves_previous_amount(tmp_path):
    """FineParseError самого parser.py конвертируется в FineProviderError
    на уровне PoliceGeProvider (см. reader/fines/police_ge_provider.py) —
    к моменту, когда его видит FineCheckService, это уже FineProviderError,
    поэтому здесь используется тот же тип исключения, что и в тесте 4, но
    отдельно, т.к. задача явно требует покрыть оба сценария по отдельности."""
    fx = _Fixture(tmp_path)
    task = fx.make_task("DD004DD")
    task = await fx.seed_positive_amount(task, amount=200)

    fx.provider.script(
        task.car_number, [[], FineProviderError("Некорректный формат ответа police.ge")],
    )
    result = await fx.check_service.check_task(task)

    assert result.status == "error"
    updated = fx.task_repository.get(task.id)
    assert updated.last_successful_total_amount == 200


# ---- 6. previous=200, first=200 -> only ONE provider request ----

async def test_nonzero_first_result_needs_no_confirmation(tmp_path):
    fx = _Fixture(tmp_path)
    task = fx.make_task("EE005EE")
    task = await fx.seed_positive_amount(task, amount=200, fingerprint="fp-x")

    fx.provider.script(
        task.car_number, [[_fine(car_number=task.car_number, fingerprint="fp-x", amount=200)]],
    )
    result = await fx.check_service.check_task(task)

    assert result.status == "ok"
    assert fx.provider.requested_plates == ["EE005EE"]
    assert fx.sleep_log.calls == []


# ---- 7. previous=0, first=0 -> only ONE provider request ----

async def test_previous_zero_first_zero_needs_no_confirmation(tmp_path):
    fx = _Fixture(tmp_path)
    task = fx.make_task("FF006FF")
    # Seed genuinely-zero previous state (empty results, not a zero-amount
    # fine record) — first-ever check, previous=None here so no confirmation
    # triggers regardless.
    fx.provider.script(task.car_number, [[]])
    seeded = await fx.check_service.check_task(task)
    assert seeded.status == "ok"
    task = fx.task_repository.get(task.id)
    assert task.last_successful_total_amount == 0
    fx.provider.requested_plates.clear()

    fx.provider.script(task.car_number, [[]])
    result = await fx.check_service.check_task(task)

    assert result.status == "ok"
    assert fx.provider.requested_plates == ["FF006FF"]
    assert fx.sleep_log.calls == []


# ---- 8. previous=NULL, first=0 -> only ONE provider request ----

async def test_previous_null_first_zero_needs_no_confirmation(tmp_path):
    fx = _Fixture(tmp_path)
    task = fx.make_task("GG007GG")
    assert task.last_successful_total_amount is None

    fx.provider.script(task.car_number, [[]])
    result = await fx.check_service.check_task(task)

    assert result.status == "ok"
    assert fx.provider.requested_plates == ["GG007GG"]
    assert fx.sleep_log.calls == []
    updated = fx.task_repository.get(task.id)
    assert updated.last_successful_total_amount == 0


# ---- 11/12: confirmation не дублирует detected_fines/notifications ----

async def test_confirmation_creates_detected_fine_exactly_once(tmp_path):
    fx = _Fixture(tmp_path)
    task = fx.make_task("HH008HH")
    task = await fx.seed_positive_amount(task, amount=200, fingerprint="fp-known")

    # confirmation находит ОДИН уже известный (fp-known) + ОДИН новый (fp-new)
    fx.provider.script(
        task.car_number,
        [
            [],
            [
                _fine(car_number=task.car_number, fingerprint="fp-known", amount=200),
                _fine(car_number=task.car_number, fingerprint="fp-new", amount=50),
            ],
        ],
    )
    result = await fx.check_service.check_task(task)

    assert result.status == "ok"
    # Ровно ОДИН новый штраф (fp-new) — fp-known уже существовал, НЕ создан
    # повторно ни на первой (пустой) попытке, ни на confirmation.
    assert len(result.new_fines) == 1
    assert result.new_fines[0].external_fine_id == "fp-new"

    rows = fx.detected_fine_repository.get_by_fingerprint(task.id, "fp-known")
    assert rows is not None
    all_new = fx.detected_fine_repository.get_by_fingerprint(task.id, "fp-new")
    assert all_new is not None

    updated = fx.task_repository.get(task.id)
    assert updated.last_successful_total_amount == 250


async def test_confirmation_does_not_mark_new_fine_as_seen_twice(tmp_path):
    """current_fines/new_fines приходят из ОДНОГО прохода _finalize() над
    ФИНАЛЬНЫМ (confirmation) набором records — первая (пустая) попытка
    физически не могла ничего создать (пустой список, пустой for-loop),
    поэтому дублирования "уведомления" (== новой detected_fines-строки с
    notification_sent_at IS NULL) быть не может."""
    fx = _Fixture(tmp_path)
    task = fx.make_task("II009II")
    task = await fx.seed_positive_amount(task, amount=200, fingerprint="fp-known")

    fx.provider.script(
        task.car_number,
        [[], [_fine(car_number=task.car_number, fingerprint="fp-fresh", amount=999)]],
    )
    result = await fx.check_service.check_task(task)

    assert len(result.new_fines) == 1
    fresh = fx.detected_fine_repository.get_by_fingerprint(task.id, "fp-fresh")
    assert fresh is not None
    assert fresh.notification_sent_at is None  # обычная semantics, не тронута confirmation


# ---- 13/14: DebtRefreshService — последовательно + 5s между РАЗНЫМИ машинами ----

async def test_debt_refresh_checks_cars_sequentially_with_delay_between_them(tmp_path):
    fx = _Fixture(tmp_path)
    task_a = fx.make_task("JJ010JJ")
    task_a = await fx.seed_positive_amount(task_a, amount=100, fingerprint="fp-a")
    task_b = fx.make_task("KK011KK")
    task_b = await fx.seed_positive_amount(task_b, amount=200, fingerprint="fp-b")

    fx.provider.script(task_a.car_number, [[_fine(car_number=task_a.car_number, fingerprint="fp-a", amount=100)]])
    fx.provider.script(task_b.car_number, [[_fine(car_number=task_b.car_number, fingerprint="fp-b", amount=200)]])

    outcome = await fx.debt_refresh_service.refresh()

    assert outcome.checked == 2
    assert outcome.failed == 0
    # Ровно один provider-запрос на машину (никакой confirmation не нужен —
    # оба сразу вернули свой прежний положительный total) + РОВНО одна
    # пауза МЕЖДУ ними (не до первой, не после последней).
    assert len(fx.provider.requested_plates) == 2
    assert fx.sleep_log.calls == [5.0]


async def test_debt_refresh_false_zero_confirmation_delay_is_separate_from_inter_car_delay(tmp_path):
    """Одна машина требует confirmation (previous>0, first=0) — её
    собственная 5s-пауза И межмашинная 5s-пауза — это ДВА разных вызова
    sleep, оба должны произойти."""
    fx = _Fixture(tmp_path)
    task_a = fx.make_task("LL012LL")
    task_a = await fx.seed_positive_amount(task_a, amount=100, fingerprint="fp-a")
    task_b = fx.make_task("MM013MM")
    task_b = await fx.seed_positive_amount(task_b, amount=200, fingerprint="fp-b")

    # task_a получает false zero, подтверждается тем же значением.
    fx.provider.script(
        task_a.car_number,
        [[], [_fine(car_number=task_a.car_number, fingerprint="fp-a", amount=100)]],
    )
    fx.provider.script(task_b.car_number, [[_fine(car_number=task_b.car_number, fingerprint="fp-b", amount=200)]])

    outcome = await fx.debt_refresh_service.refresh()

    assert outcome.checked == 2
    assert outcome.failed == 0
    # confirmation delay (5.0) для car A + inter-car delay (5.0) между A и B.
    assert fx.sleep_log.calls == [5.0, 5.0]
    updated_a = fx.task_repository.get(task_a.id)
    assert updated_a.last_successful_total_amount == 100


# ---- 16. ERROR (обычный, без confirmation) никогда не заменяет previous ----

async def test_plain_error_without_confirmation_preserves_previous_amount(tmp_path):
    fx = _Fixture(tmp_path)
    task = fx.make_task("NN014NN")
    task = await fx.seed_positive_amount(task, amount=75)

    fx.provider.script(task.car_number, [FineProviderError("network down")])
    result = await fx.check_service.check_task(task)

    assert result.status == "error"
    updated = fx.task_repository.get(task.id)
    assert updated.last_successful_total_amount == 75


# ---- P004XC163 regression (см. задачу п.11) ----

async def test_p004xc163_production_regression(tmp_path):
    """Точное воспроизведение production incident: previous=200 (4×50 ₾),
    mass refresh batch первым ответом получает [], confirmation находит
    все 4 штрафа снова — итоговое состояние ОБЯЗАНО остаться 200 ₾, машина
    остаётся в debt list."""
    fx = _Fixture(tmp_path)
    task = fx.make_task("P004XC163")

    fx.provider.script(
        task.car_number,
        [[
            _fine(car_number="P004XC163", fingerprint=f"fp-{i}", amount=50.0)
            for i in range(4)
        ]],
    )
    seeded = await fx.check_service.check_task(task)
    assert seeded.status == "ok"
    task = fx.task_repository.get(task.id)
    assert task.last_successful_total_amount == 200.0
    fx.provider.requested_plates.clear()
    fx.sleep_log.calls.clear()

    # Mass refresh batch: первый ответ — false empty; confirmation находит
    # реальные 4 штрафа снова.
    fx.provider.script(
        task.car_number,
        [
            [],
            [_fine(car_number="P004XC163", fingerprint=f"fp-{i}", amount=50.0) for i in range(4)],
        ],
    )
    outcome = await fx.debt_refresh_service.refresh()

    assert outcome.checked == 1
    assert outcome.failed == 0
    final_task = fx.task_repository.get(task.id)
    assert final_task.last_successful_total_amount == 200.0

    debt_rows = fx.task_repository.list_tasks_with_known_debt()
    assert any(row.task_id == task.id and row.total_amount == 200.0 for row in debt_rows)
