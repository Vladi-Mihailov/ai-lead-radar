"""Бизнес-логика проверки одной задачи мониторинга штрафов. Ничего не знает
про Telegram/Scheduler/CommandDispatcher/конкретный сайт-источник — только
FineProvider (интерфейс) и оба Repository. Сам никогда не отправляет
уведомления (это ответственность FineNotificationCoordinator, используется
FineJob и FineCommand) — каждая новая detected_fines-строка рождается с
notification_sent_at=NULL, независимо от того, какой вызывающий код привёл
к её созданию (мониторинг, manual "Проверить сейчас", Add Car, manager
mass refresh или "fine check-all") — все штрафы попадают в единый
FineNotificationCoordinator.flush_pending() одинаково (см. задачу "Fix
Georgia fine notifications": ранее здесь существовал notification_policy
с веткой "silent", которой "fine check-all" подавлял именно эту
доставку — этот механизм удалён как единственный известный gap в
notification pipeline).

Перевод грузинского place/violation_description на русский (см.
reader/fines/translation.py) — тоже единственная точка: и background
(FineJob/ClientFineJob/archive), и manual "Проверить сейчас"
(SubscriptionService.check_now/check_now_task), и Add Car flow — все
вызывают ИСКЛЮЧИТЕЛЬНО check_task(), поэтому перевод/backfill/кеширование
реализованы один раз здесь, а не в каждом из этих caller'ов отдельно.
"""

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Protocol

from reader.fines.detected_fine_repository import DetectedFineRepository
from reader.fines.models import (
    CheckResult,
    FineMonitoringTask,
    NewFineEvent,
    ParsedFineRecord,
)
from reader.fines.provider import FineProvider, FineProviderError, PoliceGeUnavailableError
from reader.fines.suppression import is_fine_output_suppressed
from reader.fines.task_repository import FineMonitoringTaskRepository
from reader.fines.translation import (
    FineTranslationError,
    TranslatedFineText,
    contains_georgian,
)

logger = logging.getLogger(__name__)

# "false-zero" guard (см. задачу "guard Georgia debt against false zero
# results") — сколько ждать перед повторным provider-запросом, когда
# предыдущая ЗАДОКУМЕНТИРОВАННАЯ задолженность > 0, а эта проверка внезапно
# вернула 0 — police.ge подтверждённо может отдать технически валидный
# success:true с пустым/усечённым results под нагрузкой (см. production
# incident: mass refresh уничтожил реальную задолженность P004XC163 и ещё
# 17 машин false-empty ответом). НЕ настраивается через config — это
# защитный технический таймаут одного provider-запроса, а не бизнес-
# параметр расписания.
_ZERO_CONFIRMATION_DELAY_SECONDS = 5.0


def _elapsed_ms(started_at: float) -> int:
    return round((time.monotonic() - started_at) * 1000)


def _records_total(records: list[ParsedFineRecord]) -> float:
    """COALESCE(amount, 0) — тот же принцип, что и везде в проекте (штраф без
    распознанной суммы не должен ронять проверку, см. reader/fines/parser.py)."""
    return sum((record.amount or 0.0) for record in records)


def _format_amount(value: float) -> str:
    return f"{value:g}"


def _pick_freshest_task(tasks: list[FineMonitoringTask]) -> FineMonitoringTask:
    """См. FineCheckService.check_plate_for_tasks — та же freshness/tie-
    break семантика, что и FineMonitoringTaskRepository.
    list_debt_car_groups() (max last_successful_checked_at, tie-break —
    БОЛЬШИЙ task.id), применённая к полным FineMonitoringTask вместо
    FineTaskDebtSnapshot (это ОТДЕЛЬНЫЕ данные — group listing читается
    ДО refresh, полные task-объекты запрашиваются ВНУТРИ refresh — но
    правило выбора "самого свежего" должно быть идентичным)."""
    return max(
        tasks,
        key=lambda t: (t.last_successful_checked_at or datetime.min.replace(tzinfo=timezone.utc), t.id),
    )


