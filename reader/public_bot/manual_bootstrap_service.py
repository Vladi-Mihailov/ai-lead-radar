"""ВРЕМЕННЫЙ manager workflow для новых Georgia-номеров (см. задачу
"ВРЕМЕННЫЙ manager workflow для Georgia fines bot") — пока production VPS
не имеет сетевого доступа к police.ge, менеджер вручную проверяет новую
машину в браузере и вносит результат сюда вместо живого searchByAuto.

Намеренно НЕ отдельная модель/хранилище штрафов: "Добавить штраф" проходит
через ТОТ ЖЕ FineCheckService.persist_feed_match()/DetectedFineRepository,
что и публичный feed (см. reader/jobs/georgia_public_feed_job.py) — тот же
fingerprint/dedup/notification pipeline, без параллельной реализации.

Процесс (см. reader/public_bot/main.py) НИКОГДА не вызывает
FineNotificationCoordinator.flush_pending() сам — ТОТ ЖЕ принцип, что и у
SubscriptionService.add_car()/check_now() (см. их докстроки): это отдельный
от основного процесса бот (ai-lead-radar-geshtrafbot.service), доставка
клиенту уходит через NotificationFlushJob основного процесса в течение
~30 секунд, без нового кода доставки здесь.

Известное ограничение (сознательно принятое, не недосмотр): compute_
fingerprint() хэширует СЫРОЕ (нетипизированное) значение суммы — реальный
feed отдаёт protocolAmount как голое число (см. tests/
test_georgia_public_feed_job.py/test_police_ge_parser.py, напр. 100, не
"100"). Чтобы ПОЗЖЕ обнаруженный тот же штраф в публичном feed'е дал ТОТ ЖЕ
fingerprint (см. требование "PUBLIC FEED INTERACTION"), сумма, введённая
менеджером, парсится как int, если это целое число (как и реальные
protocolAmount в известных примерах), иначе как float — НЕ как str() от
введённого текста. Это снижает, но не гарантирует полностью, совпадение
fingerprint'ов для одного и того же реального штрафа, если police.ge
когда-либо вернёт дробную сумму, которую менеджер ввёл в другом виде —
намеренно задокументированный остаточный риск этого временного fallback'а,
а не повод городить второй, параллельный dedup-путь "по одному protocolNo"
в обход canonical fingerprint-логики."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

_EPOCH_UTC = datetime(1970, 1, 1, tzinfo=timezone.utc)

from reader.fines.check_service import FineCheckService
from reader.fines.detected_fine_repository import DetectedFineRepository
from reader.fines.models import FineMonitoringTask, ParsedFineRecord
from reader.fines.parser import compute_fingerprint
from reader.fines.plate_bootstrap_repository import (
    GeorgiaPlateBootstrapRepository,
    PlateBootstrapState,
)
from reader.fines.task_repository import FineMonitoringTaskRepository

# "Вручено" (НЕ "Не вручено") — менеджер прочитал штраф напрямую со
# страницы police.ge, то же соглашение, что и в parser.py::_parse_entry
# (delivered_date is not None -> "Вручено").
_DELIVERED_STATUS = "Вручено"


def parse_manual_amount(text: str) -> int | float | None:
    """Голое число (int, если без дробной части — см. модульный докстрок
    про protocolAmount), иначе float; None, если текст вообще не число."""
    cleaned = text.strip().replace(",", ".")
    if not cleaned:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if value.is_integer():
        return int(value)
    return value


@dataclass(frozen=True)
class ManualFineSaveResult:
    """Результат одной попытки сохранить штраф вручную — car_number без
    задач (нет fine_monitoring_task) — saved=False, остальное пусто."""

    saved: bool
    duplicate: bool
    tasks_affected: int
    fingerprint: str | None = None


class ManualBootstrapService:
    """Единственная точка входа для ConversationController — оборачивает
    GeorgiaPlateBootstrapRepository/FineMonitoringTaskRepository/
    FineCheckService ровно так же, как остальные опциональные сервисы этого
    модуля (debt_refresh_service/protocol_check_provider/full_check_service,
    см. reader/public_bot/main.py) — None на уровне ConversationController
    означает "фичи нет в этой сборке", а не ошибку."""

    def __init__(
        self,
        plate_bootstrap_repository: GeorgiaPlateBootstrapRepository,
        task_repository: FineMonitoringTaskRepository,
        detected_fine_repository: DetectedFineRepository,
        check_service: FineCheckService,
    ):
        self._bootstrap = plate_bootstrap_repository
        self._tasks = task_repository
        self._detected_fines = detected_fine_repository
        self._check_service = check_service

    def get_status(self, car_number: str) -> PlateBootstrapState | None:
        return self._bootstrap.get(car_number)

    def list_pending(self) -> list[str]:
        return self._bootstrap.list_pending()

    def tasks_for_plate(self, car_number: str) -> list[FineMonitoringTask]:
        """Fan-out (см. модульный докстрок + производственный случай
        O528HY702 — одна физическая машина, несколько активных задач)."""
        return [task for task in self._tasks.list_active() if task.car_number == car_number]

    def count_fines_for_plate(self, car_number: str) -> int:
        """Для "✅ Первичная проверка завершена / Добавлено штрафов: N" (см.
        задачу п.11) — ВСЕ известные detected_fines для задач этого
        физического номера (не только добавленные именно в рамках этого
        разговора — тот же, более полезный смысл "сколько штрафов сейчас
        числится за машиной"). since=эпоха — трюк без отдельного count-
        метода в DetectedFineRepository: list_current_for_task фильтрует
        по last_seen_at >= since, а любая реальная строка новее 1970 года."""
        tasks = self.tasks_for_plate(car_number)
        total = 0
        for task in tasks:
            total += len(self._detected_fines.list_current_for_task(task.id, since=_EPOCH_UTC))
        return total

    def confirm_no_fines(self, car_number: str, *, completed_by_user_id: int) -> bool:
        """"✅ Штрафов нет" (см. задачу п.4) — НЕ создаёт никакой fake/
        synthetic detected_fines строки (см. задачу: "НЕ записывать
        synthetic zero fine") — только помечает bootstrap завершённым.
        False, если plate уже completed (идемпотентность, см. задачу п.13/
        16 "CONCURRENT MANAGERS"/"completed plate... idempotent") — другой
        менеджер (или этот же менеджер повторно) не переписывает
        completed_by_user_id/completed_at заново."""
        state = self._bootstrap.get(car_number)
        if state is not None and state.is_completed:
            return False
        self._bootstrap.mark_completed(
            car_number, completed_at=datetime.now(timezone.utc), completed_by_user_id=completed_by_user_id,
        )
        return True

    def finish_after_fines(self, car_number: str, *, completed_by_user_id: int) -> bool:
        """"✅ Завершить проверку" после одного или нескольких "➕ Добавить
        штраф" (см. задачу п.11) — ТА ЖЕ bootstrap-completion семантика,
        что и confirm_no_fines() (тот же idempotent-guard), отдельное имя
        только ради ясности на call site."""
        return self.confirm_no_fines(car_number, completed_by_user_id=completed_by_user_id)

    async def save_manual_fine(
        self,
        car_number: str,
        *,
        protocol_no: str,
        amount: float,
        violation_date: date,
        place: str | None,
        article: str | None,
    ) -> ManualFineSaveResult:
        """Canonical persistence path — см. модульный докстрок: один
        ParsedFineRecord, persist_feed_match() на КАЖДУЮ задачу этого
        физического номера (fan-out), ТОТ ЖЕ fingerprint/dedup/notification-
        pipeline, что и у публичного feed'а. due_date/penalty_date=None —
        reader/notifications/telegram_notification_service.py::
        format_fine_block показывает "Дата нарушения" из violation_date,
        когда она задана (ВСЕГДА задана здесь), и не обращается к
        penalty_date в этом случае вовсе; due_date просто не показывается,
        если отсутствует (см. его "if due_date:" guard) — ничего не
        ломается, менеджера не заставляем вводить поля, которые нигде не
        отображаются."""
        tasks = self.tasks_for_plate(car_number)
        if not tasks:
            return ManualFineSaveResult(saved=False, duplicate=False, tasks_affected=0)

        fingerprint = compute_fingerprint(
            external_fine_id=protocol_no, violation_date=violation_date, amount=amount,
        )
        # Дубликат-проверка (см. задачу п.9) — по ПЕРВОЙ задаче физического
        # номера как представителю: fan-out создаёт/находит ОДИНАКОВЫЙ
        # fingerprint независимо для каждой задачи (persist_feed_match сам
        # дедуплицирует per-task через DetectedFineRepository.
        # get_by_fingerprint), так что "уже существует" для одной задачи
        # этого номера означает "уже существует" для всех остальных тоже —
        # повторный manager-ввод того же protocolNo для той же машины
        # считается одним и тем же событием, а не N независимыми.
        already_existed = self._detected_fines.get_by_fingerprint(tasks[0].id, fingerprint) is not None

        record = ParsedFineRecord(
            car_number=car_number,
            external_fine_id=protocol_no,
            penalty_date=None,
            due_date=None,
            delivered_status=_DELIVERED_STATUS,
            fingerprint=fingerprint,
            raw_data={
                "source": "manual_manager",
                "protocolNo": protocol_no,
                "protocolAmount": amount,
                "violationDate": violation_date.isoformat(),
            },
            violation_date=violation_date,
            amount=float(amount),
            place=place,
            violation_description=article,
        )

        for task in tasks:
            await self._check_service.persist_feed_match(task, record)

        return ManualFineSaveResult(
            saved=True, duplicate=already_existed, tasks_affected=len(tasks), fingerprint=fingerprint,
        )
