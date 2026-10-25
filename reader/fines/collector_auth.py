"""HMAC-SHA256 request signing/verification shared between the production
internal collector API (reader/public_bot/collector_api.py) and the REDNET
collector's production client (reader/georgia_collector/production_client.py)
— see "Georgia remote collector architecture (REDNET)".

Deliberately stdlib-only (hmac/hashlib/time) so the REDNET side can import
this module without pulling in anything beyond requirements-collector.txt.

Canonical signed string binds timestamp + method + path + body hash, so a
valid signature can't be replayed against a different route or with a
tampered body. Never log the secret, the signature, or the canonical
string itself — only the short rejection-reason category.
"""

import hashlib
import hmac
import time

TIMESTAMP_HEADER = "X-Collector-Timestamp"
SIGNATURE_HEADER = "X-Collector-Signature"

# ±120s — generous enough for clock drift between two independent VPS without
# meaningfully widening the replay window (see module docstring).
DEFAULT_REPLAY_WINDOW_SECONDS = 120


def canonical_string(*, method: str, path: str, timestamp: str, body: bytes) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    return f"{timestamp}.{method.upper()}.{path}.{body_hash}"


def sign(secret: str, *, method: str, path: str, timestamp: str, body: bytes) -> str:
    message = canonical_string(method=method, path=path, timestamp=timestamp, body=body)
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def verify(
    secret: str,
    *,
    method: str,
    path: str,
    timestamp: str,
    body: bytes,
    signature: str,
    now: float | None = None,
    replay_window_seconds: int = DEFAULT_REPLAY_WINDOW_SECONDS,
) -> tuple[bool, str | None]:
    """(is_valid, rejection_reason) — reason is a short, log-safe category:
    "missing_timestamp" / "invalid_timestamp" / "stale_timestamp" /
    "missing_signature" / "invalid_signature". None when is_valid is True."""
    if not timestamp:
        return False, "missing_timestamp"
    try:
        ts = float(timestamp)
    except ValueError:
        return False, "invalid_timestamp"

    current = now if now is not None else time.time()
    if abs(current - ts) > replay_window_seconds:
        return False, "stale_timestamp"

    if not signature:
        return False, "missing_signature"

    expected = sign(secret, method=method, path=path, timestamp=timestamp, body=body)
    # compare_digest — constant-time, avoids leaking signature bytes via
    # response-time side channel.
    if not hmac.compare_digest(expected, signature):
        return False, "invalid_signature"

    return True, None