class FineTranslatorLike(Protocol):
    """Ровно то, что нужно отсюда от FineTranslationService (см.
    reader/fines/translation.py) — тот же приём Protocol, что и везде в
    проекте (BotMessageSenderLike/UserLookupLike и т.п.), чтобы тесты
    могли подменить перевод лёгким фейком без реального OpenAI клиента."""

    async def translate(
        self, *, place: str | None, violation_description: str | None,
    ) -> TranslatedFineText: ...


class FineCheckService:
    def __init__(
        self,
        provider: FineProvider,
        task_repository: FineMonitoringTaskRepository,
        detected_fine_repository: DetectedFineRepository,
        translator: FineTranslatorLike | None = None,
        *,
        zero_confirmation_delay_seconds: float = _ZERO_CONFIRMATION_DELAY_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._provider = provider
        self._task_repository = task_repository
        self._detected_fine_repository = detected_fine_repository
        # None — как и everywhere в проекте (см. UserRepository|None в
        # FineNotificationCoordinator) — означает "функциональность
        # недоступна" (см. задачу: нет OPENAI_API_KEY -> клиент видит
        # оригинальный грузинский текст вместо перевода), а не ошибку.
        self._translator = translator
        # False-zero guard (см. модульный докстрок про
        # _ZERO_CONFIRMATION_DELAY_SECONDS) — sleep инжектируется (тот же
        # приём, что и остальные Protocol-зависимости в проекте), чтобы
        # тесты могли подменить его лёгким фейком и не ждать реальные 5
        # секунд (см. тесты test_false_zero_confirmation.py).
        self._zero_confirmation_delay_seconds = zero_confirmation_delay_seconds
        self._sleep = sleep

    def provider_available(self) -> bool:
        """См. задачу "back off when police.ge is unavailable" — тонкий
        pass-through к FineProvider.is_available() (True по умолчанию для
        провайдеров без circuit breaker). Используется ИСКЛЮЧИТЕЛЬНО
        scheduled-джобами (FineJob/ArchiveFineJob) ЗАРАНЕЕ, чтобы
        пропустить батч целиком одним логом, а не вызывать check_task()
        для каждой задачи по отдельности, когда источник уже точно
        недоступен (см. задачу п.4: "do not generate hundreds of
        identical errors") — check_task() сам по себе остаётся
        единственным authoritative путём проверки, этот метод НЕ
        заменяет его собственную circuit-breaker проверку внутри
        PoliceGeSession.search_by_plate()."""
        return self._provider.is_available()

    async def check_task_or_read_cached(self, task: FineMonitoringTask) -> CheckResult:
        """Emergency Check Now / Add Car immediate check (см. задачу "back
        off when police.ge is unavailable" + "IMPORTANT PRODUCT DECISION —
        CHANGE PREVIOUS REQUIREMENT": "НЕ показываем пользователю никаких
        предупреждений о недоступности police.ge") — ЕДИНСТВЕННАЯ точка,
        где manual Check Now (SubscriptionService.check_now/check_now_task)
        и self-service Add Car (add_car) решают, звать ли реальный
        check_task() или читать последнее достоверное состояние из нашей
        БД. Circuit breaker открыт ДО вызова (provider_available()=False)
        ИЛИ открылся прямо во время ЭТОГО конкретного запроса (редкий
        TOCTOU race — check_task() сам вернул
        CheckResult.provider_unavailable=True) — в обоих случаях результат
        ОДИНАКОВЫЙ: read_last_known_state(), а не "попробуйте позже"/
        disclaimer (CheckResult.provider_unavailable остаётся полем
        CheckResult/используется FineJob/ArchiveFineJob'ом — ЭТОТ метод
        просто гарантирует, что ни один вызывающий его UI-путь никогда не
        увидит provider_unavailable=True наружу).

        Полностью READ-ONLY в ветке read_last_known_state (см. её докстрок)
        — ничего не пишет сверх того, что уже написал бы обычный
        check_task() в успешном случае."""
        if not self.provider_available():
            return self._read_last_known_state(task)

        result = await self.check_task(task)
        if result.provider_unavailable:
            return self._read_last_known_state(task)
        return result

    def _read_last_known_state(self, task: FineMonitoringTask) -> CheckResult:
        """"Текущее состояние по нашей БД" — см. задачу "CAN IDENTIFY
        CURRENT/UNPAID FINES": detected_fines-строки задачи, чей
        last_seen_at не старше last_successful_checked_at ПОСЛЕДНЕЙ
        успешной проверки (см. DetectedFineRepository.list_current_for_
        task докстрок) — ровно то, что police.ge вернул В ПОСЛЕДНИЙ раз,
        когда он ещё отвечал. Задача свежая (перечитана из репозитория, а
        не доверяет переданному объекту — тот же приём, что и
        _zero_needs_confirmation).

        last_successful_checked_at is None (задача ни разу не прошла
        успешную проверку, например только что создана Add Car прямо
        во время outage) — current_fines=[] (см. задачу: "не выдавать
        historical detected_fines-историю за текущее состояние" —
        единственный честный fallback без единого достоверного снимка).
        Сознательно НЕ "error"/provider_unavailable=True — see задача:
        "НЕ показываем пользователю никаких предупреждений", 0 известных
        текущих штрафов показывается как обычное "штрафов не найдено".

        НИКАКИХ записей в БД: ни mark_seen, ни record_successful_check, ни
        create(), ни mark_notified — new_fines ВСЕГДА пуст (см. задачу
        "Emergency Check Now остаётся READ-ONLY") — current_fines ТОЛЬКО
        для отображения, уведомление оператору/клиенту НЕ запускается (оно
        уже было запущено, когда эти штрафы были обнаружены по-настоящему,
        см. FineNotificationCoordinator)."""
        fresh_task = self._task_repository.get(task.id) or task
        if fresh_task.last_successful_checked_at is None:
            rows = []
        else:
            rows = self._detected_fine_repository.list_current_for_task(
                fresh_task.id, since=fresh_task.last_successful_checked_at,
            )
        current_fines = [
            NewFineEvent.from_detected_fine(row, label=fresh_task.label) for row in rows
        ]
        return CheckResult(
            status="ok",
            new_fines=[],
            current_fines=current_fines,
            error_message=None,
            total_fines_found=len(current_fines),
            duration_ms=0,
        )

    async def _resolve_translation(
        self,
        record: ParsedFineRecord,
        *,
        existing_place_ru: str | None,
        existing_violation_description_ru: str | None,
    ) -> tuple[str | None, str | None]:
        """Итоговые (place_ru, violation_description_ru) для ЭТОГО
        прохода — уже переведённое из кеша БД, свежепереведённое, или то
        же самое (не грузинский текст/уже кешировано/перевод недоступен —
        см. TranslatedFineText/FineTranslationError). Один и тот же
        результат идёт и в DetectedFineRepository.create()/mark_seen()
        (backfill), и в NewFineEvent для manual check — вычисляется
        ровно один раз на штраф за проверку, ОДНИМ запросом на оба поля,
        когда оба нуждаются в переводе (см. задачу)."""
        if self._translator is None:
            return existing_place_ru, existing_violation_description_ru

        needs_place = existing_place_ru is None and contains_georgian(record.place)
        needs_description = (
            existing_violation_description_ru is None
            and contains_georgian(record.violation_description)
        )
        if not needs_place and not needs_description:
            return existing_place_ru, existing_violation_description_ru

        try:
            result = await self._translator.translate(
                place=record.place if needs_place else None,
                violation_description=(
                    record.violation_description if needs_description else None
                ),
            )
        except FineTranslationError:
            # Fail-open (см. задачу: "Ошибка translation API никогда не
            # должна ломать мониторинг штрафов") — НЕ логируем сам
            # грузинский текст (место/описание нарушения могут быть
            # специфичны для конкретного случая) здесь и внутри
            # FineTranslationService.translate уже залогирован факт сбоя.
            # Fallback — оставить то, что уже было (для новой строки —
            # None, format_fine_block() тогда покажет оригинал вместо
            # перевода, а не пустую строку/ошибку).
            logger.warning(
                "fine translation недоступен для car_number=%s — "
                "показываем оригинальный текст, мониторинг продолжается",
                record.car_number,
            )
            return existing_place_ru, existing_violation_description_ru

        return (
            result.place_ru if needs_place else existing_place_ru,
            result.violation_description_ru if needs_description else existing_violation_description_ru,
        )

    async def check_task(self, task: FineMonitoringTask) -> CheckResult:
        """Единственный authoritative check path (см. задачу "manager
        Statistics / refresh для обоих ботов" п.11 и задачу "guard Georgia
        debt against false zero results") — вызывается мониторингом,
        manual "Проверить сейчас", Add Car, manager mass refresh и "fine
        check-all" одинаково, поэтому false-zero guard ниже защищает ВСЕ
        эти пути разом, без отдельной реализации в каждом из них.

        False-zero guard: police.ge подтверждённо может вернуть технически
        валидный success:true с пустым/усечённым results под нагрузкой (см.
        production incident — mass refresh уничтожил реальную задолженность
        нескольких машин, включая P004XC163, именно так). Если ДО этой
        проверки задолженность была известна положительной
        (task.last_successful_total_amount > 0), а ЭТА проверка вернула
        total_amount == 0 — ноль НЕ считается authoritative сразу: делаем
        ОДИН повторный provider-запрос (после паузы), и уже ЕГО результат
        становится финальным. Явно НЕ рекурсия в check_task() — повторный
        запрос идёт напрямую через self._provider (см. _fetch()), а
        persistence/detected_fines/notification-логика (см. _finalize())
        выполняется РОВНО ОДИН раз, над финальным набором records, кто бы
        их ни предоставил — первая или вторая попытка.

        Denylisted car_number (см. reader/fines/suppression.py) — ПЕРВАЯ
        проверка в методе, ДО провайдера, ДО false-zero guard и ДО snapshot:
        провайдер вообще не вызывается, task_repository/detected_fine_repository
        не трогаются вовсе — last_check_status/last_successful_*/detected_fines
        остаются РОВНО такими, какими были (см. задачу: "не изменять
        historical records без необходимости"). Это гарантирует ЛЮБОМУ
        вызывающему коду (мониторинг/manual/Add Car/manager refresh/
        операторские команды/fine check-all — см. check_plate_for_tasks()
        ниже про тот же guard для группового пути) одинаковое поведение без
        отдельной реализации в каждом из них."""
        started_at = time.monotonic()

        if is_fine_output_suppressed(task.car_number):
            return self._suppressed_result()

        try:
            records = await self._fetch_trusted(task.car_number, [task])
        except FineProviderError as exc:
            # Ошибка первого ИЛИ подтверждающего запроса — предыдущее
            # достоверное состояние (last_successful_*) остаётся
            # НЕТРОНУТЫМ: record_successful_check здесь не вызывается вовсе.
            return self._record_error(task, exc, started_at)

        # Snapshot принятого результата — ДО медленной обработки штрафов
        # (перевод/detected_fines), см. _persist_snapshot.
        self._persist_snapshot(task, records)
        return await self._finalize(task, records, started_at)

    async def check_plate_for_tasks(
        self, car_number: str, tasks: list[FineMonitoringTask],
    ) -> dict[int, CheckResult]:
        """См. задачу "Georgia debt deduplication by physical car_number" —
        когда НЕСКОЛЬКО fine_monitoring_tasks делят один и тот же
        car_number (production-находка: P004XC163, task 892 + task 1381),
        manager debt refresh должен сделать РОВНО ОДИН внешний
        police.ge-запрос (+ не более одного false-zero confirmation
        запроса, см. check_task() докстрок) на физический номер, а НЕ по
        одному на каждую задачу.

        Persistence/detected_fines/translation/notification-семантика для
        КАЖДОЙ задачи остаётся ПОЛНОСТЬЮ идентичной обычному check_task():
        _finalize() вызывается РОВНО ОДИН РАЗ НА ЗАДАЧУ (как и раньше),
        просто над уже ОБЩИМ, один раз полученным набором records, а не
        над независимо запрошенным для каждой задачи — ни одна задача не
        получает ни больше, ни меньше detected_fines-строк/уведомлений,
        чем при вызове check_task() по отдельности для КАЖДОЙ (см. задачу
        Part 6: "no duplicate/lost client notifications").

        False-zero guard (см. check_task() докстрок) применяется ОДИН раз
        для ВСЕЙ группы — "предыдущая известная задолженность" берётся у
        САМОЙ СВЕЖЕЙ задачи (см. _pick_freshest_task, та же freshness/
        tie-break семантика, что и list_debt_car_groups()), НЕ по каждой
        задаче отдельно (см. задачу Part 5: "must NOT independently
        perform the false-zero confirmation once per task").

        Provider ERROR (на любом из двух возможных запросов) — КАЖДАЯ
        задача группы получает СВОЙ error CheckResult (см. _record_error —
        last_successful_* остаётся нетронутым, per-task, как и раньше);
        успех — КАЖДАЯ задача получает результат _finalize() над одним и
        тем же records (см. задачу: "all relevant task rows for that
        plate receive consistent latest successful amount/state").

        Denylisted car_number (см. check_task() докстрок/reader/fines/
        suppression.py) — тот же guard, ДО провайдера, ОДИН раз для ВСЕЙ
        группы: КАЖДАЯ задача группы получает _suppressed_result(), ни один
        внешний запрос не выполняется. Используется DebtRefreshService.
        refresh() — но на практике не ожидается сработать там вовсе:
        list_debt_car_groups() (см. FineMonitoringTaskRepository) уже сам
        исключает denylisted car_number из кандидатов, см. докстрок
        list_tasks_with_known_debt(); guard здесь — страховка на случай
        будущего вызывающего кода, который решит передать car_number не
        через эту выборку."""
        started_at = time.monotonic()

        if is_fine_output_suppressed(car_number):
            return {task.id: self._suppressed_result() for task in tasks}

        try:
            records = await self._fetch_trusted(car_number, tasks)
        except FineProviderError as exc:
            return {task.id: self._record_error(task, exc, started_at) for task in tasks}

        # Snapshot для ВСЕЙ группы сразу — до медленной обработки штрафов
        # первой же задачи (иначе остальные задачи группы оставались бы со
        # старым snapshot всё время обработки предыдущих).
        for task in tasks:
            self._persist_snapshot(task, records)

        return {
            task.id: await self._finalize(task, records, started_at)
            for task in tasks
        }

    async def _fetch(self, car_number: str) -> list[ParsedFineRecord]:
        return await self._provider.search_by_plate(car_number)

    async def _fetch_trusted(
        self, car_number: str, tasks: list[FineMonitoringTask],
    ) -> list[ParsedFineRecord]:
        """Единственная реализация false-zero guard для check_task() и
        check_plate_for_tasks(): запрос к provider и, если результат —
        подозрительный ноль (см. _zero_needs_confirmation), ОДИН
        подтверждающий запрос после паузы; итоговый набор records — тот,
        что вернул последний запрос. FineProviderError любого из запросов
        поднимается — вызывающий код записывает error, НЕ трогая
        last_successful_*."""
        records = await self._fetch(car_number)
        first_count = len(records)
        total = _records_total(records)
        confirmation = False

        if total == 0 and self._zero_needs_confirmation(tasks):
            confirmation = True
            await self._sleep(self._zero_confirmation_delay_seconds)
            records = await self._fetch(car_number)
            logger.info(
                "Georgia zero confirmation plate=%s first_count=%d second_count=%d second_total=%s",
                car_number, first_count, len(records), _format_amount(_records_total(records)),
            )
            total = _records_total(records)

        logger.info(
            "Georgia fine check plate=%s result_count=%d total=%s zero_confirmation=%s",
            car_number, len(records), _format_amount(total), confirmation,
        )
        return records

    def _zero_needs_confirmation(self, tasks: list[FineMonitoringTask]) -> bool:
        """Нулевой результат НЕ принимается сразу, если есть положительное
        свидетельство долга — по СВЕЖЕМУ состоянию из БД, а не по объекту
        task в памяти (параллельная проверка того же номера могла только что
        сохранить положительный snapshot — production P099XO36):

        - есть сохранённый snapshot (last_successful_total_amount не NULL) —
          решает он, как и раньше: > 0 -> подтверждать; подтверждённый 0 —
          авторитетен (без повторного запроса на каждой проверке);
        - snapshot ещё не сохранён ни у одной задачи (новая задача, первая
          проверка ещё идёт) — подтверждать, если у задачи уже есть строки
          detected_fines. Только как "свидетельство", не как сумма долга.

        Для группы (check_plate_for_tasks) — snapshot самой свежей задачи
        (та же _pick_freshest_task-семантика), detected_fines — любой."""
        fresh = [self._task_repository.get(task.id) or task for task in tasks]
        with_snapshot = [task for task in fresh if task.last_successful_total_amount is not None]
        if with_snapshot:
            return _pick_freshest_task(with_snapshot).last_successful_total_amount > 0
        return any(self._detected_fine_repository.has_detected_fines(task.id) for task in fresh)

    def _persist_snapshot(self, task: FineMonitoringTask, records: list[ParsedFineRecord]) -> None:
        """"Что показала эта успешная проверка" — сумма ровно принятого
        набора records (после false-zero guard). Сохраняется СРАЗУ после
        получения результата, до перевода/detected_fines/уведомлений (см.
        _finalize): иначе всё время медленной обработки (минуты для
        машины с десятком штрафов) snapshot оставался старым/NULL, и
        параллельная проверка того же номера не могла распознать
        подозрительный ноль (production P099XO36). Сбой последующей
        обработки (перевод — fail-open, уведомления — отдельный пайплайн)
        этот snapshot не отменяет: он отражает ответ provider'а, а не
        результат уведомления."""
        self._task_repository.record_check_result(task.id, last_check_status="ok", last_error=None)
        self._task_repository.record_successful_check(task.id, total_amount=_records_total(records))

    @staticmethod
    def _suppressed_result() -> CheckResult:
        """Denylisted car_number (см. reader/fines/suppression.py,
        check_task()/check_plate_for_tasks() докстроки) — ни provider, ни
        repository здесь не вызываются вовсе (см. вызывающие методы выше),
        duration_ms=0 честно отражает "работа не выполнялась". Пустые
        current_fines/new_fines и total_fines_found=0 — снаружи такой
        результат показывается ровно как успешная проверка без штрафов (см.
        CheckResult.shows_as_ok): suppression остаётся невидимой, а реальные
        данные не выходят ни в один ответ."""
        return CheckResult(
            status="suppressed", new_fines=[], current_fines=[],
            error_message=None, total_fines_found=0, duration_ms=0,
        )

    def _record_error(self, task: FineMonitoringTask, exc: FineProviderError, started_at: float) -> CheckResult:
        error_message = str(exc)
        self._task_repository.record_check_result(
            task.id, last_check_status="error", last_error=error_message
        )
        # Существующие detected_fines не трогаем вообще — сбой источника
        # не означает "штрафов нет", ничего не удаляем и не помечаем.
        return CheckResult(
            status="error",
            new_fines=[],
            current_fines=[],
            error_message=error_message,
            total_fines_found=0,
            duration_ms=_elapsed_ms(started_at),
            # См. задачу "back off when police.ge is unavailable" —
            # PoliceGeUnavailableError (circuit breaker открыт) отличается
            # от обычного одноразового FineProviderError только ЭТИМ
            # флагом, чтобы UI-слой мог показать отдельное сообщение.
            provider_unavailable=isinstance(exc, PoliceGeUnavailableError),
        )

    async def _finalize(
        self, task: FineMonitoringTask, records: list[ParsedFineRecord], started_at: float,
    ) -> CheckResult:
        """records — ФИНАЛЬНЫЙ, уже решённый набор (см. check_task()) — эта
        часть ВСЕГДА выполняется РОВНО ОДИН раз за вызов check_task(),
        независимо от того, потребовался ли false-zero confirmation запрос
        или нет: dedup/detected_fines/translation/notification-семантика
        (см. задачу п.4/п.8: "нельзя дважды создать detected_fines/
        notification") идентична предыдущему поведению, просто теперь
        применяется к уже подтверждённому results, а не к первому попавшемуся
        ответу provider'а.

        Каждый генуинно НОВЫЙ fingerprint (ветка create() ниже) рождается с
        notification_sent_at=NULL — единое поведение для всех вызывающих
        кодов (см. модульный докстрок). Уже известные штрафы (mark_seen())
        никогда не создают notification независимо от этого."""
        new_fines: list[NewFineEvent] = []
        # ВСЕ штрафы этой проверки (новые и уже известные) — для manual
        # "Проверить сейчас" (см. CheckResult.current_fines). Фоновый
        # пайплайн его не читает вовсе, поэтому существующий new_fines/
        # notification-flow этим не затрагивается.
        current_fines: list[NewFineEvent] = []

        for record in records:
            existing = self._detected_fine_repository.get_by_fingerprint(
                task.id, record.fingerprint
            )

            if existing is not None:
                # Backfill расширенных полей для legacy-строк, созданных до
                # появления violation_date/amount/place/violation_description
                # (см. DetectedFineRepository.mark_seen — COALESCE не
                # затирает уже сохранённое значение). Перевод — тот же
                # backfill-принцип: если place_ru/violation_description_ru
                # уже сохранены — повторно не переводим (см. задачу:
                # "repeated scheduled/manual check -> no repeated
                # translation"); если ещё NULL, а текст грузинский —
                # переводим и сохраняем один раз (self-heal legacy rows).
                place_ru, violation_description_ru = await self._resolve_translation(
                    record,
                    existing_place_ru=existing.place_ru,
                    existing_violation_description_ru=existing.violation_description_ru,
                )
                self._detected_fine_repository.mark_seen(
                    existing.id,
                    violation_date=record.violation_date,
                    amount=record.amount,
                    place=record.place,
                    violation_description=record.violation_description,
                    place_ru=place_ru,
                    violation_description_ru=violation_description_ru,
                )
                current_fines.append(
                    NewFineEvent.from_parsed_record(
                        record, detected_fine_id=existing.id, task_id=task.id, label=task.label,
                        place_ru=place_ru, violation_description_ru=violation_description_ru,
                    )
                )
                continue

            place_ru, violation_description_ru = await self._resolve_translation(
                record, existing_place_ru=None, existing_violation_description_ru=None,
            )
            try:
                created = self._detected_fine_repository.create(
                    monitoring_task_id=task.id,
                    car_number=record.car_number,
                    external_fine_id=record.external_fine_id,
                    fingerprint=record.fingerprint,
                    penalty_date=record.penalty_date,
                    due_date=record.due_date,
                    delivered_status=record.delivered_status,
                    raw_data=json.dumps(record.raw_data, ensure_ascii=False),
                    violation_date=record.violation_date,
                    amount=record.amount,
                    place=record.place,
                    violation_description=record.violation_description,
                    place_ru=place_ru,
                    violation_description_ru=violation_description_ru,
                )
            except sqlite3.IntegrityError:
                # Конкурентная вставка между get_by_fingerprint() и create() —
                # кто-то другой уже создал эту же запись (тот же
                # (monitoring_task_id, fingerprint)). UNIQUE constraint не
                # должен ронять всю проверку — считаем штраф уже известным,
                # а не новым.
                existing = self._detected_fine_repository.get_by_fingerprint(
                    task.id, record.fingerprint
                )
                if existing is not None:
                    race_place_ru, race_violation_description_ru = await self._resolve_translation(
                        record,
                        existing_place_ru=existing.place_ru,
                        existing_violation_description_ru=existing.violation_description_ru,
                    )
                    self._detected_fine_repository.mark_seen(
                        existing.id,
                        violation_date=record.violation_date,
                        amount=record.amount,
                        place=record.place,
                        violation_description=record.violation_description,
                        place_ru=race_place_ru,
                        violation_description_ru=race_violation_description_ru,
                    )
                    current_fines.append(
                        NewFineEvent.from_parsed_record(
                            record, detected_fine_id=existing.id, task_id=task.id, label=task.label,
                            place_ru=race_place_ru,
                            violation_description_ru=race_violation_description_ru,
                        )
                    )
                continue

            event = NewFineEvent.from_detected_fine(created, label=task.label)
            new_fines.append(event)
            current_fines.append(event)

        # Snapshot (record_check_result/record_successful_check) уже сохранён
        # ДО этого цикла — см. _persist_snapshot.
        return CheckResult(
            status="ok",
            new_fines=new_fines,
            current_fines=current_fines,
            error_message=None,
            total_fines_found=len(records),
            duration_ms=_elapsed_ms(started_at),
        )
