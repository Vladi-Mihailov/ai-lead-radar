"""РАЗРАБОТЧЕСКИЙ диагностический инструмент, НЕ автотест. Делает РОВНО
ОДИН реальный запрос к KGM (только provider.start(), без submit()) —
только чтобы визуально увидеть формат CAPTCHA (арифметическая задача +
дополнительные символы) перед тем, как писать логику авто-решения.

Использование:
    python -m reader.turkey_bot_test.kgm_captcha_diagnostic
"""

import sys
import tempfile
from pathlib import Path

import httpx

from reader.turkey_bot_test.kgm.provider import KgmProvider
from reader.turkey_bot_test.kgm.session import KgmSession, KgmTransportError

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_OUT_DIR = Path(tempfile.gettempdir()) / "turkey_bot_test_kgm_captcha_debug"


async def _run() -> int:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": _USER_AGENT}) as client:
        provider = KgmProvider(KgmSession(client))
        try:
            challenge = await provider.start()
        except KgmTransportError as exc:
            print(f"Transport error while requesting CAPTCHA: {exc}", file=sys.stderr)
            return 1

        png_path = _OUT_DIR / "kgm_captcha_sample.png"
        png_path.write_bytes(challenge.image_png)
        print(f"CAPTCHA image saved to: {png_path}")
        return 0


def main() -> None:
    import asyncio
    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
