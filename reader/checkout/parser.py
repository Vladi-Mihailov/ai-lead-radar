"""Разбор Telegram-текста для checkout — две разные вещи:

1. parse_ocr_message() — обратный разбор СОБСТВЕННОГО сообщения бота
   "Распознано: ..." (см. reader/commands/insurance_ocr.py::_format_result)
   обратно в OcrResult. Нужен потому что checkout не хранит сам OcrResult
   нигде отдельно — единственная запись о распознанных полях это то самое
   Telegram-сообщение, на которое отвечает оператор (см. задачу и
   reader/checkout/service.py). Источник правды по меткам/порядку полей —
   reader/ocr/models.py::REPLY_FIELD_LABELS, тот же, что использует и
   _format_result — гарантирует, что разбор не разъедется с форматом вывода.

2. parse_correction_reply() — разбор ИСПРАВЛЕННЫХ полей, которые оператор
   присылает вместо "pay" (см. задачу, тот же 9-полевой формат, но не
   обязательно все поля сразу — см. apply_corrections()).

НЕ делает попытку заново распознать текст через OCR (см. задачу: "Не
пытайся повторно OCR-ить этот текст") — чистый текстовый парсинг."""

from __future__ import annotations

from dataclasses import replace
from typing import Literal, get_args

from reader.ocr.models import REPLY_FIELD_LABELS, OcrResult

_NOT_RECOGNIZED = "не распознано"
_PAY_TRIGGER = "pay"

_CATEGORY_VALUES: tuple[str, ...] = get_args(Literal["passenger_car", "motorcycle", "trailer"])

_LABEL_TO_ATTR: dict[str, str] = dict(REPLY_FIELD_LABELS)
_ATTR_SET = frozenset(_LABEL_TO_ATTR.values())


class ReplyParseError(Exception):
    """Reply на OCR-сообщение не удалось интерпретировать ни как "pay", ни
    как корректные исправленные поля — сообщение (str(exc)) показывается
    оператору как есть, по аналогии с CommandError (см.
    reader/commands/base.py)."""


def is_pay_trigger(text: str) -> bool:
    return (text or "").strip().lower() == _PAY_TRIGGER


def parse_ocr_message(text: str) -> OcrResult:
    """Обратный разбор "Распознано: ...\\n\\nСобственник: ...\\n...\\n\\nПроверь
    данные." (см. reader/commands/insurance_ocr.py::_format_result) — строго
    ожидает все 9 полей; если реплай указывает не на наше "Распознано:"
    сообщение (или на повреждённый/чужой текст) — ReplyParseError."""
    if not (text or "").strip().startswith("Распознано:"):
        raise ReplyParseError(
            "Это не похоже на сообщение с результатом распознавания (\"Распознано: ...\")."
        )

    fields = _parse_labeled_lines(text)
    missing = _ATTR_SET - fields.keys()
    if missing:
        raise ReplyParseError(
            "Не удалось разобрать исходное сообщение распознавания — отсутствуют поля: "
            + ", ".join(sorted(missing))
        )

    return OcrResult(**{attr: _none_if_not_recognized(value) for attr, value in fields.items()})


def parse_correction_reply(text: str) -> dict[str, str | None]:
    """Разбирает reply оператора с исправленными полями (тот же формат, что
    и "Распознано: ...", но без require'а всех строк сразу — см.
    apply_corrections()). ReplyParseError — если ни одной распознаваемой
    метки не найдено (значит это не исправленные поля и не "pay" — см.
    reader/checkout/service.py, который сам решает, что это "непонятный
    reply"), либо если Категория указана, но не входит в допустимый enum."""
    fields = _parse_labeled_lines(text)
    if not fields:
        raise ReplyParseError(
            "Не понял ответ. Ответьте `pay` для оформления, или пришлите исправленные "
            "поля в формате:\n\n"
            + "\n".join(f"{label}: ..." for label, _attr in REPLY_FIELD_LABELS)
        )

    corrections: dict[str, str | None] = {
        attr: _none_if_not_recognized(value) for attr, value in fields.items()
    }

    category = corrections.get("category")
    if category is not None and category not in _CATEGORY_VALUES:
        raise ReplyParseError(
            f"Недопустимое значение Категория: '{category}'. Допустимо: "
            + ", ".join(_CATEGORY_VALUES)
            + f" (или '{_NOT_RECOGNIZED}')."
        )

    return corrections


def apply_corrections(original: OcrResult, corrections: dict[str, str | None]) -> OcrResult:
    """original — распознанные поля исходного OCR-сообщения; corrections —
    только те поля, что оператор явно указал в reply (см.
    parse_correction_reply). Поля, отсутствующие в corrections, остаются
    значениями original — оператор мог прислать не все 9 строк, если менял
    только часть полей."""
    return replace(original, **corrections)


def _parse_labeled_lines(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in (text or "").splitlines():
        label, sep, value = line.partition(":")
        if not sep:
            continue
        attr = _LABEL_TO_ATTR.get(label.strip())
        if attr is None:
            continue
        fields[attr] = value.strip()
    return fields


def _none_if_not_recognized(value: str) -> str | None:
    if not value or value.strip().lower() == _NOT_RECOGNIZED:
        return None
    return value
