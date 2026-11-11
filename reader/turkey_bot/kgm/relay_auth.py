"""HMAC-подпись запросов production -> KGM relay на грузинском сервере (см.
docs/kgm-relay.md, relay_server.py и relay_client.py этого же пакета).

НАМЕРЕННО отдельный модуль, а не reader/fines/collector_auth.py: REDNET-
коллектор police.ge остаётся байт-в-байт прежним, у relay свой секрет
(KGM_RELAY_HMAC_SECRET на грузинском сервере == TURKEY_KGM_RELAY_HMAC_SECRET
на production) и свои заголовки — утечка или ротация одного секрета не
затрагивает другой канал. И строже collector_auth: окно по времени ±60 с
вместо 120 с, а каждый запрос несёт одноразовый nonce, который тоже входит
в подпись, — повтор того же подписанного запроса внутри окна (replay)
отклоняется.

Каноническая строка: f"{timestamp}.{METHOD}.{path}.{nonce}.{sha256(body)}" —
path это путь запроса ровно в том виде, в каком он ушёл по сети (вместе с
query, если она есть), body — сырые байты тела (пустое тело тоже
хэшируется). Только stdlib: модуль импортируют обе стороны, а relay ставится
с минимальным requirements-kgm-relay.txt."""

import hashlib
import hmac
import re
import secrets
import time

TIMESTAMP_HEADER = "X-Kgm-Timestamp"
NONCE_HEADER = "X-Kgm-Nonce"
SIGNATURE_HEADER = "X-Kgm-Signature"

MIN_SECRET_LENGTH = 32
MAX_CLOCK_SKEW_SECONDS = 60

# Nonce обязан помниться, пока его timestamp ещё может пройти проверку окна:
# запрос принят в момент r при |r - ts| <= 60, значит ts + 60 <= r + 120.
NONCE_TTL_SECONDS = 120

# С большим запасом: одна проверка номера — это единицы запросов в
# секунду (KgmSession сама выдерживает 1.5 с между запросами к KGM), за
# 120 с набирается порядка сотни nonce, не тысячи.
NONCE_CACHE_MAX_SIZE = 4096

# Только ASCII-цифры/hex: [0-9] в re не совпадает с цифрами других
# алфавитов (в отличие от \d), а hmac.compare_digest на str требует ASCII.
_TIMESTAMP_RE = re.compile(r"[0-9]{1,12}")
_NONCE_RE = re.compile(r"[0-9a-f]{32}")
_SIGNATURE_RE = re.compile(r"[0-9a-f]{64}")


def secret_problem(secret: object) -> str | None:
    """None — секрет пригоден; иначе короткая категория для лога ("missing"/
    "too_short") — сам секрет и его длина в неё не попадают."""
    if not isinstance(secret, str) or not secret:
        return "missing"
    if len(secret) < MIN_SECRET_LENGTH:
        return "too_short"
    return None


def require_secret(secret: object) -> None:
    problem = secret_problem(secret)
    if problem is not None:
        raise ValueError(f"KGM relay HMAC secret is {problem} (at least {MIN_SECRET_LENGTH} characters required)")


def canonical_string(*, method: str, path: str, timestamp: str, nonce: str, body: bytes) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    return f"{timestamp}.{method.upper()}.{path}.{nonce}.{body_hash}"


def sign(secret: str, *, method: str, path: str, timestamp: str, nonce: str, body: bytes) -> str:
    message = canonical_string(method=method, path=path, timestamp=timestamp, nonce=nonce, body=body)
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def new_nonce() -> str:
    return secrets.token_hex(16)


