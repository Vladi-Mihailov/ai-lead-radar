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
токены, CAPTCHA (код и image id), полное тело ответа, сведения о штрафах
(поля "data"/"BORCLAR" не читаются вовсе). Из тела берутся только:
  - имена ключей верхнего уровня (схема, не данные; без цифр);
  - тип и код сообщения (строгий формат);
  - текст сообщения из известного поля messages[].text — по БЕЛОМУ
    списку: текст пишется, только если в нём нет слов с заглавной буквы
    (кроме первого слова предложения и известных аббревиатур), нет чисел
    (кроме "5 dakika"-подобных интервалов), нет токенов из букв и цифр и
    нет e-mail. Иначе пишется только причина: text=<omitted:...>.
Запрошенный номер, код CAPTCHA и image id дополнительно маскируются как
точные значения (в любом написании с разделителями).

Категория — подсказка по наблюдаемым признакам, а не доказанная причина;
рядом пишется signal — какой именно признак сработал:
  - rate_limit     — лимит частоты запросов;
  - blocked        — блокировка/отказ в доступе;
  - service_error  — техническая ошибка или недоступность сервиса;
  - unknown_format — ответ незнакомой формы (не объект, нет известных полей);
  - undetermined   — не определено.
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

_MAX_TEXT_SOURCE_CHARS = 300
_MAX_TEXT_LOG_CHARS = 200
_MAX_TITLE_LOG_CHARS = 80
_MAX_MESSAGES_LOGGED = 3
_MAX_KEYS_LOGGED = 12
_HTML_SCAN_BYTES = 4096
_MIN_SECRET_CHARS = 4

# Фразы, а не отдельные слова: "sınır" (граница) и "limit" (лимит карты)
# встречаются и в обычных текстах.
_KEYWORDS = (
    (RATE_LIMIT, (
        "çok fazla", "fazla sayıda", "sorgu limit", "sorgu sınır", "sınırını aş", "sınırı aş",
        "limitini aş", "limiti aş", "limit aşıl", "kota", "too many", "rate limit", "throttl",
    )),
    (BLOCKED, (
        "engellen", "erişiminiz", "erişim engel", "yasaklan", "access denied", "forbidden",
        "blocked", "request rejected", "url was rejected",
    )),
    (SERVICE_ERROR, (
        "bakım", "geçici olarak", "hizmet verem", "hizmet verilem", "sistem hata", "beklenmeyen",
        "hata oluştu", "zaman aşımı", "maintenance", "unavailable", "internal server error",
        "bad gateway", "timeout", "timed out",
    )),
)

# Типовые заголовки страниц шлюзов/WAF — пишутся как есть (наш список).
_KNOWN_TITLES = frozenset({
    "request rejected", "service unavailable", "service temporarily unavailable", "bad gateway",
    "gateway timeout", "gateway time-out", "too many requests", "access denied", "forbidden",
    "internal server error", "maintenance", "error", "bakım", "hata", "erişim engellendi",
})

_ALLOWED_UPPER_WORDS = frozenset({"GİB", "GIB", "HTTP", "API", "URL", "TC", "IP", "SSL", "TL"})
_TIME_UNIT_RE = re.compile(
    r"\s*(?:saniye|sn|dakika|dk|saat|gün|seconds?|secs?|minutes?|mins?|hours?|days?)\b", re.IGNORECASE,
)
_SENTENCE_END_CHARS = ".!?:;…"
_OPENING_PUNCT = " \"'(«[“‘"

_MESSAGE_CODE_KEYS = ("code", "messageCode", "errorCode", "kod", "hataKodu")
_TOP_LEVEL_CODE_KEYS = ("code", "errorCode", "hataKodu", "status")
_CONTENT_MAIN_TYPES = frozenset({"application", "text", "image", "multipart", "message", "audio", "video", "font"})

_SAFE_KEY_RE = re.compile(r"[A-Za-z_]{1,40}")
_SAFE_CODE_RE = re.compile(r"[A-Za-z0-9_.\-]{1,40}")
_SAFE_TYPE_RE = re.compile(r"[A-Za-z_]{1,20}")
_RETRY_AFTER_SECONDS_RE = re.compile(r"\d{1,7}")
_RETRY_AFTER_DATE_RE = re.compile(
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), \d{2} (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
    r"\d{4} \d{2}:\d{2}:\d{2} GMT"
)
_CONTENT_SUBTYPE_RE = re.compile(r"[a-z.+\-]{1,40}")

