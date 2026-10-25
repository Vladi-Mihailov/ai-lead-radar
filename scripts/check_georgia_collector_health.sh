#!/usr/bin/env bash
# Confirm the REDNET collector can reach production's internal collector
# API and that the API responds correctly to a signed request — see
# docs/georgia-remote-collector.md. Run this FROM REDNET after deployment,
# or from production itself to sanity-check the API is up locally.
#
# Usage:
#   GEORGIA_COLLECTOR_PRODUCTION_BASE_URL=https://... \
#   GEORGIA_COLLECTOR_HMAC_SECRET=... \
#   ./scripts/check_georgia_collector_health.sh
#
# This does NOT prove REDNET-to-production connectivity end-to-end by
# itself if run locally on production — run it from REDNET for that.

set -euo pipefail

: "${GEORGIA_COLLECTOR_PRODUCTION_BASE_URL:?Set GEORGIA_COLLECTOR_PRODUCTION_BASE_URL}"
: "${GEORGIA_COLLECTOR_HMAC_SECRET:?Set GEORGIA_COLLECTOR_HMAC_SECRET}"

PYTHON_BIN="${PYTHON_BIN:-python3}"

"$PYTHON_BIN" - "$GEORGIA_COLLECTOR_PRODUCTION_BASE_URL" "$GEORGIA_COLLECTOR_HMAC_SECRET" <<'PYEOF'
import hashlib
import hmac
import sys
import time
import urllib.request

base_url, secret = sys.argv[1], sys.argv[2]
path = "/internal/georgia/plates"
timestamp = str(time.time())
body = b""
message = f"{timestamp}.GET.{path}.{hashlib.sha256(body).hexdigest()}"
signature = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()

req = urllib.request.Request(
    base_url.rstrip("/") + path,
    headers={"X-Collector-Timestamp": timestamp, "X-Collector-Signature": signature},
)
try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        print(f"OK: HTTP {resp.status}, {resp.read().decode()[:200]}")
except Exception as exc:
    print(f"FAILED: {exc}", file=sys.stderr)
    sys.exit(1)
PYEOF
