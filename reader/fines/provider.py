from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date

from reader.fines.models import ParsedFineRecord


class FineProviderError(Exception):
    """Ошибка при обращении к внешнему источнику штрафов (сайт недоступен,
    неожиданный формат ответа и т.п.) — общий тип для всех реализаций
    FineProvider, чтобы вызывающий код (FineCheckService) ловил один тип
    ошибки независимо от конкретного провайдера/страны."""


class PoliceGeUnavailableError(FineProviderError):
    """Подкласс FineProviderError (см. задачу "back off when police.ge is
    unavailable", production incident 2026-10-21) — circuit breaker
    (reader/fines/police_ge_circuit_breaker.py) отказал запросу СРАЗУ, без
    единого обращения к police.ge, потому что источник уже признан
    недоступным по предыдущим transport-сбоям. Специально ОТДЕЛЬНЫЙ тип
    (не просто FineProviderError с текстом) — чтобы FineCheckService мог
    пометить CheckResult.provider_unavailable=True и показать
    пользователю отдельное, понятное сообщение вместо общего "попробуйте
    позже", но при этом ЛЮБОЙ существующий `except FineProviderError`
    код (есть во ВСЕХ вызывающих путях, см. provider.py докстрок) ловит
    её точно так же, как обычную FineProviderError — никакой отдельной
    обработки isinstance не требуется там, где различие не важно."""


class PoliceGeBlockedError(FineProviderError):
    """police.ge ответил HTTP 403/429 — признак блокировки или ограничения
    частоты запросов (см. задачу "ежедневная вечерняя перепроверка": на
    таком ответе массовый проход останавливается сразу, а не после
    нескольких обычных ошибок подряд). Подкласс FineProviderError: любой
    существующий `except FineProviderError` ловит её как обычный сбой
    источника. status_code — None, если код неизвестен."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class ProviderSearchResult:
    """Ответ searchByAuto вместе с тем, чего нет в list[ParsedFineRecord]:

    - reported_count — data.count из ответа police.ge; None — источник его
      не передал (например, REDNET со старой версией кода);
    - source_updated_on — "Date of update of information" со страницы
      police.ge, дата последнего обновления опубликованных квитанций; None
      — неизвестна.

    Нужны только статусам оплаты (см. reader/fines/payment_status.py):
    неполный ответ (count не совпадает с числом записей) не считается
    доказательством того, что штраф исчез из базы полиции."""

    records: list[ParsedFineRecord]
    reported_count: int | None = None
    source_updated_on: date | None = None

    @property
    def complete(self) -> bool:
        """False только когда источник сам сообщил другое число записей.
        Без count (старый REDNET) ответ считается полным: searchByAuto
        отдаёт все квитанции номера одним ответом (у нас максимум 9 при
        странице в 30), а от разового усечённого ответа защищает правило
        двух проверок с интервалом (см. FineStatusTracker)."""
        return self.reported_count is None or self.reported_count == len(self.records)


class FineProvider(ABC):
    """Источник данных о штрафах по номеру автомобиля. PoliceGeProvider —
    первая и пока единственная реализация; интерфейс не завязан на
    police.ge, чтобы позже добавить другие страны без изменения
    FineCheckService."""

    @abstractmethod
    async def search_by_plate(self, plate: str) -> list[ParsedFineRecord]:
        """Вернуть штрафы, найденные для номера. Пустой список — штрафов
        нет. Бросает FineProviderError при сбое запроса к источнику."""

    def is_available(self) -> bool:
        """True по умолчанию (см. задачу "back off when police.ge is
        unavailable") — провайдеры БЕЗ circuit breaker (Turkey, любой
        будущий провайдер) всегда доступны с точки зрения этого
        интерфейса; PoliceGeProvider переопределяет, делегируя своей
        PoliceGeSession. Используется ИСКЛЮЧИТЕЛЬНО scheduled-джобами
        (FineJob/ArchiveFineJob) ЗАРАНЕЕ, чтобы пропустить задачу целиком
        БЕЗ единой записи в БД, когда источник уже точно недоступен (см.
        задачу п.4: "do not generate hundreds of identical errors") — не
        заменяет сам circuit breaker внутри search_by_plate(), который
        всё равно является единственным источником истины при реальном
        вызове."""
        return True

    async def search_by_plate_detailed(
        self, plate: str, *, refresh_page: bool = False,
    ) -> ProviderSearchResult:
        """То же, что search_by_plate(), плюс метаданные ответа (см.
        ProviderSearchResult). refresh_page — перед поиском заново
        загрузить страницу police.ge (свежая дата обновления базы); здесь,
        в реализации по умолчанию для провайдеров без этих данных,
        игнорируется."""
        return ProviderSearchResult(records=await self.search_by_plate(plate))


async def search_detailed(provider, plate: str, *, refresh_page: bool = False) -> ProviderSearchResult:
    """search_by_plate_detailed(), если провайдер его поддерживает, иначе
    обычный search_by_plate() без метаданных — для провайдеров, которые
    реализуют только базовый метод, не наследуя FineProvider (тестовые
    фейки, сторонние обёртки)."""
    detailed = getattr(provider, "search_by_plate_detailed", None)
    if detailed is None:
        return ProviderSearchResult(records=await provider.search_by_plate(plate))
    return await detailed(plate, refresh_page=refresh_page)
