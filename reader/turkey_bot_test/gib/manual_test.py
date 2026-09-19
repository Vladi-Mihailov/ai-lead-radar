"""РАЗРАБОТЧЕСКИЙ инструмент, НЕ часть продакшн-функциональности (см.
задачу: "must not become part of the production Telegram UX"). Делает
РЕАЛЬНЫЕ сетевые запросы к https://dijital.gib.gov.tr — не запускать в CI,
не оборачивать автотестами, не вызывать из reader/turkey_bot_test/main.py (его
ещё не существует — Stage 3).

Единственное назначение: дать разработчику вручную пройти цикл
plate -> CAPTCHA -> человек читает картинку и вводит код -> submit -> сырой
(но безопасно очищенный) результат, чтобы РЕАЛЬНО увидеть форму ответа
with-mys-borc-list хотя бы один раз (см. reader/turkey_bot_test/gib/parser.py:
"не гадать схему" — этот файл существует именно для того, чтобы её
увидеть). Никакого автоматического решения CAPTCHA здесь нет и не будет
(см. задачу).

Использование:
    python -m reader.turkey_bot_test.gib.manual_test A123AA123

НИКОГДА не печатает и не логирует: введённый код CAPTCHA, cookies,
заголовки запроса целиком (см. задачу) — печатает только kind/messages/
raw_data результата и путь к временному PNG.
"""

import asyncio
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402

from reader.turkey_bot_test.gib.provider import GibProvider  # noqa: E402
from reader.turkey_bot_test.gib.session import GibSession, GibTransportError  # noqa: E402

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _save_captcha_png(image_id: str, image_png: bytes) -> Path:
    path = Path(tempfile.gettempdir()) / f"gib_captcha_{image_id}.png"
    path.write_bytes(image_png)
    return path


def _print_outcome(outcome) -> None:
    print("--- Result (sanitized: no captcha code, no cookies, no raw headers) ---")
    print(f"kind: {outcome.kind}")
    if outcome.messages:
        for message in outcome.messages:
            print(f"message: type={message.type!r} text={message.text!r}")
    else:
        print("message: (none)")
    print(f"raw_data: {outcome.raw_data!r}")


async def _run(plate: str) -> int:
    async with httpx.AsyncClient(
        timeout=30.0, headers={"User-Agent": _USER_AGENT},
    ) as client:
        provider = GibProvider(GibSession(client))

        try:
            challenge = await provider.start()
        except GibTransportError as exc:
            print(f"Transport error while requesting CAPTCHA: {exc}", file=sys.stderr)
            return 1

        png_path = _save_captcha_png(challenge.image_id, challenge.image_png)
        print(f"CAPTCHA image saved to: {png_path}")
        print("Open it yourself and read the code — it is never printed/logged here.")

        code = input("Enter CAPTCHA code: ").strip()
        if not code:
            print("No code entered, aborting.", file=sys.stderr)
            return 1

        try:
            outcome = await provider.submit(
                plate=plate, image_id=challenge.image_id, captcha_code=code,
            )
        except GibTransportError as exc:
            print(f"Transport error while submitting check: {exc}", file=sys.stderr)
            return 1

        _print_outcome(outcome)
        return 0


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python -m reader.turkey_bot_test.gib.manual_test <PLATE>", file=sys.stderr)
        sys.exit(2)

    exit_code = asyncio.run(_run(sys.argv[1]))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
