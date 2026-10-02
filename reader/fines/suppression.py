"""Жёсткий denylist car_number для Georgia fine output (см. задачу про
приватность конкретного автомобиля) — ни один путь (мониторинг/manual
"Проверить сейчас"/Add Car/Search/Statistics/debt refresh/"fine check-all"/
операторские команды/клиентские и операторские уведомления) не должен
показать штрафы denylisted номера НИКОМУ, включая trusted-оператора/
fine-admin.

Единственная точка сравнения — is_fine_output_suppressed() ниже.
Используется ИСКЛЮЧИТЕЛЬНО из reader/fines/check_service.py (центральный
guard ПЕРЕД любым provider-запросом/persistence — см. FineCheckService.
check_task()/check_plate_for_tasks()), а также из нескольких read-путей,
читающих УЖЕ persisted данные в обход check_task() (list_tasks_with_known_
debt/get_stats_by_car/flush_pending/ClientDeliveryService.run_once) — на
случай, если для denylisted номера уже существовали detected_fines/
last_successful_total_amount ДО появления этого guard'а (см. задачу:
"не удалять historical records" — старые строки остаются в БД, но ни один
read-путь их больше не показывает).

Код-level immutable set, а НЕ config.yaml-ключ: сам список номеров, за
приватностью которых следим особо, — чувствительная информация сама по
себе, и config.yaml виден куда более широкому кругу (деплой-диффы,
бэкапы конфига, обычный доступ к конфигурации на сервере), чем история
git-коммитов с code review. Добавление/удаление номера в будущем всё
равно требует правки кода и деплоя — ровно то же самое, что и правка
конфига, но без лишней поверхности доступа. Если список когда-либо
вырастет настолько, что потребуется управлять им без деплоя — тогда
имеет смысл переоценить это решение в пользу config-ключа.
"""

from reader.fines.validation import FineValidationError, normalize_car_number

_SUPPRESSED_CAR_NUMBERS: frozenset[str] = frozenset({
    "O687KE761",
})


def is_fine_output_suppressed(car_number: str) -> bool:
    """car_number сравнивается ПОСЛЕ той же нормализации, что применяется
    везде в reader/fines/* (см. normalize_car_number) — регистр, пробелы,
    дефисы и визуально похожая кириллица не позволяют обойти denylist.
    Невалидный номер (пустой/недопустимые символы) — не suppressed: это не
    забота этой функции, вызывающий код сам обрабатывает ошибку валидации
    там, где она возникает."""
    try:
        normalized = normalize_car_number(car_number)
    except FineValidationError:
        return False
    return normalized in _SUPPRESSED_CAR_NUMBERS
