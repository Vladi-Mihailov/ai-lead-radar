"""Тесты reader/telegram_markdown.py — escape_telethon_markdown проверяется
ровно тем парсером, которым Telethon разбирает исходящие сообщения ботов
(parse_mode='md' по умолчанию, см. reader/plate_display.py)."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402
from telethon.extensions import markdown  # noqa: E402
from telethon.tl.types import MessageEntityBold  # noqa: E402

from reader.plate_display import format_plate_for_telegram  # noqa: E402
from reader.telegram_markdown import escape_telethon_markdown, safe_telegram_username  # noqa: E402


@pytest.mark.parametrize("text", [
    "**Иван**", "__Пётр__", "~~зачёркнуто~~", "`код`", "```pre```", "[Нажми](http://evil.example)",
    "a***b", "_одно_подчёркивание_", "*одна*", "] (x)", "evil__user", "Нино [BMW]", "[x",
])
def test_user_text_never_becomes_markup_and_never_breaks_plate_bold(text):
    message = (
        f"🚗 Госномер: {format_plate_for_telegram('AB123CD')}\n"
        f"👤 Пользователь: {escape_telethon_markdown(text)}\n"
        f"👤 Пользователь: {escape_telethon_markdown('x](http://evil.example)')}\n\n"
        f"🚗 Госномер: {format_plate_for_telegram('XY777')}"
    )

    plain_text, entities = markdown.parse(message)

    assert all(isinstance(entity, MessageEntityBold) for entity in entities)
    assert len(entities) == 3  # AB, CD, XY — буквы госномеров, и ничего из текста пользователя
    visible = plain_text.splitlines()[1].replace("​", "")
    assert visible == "👤 Пользователь: " + text.replace("`", "ˋ")


def test_plain_names_are_unchanged_so_copied_text_matches():
    for text in ("Иван Петров", "Ivan_Petrov", "nino.b", "Айше", "O'Brien", "Ana-Maria (Ани)", "Нино [BMW]", "[x"):
        assert escape_telethon_markdown(text) == text


@pytest.mark.parametrize(("raw", "expected"), [
    ("ivan_petrov", "ivan_petrov"),
    ("@Ivan_77", "Ivan_77"),
    ("  user_one  ", "user_one"),
])
def test_safe_telegram_username_keeps_real_usernames_unchanged(raw, expected):
    assert safe_telegram_username(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "@", "   ", "evil__user", "bad.user", "имя", "two words", "x](y)"])
def test_safe_telegram_username_rejects_anything_that_is_not_a_telegram_username(raw):
    assert safe_telegram_username(raw) is None
