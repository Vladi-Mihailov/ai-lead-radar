"""Единая точка визуального форматирования номерного знака для показа в
Telegram (@ProtocolGEbot/@ProtocolTRbot, см. задачу "plate readability
improvement") — буквы жирным, цифры обычным текстом, чтобы номер было
легче прочитать на глаз (M606OM35 -> **M**606**OM**35).

DISPLAY ONLY: не трогает хранимое значение car_number, normalize_car_number/
normalize_plate, provider-запросы, поиск, расчёт задолженности, callback_data
или любые repository/DB ключи — везде, где car_number используется НЕ для
отображения человеку (кнопки, callback_data, lookup), передаётся исходная
каноническая строка как раньше, эта функция там не вызывается.

Оба бота используют Telethon TelegramClient БЕЗ явного parse_mode (см.
reader/turkey_bot/main.py, reader/public_bot/main.py) — значит действует
библиотечный default parse_mode='md' (Telethon-flavoured markdown,
telethon.extensions.markdown), поэтому mode="markdown" — то, что реально
используется; mode="html" добавлен только для полноты API (на случай
будущего UI с HTML parse_mode), production-код его сейчас не вызывает."""

import html as _html
import re
from typing import Literal

_LATIN_LETTER_RUN_RE = re.compile(r"[A-Za-z]+")

PlateDisplayMode = Literal["markdown", "html"]


def format_plate_for_telegram(car_number: str, *, mode: PlateDisplayMode = "markdown") -> str:
    """Оборачивает КАЖДЫЙ максимальный по длине run латинских букв в bold,
    цифры и любые другие символы остаются как есть (см. задачу: "no
    normalization inside formatter... must not mutate value semantics" —
    строка не обрезается/не upper-кейсится/не транслитерируется, меняется
    только разметка). Telethon 'md' не поддерживает backslash-escaping
    (см. telethon.extensions.markdown.parse — чистый paired-delimiter
    парсер без escape-синтаксиса), а канонический car_number — всегда
    только [A-Z0-9] (см. normalize_car_number/normalize_plate), поэтому
    markdown-спецсимволы здесь взяться не могут; mode="html" экранирует
    через html.escape() на случай произвольного небезопасного входа."""
    if mode == "html":
        parts: list[str] = []
        last = 0
        for m in _LATIN_LETTER_RUN_RE.finditer(car_number):
            parts.append(_html.escape(car_number[last:m.start()]))
            parts.append(f"<b>{_html.escape(m.group(0))}</b>")
            last = m.end()
        parts.append(_html.escape(car_number[last:]))
        return "".join(parts)

    if mode == "markdown":
        return _LATIN_LETTER_RUN_RE.sub(lambda m: f"**{m.group(0)}**", car_number)

    raise ValueError(f"Неизвестный mode: {mode!r}")
