"""Безопасная вставка пользовательского текста (имя/username Telegram-
пользователя) в сообщения @ProtocolGEbot/@ProtocolTRbot (см. задачу
"дополнить статистику штрафов информацией о пользователях").

Оба бота отправляют сообщения с Telethon parse_mode по умолчанию — 'md'
(см. reader/plate_display.py), а telethon.extensions.markdown НЕ
поддерживает backslash-escaping (чистый paired-delimiter парсер): "**",
"__", "~~", "`", "```" или "[текст](ссылка)" внутри имени стали бы
разметкой — разделитель из имени склеился бы с жирными буквами госномера
ниже по сообщению (символы имени пропали бы, форматирование съехало), а
"[текст](ссылка)" превратился бы в кликабельную ссылку со скрытым адресом.

Поэтому разметка в имени обезвреживается, а не экранируется (см.
escape_telethon_markdown), а username не экранируется вовсе — он
проверяется (см. safe_telegram_username): невидимый символ внутри
username разорвал бы автоматическое упоминание Telegram, и клик вёл бы
на ДРУГОЙ аккаунт."""

import re

_ZERO_WIDTH_SPACE = "​"
_BACKTICK_LOOKALIKE = "ˋ"
_PAIRED_DELIMITER_CHAR_RE = re.compile(r"([*_~])(?=\1)")
_TELEGRAM_USERNAME_RE = re.compile(r"[A-Za-z0-9_]+")


def escape_telethon_markdown(text: str) -> str:
    """Внутрь парных разделителей ("**"/"__"/"~~") и между "]" и "("
    вставляется zero-width space (невидим, но разделитель/ссылка больше не
    распознаются), одиночный "`" заменяется визуально близким "ˋ"
    (U+02CB) — у односимвольного разделителя нет места для вставки. Всё
    остальное, включая одиночные "*"/"_"/"~" и "[" / "]" вне "](", не
    меняется — скопированное имя совпадает с исходным.

    Шаблон не должен продолжать вставленный текст символом "(" вплотную
    (иначе "]" в конце имени + "(" шаблона снова дали бы "]("): в
    debt-списках после имени всегда перевод строки."""
    text = text.replace("`", _BACKTICK_LOOKALIKE)
    text = _PAIRED_DELIMITER_CHAR_RE.sub(lambda m: m.group(1) + _ZERO_WIDTH_SPACE, text)
    return text.replace("](", "]" + _ZERO_WIDTH_SPACE + "(")


def safe_telegram_username(username: str | None) -> str | None:
    """Username без "@", если он похож на настоящий Telegram username
    (латинские буквы, цифры и одиночные "_" — двух "_" подряд Telegram не
    допускает, а в Telethon md это был бы разделитель курсива), иначе None
    — показывать нечего. Такой username безопасен для md без изменений,
    поэтому вставляется как есть: упоминание кликабельно и копируется
    точно."""
    value = (username or "").strip().lstrip("@")
    if not _TELEGRAM_USERNAME_RE.fullmatch(value) or "__" in value:
        return None
    return value
