"""Тесты reader/turkey_bot/kgm/relay_auth.py — HMAC-подпись запросов
production -> KGM relay: подпись, окно ±60 с, nonce против replay,
ограниченный NonceCache, минимальная длина секрета."""

import hashlib
import ipaddress

import pytest

from reader.turkey_bot.kgm import relay_auth
from reader.turkey_bot.kgm.relay_auth import NonceCache

SECRET = "kgm-relay-test-secret-0123456789abcdef"
NOW = 1_800_000_000.0
PATH = "/internal/kgm/sessions/0123456789abcdef0123456789abcdef/submit"
BODY = b'{"plate": "34TST987", "captcha_code": "22 03713"}'
NONCE = "0123456789abcdef0123456789abcdef"


def _verify(headers: dict[str, str], *, method="POST", path=PATH, body=BODY, cache=None, now=NOW, secret=SECRET):
    return relay_auth.verify(
        secret, method=method, path=path, body=body,
        timestamp=headers.get(relay_auth.TIMESTAMP_HEADER),
        nonce=headers.get(relay_auth.NONCE_HEADER),
        signature=headers.get(relay_auth.SIGNATURE_HEADER),
        nonce_cache=cache if cache is not None else NonceCache(),
        now=now,
    )


def _headers(**overrides) -> dict[str, str]:
    kwargs = {"now": NOW, "nonce": NONCE} | overrides
    return relay_auth.signed_headers(SECRET, "POST", PATH, BODY, **kwargs)


def _header_kwargs(headers: dict[str, str]) -> dict[str, str]:
    return {
        "timestamp": headers[relay_auth.TIMESTAMP_HEADER],
        "nonce": headers[relay_auth.NONCE_HEADER],
        "signature": headers[relay_auth.SIGNATURE_HEADER],
    }


def test_canonical_string_covers_timestamp_method_path_nonce_and_body_hash():
    canonical = relay_auth.canonical_string(method="post", path=PATH, timestamp="1800000000", nonce=NONCE, body=BODY)

    assert canonical == f"1800000000.POST.{PATH}.{NONCE}.{hashlib.sha256(BODY).hexdigest()}"


def test_signed_headers_have_expected_shape():
    headers = relay_auth.signed_headers(SECRET, "POST", PATH, BODY)

    assert set(headers) == {"X-Kgm-Timestamp", "X-Kgm-Nonce", "X-Kgm-Signature"}
    assert headers["X-Kgm-Timestamp"].isdigit()
    assert len(headers["X-Kgm-Nonce"]) == 32 and headers["X-Kgm-Nonce"] == headers["X-Kgm-Nonce"].lower()
    assert len(headers["X-Kgm-Signature"]) == 64
    assert SECRET not in "".join(headers.values())


def test_each_call_gets_a_fresh_nonce():
    nonces = {relay_auth.signed_headers(SECRET, "GET", "/internal/kgm/health", b"")["X-Kgm-Nonce"] for _ in range(50)}

    assert len(nonces) == 50


def test_valid_signature_passes_and_remembers_the_nonce():
    cache = NonceCache()

    assert _verify(_headers(), cache=cache) == (True, None)
    assert len(cache) == 1


@pytest.mark.parametrize("field, value", [
    ("method", "PUT"),
    ("path", "/internal/kgm/sessions/ffffffffffffffffffffffffffffffff/submit"),
    ("body", b'{"plate": "34TST988", "captcha_code": "22 03713"}'),
    ("secret", "a-completely-different-secret-of-enough-length"),
])
def test_tampered_request_is_rejected(field, value):
    assert _verify(_headers(), **{field: value}) == (False, "invalid_signature")


def test_tampered_nonce_header_is_rejected():
    headers = _headers()
    headers["X-Kgm-Nonce"] = "f" * 32

    assert _verify(headers) == (False, "invalid_signature")


def test_tampered_timestamp_inside_the_window_is_rejected():
    headers = _headers()
    headers["X-Kgm-Timestamp"] = str(int(NOW) + 1)

    assert _verify(headers) == (False, "invalid_signature")


@pytest.mark.parametrize("skew, expected", [
    (-60, (True, None)),
    (60, (True, None)),
    (-61, (False, "stale_timestamp")),
    (61, (False, "stale_timestamp")),
    (-3600, (False, "stale_timestamp")),
])
def test_clock_skew_window_is_plus_minus_60_seconds(skew, expected):
    assert _verify(_headers(now=NOW + skew)) == expected


def test_request_signed_before_the_relay_started_is_rejected():
    started = int(NOW)

    assert relay_auth.verify(
        SECRET, method="POST", path=PATH, body=BODY, nonce_cache=NonceCache(), now=NOW, not_before=started,
        **_header_kwargs(_headers(now=NOW - 1)),
    ) == (False, "before_start")
    assert relay_auth.verify(
        SECRET, method="POST", path=PATH, body=BODY, nonce_cache=NonceCache(), now=NOW, not_before=started,
        **_header_kwargs(_headers(now=NOW)),
    ) == (True, None)


def test_replayed_request_is_rejected():
    cache = NonceCache()
    headers = _headers()

    assert _verify(headers, cache=cache) == (True, None)
    assert _verify(headers, cache=cache, now=NOW + 5) == (False, "replayed_nonce")


def test_replay_after_nonce_expiry_is_still_rejected_by_the_window():
    cache = NonceCache()
    headers = _headers()
    assert _verify(headers, cache=cache) == (True, None)

    assert _verify(headers, cache=cache, now=NOW + relay_auth.NONCE_TTL_SECONDS + 1) == (False, "stale_timestamp")


