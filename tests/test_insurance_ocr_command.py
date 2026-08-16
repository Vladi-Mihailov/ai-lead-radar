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
        owner_full_name="Иванов Иван Иванович",
        driver_full_name="Петров Пётр Петрович",
        policyholder_full_name="Петров Пётр Петрович",
        passport_number="AB1234567",
        citizenship="Georgia",
        category="passenger_car",
        registration_number="A123BC777", vin="JTMBR12345678901", chassis_number=None,
        manufacturer="Toyota", model="RAV4",
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
    assert "Собственник: Иванов Иван Иванович" in reply
    assert "Водитель: Петров Пётр Петрович" in reply
    assert "Страхователь: Петров Пётр Петрович" in reply
    assert "Номер паспорта: AB1234567" in reply
    assert "Гражданство: Georgia" in reply
    assert "Категория: passenger_car" in reply
    assert "Марка: Toyota" in reply
    assert "Модель: RAV4" in reply
    assert "VIN: JTMBR12345678901" in reply
    assert "Госномер: A123BC777" in reply
    assert "Проверь данные." in reply


async def test_partial_result_shows_not_recognized_for_missing_fields():
    event = _FakeEvent(photo=object())
    partial = _full_result(
        vin=None, chassis_number=None, category=None,
        owner_full_name=None, driver_full_name=None, policyholder_full_name=None,
        passport_number=None, citizenship=None,
    )
    command = _command(_FakeOcrService(result=partial))

    await command.handle(_ctx(event))

    reply = event.replies[0]
    assert "VIN: не распознано" in reply
    assert "Номер шасси: не распознано" in reply
    assert "Собственник: не распознано" in reply
    assert "Водитель: не распознано" in reply
    assert "Страхователь: не распознано" in reply
    assert "Номер паспорта: не распознано" in reply
    assert "Гражданство: не распознано" in reply
    assert "Категория: не распознано" in reply
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
        owner_full_name=None, driver_full_name=None, policyholder_full_name=None,
        passport_number=None, citizenship=None,
        category=None,
        registration_number=None, vin=None, chassis_number=None,
        manufacturer=None, model=None,
    )
    command = _command(_FakeOcrService(result=empty))

    await command.handle(_ctx(event))

    assert event.replies == [
        "Не удалось распознать документ. Попробуйте прислать более чёткое фото."
    ]


# ---- категория ТС (category) ----


async def test_category_motorcycle_is_shown_as_recognized_by_the_model():
    event = _FakeEvent(photo=object())
    result = _full_result(category="motorcycle")
    command = _command(_FakeOcrService(result=result))

    await command.handle(_ctx(event))

    assert "Категория: motorcycle" in event.replies[0]


async def test_category_trailer_is_shown_as_recognized_by_the_model():
    event = _FakeEvent(photo=object())
    result = _full_result(category="trailer")
    command = _command(_FakeOcrService(result=result))

    await command.handle(_ctx(event))

    assert "Категория: trailer" in event.replies[0]


async def test_category_null_is_shown_as_not_recognized_not_defaulted_to_passenger_car():
    """category=null от модели должен показаться как "не распознано" —
    код НИГДЕ не подставляет passenger_car по умолчанию (см. задачу:
    "нельзя просто возвращать passenger_car по умолчанию, не анализируя
    документ")."""
    event = _FakeEvent(photo=object())
    result = _full_result(category=None)
    command = _command(_FakeOcrService(result=result))

    await command.handle(_ctx(event))

    reply = event.replies[0]
    assert "Категория: не распознано" in reply
    assert "Категория: passenger_car" not in reply


# ---- разделение ролей ФИО: owner (техпаспорт) / driver+policyholder (права) ----


async def test_individual_owner_in_techpassport_plus_license_fills_all_three_roles():
    """Сценарий 1 задачи: физлицо в техпаспорте + права — owner из
    техпаспорта, driver/policyholder из прав, все три роли заполнены и не
    перепутаны."""
    event = _FakeEvent(photo=object())
    result = _full_result(
        owner_full_name="Иванов Иван Иванович",
        driver_full_name="Петров Пётр Петрович",
        policyholder_full_name="Петров Пётр Петрович",
    )
    command = _command(_FakeOcrService(result=result))

    await command.handle(_ctx(event))

    reply = event.replies[0]
    assert "Собственник: Иванов Иван Иванович" in reply
    assert "Водитель: Петров Пётр Петрович" in reply
    assert "Страхователь: Петров Пётр Петрович" in reply


async def test_legal_entity_owner_in_techpassport_yields_null_owner_plus_license_roles():
    """Сценарий 2 задачи: юрлицо в техпаспорте + права — owner_full_name
    приходит от модели уже как None (классификация "юрлицо" — задача
    prompt/модели, не кода), driver/policyholder из прав по-прежнему
    заполнены."""
    event = _FakeEvent(photo=object())
    result = _full_result(
        owner_full_name=None,
        driver_full_name="Петров Пётр Петрович",
        policyholder_full_name="Петров Пётр Петрович",
    )
    command = _command(_FakeOcrService(result=result))

    await command.handle(_ctx(event))

    reply = event.replies[0]
    assert "Собственник: не распознано" in reply
    assert "Водитель: Петров Пётр Петрович" in reply
    assert "Страхователь: Петров Пётр Петрович" in reply