_TAG_RE = re.compile(r"<[^>]{0,200}>")
_EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_MIXED_TOKEN_RE = re.compile(r"\b(?=\w*\d)(?=\w*[^\W\d_])\w+\b")
_NUMBER_RE = re.compile(r"\d+(?:[.,:/\-]\d+)*")
_WORD_RE = re.compile(r"[^\W\d_]+")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_STATUS_PREFIX_RE = re.compile(r"^\d{3}\s+")


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
    main, _, subtype = value.split(";", 1)[0].strip().lower().partition("/")
    if main in _CONTENT_MAIN_TYPES and _CONTENT_SUBTYPE_RE.fullmatch(subtype):
        return f"{main}/{subtype}"
    return "<other>"


def _turkish_lower(text: str) -> str:
    return text.replace("İ", "i").replace("I", "ı").lower()


def _compact(value: str) -> str:
    return re.sub(r"[\W_]", "", value).casefold()


def _secret_patterns(secrets: dict[str, str | None]) -> list[tuple[re.Pattern[str], str]]:
    patterns = []
    for label, value in secrets.items():
        compact = re.sub(r"[\W_]", "", value or "")
        if len(compact) < _MIN_SECRET_CHARS:
            continue
        patterns.append((re.compile(r"[\W_]*".join(re.escape(ch) for ch in compact), re.IGNORECASE), f"<{label}>"))
    return patterns


def _is_secret(value: str, secrets: dict[str, str | None]) -> bool:
    compact = _compact(value)
    return any(compact and compact == _compact(s or "") for s in secrets.values())


def _unsafe_reason(cleaned: str) -> str | None:
    """Причина, по которой текст нельзя писать в лог, или None (белый список)."""
    if _EMAIL_RE.search(cleaned):
        return "email"
    if _MIXED_TOKEN_RE.search(cleaned):
        return "id_like"
    for match in _NUMBER_RE.finditer(cleaned):
        number = match.group(0)
        if not (number.isdigit() and len(number) <= 4 and _TIME_UNIT_RE.match(cleaned, match.end())):
            return "number"
    for match in _WORD_RE.finditer(cleaned):
        word = match.group(0)
        if not any(ch.isupper() for ch in word) or word in _ALLOWED_UPPER_WORDS:
            continue
        prefix = cleaned[:match.start()].rstrip(_OPENING_PUNCT)
        at_sentence_start = not prefix or prefix[-1] in _SENTENCE_END_CHARS
        if at_sentence_start and not any(ch.isupper() for ch in word[1:]):
            continue
        return "capitalised_word"
    return None


def safe_text(
    text: object, *, secrets: dict[str, str | None], max_chars: int = _MAX_TEXT_LOG_CHARS,
) -> tuple[str | None, str | None]:
    """(текст, None), если его можно писать в лог, иначе (None, причина)."""
    if not isinstance(text, str):
        return None, "not_text"
    if len(text) > _MAX_TEXT_SOURCE_CHARS:
        return None, "too_long"
    cleaned = unicodedata.normalize("NFC", text)
    cleaned = "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in cleaned)
    cleaned = _TAG_RE.sub(" ", cleaned)
    for pattern, replacement in _secret_patterns(secrets):
        cleaned = pattern.sub(replacement, cleaned)
    cleaned = _URL_RE.sub("<url>", cleaned)
    cleaned = " ".join(cleaned.split())
    reason = _unsafe_reason(cleaned)
    if reason is not None:
        return None, reason
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rstrip() + "…"
    return cleaned, None


def classify(
    *, status_code: int | None, retry_after: str | None, texts: list[str], known_shape: bool,
) -> tuple[str, str]:
    """(категория, сработавший признак) — подсказка, не доказательство."""
    if status_code == 429:
        return RATE_LIMIT, "http_429"
    if status_code in (401, 403):
        return BLOCKED, f"http_{status_code}"
    if status_code is not None and status_code >= 500:
        return SERVICE_ERROR, f"http_{status_code}"
    haystacks = [_turkish_lower(t) for t in texts] + [t.casefold() for t in texts]
    for category, words in _KEYWORDS:
        for word in words:
            if any(word in haystack for haystack in haystacks):
                return category, "keyword:" + word.replace(" ", "_")
    if retry_after is not None:
        return RATE_LIMIT, "retry_after"
    if not known_shape:
        return UNKNOWN_FORMAT, "shape"
    return UNDETERMINED, "-"


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


def _safe_key(key: object, secrets: dict[str, str | None]) -> str:
    if isinstance(key, str) and _SAFE_KEY_RE.fullmatch(key) and not _is_secret(key, secrets):
        return key
    return "?"


def _safe_code(value: object, secrets: dict[str, str | None]) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if 0 <= value < 100000 else None
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not _SAFE_CODE_RE.fullmatch(value) or _is_secret(value, secrets):
        return None
    for group in re.split(r"[_.\-]", value):
        if group and not (group.isalpha() or (group.isdigit() and len(group) <= 5)):
            return None
    return value


