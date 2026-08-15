"""
Тесты reader/commands/insurance_ocr.py::InsuranceOcrCommand — Telethon и
OcrService полностью фейковые, ни один тест не обращается к реальному
Telegram/OpenAI.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.commands.base import CommandContext, CommandError  # noqa: E402
from reader.commands.insurance_ocr import InsuranceOcrCommand  # noqa: E402
from reader.ocr.models import OcrResult  # noqa: E402
from reader.ocr.service import OcrServiceError  # noqa: E402

_USER_ID = 111
_CHAT_ID = -100999


class _FakeDocument:
    def __init__(self, mime_type: str):
        self.mime_type = mime_type


class _FakeEvent:
    """Минимальная имитация telethon.events.NewMessage.Event — ровно то,
    что использует InsuranceOcrCommand (photo/document/media/grouped_id/
    raw_text/sender_id/download_media/reply). event.respond НЕ определён
    специально: если бы команда случайно вызвала его вместо reply(), тест
    упал бы с AttributeError."""

    def __init__(
        self, *, raw_text="insurance ocr", sender_id=_USER_ID, chat_id=_CHAT_ID,
        grouped_id=None, photo=None, document=None, media=None,
        download_bytes=b"fake-image-bytes", download_error=None,
    ):
        self.raw_text = raw_text
        self.sender_id = sender_id
        self.chat_id = chat_id
        self.grouped_id = grouped_id
        self.photo = photo
        self.document = document
        self.media = media if media is not None else (photo or document)
        self._download_bytes = download_bytes
        self._download_error = download_error
        self.download_calls: list = []
        self.replies: list[str] = []

    async def download_media(self, *, file):
        self.download_calls.append(file)
        if self._download_error is not None:
            raise self._download_error
        return self._download_bytes

    async def reply(self, text):
        self.replies.append(text)


class _FakeOcrService:
    def __init__(self, *, result=None, error=None):
        self._result = result
        self._error = error
        self.extract_calls: list = []

    async def extract(self, images):
        self.extract_calls.append(images)
        if self._error is not None:
            raise self._error
        return self._result


def _full_result(**overrides) -> OcrResult:
    fields = dict(
        registration_number="A123BC777", vin="JTMBR12345678901", chassis_number=None,
        manufacturer="Toyota", model="RAV4", full_name="Иванов Иван Иванович",
    )
    fields.update(overrides)
    return OcrResult(**fields)


def _ctx(event, args=("ocr",)) -> CommandContext:
    return CommandContext(
        chat_id=event.chat_id, user_id=event.sender_id, args=list(args),
        raw_text=event.raw_text, event=event,
    )


def _command(ocr_service=None, *, allowed_user_ids=(_USER_ID,)) -> InsuranceOcrCommand:
    return InsuranceOcrCommand(ocr_service or _FakeOcrService(), allowed_user_ids=list(allowed_user_ids))


# ---- один photo / image document ----


async def test_single_photo_triggers_ocr_and_replies_to_original_message():
    event = _FakeEvent(photo=object())
    ocr_service = _FakeOcrService(result=_full_result())
    command = _command(ocr_service)

    result = await command.handle(_ctx(event))

    assert result.text == ""  # dispatcher не должен слать ничего повторно
    assert len(event.replies) == 1
    assert "Распознано:" in event.replies[0]
    assert "Госномер: A123BC777" in event.replies[0]
    assert len(ocr_service.extract_calls) == 1
    assert ocr_service.extract_calls[0] == [(b"fake-image-bytes", "image/jpeg")]
    # Скачано напрямую в память (file=bytes), ни один temp-файл не создан.
    assert event.download_calls == [bytes]


async def test_image_document_is_accepted():
    event = _FakeEvent(document=_FakeDocument("image/png"))
    ocr_service = _FakeOcrService(result=_full_result())
    command = _command(ocr_service)

    await command.handle(_ctx(event))

    assert ocr_service.extract_calls[0] == [(b"fake-image-bytes", "image/png")]


async def test_reply_uses_event_reply_not_respond():
    """event.respond не определён у фейка — если бы команда вызвала его,
    тест упал бы с AttributeError; успешное завершение подтверждает, что
    использован именно .reply()."""
    event = _FakeEvent(photo=object())
    command = _command(_FakeOcrService(result=_full_result()))

    await command.handle(_ctx(event))

    assert len(event.replies) == 1


async def test_no_double_reply_for_single_message():
    event = _FakeEvent(photo=object())
    command = _command(_FakeOcrService(result=_full_result()))

    result = await command.handle(_ctx(event))

    assert result.text == ""
    assert len(event.replies) == 1


# ---- без вложения / неподдерживаемый формат ----


async def test_command_without_attachment_replies_with_no_image_error():
    event = _FakeEvent(photo=None, document=None)
    ocr_service = _FakeOcrService(result=_full_result())
    command = _command(ocr_service)

    await command.handle(_ctx(event))

    assert event.replies == ["Не найдено изображение документа."]
    assert ocr_service.extract_calls == []


async def test_unsupported_document_mime_type_replies_with_unsupported_media_error():
    event = _FakeEvent(document=_FakeDocument("application/pdf"))
    ocr_service = _FakeOcrService(result=_full_result())
    command = _command(ocr_service)

    await command.handle(_ctx(event))

    assert "Неподдерживаемый формат" in event.replies[0]
    assert ocr_service.extract_calls == []


async def test_missing_ocr_subcommand_raises_command_error():
    event = _FakeEvent()
    command = _command()

    try:
        await command.handle(_ctx(event, args=()))
        assert False, "ожидался CommandError"
    except CommandError as exc:
        assert "insurance ocr" in exc.message


# ---- OpenAI success / partial / error / invalid ----


async def test_openai_success_shows_all_fields():
    event = _FakeEvent(photo=object())
    command = _command(_FakeOcrService(result=_full_result()))

    await command.handle(_ctx(event))

    reply = event.replies[0]
    assert "ФИО: Иванов Иван Иванович" in reply
    assert "Марка: Toyota" in reply
    assert "Модель: RAV4" in reply
    assert "VIN: JTMBR12345678901" in reply
    assert "Госномер: A123BC777" in reply
    assert "Проверь данные." in reply


async def test_partial_result_shows_not_recognized_for_missing_fields():
    event = _FakeEvent(photo=object())
    partial = _full_result(vin=None, chassis_number=None, full_name=None)
    command = _command(_FakeOcrService(result=partial))

    await command.handle(_ctx(event))

    reply = event.replies[0]
    assert "VIN: не распознано" in reply
    assert "Номер шасси: не распознано" in reply
    assert "ФИО: не распознано" in reply
    assert "Марка: Toyota" in reply


async def test_openai_error_replies_with_generic_message():
    event = _FakeEvent(photo=object())
    command = _command(_FakeOcrService(error=OcrServiceError("boom")))

    await command.handle(_ctx(event))

    assert event.replies == [
        "Не удалось получить ответ сервиса распознавания. Попробуйте ещё раз."
    ]


async def test_all_fields_empty_result_replies_with_nothing_recognized_error():
    event = _FakeEvent(photo=object())
    empty = OcrResult(
        registration_number=None, vin=None, chassis_number=None,
        manufacturer=None, model=None, full_name=None,
    )
    command = _command(_FakeOcrService(result=empty))

    await command.handle(_ctx(event))

    assert event.replies == [
        "Не удалось распознать документ. Попробуйте прислать более чёткое фото."
    ]


# ---- права доступа (unauthorized sender) — на уровне альбома, т.к. для
# одиночного сообщения это уже проверяет CommandDispatcher до handle() ----


async def test_handle_album_ignores_unauthorized_sender():
    trigger = _FakeEvent(raw_text="insurance ocr", sender_id=999, photo=object())
    ocr_service = _FakeOcrService(result=_full_result())
    command = _command(ocr_service, allowed_user_ids=(_USER_ID,))

    await command.handle_album([trigger])

    assert trigger.replies == []
    assert ocr_service.extract_calls == []


# ---- альбом (несколько фото, caption на одном из сообщений) ----


async def test_album_with_multiple_photos_collects_all_images():
    e1 = _FakeEvent(raw_text="", grouped_id=555, photo=object(), download_bytes=b"photo-1")
    trigger = _FakeEvent(raw_text="insurance ocr", grouped_id=555, photo=object(), download_bytes=b"photo-2")
    e3 = _FakeEvent(raw_text="", grouped_id=555, photo=object(), download_bytes=b"photo-3")
    ocr_service = _FakeOcrService(result=_full_result())
    command = _command(ocr_service)

    await command.handle_album([e1, trigger, e3])

    assert len(ocr_service.extract_calls) == 1
    images = ocr_service.extract_calls[0]
    assert {data for data, _mime in images} == {b"photo-1", b"photo-2", b"photo-3"}
    # Reply идёт именно на сообщение с командой, а не на первое/последнее фото.
    assert trigger.replies != []
    assert e1.replies == []
    assert e3.replies == []


async def test_album_without_our_caption_is_ignored():
    """Альбом без "insurance ocr" ни на одном из сообщений — не наша
    команда, игнорируется молча."""
    e1 = _FakeEvent(raw_text="", grouped_id=777, photo=object())
    e2 = _FakeEvent(raw_text="просто фото без команды", grouped_id=777, photo=object())
    ocr_service = _FakeOcrService(result=_full_result())
    command = _command(ocr_service)

    await command.handle_album([e1, e2])

    assert e1.replies == []
    assert e2.replies == []
    assert ocr_service.extract_calls == []


async def test_handle_returns_empty_and_does_not_reply_for_grouped_message():
    """Одиночный вызов handle() (через CommandDispatcher) для сообщения —
    части альбома — не должен ничего делать сам: реальная обработка — за
    AlbumCollector/handle_album (см. докстрок класса)."""
    event = _FakeEvent(raw_text="insurance ocr", grouped_id=888, photo=object())
    ocr_service = _FakeOcrService(result=_full_result())
    command = _command(ocr_service)

    result = await command.handle(_ctx(event))

    assert result.text == ""
    assert event.replies == []
    assert ocr_service.extract_calls == []


async def test_album_bytes_downloaded_directly_to_memory_not_disk():
    e1 = _FakeEvent(raw_text="insurance ocr", grouped_id=999, photo=object())
    e2 = _FakeEvent(raw_text="", grouped_id=999, photo=object())
    command = _command(_FakeOcrService(result=_full_result()))

    await command.handle_album([e1, e2])

    assert e1.download_calls == [bytes]
    assert e2.download_calls == [bytes]
