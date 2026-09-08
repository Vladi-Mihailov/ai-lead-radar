from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

FineTaskStatus = Literal["active", "completed", "stopped"]

# Владелец расписания проверки задачи (см. design про "минимальную и чистую
# scheduling-модель"): 'operator' — существующий FineJob (текущее расписание,
# не меняется), 'client_bot' — будущий ClientFineJob (2 раза в сутки,
# см. reader/public_bot/). Партиционирование ВЗАИМОИСКЛЮЧАЮЩЕЕ — одна задача
# проверяется ровно одним фоновым job'ом, без дублирующих проверок общих
# машин. Апгрейд 'client_bot' -> 'operator' возможен (см.
# FineMonitoringTaskRepository.ensure_operator_scope), обратно — никогда.
FineMonitoringScope = Literal["operator", "client_bot"]


@dataclass(frozen=True)
class FineMonitoringTask:
    id: int
    car_number: str
    label: str | None
    start_date: date
    end_date: date
    status: FineTaskStatus
    telegram_chat_id: int
    created_by_user_id: int
    created_at: datetime
    updated_at: datetime
    last_checked_at: datetime | None
    last_check_status: str | None
    last_error: str | None
    # Архивный режим (см. reader/jobs/archive_fine_job.py) — независим от
    # status: задача остаётся 'completed' (не попадает в list_active()/
    # обычные 3 проверки в день), пока archive_check_enabled=1 не выставлен
    # явно (FineJob при завершении периода — для новых задач, либо разовым
    # enrollment — для существующих, см. reader/fines/archive_enrollment.py).
    # Default'ы сохраняют старое поведение для существующих вызовов
    # FineMonitoringTask(...) (см. tests/test_fine_validation.py) без правки.
    archive_check_enabled: bool = False
    next_archive_check_at: datetime | None = None
    # Default 'operator' сохраняет поведение для существующих строк
    # (миграция) и для существующих вызовов FineMonitoringTask(...)/
    # create(...) без этого параметра — см. FineMonitoringScope выше.
    monitoring_scope: FineMonitoringScope = "operator"


@dataclass(frozen=True)
class DetectedFine:
    id: int
    monitoring_task_id: int
    car_number: str
    external_fine_id: str | None
    fingerprint: str
    penalty_date: date | None
    due_date: date | None
    delivered_status: str | None
    raw_data: str
    first_detected_at: datetime
    last_seen_at: datetime
    notification_sent_at: datetime | None
    # Расширенный fine block (см. design report про новый формат
    # уведомлений) — присутствуют в сыром ответе police.ge, но раньше не
    # сохранялись отдельно (только внутри raw_data). Default None — и для
    # существующих вызовов DetectedFine(...) без них, и для старых строк
    # detected_fines, где новые колонки NULL (см. миграцию в
    # DetectedFineRepository) — доставка таких штрафов не должна падать,
    # просто эти строки в сообщении не показываются (см. format_fine_block).
    violation_date: date | None = None
    amount: float | None = None
    place: str | None = None
    violation_description: str | None = None
    # Русский перевод place/violation_description (см. design report про
    # перевод грузинского текста, reader/fines/translation.py) —
    # ОТДЕЛЬНЫЕ колонки, а не перезапись place/violation_description:
    # оригинал остаётся source-of-truth (см. задачу). None — перевод ещё
    # не выполнен (временно недоступен API или сам штраф ещё не
    # переведён, legacy-строка) — format_fine_block показывает оригинал
    # как safe fallback, а не пустую строку/ошибку.
    place_ru: str | None = None
    violation_description_ru: str | None = None


@dataclass(frozen=True)
class CarFineStats:
    """Одна строка статистики fine stats — сколько штрафов опубликовано
    по конкретному автомобилю (detected_fines, сгруппированные по
    car_number)."""

    car_number: str
    fine_count: int


@dataclass(frozen=True)
class ParsedFineRecord:
    """Одна запись из ответа police.ge (или другого будущего FineProvider),
    уже приведённая к доменному виду — независимо от того, как именно
    источник называет свои поля."""

    car_number: str
    external_fine_id: str | None
    penalty_date: date | None
    due_date: date | None
    delivered_status: str
    fingerprint: str
    raw_data: dict[str, Any]
    # Расширенные поля police.ge (violationDate/protocolAmount/protocolPlace/
    # protocolLawDescription) — см. DetectedFine про то же самое. Default
    # None сохраняет существующие вызовы ParsedFineRecord(...) в тестах без
    # изменений.
    violation_date: date | None = None
    amount: float | None = None
    place: str | None = None
    violation_description: str | None = None