def describe_unexpected(
    payload: object, *, meta: GibResponseMeta | None, plate: str | None,
    captcha_code: str | None = None, image_id: str | None = None,
) -> str:
    """Одна строка для лога по "unexpected"-ответу submit."""
    secrets = {"plate": plate, "captcha": captcha_code, "cid": image_id}
    keys = "-"
    messages_info = "absent"
    types: list[str] = []
    codes: list[str] = []
    raw_texts: list[str] = []
    safe_texts: list[str] = []
    known_shape = False

    if isinstance(payload, dict):
        key_names = [_safe_key(k, secrets) for k in list(payload)[:_MAX_KEYS_LOGGED]]
        keys = ",".join(key_names) if key_names else "-"
        if len(payload) > _MAX_KEYS_LOGGED:
            keys += ",…"
        for key in _TOP_LEVEL_CODE_KEYS:
            code = _safe_code(payload.get(key), secrets)
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
                    code = _safe_code(item.get(key), secrets)
                    if code is not None:
                        codes.append(code)
                text = item.get("text")
                if text is None:
                    continue
                if isinstance(text, str):
                    raw_texts.append(text[:_MAX_TEXT_SOURCE_CHARS])
                masked, reason = safe_text(text, secrets=secrets)
                safe_texts.append(f'"{masked}"' if masked is not None else f"<omitted:{reason}>")
        elif "messages" in payload:
            messages_info = "null" if raw_messages is None else "invalid"

    status_code = meta.status_code if meta else None
    retry_after = meta.retry_after if meta else None
    category, signal = classify(
        status_code=status_code, retry_after=retry_after, texts=raw_texts, known_shape=known_shape,
    )
    return (
        f"GIB unexpected response: category={category} signal={signal}"
        f" http_status={status_code if status_code is not None else '-'}"
        f" retry_after={retry_after or '-'}"
        f" duration_ms={meta.duration_ms if meta else '-'}"
        f" content_type={(meta.content_type if meta else None) or '-'}"
        f" shape={_shape_of(payload)} keys={keys} messages={messages_info}"
        f" types={','.join(types) or '-'} codes={','.join(codes) or '-'}"
        f" text={' | '.join(safe_texts) or '-'}"
    )


def log_unexpected_response(
    payload: object, *, meta: GibResponseMeta | None, plate: str | None,
    captcha_code: str | None = None, image_id: str | None = None,
) -> None:
    """Пишет строку WARNING; сама диагностика никогда не ломает проверку."""
    try:
        line = describe_unexpected(
            payload, meta=meta, plate=plate, captcha_code=captcha_code, image_id=image_id,
        )
    except Exception as exc:  # noqa: BLE001 - диагностика не должна влиять на проверку
        logger.warning("GIB unexpected response: diagnostics failed (%s)", type(exc).__name__)
        return
    logger.warning("%s", line)


def _safe_title(raw: str, secrets: dict[str, str | None]) -> str:
    normalized = " ".join(raw.split())
    if _STATUS_PREFIX_RE.sub("", normalized).casefold() in _KNOWN_TITLES:
        return f'"{normalized}"'
    title, reason = safe_text(raw, secrets=secrets, max_chars=_MAX_TITLE_LOG_CHARS)
    return f'"{title}"' if title is not None else f"<omitted:{reason}>"


def describe_non_json(
    response: httpx.Response, *, meta: GibResponseMeta | None, plate: str | None,
    captcha_code: str | None = None, image_id: str | None = None,
) -> str:
    """Поля для строки "response is not valid JSON": без тела, только <title>.

    Читаются только первые 4 КБ тела (без полного декодирования).
    """
    secrets = {"plate": plate, "captcha": captcha_code, "cid": image_id}
    head = response.content[:_HTML_SCAN_BYTES].decode("utf-8", "replace")
    match = _TITLE_RE.search(head)
    raw_title = match.group(1)[:_MAX_TEXT_SOURCE_CHARS] if match else None
    status_code = meta.status_code if meta else response.status_code
    retry_after = meta.retry_after if meta else None
    category, signal = classify(
        status_code=status_code, retry_after=retry_after,
        texts=[raw_title] if raw_title else [], known_shape=False,
    )
    return (
        f"category={category} signal={signal} retry_after={retry_after or '-'}"
        f" duration_ms={meta.duration_ms if meta else '-'}"
        f" content_type={(meta.content_type if meta else None) or '-'}"
        f" length={len(response.content)} title={_safe_title(raw_title, secrets) if raw_title else '-'}"
    )
