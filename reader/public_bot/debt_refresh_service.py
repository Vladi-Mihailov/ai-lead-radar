"""DebtRefreshService — trusted-manager "🔄 Обновить задолженности"
(@ProtocolGEbot). НИЧЕГО не знает про Telegram — только про
FineMonitoringTaskRepository/DetectedFineRepository/FineCheckService.

ВАЖНО (см. задачу): police.ge не отслеживает оплату штрафа (см.
reader/fines/payment_status.py — research-only, не подключён к production),
поэтому "задолженность" здесь = TaskFineTotal.total_amount = сумма ВСЕХ
когда-либо обнаруженных detected_fines задачи, НЕ за вычетом оплаченных.
Это осознанно принятая (см. AskUserQuestion в рамках этой задачи) семантика
"известные штрафы", а не "текущий долг" — total_amount монотонно не
убывает: обновление может его увеличить (найден новый штраф) или оставить
без изменений, но никогда не обнулить и не уменьшить, поэтому машина
никогда не "исчезает из списка" в результате refresh (в отличие от
изначально предполагавшейся, но архитектурно невозможной при текущих
данных семантики "оплачено -> пропало").

Отсутствие уведомлений пользователю — АРХИТЕКТУРНОЕ свойство, а не
дисциплина вызывающего кода: этот класс не имеет ссылки ни на
NotificationService, ни на FineNotificationCoordinator, поэтому физически
не может ничего разослать. Реально новый штраф, найденный во время
refresh, останется в detected_fines с notification_sent_at IS NULL и будет
доставлен ОБЫЧНЫМ следующим проходом FineJob.flush_pending() — то же самое
поведение, что и у уже существующего SubscriptionService.check_now()/
check_now_task() (см. design report), не новый случай.
"""

from dataclasses import dataclass, field
from datetime import datetime

from reader.fines.check_service import FineCheckService
from reader.fines.detected_fine_repository import DetectedFineRepository
from reader.fines.models import TaskFineTotal
from reader.fines.task_repository import FineMonitoringTaskRepository


@dataclass(frozen=True)
class DebtSummary:
    car_count: int
    total_amount: float


@dataclass(frozen=True)
class DebtDisplayRow:
    """Одна строка itemized-списка "🚨 Известные штрафы" (задача:
    "Нужен полный itemized список") — owner_display НЕ входит сюда: он
    строится вызывающим кодом (ConversationController), у которого есть
    SubscriptionService/UserRepository, а DebtRefreshService намеренно их
    не знает (см. модульный докстрок)."""

    task_id: int
    car_number: str
    total_amount: float
    checked_at: datetime


@dataclass(frozen=True)
class RefreshOutcome:
    """Итог одного прогона refresh() — см. задачу п.7 "manager summary".
    "increased" — по этой задаче найдена БОЛЬШАЯ известная сумма, чем была
    до проверки (появился новый штраф). "unchanged" — сумма не изменилась;
    это НЕ значит "штраф оплачен" (см. модульный докстрок), только что
    новых штрафов на этой проверке не нашлось — текст manager summary
    формулирует это честно (см. reader/public_bot/texts.py), а не как
    "✅ Оплачено"."""

    checked: int
    increased: int
    unchanged: int
    failed: int
    failed_car_numbers: tuple[str, ...] = field(default_factory=tuple)


class RefreshAlreadyInProgressError(Exception):
    """См. задачу п.11 — защита от повторного/параллельного нажатия.
    In-memory флаг (см. _in_progress) — тот же осознанно простой приём, что
    и FineJob._last_run_slot (только в памяти процесса, без persistent
    lock): у @ProtocolGEbot один процесс/один event loop, второй запуск
    поверх уже выполняющегося физически может прийти только вторым
    нажатием той же кнопки в том же процессе."""


