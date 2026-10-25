"""Unit tests for reader/fines/collector_auth.py — the shared HMAC sign/
verify module between production's collector API and the REDNET collector's
production client. See tests/test_georgia_collector_api.py for the same
behavior exercised through a real HTTP request."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.fines.collector_auth import sign, verify

_SECRET = "s3cr3t"


def test_valid_signature_verifies():
    ts = "1000.0"
    signature = sign(_SECRET, method="GET", path="/x", timestamp=ts, body=b"")
    is_valid, reason = verify(
        _SECRET, method="GET", path="/x", timestamp=ts, body=b"", signature=signature, now=1000.0,
    )
    assert is_valid is True
    assert reason is None


def test_wrong_secret_fails():
    ts = "1000.0"
    signature = sign("other-secret", method="GET", path="/x", timestamp=ts, body=b"")
    is_valid, reason = verify(
        _SECRET, method="GET", path="/x", timestamp=ts, body=b"", signature=signature, now=1000.0,
    )
    assert is_valid is False
    assert reason == "invalid_signature"


def test_tampered_body_fails():
    ts = "1000.0"
    signature = sign(_SECRET, method="POST", path="/x", timestamp=ts, body=b'{"a":1}')
    is_valid, reason = verify(
        _SECRET, method="POST", path="/x", timestamp=ts, body=b'{"a":2}',
        signature=signature, now=1000.0,
    )
    assert is_valid is False
    assert reason == "invalid_signature"


def test_tampered_path_fails():
    ts = "1000.0"
    signature = sign(_SECRET, method="GET", path="/x", timestamp=ts, body=b"")
    is_valid, reason = verify(
        _SECRET, method="GET", path="/y", timestamp=ts, body=b"", signature=signature, now=1000.0,
    )
    assert is_valid is False
    assert reason == "invalid_signature"


def test_stale_timestamp_rejected():
    ts = "1000.0"
    signature = sign(_SECRET, method="GET", path="/x", timestamp=ts, body=b"")
    is_valid, reason = verify(
        _SECRET, method="GET", path="/x", timestamp=ts, body=b"", signature=signature,
        now=1000.0 + 500, replay_window_seconds=120,
    )
    assert is_valid is False
    assert reason == "stale_timestamp"


def test_missing_timestamp_rejected():
    is_valid, reason = verify(_SECRET, method="GET", path="/x", timestamp="", body=b"", signature="abc")
    assert is_valid is False
    assert reason == "missing_timestamp"


def test_missing_signature_rejected():
    is_valid, reason = verify(
        _SECRET, method="GET", path="/x", timestamp="1000.0", body=b"", signature="", now=1000.0,
    )
    assert is_valid is False
    assert reason == "missing_signature"


def test_invalid_timestamp_format_rejected():
    is_valid, reason = verify(
        _SECRET, method="GET", path="/x", timestamp="not-a-number", body=b"", signature="abc",
    )
    assert is_valid is False
    assert reason == "invalid_timestamp"