async def test_only_techpassport_present_fills_owner_but_not_driver_or_policyholder():
    """Сценарий 3 задачи: среди изображений только техпаспорт — owner
    может быть заполнен, driver/policyholder — null (нет источника)."""
    event = _FakeEvent(photo=object())
    result = _full_result(
        owner_full_name="Иванов Иван Иванович",
        driver_full_name=None,
        policyholder_full_name=None,
    )
    command = _command(_FakeOcrService(result=result))

    await command.handle(_ctx(event))

    reply = event.replies[0]
    assert "Собственник: Иванов Иван Иванович" in reply
    assert "Водитель: не распознано" in reply
    assert "Страхователь: не распознано" in reply


async def test_only_license_present_fills_driver_and_policyholder_but_not_owner():
    """Сценарий 4 задачи: среди изображений только права — owner = null
    (нет техпаспорта), driver/policyholder заполнены."""
    event = _FakeEvent(photo=object())
    result = _full_result(
        owner_full_name=None,
        driver_full_name="Петров Пётр Петрович",
        policyholder_full_name="Петров Пётр Петрович",
    )
    command = _command(_FakeOcrService(result=result))

    await command.handle(_ctx(event))

    reply = event.replies[0]
    assert "Собственник: не распознано" in reply
    assert "Водитель: Петров Пётр Петрович" in reply
    assert "Страхователь: Петров Пётр Петрович" in reply


async def test_image_order_does_not_affect_which_roles_are_filled():
    """Сценарий 5 задачи: порядок изображений не влияет на результат — на
    уровне команды это означает, что порядок событий в альбоме не меняет
    ни набор картинок, переданных в OcrService, ни то, куда уходит reply
    (см. также test_album_with_multiple_photos_collects_all_images и
    test_recipient_order_does_not_affect_outcome-подобные тесты в других
    частях проекта)."""
    result = _full_result()

    order_a = [
        _FakeEvent(raw_text="", grouped_id=1, photo=object(), download_bytes=b"techpassport"),
        _FakeEvent(raw_text="insurance ocr", grouped_id=1, photo=object(), download_bytes=b"license"),
    ]
    order_b = [
        _FakeEvent(raw_text="insurance ocr", grouped_id=2, photo=object(), download_bytes=b"license"),
        _FakeEvent(raw_text="", grouped_id=2, photo=object(), download_bytes=b"techpassport"),
    ]

    ocr_a = _FakeOcrService(result=result)
    await _command(ocr_a).handle_album(order_a)

    ocr_b = _FakeOcrService(result=result)
    await _command(ocr_b).handle_album(order_b)

    images_a = {data for data, _mime in ocr_a.extract_calls[0]}
    images_b = {data for data, _mime in ocr_b.extract_calls[0]}
    assert images_a == images_b == {b"techpassport", b"license"}

    # В обоих случаях reply ушёл на сообщение с командой, с тем же текстом.
    trigger_a = next(e for e in order_a if e.raw_text == "insurance ocr")
    trigger_b = next(e for e in order_b if e.raw_text == "insurance ocr")
    assert trigger_a.replies == trigger_b.replies


async def test_vehicle_fields_are_independent_of_name_roles():
    """Сценарий 6 задачи: vehicle-поля (category/registration_number/vin/
    chassis_number/manufacturer/model) всегда показываются одинаково,
    независимо от того, какие роли ФИО заполнены — они читаются из
    отдельных атрибутов OcrResult и не связаны с owner/driver/policyholder
    никаким кодом. category — тоже vehicle-поле (техпаспорт), см. задачу
    про добавление категории ТС."""
    event = _FakeEvent(photo=object())
    result = _full_result(
        owner_full_name=None, driver_full_name=None, policyholder_full_name=None,
    )
    command = _command(_FakeOcrService(result=result))

    await command.handle(_ctx(event))

    reply = event.replies[0]
    assert "Категория: passenger_car" in reply
    assert "Марка: Toyota" in reply
    assert "Модель: RAV4" in reply
    assert "VIN: JTMBR12345678901" in reply
    assert "Госномер: A123BC777" in reply


async def test_no_fallback_between_owner_driver_and_policyholder_roles():
    """Сценарий 7 задачи: если policyholder_full_name = null, а
    driver_full_name заполнен — reply НЕ должен подставить driver вместо
    страхователя (и наоборот). Каждая роль читается независимо (см.
    reader/commands/insurance_ocr.py::_REPLY_FIELDS — простой getattr на
    свой атрибут, без единого места, которое копировало бы значение
    одного поля в другое)."""
    event = _FakeEvent(photo=object())
    result = _full_result(
        owner_full_name="Иванов Иван Иванович",
        driver_full_name="Петров Пётр Петрович",
        policyholder_full_name=None,
    )
    command = _command(_FakeOcrService(result=result))

    await command.handle(_ctx(event))

    reply = event.replies[0]
    assert "Водитель: Петров Пётр Петрович" in reply
    assert "Страхователь: не распознано" in reply
    assert "Страхователь: Петров Пётр Петрович" not in reply


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
