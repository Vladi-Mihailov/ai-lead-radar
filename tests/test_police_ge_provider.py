"""
Тесты PoliceGeProvider — склейка PoliceGeSession + parser.py. Сессия
подменяется лёгким фейком (без httpx/сети): PoliceGeProvider не должен
делать ничего, кроме передачи сырого ответа в parser.py и перевода ошибок
парсинга в общий FineProviderError.
"""

import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from reader.fines.police_ge_provider import PoliceGeProvider  # noqa: E402
from reader.fines.provider import FineProviderError  # noqa: E402


class _FakeSession:
    def __init__(self, result=None, error: Exception | None = None):
        self._result = result
        self._error = error
        self.requested_plates: list[str] = []

    async def search_by_plate(self, plate: str) -> dict:
        self.requested_plates.append(plate)
        if self._error is not None:
            raise self._error
        return self._result


async def test_search_by_plate_returns_parsed_records():
    session = _FakeSession(
        result={
            "success": True,
            "data": {
                "results": [
                    {
                        "protocolAuto": "B957MA09",
                        "protocolNo": "AB123456",
                        "protocolDate": "2026-08-06",
                        "violationDate": "2026-08-05",
                        "lastDate": "2026-08-20",
                        "activeDate": None,
                        "protocolAmount": 100,
                    }
                ]
            },
        }
    )
    provider = PoliceGeProvider(session)

    records = await provider.search_by_plate("B957MA09")

    assert len(records) == 1
    assert records[0].external_fine_id == "AB123456"
    assert records[0].penalty_date == date(2026, 8, 6)
    assert records[0].delivered_status == "Не вручено"
    assert session.requested_plates == ["B957MA09"]


async def test_search_by_plate_returns_empty_list_when_no_fines():
    session = _FakeSession(result={"success": True, "data": {"count": 0, "results": []}})
    provider = PoliceGeProvider(session)

    records = await provider.search_by_plate("AA001AA")

    assert records == []


async def test_session_error_propagates_unchanged():
    session = _FakeSession(error=FineProviderError("сайт недоступен"))
    provider = PoliceGeProvider(session)

    with pytest.raises(FineProviderError, match="сайт недоступен"):
        await provider.search_by_plate("AA001AA")


async def test_malformed_response_is_wrapped_into_fine_provider_error():
    # Даже если PoliceGeSession по какой-то причине вернёт success:true с
    # некорректной структурой data — PoliceGeProvider не должен пробрасывать
    # внутренний FineParseError наружу, а только общий FineProviderError.
    session = _FakeSession(result={"success": True, "data": {"results": "not-a-list"}})
    provider = PoliceGeProvider(session)

    with pytest.raises(FineProviderError):
        await provider.search_by_plate("AA001AA")
