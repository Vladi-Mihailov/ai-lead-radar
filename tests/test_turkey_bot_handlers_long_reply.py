"""
Тесты reader/turkey_bot/handlers.py::_send_reply/_split_telegram_text —
production bug (K935EC07, 2026-10-21): event.respond(reply.text, ...)
падал с telethon.errors.rpcerrorlist.MessageTooLongError для машин с
большим количеством штрафов (40 combined debt items по gib+kgm+avrasya,
₺6265) — unified check успевал завершиться, но сгенерированный текст
ответа превышал предел Telegram, exception падал unhandled, пользователь
не получал вообще никакого ответа. Fix переносит ТОТ ЖЕ proven pattern,
что уже чинил идентичный баг на Georgia (см.
reader/public_bot/handlers.py::_TELEGRAM_SAFE_TEXT_LIMIT/
_split_telegram_text, commit 6869009 "fix: split long Telegram replies",
и tests/test_public_bot_handlers.py — те же тесты один в один, плюс
Turkey-специфичные: extra_texts chunking, markdown plate bolding, K935EC07
fixture).

Реальный Telethon не используется — event подменяется лёгким фейком,
записывающим вызовы .respond(...)/.edit(...) (тот же приём, что и в
tests/test_public_bot_handlers.py). reply — НАСТОЯЩИЙ
reader.turkey_bot.conversation.BotReply (а не отдельный hand-rolled
_FakeReply, см. design report: "избежать 'забыли добавить новое поле в
fake stub'-класса багов, которые уже случались в этой сессии").
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.plate_display import format_plate_for_telegram  # noqa: E402
from reader.turkey_bot.conversation import BotReply  # noqa: E402
from reader.turkey_bot.handlers import (  # noqa: E402
    _TELEGRAM_SAFE_TEXT_LIMIT,
    _send_reply,
    _split_telegram_text,
)


class _FakeEvent:
    def __init__(self):
        self.calls: list[dict] = []

    async def respond(self, text, *, buttons=None):
        self.calls.append({"text": text, "buttons": buttons})

    async def edit(self, text, *, buttons=None):
        self.calls.append({"text": text, "buttons": buttons})


# ---- _split_telegram_text() — байт-в-байт тот же алгоритм/тесты, что и
# Georgia (см. tests/test_public_bot_handlers.py) ----


def test_split_short_text_returns_single_chunk():
    text = "короткий текст"
    assert _split_telegram_text(text) == [text]


def test_split_long_text_produces_multiple_chunks():
    text = "\n".join("A" * 1000 for _ in range(5))
    chunks = _split_telegram_text(text)
    assert len(chunks) > 1


def test_split_reproduces_original_text_exactly():
    text = "\n".join(f"строка {i}: {'x' * 200}" for i in range(50))
    chunks = _split_telegram_text(text)
    assert "".join(chunks) == text


def test_every_chunk_is_within_the_safe_limit():
    text = "\n".join(f"строка {i}: {'x' * 200}" for i in range(50))
    chunks = _split_telegram_text(text)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= _TELEGRAM_SAFE_TEXT_LIMIT


def test_split_prefers_newline_boundaries():
    lines = [f"line-{i}-{'y' * 90}" for i in range(60)]
    text = "\n".join(lines)
    chunks = _split_telegram_text(text)
    assert len(chunks) > 1
    for chunk in chunks:
        stripped = chunk.removesuffix("\n")
        for line in stripped.split("\n"):
            assert line in lines


def test_single_extremely_long_line_hard_splits_safely():
    text = "A" * 10000
    chunks = _split_telegram_text(text)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= _TELEGRAM_SAFE_TEXT_LIMIT
    assert "".join(chunks) == text


def test_mixed_normal_lines_and_one_oversized_line():
    text = "короткая строка 1\n" + ("B" * 9000) + "\nкороткая строка 2"
    chunks = _split_telegram_text(text)
    assert "".join(chunks) == text
    for chunk in chunks:
        assert len(chunk) <= _TELEGRAM_SAFE_TEXT_LIMIT


# ---- _send_reply() main-text chunking — см. tests/test_public_bot_handlers.py
# (short/long/buttons-on-final-chunk/edit-path/edit-failure-fallback) ----


async def test_send_reply_short_message_unchanged_behavior():
    """Существующее поведение (подавляющее большинство случаев) бит в бит
    то же — один chunk, один respond, кнопки на нём."""
    reply = BotReply(text="короткий ответ", show_main_menu=True)
    event = _FakeEvent()

    await _send_reply(event, reply)

    assert len(event.calls) == 1
    assert event.calls[0]["text"] == "короткий ответ"
    assert event.calls[0]["buttons"] is not None


async def test_send_reply_long_message_sends_multiple_chunks():
    long_text = "\n".join(f"строка {i}: {'x' * 200}" for i in range(50))
    reply = BotReply(text=long_text, show_main_menu=True)
    event = _FakeEvent()

    await _send_reply(event, reply)

    assert len(event.calls) > 1
    assert "".join(call["text"] for call in event.calls) == long_text


async def test_send_reply_buttons_only_on_final_chunk():
    long_text = "\n".join(f"строка {i}: {'x' * 200}" for i in range(50))
    reply = BotReply(text=long_text, show_main_menu=True)
    event = _FakeEvent()

    await _send_reply(event, reply)

    assert len(event.calls) > 1
    for call in event.calls[:-1]:
        assert call["buttons"] is None
    assert event.calls[-1]["buttons"] is not None


async def test_send_reply_long_message_on_edit_path_edits_first_chunk_then_responds():
    long_text = "\n".join(f"строка {i}: {'x' * 200}" for i in range(50))
    reply = BotReply(text=long_text, show_main_menu=True)

    class _EditCapableEvent(_FakeEvent):
        def __init__(self):
            super().__init__()
            self.edit_calls: list[dict] = []
            self.respond_calls: list[dict] = []

        async def edit(self, text, *, buttons=None):
            self.edit_calls.append({"text": text, "buttons": buttons})
            self.calls.append({"text": text, "buttons": buttons})

        async def respond(self, text, *, buttons=None):
            self.respond_calls.append({"text": text, "buttons": buttons})
            self.calls.append({"text": text, "buttons": buttons})

    event = _EditCapableEvent()
    await _send_reply(event, reply, prefer_edit=True)

    assert len(event.edit_calls) == 1
    assert event.edit_calls[0]["buttons"] is None  # не последний chunk
    assert len(event.respond_calls) >= 1
    assert event.respond_calls[-1]["buttons"] is not None  # последний chunk
    assert "".join(call["text"] for call in event.calls) == long_text


async def test_send_reply_edit_failure_falls_back_to_respond_for_all_chunks():
    long_text = "\n".join(f"строка {i}: {'x' * 200}" for i in range(50))
    reply = BotReply(text=long_text, show_main_menu=True)

    class _EditFailsEvent(_FakeEvent):
        async def edit(self, text, *, buttons=None):
            raise RuntimeError("message too old to edit")

    event = _EditFailsEvent()
    await _send_reply(event, reply, prefer_edit=True)

    assert "".join(call["text"] for call in event.calls) == long_text
    assert event.calls[-1]["buttons"] is not None
    for call in event.calls[:-1]:
        assert call["buttons"] is None


async def test_send_reply_short_message_still_uses_single_edit_no_fallback():
    """Короткий reply.text на edit-path — РОВНО один event.edit(...), без
    respond вовсе (не регрессия на обычный, самый частый путь)."""
    reply = BotReply(text="🔎 K935EC07: штрафов не найдено", show_main_menu=True)

    class _EditCapableEvent(_FakeEvent):
        def __init__(self):
            super().__init__()
            self.edit_calls: list[dict] = []
            self.respond_calls: list[dict] = []

        async def edit(self, text, *, buttons=None):
            self.edit_calls.append({"text": text, "buttons": buttons})
            self.calls.append({"text": text, "buttons": buttons})

        async def respond(self, text, *, buttons=None):
            self.respond_calls.append({"text": text, "buttons": buttons})
            self.calls.append({"text": text, "buttons": buttons})

    event = _EditCapableEvent()
    await _send_reply(event, reply, prefer_edit=True)

    assert len(event.edit_calls) == 1
    assert event.edit_calls[0]["buttons"] is not None
    assert event.respond_calls == []


# ---- extra_texts — Turkey-специфичная концепция (Georgia её не имеет) —
# должны ТОЖЕ безопасно резаться, кнопки — только на ПОСЛЕДНЕМ chunk
# ПОСЛЕДНЕГО extra_text, не дублируясь ни на одном другом сообщении ----


async def test_send_reply_long_extra_text_is_also_split():
    long_extra = "\n".join(f"extra {i}: {'z' * 200}" for i in range(50))
    reply = BotReply(text="основной короткий текст", extra_texts=(long_extra,), show_main_menu=True)
    event = _FakeEvent()

    await _send_reply(event, reply)

    # Первое сообщение — основной текст (один chunk, без кнопок — есть extra_texts).
    assert event.calls[0]["text"] == "основной короткий текст"
    assert event.calls[0]["buttons"] is None
    extra_calls = event.calls[1:]
    assert len(extra_calls) > 1
    assert "".join(call["text"] for call in extra_calls) == long_extra


async def test_send_reply_buttons_land_on_last_chunk_of_last_extra_text_only():
    """show_main_menu с extra_texts — кнопка принадлежит ПОСЛЕДНЕМУ
    extra_text (см. _first_message_buttons: "show_main_menu and not
    has_extra"), а не основному тексту — и даже если этот extra_text сам
    разрезан на несколько chunks, кнопка — только на самом последнем."""
    long_extra = "\n".join(f"extra {i}: {'z' * 200}" for i in range(50))
    reply = BotReply(text="основной текст", extra_texts=("короткий extra", long_extra), show_main_menu=True)
    event = _FakeEvent()

    await _send_reply(event, reply)

    for call in event.calls[:-1]:
        assert call["buttons"] is None
    assert event.calls[-1]["buttons"] is not None


async def test_send_reply_multiple_extra_texts_none_duplicate_buttons():
    """Явное требование п.6.5 задачи — buttons не дублируются ни на одном
    chunk, кроме самого последнего во всей последовательности."""
    reply = BotReply(
        text="основной", extra_texts=("extra 1", "extra 2", "extra 3"), show_main_menu=True,
    )
    event = _FakeEvent()

    await _send_reply(event, reply)

    assert len(event.calls) == 4  # основной + 3 extra, все короткие -> по одному chunk каждый
    buttons_count = sum(1 for call in event.calls if call["buttons"] is not None)
    assert buttons_count == 1
    assert event.calls[-1]["buttons"] is not None


# ---- Markdown safety — plate bolding (см. задачу "plate readability
# improvement") должен пережить chunking невредимым ----


def test_split_does_not_break_a_realistic_bold_plate_marker():
    """Жирный номер (**K**935**EC**07) — короткий, в начале реалистичного
    длинного ответа — должен целиком остаться в ОДНОМ chunk (та же логика,
    что и у Georgia: сначала режем по границам строк, короткая строка с
    маркером никогда не делится пополам, пока сама не длиннее лимита)."""
    bold_plate = format_plate_for_telegram("K935EC07")
    assert bold_plate.count("**") % 2 == 0  # валиден сам по себе

    header = f"🚗 {bold_plate}\n\nИтого: ₺6265.00\n\n"
    violation_lines = "\n".join(f"{i}. Нарушение №{1000 + i} — ₺{100 + i}.00" for i in range(40))
    text = header + violation_lines

    chunks = _split_telegram_text(text)
    assert "".join(chunks) == text
    assert bold_plate in chunks[0]  # маркер не разрезан между chunks
    assert chunks[0].count("**") % 2 == 0  # первый chunk сам по себе валиден


async def test_send_reply_preserves_bold_plate_through_chunking():
    bold_plate = format_plate_for_telegram("K935EC07")
    header = f"🚗 {bold_plate}\n\nИтого: ₺6265.00\n\n"
    violation_lines = "\n".join(
        f"{i}. Нарушение №{20260000 + i} — Avtomobil yolu ücreti ödenmemiş, "
        f"Trabzon-Gümüşhane karayolu geçiş noktası — дата 0{1 + i % 9}.10.2026 — ₺{100 + i}.00"
        for i in range(80)
    )
    text = header + violation_lines
    reply = BotReply(text=text, show_main_menu=True)
    event = _FakeEvent()

    await _send_reply(event, reply)

    assert len(event.calls) > 1
    full_text = "".join(call["text"] for call in event.calls)
    assert full_text == text
    assert bold_plate in full_text


# ---- K935EC07-shaped regression fixture (см. production bug report:
# 40 combined debt items, ₺6265 total) ----


async def test_k935ec07_shaped_long_reply_delivers_without_messagetoolongerror():
    """Регрессия на точный production bug — реалистичная форма ответа
    unified check (жирный номер + сводка по 3 provider'ам + ~40 строк
    детализации) больше не теряется целиком: каждый chunk <= safe limit
    (гарантия, что реальный MessageTooLongError больше невозможен), ничего
    не потеряно, итоговая сумма присутствует в финальном выводе."""
    plate = format_plate_for_telegram("K935EC07")
    lines = [f"🚗 {plate}", "", "Итого: ₺6265.00", "", "GIB: ⚠️ Есть задолженность — ₺1478.00"]
    lines.append("KGM: ⚠️ Есть задолженность — ₺4787.00")
    lines.append("Avrasya: ⚠️ Есть задолженность — ₺1440.00")
    lines.append("")
    lines.append("Детализация:")
    lines.append("")
    # 11 (gib) + 27 (kgm) + 2 (avrasya) = 40, см. production turkey_provider_results
    # (snaplen 1863+4342+254=6459 байт сырых данных — отформатированный
    # текст с описанием места/нарушения на строку реалистично ещё длиннее).
    for i in range(40):
        lines.append(
            f"{i + 1}. Нарушение №{20260000 + i} от {1 + i % 28:02d}.10.2026 — "
            f"Avtomobil yolu ücreti ödenmemiş, Trabzon-Gümüşhane karayolu geçiş noktası "
            f"№{i} — ₺{50 + i * 3}.00"
        )
    lines.append("")
    lines.append("Проверено: 21.10.2026 09:02")
    text = "\n".join(lines)

    reply = BotReply(text=text, show_main_menu=True)
    event = _FakeEvent()

    await _send_reply(event, reply)

    assert len(event.calls) > 1  # подтверждает, что реальный K935EC07-текст действительно требует chunking
    for call in event.calls:
        assert len(call["text"]) <= _TELEGRAM_SAFE_TEXT_LIMIT
    full_text = "".join(call["text"] for call in event.calls)
    assert full_text == text
    assert "₺6265.00" in full_text
    assert plate in full_text
    for call in event.calls[:-1]:
        assert call["buttons"] is None
    assert event.calls[-1]["buttons"] is not None


# ---- Edge cases — пустой текст (см. задача п.6.8: "если _send_reply их
# поддерживает сейчас, сохранить поведение") ----


async def test_send_reply_empty_text_unchanged_single_send():
    reply = BotReply(text="", show_main_menu=True)
    event = _FakeEvent()

    await _send_reply(event, reply)

    assert len(event.calls) == 1
    assert event.calls[0]["text"] == ""
    assert event.calls[0]["buttons"] is not None
