from reader.fines.models import ParsedFineRecord
from reader.fines.parser import FineParseError, extract_reported_count, parse_search_response
from reader.fines.police_ge_session import PoliceGeSession
from reader.fines.provider import FineProvider, FineProviderError, ProviderSearchResult


class PoliceGeProvider(FineProvider):
    """FineProvider поверх police.ge. Намеренно тонкий: вся HTTP-механика —
    в PoliceGeSession, весь разбор ответа — в parser.py. Здесь только их
    склейка и перевод ошибок парсинга в общий FineProviderError, чтобы
    вызывающий код видел один тип ошибки независимо от того, что именно
    подвело — сеть или формат ответа."""

    def __init__(self, session: PoliceGeSession):
        self._session = session

    def is_available(self) -> bool:
        """См. FineProvider.is_available() — делегирует PoliceGeSession's
        circuit breaker (см. задачу "back off when police.ge is
        unavailable")."""
        return self._session.is_available()

    async def search_by_plate(self, plate: str) -> list[ParsedFineRecord]:
        return (await self.search_by_plate_detailed(plate)).records

    async def search_by_plate_detailed(
        self, plate: str, *, refresh_page: bool = False,
    ) -> ProviderSearchResult:
        """См. FineProvider.search_by_plate_detailed — count берётся из того
        же ответа searchByAuto, дата обновления базы — со страницы, которую
        сессия загружала ради csrf_token (refresh_page=True загружает её
        заново перед поиском, один дополнительный GET)."""
        if refresh_page:
            await self._session.refresh_page()
        raw = await self._session.search_by_plate(plate)

        try:
            records = parse_search_response(raw, car_number=plate)
        except FineParseError as exc:
            raise FineProviderError(str(exc)) from exc
        return ProviderSearchResult(
            records=records,
            reported_count=extract_reported_count(raw),
            source_updated_on=getattr(self._session, "source_updated_on", None),
        )
