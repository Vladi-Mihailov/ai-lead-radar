"""Бизнес-логика проверки одной задачи мониторинга штрафов. Ничего не знает
про Telegram/Scheduler/CommandDispatcher/конкретный сайт-источник — только
FineProvider (интерфейс) и оба Repository. Уведомления не отправляет и
notification_sent_at не выставляет — это ответственность
FineNotificationCoordinator (используется FineJob и FineCommand).

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
from typing import Protocol

from reader.fines.detected_fine_repository import DetectedFineRepository
from reader.fines.models import CheckResult, FineMonitoringTask, NewFineEvent, ParsedFineRecord
from reader.fines.provider import FineProvider, FineProviderError
from reader.fines.task_repository import FineMonitoringTaskRepository
from reader.fines.translation import FineTranslationError, TranslatedFineText, contains_georgian

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
        manual "Проверить сейчас", Add Car и manager mass refresh
        одинаково, поэтому false-zero guard ниже защищает ВСЕ эти пути
        разом, без отдельной реализации в каждом из них.

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
        их ни предоставил — первая или вторая попытка."""
        started_at = time.monotonic()

        try:
            records = await self._fetch(task.car_number)
        except FineProviderError as exc:
            return self._record_error(task, exc, started_at)

        previous_amount = task.last_successful_total_amount
        candidate_total = sum((record.amount or 0.0) for record in records)

        if previous_amount is not None and previous_amount > 0 and candidate_total == 0:
            # Suspicious zero (см. докстрок метода) — НЕ персистим этот
            # результат вовсе, ни как ok, ни как error: task.last_successful_*
            # должны продолжать отражать предыдущее достоверное состояние
            # ровно до тех пор, пока подтверждающий запрос не даст финальный
            # ответ (см. задачу п.7: "DB между двумя запросами должна
            # продолжать содержать последнее достоверное positive состояние").
            await self._sleep(self._zero_confirmation_delay_seconds)
            try:
                records = await self._fetch(task.car_number)
            except FineProviderError as exc:
                # Confirmation сама завершилась ошибкой — предыдущее
                # достоверное состояние (last_successful_total_amount/
                # last_successful_checked_at) остаётся НЕТРОНУТЫМ (см.
                # задачу п.9) — record_successful_check здесь НЕ вызывается
                # вовсе, ни с 0, ни с previous_amount: repository просто не
                # трогает эти поля на error-пути (тот же принцип "не
                # трогать поле", что и везде в этом методе).
                return self._record_error(task, exc, started_at)
            # records теперь — ЛИБО подтверждённый настоящий 0 (car A),
            # ЛИБО исправленное положительное значение (car B) — в обоих
            # случаях именно ЭТОТ набор становится единственным финальным
            # результатом ниже, первая (подозрительная) попытка нигде не
            # персистится.

        return await self._finalize(task, records, started_at)

    async def _fetch(self, car_number: str) -> list[ParsedFineRecord]:
        return await self._provider.search_by_plate(car_number)

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
        ответу provider'а."""
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

        self._task_repository.record_check_result(task.id, last_check_status="ok", last_error=None)
        # Единственная authoritative точка persistence "что показала
        # последняя УСПЕШНАЯ проверка" (см. задачу "manager Statistics /
        # refresh для обоих ботов" п.3/п.11) — current_fines, а НЕ
        # detected_fines-история (см. FineMonitoringTaskRepository.
        # record_successful_check/list_tasks_with_known_debt). ЛЮБОЙ
        # вызывающий код (мониторинг/manual "Проверить сейчас"/Add Car/
        # manager refresh) получает эту persistence бесплатно, без
        # отдельной реализации в каждом из них. COALESCE(amount, 0) — тот
        # же принцип, что и везде в проекте (fine без распознанной суммы
        # не должен молча занижать сумму, но и не должен ронять всю
        # проверку — редкий defensive-случай, см. reader/fines/parser.py).
        total_amount = sum((fine.amount or 0.0) for fine in current_fines)
        self._task_repository.record_successful_check(task.id, total_amount=total_amount)

        return CheckResult(
            status="ok",
            new_fines=new_fines,
            current_fines=current_fines,
            error_message=None,
            total_fines_found=len(records),
            duration_ms=_elapsed_ms(started_at),
        )
