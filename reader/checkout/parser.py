"""Разбор Telegram-текста для checkout — две разные вещи:

1. parse_ocr_message() — обратный разбор СОБСТВЕННОГО сообщения бота
   "Распознано: ..." (см. reader/commands/insurance_ocr.py::_format_result)
   обратно в OcrResult. Нужен потому что checkout не хранит сам OcrResult
   нигде отдельно — единственная запись о распознанных полях это то самое
   Telegram-сообщение, на которое отвечает оператор. Источник правды по
   меткам/порядку полей — reader/ocr/models.py::REPLY_SECTIONS/
   REPLY_FIELD_LABELS, тот же, что использует и _format_result — гарантирует,
   что разбор не разъедется с форматом вывода. Лениентный разбор: строки, не
   похожие ни на одну известную метку (в т.ч. собственный заголовок
   "Распознано:"), просто пропускаются — сообщение уже гарантированно
   сгенерировано нашим кодом.

2. parse_correction_reply() — строгий разбор ИСПРАВЛЕННЫХ полей, которые
   оператор присылает вместо "pay" (не обязательно все поля сразу — см.
   apply_corrections()). В отличие от (1), это текст от человека, поэтому
   разбор строгий: неизвестная метка и повторно указанное поле — ошибка, а
   не молчаливый пропуск.

Оба флага "... = страхователь" поддерживают только "+"/"-" (никаких yes/no/
true/false) — см. FLAG_ATTRS.

НЕ делает попытку заново распознать текст через OCR — чистый текстовый
парсинг."""

from __future__ import annotations

from dataclasses import replace
from typing import Literal, get_args

from reader.ocr.models import FLAG_ATTRS, REPLY_FIELD_LABELS, OcrResult

_NOT_RECOGNIZED = "не распознано"
_PAY_TRIGGER = "pay"
_FLAG_TRUE = "+"
_FLAG_FALSE = "-"
_HEADER_LABEL = "Распознано"

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
    """Строго ожидает все поля reader/ocr/models.py::REPLY_FIELD_LABELS
    (в т.ч. оба флага); если реплай указывает не на наше "Распознано:"
    сообщение (или на повреждённый/чужой текст) — ReplyParseError."""
    if not (text or "").strip().startswith(f"{_HEADER_LABEL}:"):
        raise ReplyParseError(
            "Это не похоже на сообщение с результатом распознавания (\"Распознано: ...\")."
        )

    raw_fields = _scan_lines(text, strict=False)
    missing = _ATTR_SET - raw_fields.keys()
    if missing:
        raise ReplyParseError(
            "Не удалось разобрать исходное сообщение распознавания — отсутствуют поля: "
            + ", ".join(sorted(missing))
        )

    values = {
        attr: _convert_value(label=label, attr=attr, raw=raw_fields[attr])
        for label, attr in REPLY_FIELD_LABELS
    }
    return OcrResult(**values)


def parse_correction_reply(text: str) -> dict[str, str | bool | None]:
    """Разбирает reply оператора с исправленными полями — не обязательно все
    сразу (см. apply_corrections()). ReplyParseError — если ни одной
    распознаваемой метки не найдено (значит это не исправленные поля и не
    "pay"), если метка неизвестна, если метка повторяется, если значение
    флага не "+"/"-", либо если Категория указана, но не входит в
    допустимый enum."""
    raw_fields = _scan_lines(text, strict=True)
    if not raw_fields:
        raise ReplyParseError(
            "Не понял ответ. Ответьте `pay` для оформления, или пришлите исправленные "
            "поля в формате:\n\n"
            + "\n".join(f"{label}: ..." for label, _attr in REPLY_FIELD_LABELS)
        )

    label_by_attr = {attr: label for label, attr in REPLY_FIELD_LABELS}
    corrections: dict[str, str | bool | None] = {
        attr: _convert_value(label=label_by_attr[attr], attr=attr, raw=raw)
        for attr, raw in raw_fields.items()
    }

    category = corrections.get("category")
    if category is not None and category not in _CATEGORY_VALUES:
        raise ReplyParseError(
            f"Недопустимое значение Категория: '{category}'. Допустимо: "
            + ", ".join(_CATEGORY_VALUES)
            + f" (или '{_NOT_RECOGNIZED}')."
        )

    return corrections


def apply_corrections(original: OcrResult, corrections: dict[str, str | bool | None]) -> OcrResult:
    """original — эффективные поля исходного OCR-сообщения; corrections —
    только те поля, что оператор явно указал в reply (см.
    parse_correction_reply). Поля, отсутствующие в corrections, остаются
    значениями original — оператор мог прислать не все строки, если менял
    только часть полей."""
    return replace(original, **corrections)


def _scan_lines(text: str, *, strict: bool) -> dict[str, str]:
    """strict=False (parse_ocr_message, наше собственное сообщение) —
    неизвестные метки (включая заголовок "Распознано:") молча пропускаются.
    strict=True (parse_correction_reply, текст оператора) — неизвестная или
    повторная метка это ReplyParseError."""
    fields: dict[str, str] = {}
    for line in (text or "").splitlines():
        label, sep, value = line.partition(":")
        if not sep:
            continue
        label = label.strip()
        if not strict and label == _HEADER_LABEL:
            continue

        attr = _LABEL_TO_ATTR.get(label)
        if attr is None:
            if strict:
                raise ReplyParseError(f"Неизвестное поле '{label}'.")
            continue

        if attr in fields:
            if strict:
                raise ReplyParseError(f"Поле '{label}' указано более одного раза.")
            continue

        fields[attr] = value.strip()
    return fields


def _convert_value(*, label: str, attr: str, raw: str) -> str | bool | None:
    if attr in FLAG_ATTRS:
        return _parse_flag(label=label, raw=raw)
    return _none_if_not_recognized(raw)


def _parse_flag(*, label: str, raw: str) -> bool:
    value = raw.strip()
    if value == _FLAG_TRUE:
        return True
    if value == _FLAG_FALSE:
        return False
    raise ReplyParseError(
        f"Недопустимое значение '{label}: {raw}'. Допустимо только '{_FLAG_TRUE}' или '{_FLAG_FALSE}'."
    )


def _none_if_not_recognized(value: str) -> str | None:
    if not value or value.strip().lower() == _NOT_RECOGNIZED:
        return None
    return value
