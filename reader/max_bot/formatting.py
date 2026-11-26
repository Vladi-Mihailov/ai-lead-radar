"""Тексты Telegram-ботов (Telethon md: **жирный**, __курсив__, ~~зачёркнутый~~,
`код`, [текст](ссылка)) -> HTML для MAX (format="html").

Почему HTML, а не markdown MAX: в markdown MAX "__x__" — жирный, а не
курсив, и правила экранирования не документированы; в HTML достаточно
экранировать &, <, >. Имена пользователей в общих текстах уже обезврежены
escape_telethon_markdown (zero-width space внутри парных разделителей) —
такие "разделители" и здесь не распознаются.
"""

from __future__ import annotations

import html
import re

from reader.max_bot.api import MAX_TEXT_LENGTH

_LINK_RE = re.compile(r"\[([^\[\]\n]+)\]\((https?://[^)\s]+)\)")
_PAIRS = (
    (re.compile(r"\*\*([^\n]+?)\*\*"), "b"),
    (re.compile(r"__([^\n]+?)__"), "i"),
    (re.compile(r"~~([^\n]+?)~~"), "s"),
    (re.compile(r"`([^`\n]+)`"), "code"),
)
_PLACEHOLDER_RE = re.compile("\x00(\\d+)\x00")


def telethon_md_to_html(text: str) -> str:
    """Парные разделители распознаются только в пределах одной строки — так
    общие тексты и размечены, и split_text не разрывает тег. Адреса ссылок
    прячутся в заглушки до обработки разделителей."""
    escaped = html.escape(text.replace("\x00", ""), quote=False)
    hrefs: list[str] = []

    def stash(match: re.Match) -> str:
        # Текст уже экранирован выше (& -> &amp;), в адресе остаётся заменить кавычку.
        hrefs.append(match.group(2).replace('"', "%22"))
        return f'<a href="\x00{len(hrefs) - 1}\x00">{match.group(1)}</a>'

    escaped = _LINK_RE.sub(stash, escaped)
    for pattern, tag in _PAIRS:
        escaped = pattern.sub(lambda m, tag=tag: f"<{tag}>{m.group(1)}</{tag}>", escaped)
    return _PLACEHOLDER_RE.sub(lambda m: hrefs[int(m.group(1))], escaped)


def plain_to_html(text: str) -> str:
    return html.escape(text, quote=False)


def split_text(text: str, *, limit: int = MAX_TEXT_LENGTH) -> list[str]:
    """Режет по строкам, чтобы каждая часть была не длиннее limit. Тег не
    разрывается: парная разметка распознаётся только в пределах строки (см.
    telethon_md_to_html). Сверхдлинная строка режется жёстко, но не внутри
    HTML-сущности (&amp; и т.п.)."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            cut = limit
            amp = line.rfind("&", max(0, limit - 10), limit)
            if amp > 0 and ";" not in line[amp:limit]:
                cut = amp
            parts.append(line[:cut])
            line = line[cut:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts
