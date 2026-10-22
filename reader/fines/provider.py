from abc import ABC, abstractmethod

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
