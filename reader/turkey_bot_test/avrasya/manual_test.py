"""РАЗРАБОТЧЕСКИЙ инструмент, НЕ часть продакшн-функциональности (см. Stage
2A задачу: "Do NOT modify Telegram UX... yet"). Делает РЕАЛЬНЫЕ сетевые
запросы к https://www.avrasyatuneli.com — не запускать в CI, не
оборачивать автотестами, не вызывать из reader/turkey_bot_test/main.py.

Единственное назначение: дать разработчику вручную пройти цикл
plate -> CAPTCHA -> человек читает картинку и вводит код -> submit ->
сырой (но безопасно очищенный) результат, чтобы РЕАЛЬНО увидеть форму
ответа /api/debt/query хотя бы один раз для HTTP 200 (долг найден) и
HTTP 404 (долга нет) — см. reader/turkey_bot_test/avrasya/parser.py: "не
гадать схему" — этот файл существует именно для того, чтобы её увидеть.
Никакого автоматического решения CAPTCHA здесь нет и не будет (см. задачу:
"Do not implement CAPTCHA OCR or solving").

Использование:
    python -m reader.turkey_bot_test.avrasya.manual_test A123AA123

Плата нормализуется ЧЕРЕЗ существующий reader/turkey_bot_test/validation.py::
normalize_plate() (см. задачу: "Do not duplicate the Cyrillic->Latin
logic") — можно передавать и кириллический российский номер
(например, А123АА123), не только уже латинский.

НИКОГДА не печатает и не логирует: введённый код CAPTCHA, cookies,
авторизационные заголовки/токены, Telegram-токены/API-ключи, заголовки
запроса целиком (см. задачу) — печатает только путь к временному JPEG,
sanitized результат (status_code/kind/messages/raw_data) и текстовые
статусы ошибок транспорта/rate-limit."""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402

from reader.turkey_bot_test.avrasya.provider import AvrasyaProvider  # noqa: E402
from reader.turkey_bot_test.avrasya.session import (  # noqa: E402
    AvrasyaRateLimitedError,
    AvrasyaSession,
    AvrasyaTransportError,
)
from reader.turkey_bot_test.validation import normalize_plate  # noqa: E402

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _save_captcha_jpeg(image_png: bytes) -> Path:
    fd, name = tempfile.mkstemp(prefix="avrasya_captcha_", suffix=".jpg")
    os.close(fd)
    path = Path(name)
    path.write_bytes(image_png)
    return path


def _print_outcome(outcome) -> None:
    print("--- Result (sanitized: no captcha code, no cookies, no auth/raw headers) ---")
    print(f"status_code: {outcome.status_code}")
    print(f"kind: {outcome.kind}")
    if outcome.messages:
        for message in outcome.messages:
            print(
                f"message: property_name={message.property_name!r} "
                f"error_message={message.error_message!r}"
            )
    else:
        print("message: (none)")
    print(f"raw_data: {outcome.raw_data!r}")


async def _run(raw_plate: str) -> int:
    plate = normalize_plate(raw_plate)
    if plate is None:
        print(f"Not a plausible plate number: {raw_plate!r}", file=sys.stderr)
        return 2

    async with httpx.AsyncClient(
        timeout=30.0, headers={"User-Agent": _USER_AGENT},
    ) as client:
        provider = AvrasyaProvider(AvrasyaSession(client))

        try:
            challenge = await provider.start()
        except AvrasyaRateLimitedError:
            print("Rate limited while requesting CAPTCHA — try again in a bit.", file=sys.stderr)
            return 1
        except AvrasyaTransportError as exc:
            print(f"Transport error while requesting CAPTCHA: {exc}", file=sys.stderr)
            return 1

        png_path = _save_captcha_jpeg(challenge.image_png)
        print(png_path)

        code = input("Enter CAPTCHA code: ").strip()
        if not code:
            print("No code entered, aborting.", file=sys.stderr)
            return 1

        try:
            outcome = await provider.submit(plate=plate, captcha_code=code)
        except AvrasyaRateLimitedError:
            print("Rate limited while submitting check — try again in a bit.", file=sys.stderr)
            return 1
        except AvrasyaTransportError as exc:
            print(f"Transport error while submitting check: {exc}", file=sys.stderr)
            return 1

        _print_outcome(outcome)
        return 0


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python -m reader.turkey_bot_test.avrasya.manual_test <PLATE>", file=sys.stderr)
        sys.exit(2)

    exit_code = asyncio.run(_run(sys.argv[1]))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
