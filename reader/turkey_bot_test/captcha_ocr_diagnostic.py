"""РАЗРАБОТЧЕСКИЙ диагностический инструмент, НЕ часть продакшн-функциональности
и НЕ автотест (имя без префикса test_ — pytest его не подхватывает). Делает
РОВНО ОДИН реальный сетевой запрос к https://dijital.gib.gov.tr — только
provider.start() (получение CAPTCHA-challenge), НИКОГДА не вызывает
provider.submit() и вообще не знает про номер машины.

Единственное назначение: офлайн-оценка качества
reader/turkey_bot_test/captcha_solver.py::CaptchaSolver.solve_captcha() на
одной живой CAPTCHA GİB — сохранить картинку и сравнить, что распознал OCR,
с тем, что на ней реально написано (открыть файл вручную). Никакого retry/
цикла — один запуск, один challenge (не долбим GİB-сайт).

НЕ трогает reader/turkey_bot_test/conversation.py и рабочий flow бота —
полностью отдельный скрипт, использует тот же test-clone GibProvider/
GibSession, что и reader/turkey_bot_test/gib/manual_test.py.

Использование:
    python -m reader.turkey_bot_test.captcha_ocr_diagnostic
"""

import asyncio
import sys
import tempfile
import time
from pathlib import Path

import httpx

from reader.turkey_bot_test.captcha_solver import CaptchaSolver
from reader.turkey_bot_test.gib.provider import GibProvider
from reader.turkey_bot_test.gib.session import GibSession, GibTransportError

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_DEBUG_DIR = Path(tempfile.gettempdir()) / "turkey_bot_test_captcha_ocr_debug"


async def _run() -> int:
    _DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": _USER_AGENT}) as client:
        provider = GibProvider(GibSession(client))

        try:
            challenge = await provider.start()
        except GibTransportError as exc:
            print(f"Transport error while requesting CAPTCHA: {exc}", file=sys.stderr)
            return 1

        png_path = _DEBUG_DIR / f"gib_captcha_{challenge.image_id}.png"
        png_path.write_bytes(challenge.image_png)

        start = time.perf_counter()
        recognized_code = CaptchaSolver.solve_captcha(challenge.image_png)
        elapsed_seconds = time.perf_counter() - start

        print(f"image_id: {challenge.image_id}")
        print(f"OCR code: {recognized_code!r}")
        print(f"saved to: {png_path}")
        print(f"OCR time: {elapsed_seconds:.3f}s")
        return 0


def main() -> None:
    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