def signed_headers(
    secret: str,
    method: str,
    path: str,
    body: bytes,
    *,
    now: float | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """Три заголовка для ОДНОГО запроса — вызывающий код (relay_client.py)
    обязан получать свежий nonce на каждый запрос, повторная отправка тех
    же заголовков будет отклонена как replay. now/nonce — только для
    тестов."""
    require_secret(secret)
    nonce = new_nonce() if nonce is None else nonce
    if not _NONCE_RE.fullmatch(nonce):
        raise ValueError("nonce must be 32 lowercase hex characters")
    timestamp = str(int(time.time() if now is None else now))
    return {
        TIMESTAMP_HEADER: timestamp,
        NONCE_HEADER: nonce,
        SIGNATURE_HEADER: sign(secret, method=method, path=path, timestamp=timestamp, nonce=nonce, body=body),
    }


class NonceCache:
    """Ограниченный in-memory набор уже принятых nonce. Живёт только в
    процессе relay и никуда не сохраняется: после рестарта он пуст, и
    перехваченный за последние 60 с запрос теоретически можно повторить
    один раз — канал при этом и так внутри WireGuard.

    Время — тот же wall clock, что и у проверки окна в verify(): при
    скачке часов обе проверки сдвигаются вместе и инвариант из
    NONCE_TTL_SECONDS сохраняется."""

    def __init__(self, *, ttl_seconds: float = NONCE_TTL_SECONDS, max_size: int = NONCE_CACHE_MAX_SIZE):
        self._ttl = ttl_seconds
        self._max_size = max_size
        self._expires_at: dict[str, float] = {}

    def __len__(self) -> int:
        return len(self._expires_at)

    def purge(self, now: float) -> None:
        # Строго "<": nonce ещё помнится в самый последний момент, когда
        # его timestamp проходит окно (граница |now - ts| == 60).
        for nonce in [n for n, expires_at in self._expires_at.items() if expires_at < now]:
            del self._expires_at[nonce]

    def seen(self, nonce: str, now: float) -> bool:
        expires_at = self._expires_at.get(nonce)
        return expires_at is not None and expires_at >= now

    def remember(self, nonce: str, now: float) -> bool:
        """False — кэш заполнен НЕпросроченными nonce: запрос отклоняется
        (fail closed), а не вытесняется самый старый nonce — вытеснение
        открыло бы replay для него до конца его окна. Заполнить кэш может
        только владелец секрета: сюда попадают лишь запросы с верной
        подписью (см. verify())."""
        self.purge(now)
        if len(self._expires_at) >= self._max_size:
            return False
        self._expires_at[nonce] = now + self._ttl
        return True


def verify(
    secret: str,
    *,
    method: str,
    path: str,
    body: bytes,
    timestamp: str | None,
    nonce: str | None,
    signature: str | None,
    nonce_cache: NonceCache,
    now: float | None = None,
    max_skew_seconds: float = MAX_CLOCK_SKEW_SECONDS,
) -> tuple[bool, str | None]:
    """(True, None) — запрос подлинный и его nonce увиден впервые (теперь
    запомнен). Иначе (False, reason), reason — только из фиксированного
    набора категорий для лога: invalid_secret / missing_headers /
    invalid_timestamp / stale_timestamp / invalid_nonce / invalid_signature
    / replayed_nonce / nonce_cache_full. Nonce запоминается ТОЛЬКО после
    верной подписи — неподписанный запрос не может ни заполнить кэш, ни
    "сжечь" чужой nonce заранее."""
    if secret_problem(secret) is not None:
        return False, "invalid_secret"
    if not timestamp or not nonce or not signature:
        return False, "missing_headers"
    if not _TIMESTAMP_RE.fullmatch(timestamp):
        return False, "invalid_timestamp"
    current = time.time() if now is None else now
    if abs(current - int(timestamp)) > max_skew_seconds:
        return False, "stale_timestamp"
    if not _NONCE_RE.fullmatch(nonce):
        return False, "invalid_nonce"
    if not _SIGNATURE_RE.fullmatch(signature):
        return False, "invalid_signature"
    expected = sign(secret, method=method, path=path, timestamp=timestamp, nonce=nonce, body=body)
    if not hmac.compare_digest(expected, signature):
        return False, "invalid_signature"
    if nonce_cache.seen(nonce, current):
        return False, "replayed_nonce"
    if not nonce_cache.remember(nonce, current):
        return False, "nonce_cache_full"
    return True, None