class DebtRefreshService:
    def __init__(
        self,
        task_repository: FineMonitoringTaskRepository,
        detected_fine_repository: DetectedFineRepository,
        check_service: FineCheckService,
    ):
        self._task_repository = task_repository
        self._detected_fines = detected_fine_repository
        self._check_service = check_service
        self._in_progress = False

    def is_in_progress(self) -> bool:
        return self._in_progress

    def list_debt_rows(self) -> list[TaskFineTotal]:
        """Только чтение — используется и для "🚨 Известные штрафы" в 📊
        Статистика, и для превью "Будет проверено автомобилей: N" перед
        refresh (см. задачу п.11 production smoke: "сначала показать
        количество, не жать массово") — ни одного provider/network запроса."""
        return self._detected_fines.list_task_totals()

    def get_debt_summary(self) -> DebtSummary:
        rows = self.list_debt_rows()
        return DebtSummary(
            car_count=len(rows), total_amount=sum(row.total_amount for row in rows),
        )

    def get_debt_rows_for_display(self) -> list[DebtDisplayRow]:
        """checked_at — last_successful_checked_at задачи (см.
        record_successful_check), а НЕ TaskFineTotal.last_seen_at: именно
        last_successful_checked_at переживает последующий ERROR (см.
        refresh()), поэтому это единственно честная "дата последней
        ДОСТОВЕРНОЙ проверки". Fallback на last_seen_at — ТОЛЬКО для
        задач, у которых ещё ни разу не было успешного прогона именно
        ЭТОЙ фичи (last_successful_checked_at ещё NULL, миграция задним
        числом не делалась) — best-effort "когда мы вообще в последний
        раз это видели", не выдаётся за подтверждённый refresh.

        Сортировка (см. задачу п.2): сумма DESC, при равенстве — более
        свежий checked_at выше. SQL в list_task_totals() уже сортирует по
        сумме, но не знает про last_successful_checked_at (это отдельная
        таблица) — досортировываем здесь в Python, набор задач с
        известными штрафами достаточно мал для этого."""
        display_rows = []
        for total in self.list_debt_rows():
            task = self._task_repository.get(total.task_id)
            checked_at = (
                task.last_successful_checked_at
                if task is not None and task.last_successful_checked_at is not None
                else total.last_seen_at
            )
            display_rows.append(
                DebtDisplayRow(
                    task_id=total.task_id, car_number=total.car_number,
                    total_amount=total.total_amount, checked_at=checked_at,
                )
            )
        display_rows.sort(key=lambda row: (-row.total_amount, -row.checked_at.timestamp()))
        return display_rows

    async def refresh(self) -> RefreshOutcome:
        """См. задачу п.1-6: только tasks с известной суммой > 0 (п.1-2),
        по одному police.ge-запросу на task_id независимо от числа
        subscribers (п.4 — list_task_totals() уже группирует по
        monitoring_task_id, дублирования здесь в принципе не может
        возникнуть), тот же authoritative FineCheckService.check_task()
        (п.3), без вызова flush_pending() (см. модульный докстрок про
        отсутствие уведомлений). ERROR не трогает detected_fines
        (check_task() сам это гарантирует, см. reader/fines/
        check_service.py) и не трогает last_successful_checked_at (просто
        не вызываем record_successful_check в этом случае) — "сохранить
        старое достоверное состояние" (п.6) получается ПО ПОСТРОЕНИЮ, без
        отдельной ветки отката."""
        if self._in_progress:
            raise RefreshAlreadyInProgressError()

        self._in_progress = True
        try:
            task_ids = [row.task_id for row in self.list_debt_rows()]

            increased = 0
            unchanged = 0
            failed = 0
            failed_car_numbers: list[str] = []

            for task_id in task_ids:
                task = self._task_repository.get(task_id)
                if task is None:
                    # Задача удалена конкурентно между чтением списка и
                    # проверкой — просто пропускаем, не считаем ни успехом,
                    # ни ошибкой (её больше нет вообще).
                    continue

                amount_before = self._detected_fines.sum_amount_for_task(task_id)
                result = await self._check_service.check_task(task)

                if result.status == "error":
                    failed += 1
                    failed_car_numbers.append(task.car_number)
                    continue

                self._task_repository.record_successful_check(task_id)
                amount_after = self._detected_fines.sum_amount_for_task(task_id)
                if amount_after > amount_before:
                    increased += 1
                else:
                    unchanged += 1

            return RefreshOutcome(
                checked=len(task_ids), increased=increased, unchanged=unchanged,
                failed=failed, failed_car_numbers=tuple(failed_car_numbers),
            )
        finally:
            self._in_progress = False
