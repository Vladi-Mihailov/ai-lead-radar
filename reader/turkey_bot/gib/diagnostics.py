"""Диагностика неожиданных ответов GİB — ТОЛЬКО логирование.

Зачем: 27.11.2026 GİB 9 раз подряд вернул ответ, который parser.py
честно отнёс к "unexpected", но в логе не осталось ничего, кроме самого
слова "unexpected" — ни HTTP-статуса, ни Retry-After, ни текста
сообщения. Этот модуль добавляет одну строку WARNING с безопасными
метаданными, чтобы следующий сбой можно было разобрать по логу, без
дополнительных запросов к GİB.

Что этот модуль НЕ делает (и не должен делать):
  - не меняет классификацию ответа (parser.py не трогается, исход
    "unexpected" остаётся "unexpected");
  - не делает запросов, повторов, пауз, не меняет маршруты;
  - не пишет в БД и не влияет на уведомления.

Что НИКОГДА не попадает в лог: госномер, персональные данные, cookies,
токены, CAPTCHA (код и image id), полное тело ответа, сведения о
штрафах (поля "data"/"BORCLAR" не читаются вовсе). Из тела берутся
только имена ключей верхнего уровня (схема, не данные), тип и код
сообщения и текст сообщения из известного поля messages[].text — и
этот текст проходит маскирование ниже. Если текст нельзя безопасно
проверить (не строка или слишком длинный), он не логируется вовсе:
остаются только категория и метаданные.

Категория — подсказка по наблюдаемым признакам (HTTP-статус, Retry-After,
ключевые слова сообщения, форма ответа), а не доказанная причина:
  - rate_limit     — лимит частоты запросов;
  - blocked        — блокировка/отказ в доступе;
  - service_error  — техническая ошибка или недоступность сервиса;
  - unknown_format — ответ незнакомой формы (не объект, нет известных полей);
  - undetermined   — не определено (например, незнакомый текст ERROR/WARNING
    при HTTP 200 без других признаков).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

RATE_LIMIT = "rate_limit"
BLOCKED = "blocked"
SERVICE_ERROR = "service_error"
UNKNOWN_FORMAT = "unknown_format"
UNDETERMINED = "undetermined"

# Текст длиннее этого не проверяется и не логируется: длинный текст —
# уже не короткое системное сообщение, и маскированию в нём доверять нельзя.
_MAX_TEXT_SOURCE_CHARS = 500
_MAX_TEXT_LOG_CHARS = 200
_MAX_TITLE_LOG_CHARS = 80
_MAX_MESSAGES_LOGGED = 3
_MAX_KEYS_LOGGED = 12
_HTML_SCAN_CHARS = 4096

# Ключевые слова — в "турецком" нижнем регистре (см. _turkish_lower).
_RATE_LIMIT_WORDS = (
    "çok fazla", "fazla sayıda", "sorgu limit", "limit", "sınır", "kota",
    "too many", "rate limit", "throttl",
)
_BLOCKED_WORDS = (
    "engellen", "erişiminiz", "erişim engel", "yasaklan", "access denied",
    "forbidden", "blocked", "request rejected", "url was rejected",
)
_SERVICE_WORDS = (
    "bakım", "geçici olarak", "hizmet ver", "sistem hata", "beklenmeyen",
    "hata oluştu", "zaman aşımı", "maintenance", "unavailable",
    "internal server error", "bad gateway", "timeout", "timed out",
)

# Аббревиатуры в верхнем регистре, которые не маскируются (не имена).
_ALLOWED_UPPER_WORDS = frozenset({"GİB", "GIB", "HTTP", "API", "URL", "TC", "IP", "SSL", "TL", "ERROR", "WARNING"})

# Известные поля с кодом ошибки: в элементе messages[] и на верхнем уровне.
_MESSAGE_CODE_KEYS = ("code", "messageCode", "errorCode", "kod", "hataKodu")
_TOP_LEVEL_CODE_KEYS = ("code", "errorCode", "hataKodu", "status")

_SAFE_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,39}")
_SAFE_CODE_RE = re.compile(r"[A-Za-z0-9_.\-]{1,40}")
_SAFE_TYPE_RE = re.compile(r"[A-Za-z_]{1,20}")
_RETRY_AFTER_SECONDS_RE = re.compile(r"\d{1,7}")
_RETRY_AFTER_DATE_RE = re.compile(r"[A-Za-z0-9 ,:\-]{1,40}")
_CONTENT_TYPE_RE = re.compile(r"[a-z0-9.+\-]{1,40}/[a-z0-9.+\-]{1,40}")

_TAG_RE = re.compile(r"<[^>]{0,200}>")
_EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_MIXED_TOKEN_RE = re.compile(r"\b(?=\w*\d)(?=\w*[^\W\d_])\w+\b")
_DECIMAL_RE = re.compile(r"\d+(?:[.,]\d+)+")
_LONG_DIGITS_RE = re.compile(r"\d{5,}")
_WORD_RE = re.compile(r"[^\W\d_]{2,}")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class GibResponseMeta:
    """Метаданные ответа submit — без тела, cookies и заголовков запроса."""

    status_code: int
    duration_ms: int
    retry_after: str | None
    content_type: str | None


def response_meta(response: httpx.Response, *, duration_ms: int) -> GibResponseMeta:
    return GibResponseMeta(
        status_code=response.status_code,
        duration_ms=duration_ms,
        retry_after=_safe_retry_after(response.headers.get("retry-after")),
        content_type=_safe_content_type(response.headers.get("content-type")),
    )


def _safe_retry_after(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if _RETRY_AFTER_SECONDS_RE.fullmatch(value) or _RETRY_AFTER_DATE_RE.fullmatch(value):
        return value
    return "<invalid>"


def _safe_content_type(value: str | None) -> str | None:
    if not value:
        return None
    main = value.split(";", 1)[0].strip().lower()
    return main if _CONTENT_TYPE_RE.fullmatch(main) else "<invalid>"


def _turkish_lower(text: str) -> str:
    return text.replace("İ", "i").replace("I", "ı").lower()


def _plate_pattern(plate: str | None) -> re.Pattern[str] | None:
    if not plate:
        return None
    compact = re.sub(r"[\s\-]", "", plate)
    if not compact:
        return None
    return re.compile(r"[\s\-]*".join(re.escape(ch) for ch in compact), re.IGNORECASE)


def _mask_upper_word(match: re.Match[str]) -> str:
    word = match.group(0)
    if word.isupper() and word not in _ALLOWED_UPPER_WORDS:
        return "<caps>"
    return word


def safe_text(text: object, *, plate: str | None, max_chars: int = _MAX_TEXT_LOG_CHARS) -> str | None:
    """Маскированный короткий текст или None, если безопасно нельзя.

    Маскируются: запрошенный госномер (в любом написании с пробелами/
    дефисами), e-mail, ссылки, токены из букв и цифр (номера, id, хеши),
    десятичные числа (суммы), числа из 5+ цифр (документы, телефоны,
    номера протоколов), слова целиком в верхнем регистре (имена в данных
    GİB пишутся заглавными) кроме известных аббревиатур. Короткие числа
    (до 4 цифр, например "5 dakika") остаются — они нужны для диагностики.
    """
    if not isinstance(text, str) or len(text) > _MAX_TEXT_SOURCE_CHARS:
        return None
    cleaned = unicodedata.normalize("NFC", text)
    cleaned = "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in cleaned)
    cleaned = _TAG_RE.sub(" ", cleaned)
    pattern = _plate_pattern(plate)
    if pattern is not None:
        cleaned = pattern.sub("<plate>", cleaned)
    cleaned = _EMAIL_RE.sub("<email>", cleaned)
    cleaned = _URL_RE.sub("<url>", cleaned)
    cleaned = _MIXED_TOKEN_RE.sub("<id>", cleaned)
    cleaned = _DECIMAL_RE.sub("<num>", cleaned)
    cleaned = _LONG_DIGITS_RE.sub("<num>", cleaned)
    cleaned = _WORD_RE.sub(_mask_upper_word, cleaned)
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rstrip() + "…"
    return cleaned


def classify(*, status_code: int | None, retry_after: str | None, texts: list[str], known_shape: bool) -> str:
    """Категория по признакам — подсказка, не доказательство (см. докстрок модуля)."""
    if status_code == 429:
        return RATE_LIMIT
    if status_code in (401, 403):
        return BLOCKED
    if status_code is not None and status_code >= 500:
        return SERVICE_ERROR
    lowered = " ".join(_turkish_lower(t) for t in texts)
    if any(word in lowered for word in _RATE_LIMIT_WORDS):
        return RATE_LIMIT
    if any(word in lowered for word in _BLOCKED_WORDS):
        return BLOCKED
    if any(word in lowered for word in _SERVICE_WORDS):
        return SERVICE_ERROR
    if retry_after is not None:
        return RATE_LIMIT
    if not known_shape:
        return UNKNOWN_FORMAT
    return UNDETERMINED


def _shape_of(payload: object) -> str:
    if isinstance(payload, dict):
        return "object"
    if isinstance(payload, list):
        return "array"
    if isinstance(payload, str):
        return "string"
    if payload is None:
        return "null"
    if isinstance(payload, (bool, int, float)):
        return "scalar"
    return "other"


def _safe_code(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if 0 <= value < 100000 else None
    if isinstance(value, str) and _SAFE_CODE_RE.fullmatch(value.strip()) and not _LONG_DIGITS_RE.search(value):
        return value.strip()
    return None


def describe_unexpected(payload: object, *, meta: GibResponseMeta | None, plate: str | None) -> str:
    """Одна строка для лога по "unexpected"-ответу submit."""
    keys = "-"
    messages_info = "absent"
    types: list[str] = []
    codes: list[str] = []
    raw_texts: list[str] = []
    safe_texts: list[str] = []
    known_shape = False

    if isinstance(payload, dict):
        key_names = [k if isinstance(k, str) and _SAFE_KEY_RE.fullmatch(k) else "?" for k in list(payload)[:_MAX_KEYS_LOGGED]]
        keys = ",".join(key_names) if key_names else "-"
        if len(payload) > _MAX_KEYS_LOGGED:
            keys += ",…"
        for key in _TOP_LEVEL_CODE_KEYS:
            code = _safe_code(payload.get(key))
            if code is not None:
                codes.append(f"{key}:{code}")
        raw_messages = payload.get("messages")
        if isinstance(raw_messages, list):
            messages_info = str(len(raw_messages))
            for item in raw_messages[:_MAX_MESSAGES_LOGGED]:
                if not isinstance(item, dict):
                    types.append("?")
                    continue
                item_type = item.get("type")
                if isinstance(item_type, str) and _SAFE_TYPE_RE.fullmatch(item_type):
                    types.append(item_type)
                    if item_type in ("ERROR", "WARNING"):
                        known_shape = True
                else:
                    types.append("?")
                for key in _MESSAGE_CODE_KEYS:
                    code = _safe_code(item.get(key))
                    if code is not None:
                        codes.append(code)
                text = item.get("text")
                if isinstance(text, str):
                    raw_texts.append(text[:_MAX_TEXT_SOURCE_CHARS])
                if text is None:
                    continue
                masked = safe_text(text, plate=plate)
                safe_texts.append(f'"{masked}"' if masked is not None else "<omitted>")
        elif raw_messages is None and "messages" in payload:
            messages_info = "null"
        elif "messages" in payload:
            messages_info = "invalid"

    status_code = meta.status_code if meta else None
    retry_after = meta.retry_after if meta else None
    category = classify(status_code=status_code, retry_after=retry_after, texts=raw_texts, known_shape=known_shape)
    return (
        f"GIB unexpected response: category={category}"
        f" http_status={status_code if status_code is not None else '-'}"
        f" retry_after={retry_after or '-'}"
        f" duration_ms={meta.duration_ms if meta else '-'}"
        f" content_type={(meta.content_type if meta else None) or '-'}"
        f" shape={_shape_of(payload)} keys={keys} messages={messages_info}"
        f" types={','.join(types) or '-'} codes={','.join(codes) or '-'}"
        f" text={' | '.join(safe_texts) or '-'}"
    )


def log_unexpected_response(payload: object, *, meta: GibResponseMeta | None, plate: str | None) -> None:
    """Пишет строку WARNING; сама диагностика никогда не ломает проверку."""
    try:
        line = describe_unexpected(payload, meta=meta, plate=plate)
    except Exception as exc:  # noqa: BLE001 - диагностика не должна влиять на проверку
        logger.warning("GIB unexpected response: diagnostics failed (%s)", type(exc).__name__)
        return
    logger.warning("%s", line)


def describe_non_json(response: httpx.Response, *, meta: GibResponseMeta, plate: str | None) -> str:
    """Поля для строки "response is not valid JSON": без тела, только <title>."""
    title = None
    try:
        match = _TITLE_RE.search(response.text[:_HTML_SCAN_CHARS])
    except Exception:  # noqa: BLE001 - кодировка тела не должна ломать лог
        match = None
    if match:
        title = safe_text(match.group(1), plate=plate, max_chars=_MAX_TITLE_LOG_CHARS)
    category = classify(
        status_code=meta.status_code, retry_after=meta.retry_after,
        texts=[match.group(1)[:_MAX_TEXT_SOURCE_CHARS]] if match else [], known_shape=False,
    )
    title_field = f'"{title}"' if title else "-"
    return (
        f"category={category} retry_after={meta.retry_after or '-'}"
        f" duration_ms={meta.duration_ms} content_type={meta.content_type or '-'}"
        f" length={len(response.content)} title={title_field}"
    )
