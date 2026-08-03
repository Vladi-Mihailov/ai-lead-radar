"""Ручной live-тест интеграции с police.ge. НЕ входит в pytest-сьют и не
запускается автоматически — обращается к реальному сайту. Запускать вручную:

    python scripts/manual_check_police_ge.py B957MA09

Нужен для проверки, что сайт не поменял разметку/поведение с момента
разработки PoliceGeSession/PoliceGeProvider.
"""

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402

from reader.fines.police_ge_provider import PoliceGeProvider  # noqa: E402
from reader.fines.police_ge_session import PoliceGeSession  # noqa: E402

_PAGE_URL = "https://police.ge/protocol/index.php?lang=en"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


async def main() -> None:
    if len(sys.argv) != 2:
        print(f"Использование: python {sys.argv[0]} <НОМЕР_АВТО>", file=sys.stderr)
        raise SystemExit(1)

    plate = sys.argv[1]

    async with httpx.AsyncClient(
        base_url="https://police.ge/protocol/",
        headers={"User-Agent": _USER_AGENT},
    ) as client:
        session = PoliceGeSession(client, page_url=_PAGE_URL, request_timeout=30)
        provider = PoliceGeProvider(session)

        records = await provider.search_by_plate(plate)

        if not records:
            print(f"Штрафов для {plate} не найдено.")
            return

        print(f"Найдено штрафов: {len(records)}")
        for record in records:
            print("-" * 40)
            print(f"Номер: {record.car_number}")
            print(f"Протокол: {record.external_fine_id}")
            print(f"Дата штрафа: {record.penalty_date}")
            print(f"Срок оплаты: {record.due_date}")
            print(f"Статус вручения: {record.delivered_status}")
            print(f"Fingerprint: {record.fingerprint}")


if __name__ == "__main__":
    asyncio.run(main())