@pytest.mark.parametrize("missing", ["X-Kgm-Timestamp", "X-Kgm-Nonce", "X-Kgm-Signature"])
def test_missing_header_is_rejected(missing):
    headers = _headers()
    del headers[missing]

    assert _verify(headers) == (False, "missing_headers")


@pytest.mark.parametrize("nonce", [
    "0123456789ABCDEF0123456789ABCDEF",
    "0123456789abcdef0123456789abcde",
    "0123456789abcdef0123456789abcdef0",
    "0123456789abcdef0123456789abcdeg",
    "0123456789abcdef-123456789abcdef",
])
def test_malformed_nonce_is_rejected_even_with_a_matching_signature(nonce):
    timestamp = str(int(NOW))
    headers = {
        "X-Kgm-Timestamp": timestamp,
        "X-Kgm-Nonce": nonce,
        "X-Kgm-Signature": relay_auth.sign(SECRET, method="POST", path=PATH, timestamp=timestamp, nonce=nonce, body=BODY),
    }

    assert _verify(headers) == (False, "invalid_nonce")


@pytest.mark.parametrize("nonce", ["0123456789ABCDEF0123456789ABCDEF", "short"])
def test_signed_headers_refuses_malformed_nonce(nonce):
    with pytest.raises(ValueError):
        relay_auth.signed_headers(SECRET, "POST", PATH, BODY, nonce=nonce)


@pytest.mark.parametrize("timestamp", ["1800000000.5", "-1800000000", " 1800000000", "+1800000000", "١٨٠٠٠٠٠٠٠٠", "1" * 13])
def test_malformed_timestamp_is_rejected(timestamp):
    headers = _headers()
    headers["X-Kgm-Timestamp"] = timestamp

    assert _verify(headers) == (False, "invalid_timestamp")


@pytest.mark.parametrize("signature", ["A" * 64, "0" * 63, "ф" * 64, "z" * 64])
def test_malformed_signature_is_rejected_without_raising(signature):
    headers = _headers()
    headers["X-Kgm-Signature"] = signature

    assert _verify(headers) == (False, "invalid_signature")


def test_uppercase_hex_signature_is_rejected():
    headers = _headers()
    headers["X-Kgm-Signature"] = headers["X-Kgm-Signature"].upper()

    assert _verify(headers) == (False, "invalid_signature")


@pytest.mark.parametrize("secret", ["", "x" * 31, None])
def test_short_or_missing_secret_is_refused(secret):
    with pytest.raises(ValueError):
        relay_auth.signed_headers(secret, "POST", PATH, BODY)
    with pytest.raises(ValueError):
        relay_auth.require_secret(secret)
    assert _verify(_headers(), secret=secret) == (False, "invalid_secret")


def test_secret_problem_never_echoes_the_secret():
    assert relay_auth.secret_problem("x" * 31) == "too_short"
    assert relay_auth.secret_problem("") == "missing"
    assert relay_auth.secret_problem("x" * 32) is None
    with pytest.raises(ValueError) as excinfo:
        relay_auth.require_secret("my-short-secret")
    assert "my-short-secret" not in str(excinfo.value)


def test_invalid_signature_does_not_burn_the_nonce():
    cache = NonceCache()
    forged = _headers()
    forged["X-Kgm-Signature"] = "0" * 64

    assert _verify(forged, cache=cache) == (False, "invalid_signature")
    assert len(cache) == 0
    assert _verify(_headers(), cache=cache) == (True, None)


def test_nonce_cache_remembers_until_ttl_then_purges():
    cache = NonceCache(ttl_seconds=120)
    assert cache.remember(NONCE, 1000.0)

    assert cache.seen(NONCE, 1120.0)
    cache.purge(1120.0)
    assert len(cache) == 1
    assert not cache.seen(NONCE, 1120.5)
    cache.purge(1120.5)
    assert len(cache) == 0


def test_nonce_cache_is_bounded_and_fails_closed_when_full():
    cache = NonceCache(ttl_seconds=120, max_size=3)
    for index in range(3):
        assert cache.remember(f"{index:032x}", 1000.0 + index)

    assert not cache.remember("f" * 32, 1010.0)
    assert len(cache) == 3
    assert not cache.seen("f" * 32, 1010.0)
    # Как только старые nonce истекают, место освобождается.
    assert cache.remember("f" * 32, 1121.5)
    assert len(cache) == 2


def test_nonce_cache_size_stays_bounded_under_many_requests():
    cache = NonceCache(ttl_seconds=120, max_size=100)
    accepted = sum(cache.remember(f"{index:032x}", 1000.0 + index * 0.01) for index in range(1000))

    assert accepted == 100
    assert len(cache) == 100


def test_verify_rejects_when_nonce_cache_is_full():
    cache = NonceCache(max_size=1)
    assert _verify(_headers(), cache=cache) == (True, None)

    assert _verify(_headers(nonce="f" * 32), cache=cache) == (False, "nonce_cache_full")


@pytest.mark.parametrize("network", [
    "10.50.0.0/24", "127.0.0.0/8", "fd00::/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.1.0/24", "::1/128",
])
def test_private_networks_are_accepted(network):
    assert relay_auth.is_private_network(ipaddress.ip_network(network))


@pytest.mark.parametrize("network", [
    # 0.0.0.0/0 проходил ip_network(...).is_private до CPython 3.12.4 (CVE-2024-4032).
    "0.0.0.0/0", "0.0.0.0/1", "10.0.0.0/7", "8.8.8.0/24", "172.32.0.0/16", "169.254.0.0/16",
    "::/0", "fe80::/10", "2001:db8::/32",
])
def test_public_or_too_wide_networks_are_rejected(network):
    assert not relay_auth.is_private_network(ipaddress.ip_network(network))