@dataclass(frozen=True)
class NewFineEvent:
    """Штраф, требующий уведомления оператора — либо только что обнаруженный
    FineCheckService, либо ожидающий повторной отправки после прошлой
    неудачи (см. DetectedFineRepository.list_pending_notifications()).
    Никакой Telegram-специфики здесь нет.

    detected_fine_id — id строки в detected_fines: по нему NotificationService
    сообщает, какие события доставлены, а FineNotificationCoordinator
    отмечает notification_sent_at только для реально доставленных.
    """

    detected_fine_id: int
    task_id: int
    car_number: str
    label: str | None
    external_fine_id: str | None
    penalty_date: date | None
    due_date: date | None
    delivered_status: str | None
    # Расширенный fine block (см. DetectedFine/ParsedFineRecord) — те же 4
    # поля, прокинутые через from_detected_fine(), чтобы
    # format_fine_block() могло их показать и оператору, и клиенту без
    # второй реализации форматирования.
    violation_date: date | None = None
    amount: float | None = None
    place: str | None = None
    violation_description: str | None = None
    # Русский перевод (см. DetectedFine.place_ru/violation_description_ru
    # и reader/fines/translation.py) — None означает "перевода ещё нет",
    # format_fine_block() тогда показывает оригинал (place/
    # violation_description) как safe fallback, а не пустую строку.
    place_ru: str | None = None
    violation_description_ru: str | None = None
    # Готовая для показа строка вида "@ivan_petrov"/"Иван Петров (@ivan_petrov)"/
    # "не найден"/"@ivan_petrov, @another_user" (несколько владельцев одного
    # car_number — валидное состояние, см. format_car_owner_display) —
    # Telegram-ВЛАДЕЛЕЦ(Ы) автомобиля, определяемый по car_number -> users.car_numbers ->
    # UserRepository.find_by_car_number() (см. FineNotificationCoordinator).
    # Это НЕ fine_monitoring_tasks.created_by_user_id (тот, кто создал
    # задачу мониторинга) — раньше поле ошибочно показывало именно его,
    # см. задачу про production-баг. None, только если задачу мониторинга
    # вообще не удалось найти. Default сохраняет старое поведение для
    # существующих вызовов (например, FineCheckService.check_task(),
    # которому эта информация не нужна).
    car_owner_display: str | None = None

    @classmethod
    def from_detected_fine(
        cls, fine: DetectedFine, *, label: str | None, car_owner_display: str | None = None,
    ) -> "NewFineEvent":
        return cls(
            detected_fine_id=fine.id,
            task_id=fine.monitoring_task_id,
            car_number=fine.car_number,
            label=label,
            external_fine_id=fine.external_fine_id,
            penalty_date=fine.penalty_date,
            due_date=fine.due_date,
            delivered_status=fine.delivered_status,
            violation_date=fine.violation_date,
            amount=fine.amount,
            place=fine.place,
            violation_description=fine.violation_description,
            place_ru=fine.place_ru,
            violation_description_ru=fine.violation_description_ru,
            car_owner_display=car_owner_display,
        )

    @classmethod
    def from_parsed_record(
        cls,
        record: ParsedFineRecord,
        *,
        detected_fine_id: int,
        task_id: int,
        label: str | None,
        place_ru: str | None = None,
        violation_description_ru: str | None = None,
        car_owner_display: str | None = None,
    ) -> "NewFineEvent":
        """Для manual "Проверить сейчас" (см. CheckResult.current_fines) —
        в отличие от from_detected_fine(), строится НЕ из уже сохранённой
        строки БД (которая для уже известных штрафов может на секунду
        отставать от свежего ответа police.ge до COALESCE-backfill'а в
        DetectedFineRepository.mark_seen), а напрямую из только что
        распарсенного record — это ровно то, что реально вернул police.ge
        В ЭТОТ раз, без риска показать клиенту устаревшее null-значение.

        place_ru/violation_description_ru — ПЕРЕДАЮТСЯ явно, а не читаются
        из record: ParsedFineRecord никогда их не содержит (перевод — не
        часть парсинга police.ge, см. reader/fines/translation.py) —
        вызывающий код (FineCheckService) вычисляет итоговое значение
        (из кеша БД и/или свежего перевода) и передаёт сюда готовым."""
        return cls(
            detected_fine_id=detected_fine_id,
            task_id=task_id,
            car_number=record.car_number,
            label=label,
            external_fine_id=record.external_fine_id,
            penalty_date=record.penalty_date,
            due_date=record.due_date,
            delivered_status=record.delivered_status,
            violation_date=record.violation_date,
            amount=record.amount,
            place=record.place,
            violation_description=record.violation_description,
            place_ru=place_ru,
            violation_description_ru=violation_description_ru,
            car_owner_display=car_owner_display,
        )


CheckStatus = Literal["ok", "error"]


@dataclass(frozen=True)
class CheckResult:
    status: CheckStatus
    new_fines: list[NewFineEvent]
    # ВСЕ штрафы, которые police.ge вернул на ЭТОЙ проверке — новые И уже
    # существующие (см. задачу про manual "Проверить сейчас": пользователь,
    # явно нажавший эту кнопку, хочет видеть текущее состояние машины, а не
    # только delta с прошлой проверки). Фоновый пайплайн (FineJob/
    # ClientFineJob/archive/NotificationFlushJob) продолжает использовать
    # ИСКЛЮЧИТЕЛЬНО new_fines — current_fines НЕ должно приводить к
    # повторному уведомлению оператора/trusted, см. check_service.py.
    current_fines: list[NewFineEvent]
    error_message: str | None
    total_fines_found: int
    duration_ms: int
