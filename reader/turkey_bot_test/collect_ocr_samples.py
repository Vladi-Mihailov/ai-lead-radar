"""РАЗРАБОТЧЕСКИЙ диагностический инструмент, НЕ часть продакшн-функциональности
и НЕ автотест (имя без префикса test_ — pytest его не подхватывает). Делает
10 РЕАЛЬНЫХ сетевых запросов к https://dijital.gib.gov.tr — только
provider.start() (получение CAPTCHA-challenge), по одному новому
httpx.AsyncClient/GibSession на каждый запрос (та же схема, что и в
reader/turkey_bot_test/gib/manual_test.py и captcha_ocr_diagnostic.py).

НИКОГДА не вызывает provider.submit(), не знает про номер машины и не
пытается автоматически распознать/проверить CAPTCHA — только скачивает и
сохраняет картинки для последующей ручной разметки (см. CSV с пустым полем
truth).

НЕ трогает reader/turkey_bot_test/conversation.py, captcha_solver.py и
рабочий flow бота.

Использование:
    python -m reader.turkey_bot_test.collect_ocr_samples
"""

import asyncio
import csv
import sys
from pathlib import Path

import httpx

from reader.turkey_bot_test.gib.provider import GibProvider
from reader.turkey_bot_test.gib.session import GibSession, GibTransportError

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_OUT_DIR = Path(__file__).resolve().parent / "ocr_samples"
_CSV_PATH = _OUT_DIR / "samples.csv"
_COUNT = 10
_DELAY_SECONDS = 3.0


async def _fetch_one() -> tuple[str, bytes] | None:
    """Один challenge — свежий client/session, ТОЛЬКО start(), без submit()."""
    async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": _USER_AGENT}) as client:
        provider = GibProvider(GibSession(client))
        try:
            challenge = await provider.start()
        except GibTransportError as exc:
            print(f"  transport error: {exc}", file=sys.stderr)
            return None
        return challenge.image_id, challenge.image_png


async def _run() -> int:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)

    rows: list[tuple[str, str, str]] = []
    saved = 0
    errors = 0

    for i in range(1, _COUNT + 1):
        sample_name = f"captcha_{i:02d}"
        print(f"[{i}/{_COUNT}] requesting CAPTCHA ({sample_name})...")
        result = await _fetch_one()

        if result is None:
            errors += 1
            rows.append((sample_name, "", ""))
            print(f"[{i}/{_COUNT}] FAILED")
        else:
            image_id, image_png = result
            png_path = _OUT_DIR / f"{sample_name}.png"
            png_path.write_bytes(image_png)
            rows.append((sample_name, image_id, ""))
            saved += 1
            print(f"[{i}/{_COUNT}] saved: {png_path.name} (image_id={image_id})")

        if i < _COUNT:
            await asyncio.sleep(_DELAY_SECONDS)

    with _CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["sample", "image_id", "truth"])
        writer.writerows(rows)

    print()
    print(f"Saved {saved}/{_COUNT} CAPTCHA images to: {_OUT_DIR}")
    print(f"CSV written to: {_CSV_PATH}")
    print(f"Transport errors: {errors}")
    return 0


def main() -> None:
    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
