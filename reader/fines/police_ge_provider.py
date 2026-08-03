from reader.fines.models import ParsedFineRecord
from reader.fines.parser import FineParseError, parse_search_response
from reader.fines.police_ge_session import PoliceGeSession
from reader.fines.provider import FineProvider, FineProviderError


class PoliceGeProvider(FineProvider):
    """FineProvider поверх police.ge. Намеренно тонкий: вся HTTP-механика —
    в PoliceGeSession, весь разбор ответа — в parser.py. Здесь только их
    склейка и перевод ошибок парсинга в общий FineProviderError, чтобы
    вызывающий код видел один тип ошибки независимо от того, что именно
    подвело — сеть или формат ответа."""

    def __init__(self, session: PoliceGeSession):
        self._session = session

    async def search_by_plate(self, plate: str) -> list[ParsedFineRecord]:
        raw = await self._session.search_by_plate(plate)

        try:
            return parse_search_response(raw, car_number=plate)
        except FineParseError as exc:
            raise FineProviderError(str(exc)) from exc
