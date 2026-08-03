from abc import ABC, abstractmethod

from reader.fines.models import ParsedFineRecord


class FineProviderError(Exception):
    """Ошибка при обращении к внешнему источнику штрафов (сайт недоступен,
    неожиданный формат ответа и т.п.) — общий тип для всех реализаций
    FineProvider, чтобы вызывающий код (FineCheckService) ловил один тип
    ошибки независимо от конкретного провайдера/страны."""


class FineProvider(ABC):
    """Источник данных о штрафах по номеру автомобиля. PoliceGeProvider —
    первая и пока единственная реализация; интерфейс не завязан на
    police.ge, чтобы позже добавить другие страны без изменения
    FineCheckService."""

    @abstractmethod
    async def search_by_plate(self, plate: str) -> list[ParsedFineRecord]:
        """Вернуть штрафы, найденные для номера. Пустой список — штрафов
        нет. Бросает FineProviderError при сбое запроса к источнику."""
