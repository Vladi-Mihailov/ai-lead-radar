# KGM relay (Georgian server) — deployment guide

Since 2026-11-11, `webihlaltakip.kgm.gov.tr` (KGM) drops traffic from the
production server's network: DNS and TCP 443 both time out. The same site
answers from the Georgian (REDNET) server. The KGM relay is a small HTTP
service on the Georgian server that carries the Turkey bot's KGM requests over
WireGuard:

```
Turkey bot (production)                      Georgian server                KGM
  UnifiedTurkeyCheckService                    kgm-relay.service
  KgmProvider + parser + OCR   -- HTTP/WG -->  10.50.0.2:8767       -- HTTPS -->  webihlaltakip.kgm.gov.tr
  RelayKgmSession              10.50.0.1       KgmSession (unchanged)
```

The relay only moves bytes. It runs the existing, unchanged
`reader/turkey_bot/kgm/session.py::KgmSession`. CAPTCHA OCR and the parsing of
KGM's answer (`parser.parse_submit_response`) stay on production, exactly as
with the direct route.

**Nothing here is deployed by this change.** The operator runs the steps
below by hand, each one only after an explicit go. The bot's default route
stays `direct`, so merging the code changes nothing until the route is
switched in step 3.

## How one check works

1. `check_service._check_kgm` builds a `RelayKgmSession` and a `KgmProvider`
   from the route factory (`reader/turkey_bot/kgm/route.py`).
2. `start()` calls `POST /internal/kgm/sessions`. The relay creates a
   `KgmSession` with its own `httpx.AsyncClient`, loads the KGM page and the
   CAPTCHA, and returns the image (base64) plus a `session_id`.
3. Production solves the CAPTCHA with OCR, as before.
4. `submit()` calls `POST /internal/kgm/sessions/{id}/submit`. The relay
   posts the form through the **same** `KgmSession` and returns KGM's raw
   ASP.NET delta text. Production parses it.
5. On `rejected`, production calls `.../captcha` for a new image and submits
   again in the same session. The `ASP.NET_SessionId` cookie lives in the
   relay's httpx client and `__VIEWSTATE`/`hdnx` live in its `KgmSession`, so
   both survive between calls.
6. When the check ends, `aclose()` sends `DELETE /internal/kgm/sessions/{id}`.
   The relay closes the KGM client. Idle sessions are closed after 120 s
   anyway. A failed `DELETE` never changes the check's result.

There is **no automatic fallback** to the direct route. If the relay is down
or misconfigured, KGM ends with `transport_error`, the overall result is
`PARTIAL`, and GİB/Avrasya work as usual.

## API contract

The relay accepts connections only from `KGM_RELAY_ALLOWED_NETWORK` (default
`10.50.0.0/24`); other addresses get `401` before the signature is even read.
Every request carries three headers (`reader/turkey_bot/kgm/relay_auth.py`):

| Header | Value |
|---|---|
| `X-Kgm-Timestamp` | Unix time in seconds; accepted within ±60 s and not earlier than the relay's start |
| `X-Kgm-Nonce` | 32 lowercase hex characters, new for every request |
| `X-Kgm-Signature` | `HMAC-SHA256(secret, "{timestamp}.{METHOD}.{path}.{nonce}.{sha256(body)}")`, hex; `path` includes the query string |

A repeated nonce is rejected (replay). A nonce is remembered only after its
signature has been verified. Requests signed before the relay process started
are rejected too, so a restart does not reopen replay. Both servers therefore
need NTP-synchronised clocks.

| Request | Success | Body |
|---|---|---|
| `POST /internal/kgm/sessions` | `201` | `{"session_id", "captcha_png_b64", "expires_in": 120}` |
| `POST /internal/kgm/sessions/{id}/captcha` | `200` | `{"captcha_png_b64"}` |
| `POST /internal/kgm/sessions/{id}/submit` with `{"plate", "captcha_code"}` | `200` | `{"response_text", "submit_seconds"}` |
| `DELETE /internal/kgm/sessions/{id}` | `204` | always, even for an unknown session |
| `GET /internal/kgm/health` | `200` | `{"ok": true, "open_sessions", "sessions_today", "version", "kgm_code"}` |

`submit` accepts exactly two string fields: `plate` (1–20 characters of
`[A-Za-z0-9 ]`) and `captcha_code` (1–128 printable characters). `version` is
the relay protocol version and `kgm_code` a fingerprint of the relay's
`kgm/session.py`, `parser.py` and `models.py` (`relay_version.py`).

| Error | Status | Body |
|---|---|---|
| Address outside the allowed network, bad or missing signature, stale or pre-start timestamp, replay | `401` | `{"error": "unauthorized"}` |
| Malformed `submit` body | `400` | `{"error": "bad_request"}` |
| Unknown or expired session | `404` | `{"error": "session_not_found"}` |
| Wrong method | `405` | `{"error": "method_not_allowed"}` with an `Allow` header |
| Body over 16 KiB | `413` | `{"error": "payload_too_large"}` |
| More than 1 new session per 2 s (burst 3), or 600 sessions in the UTC day | `429` | `{"error": "rate_limited"}` |
| Two sessions open and both in the middle of an operation | `503` | `{"error": "busy"}` |
| KGM failed or missed the deadline (start 65 s, captcha/submit 35 s) | `502` | `{"error": "kgm_transport", "detail": "<exception type, HTTP status or errno>"}` |

When two sessions are open and one of them is idle, a new session closes the
least recently used idle one (logged as `evicted_idle`) instead of failing
with `503`. One KGM operation runs at a time (`asyncio.Semaphore(1)`). A
session is closed right after any KGM error, because its ASP.NET state is
unknown and production stops the KGM check after `transport_error` anyway.
The deadlines follow from KGM's 30 s request timeout and the 1.5 s pause
between requests; production's read timeouts (start 70 s, captcha/submit
40 s) are longer, so the relay always answers first.

The relay log has one line per request: method, route template (never the
session id), status, duration, response size and error category. The plate,
CAPTCHA code, cookies, ViewState, KGM's answer, session id, client address,
secret and signature are never logged, and error responses never echo the
request. On production, httpx's own request lines show `{session_id}` in
place of the id.

## Prerequisite: the same KGM code on both servers

The relay builds the request to KGM (`kgm/session.py`), and production parses
the answer (`kgm/parser.py`, `kgm/models.py`). Both servers must run the same
commit.

- Install the relay **only from a commit that contains the KGM form fix from
  PR #50**. This command must print the line with `hdnSorguTipi` and
  `YABANCI`; if it prints nothing, **stop** and do not install:

  ```bash
  git grep -n "hdnSorguTipi" <commit> -- reader/turkey_bot/kgm/session.py
  ```

- Use the commit production runs:

  ```bash
  sudo -u leadradar -H git -C /home/leadradar/ai-lead-radar rev-parse HEAD   # on production
  ```

- **Rule:** any change to `reader/turkey_bot/kgm/session.py`, `parser.py` or
  `models.py` is rolled out to **both** servers (see "Update the relay").
  The connection check in step 2 prints `MISMATCH` until they match.

## 1. Install on the Georgian server

The server already has Python 3.12, `python3-venv` and WireGuard `wg0`
(10.50.0.2 ↔ production 10.50.0.1). Nothing about `georgia-collector`
changes: not its unit, user, install directory (on the live server
`/opt/georgia-collector`, as checked on 2026-11-11), venv, env file, port
8766, WireGuard or UFW.

1. Check the firewall:

   ```bash
   ufw status verbose
   ```

   It must show an `ALLOW IN` rule on `wg0` **without a port restriction**
   (for example `Anywhere on wg0  ALLOW IN  Anywhere`). If `wg0` allows only
   port 8766, or there is no `wg0` rule, **stop**: changing UFW needs a
   separate permission.

2. Create a dedicated system user:

   ```bash
   useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin kgmrelay
   ```

3. Copy only the code the relay needs, from the commit checked in the
   prerequisite, on a machine with the repository:

   ```bash
   git archive --format=tar <commit> \
       reader/__init__.py reader/turkey_bot/__init__.py reader/turkey_bot/kgm \
       requirements-kgm-relay.txt deploy/kgm-relay.service \
     | ssh root@<georgian-server> 'mkdir -p /opt/kgm-relay && tar -x -C /opt/kgm-relay'
   ```

4. Install the pinned dependencies into the relay's own venv and check them.
   Do **not** install `requirements.txt`: OCR and Telegram packages are not
   needed here.

   ```bash
   chown -R root:root /opt/kgm-relay
   python3 -m venv /opt/kgm-relay/.venv
   /opt/kgm-relay/.venv/bin/pip install -r /opt/kgm-relay/requirements-kgm-relay.txt
   /opt/kgm-relay/.venv/bin/pip check
   # Must print "pins match": the venv holds exactly the pinned versions.
   diff <(/opt/kgm-relay/.venv/bin/pip freeze | tr 'A-Z_' 'a-z-' | sort) \
        <(grep -E '^[A-Za-z]' /opt/kgm-relay/requirements-kgm-relay.txt | tr 'A-Z_' 'a-z-' | sort) \
     && echo "pins match"
   # The unit makes the file system read-only, so compile the bytecode now.
   /opt/kgm-relay/.venv/bin/python -m compileall -q /opt/kgm-relay/reader
   ```

5. Generate a **new** secret. Never reuse `GEORGIA_COLLECTOR_HMAC_SECRET`.

   ```bash
   python3 -c "import secrets; print(secrets.token_hex(32))"
   install -d -m 0700 /etc/kgm-relay
   install -m 0600 -o root -g root /dev/null /etc/kgm-relay/kgm-relay.env
   ```

   Put the value into `/etc/kgm-relay/kgm-relay.env`, as plain `KEY=value`
   lines without quotes:

   ```
   KGM_RELAY_HMAC_SECRET=<64 hex characters>
   # Defaults. The host must be an IP inside the network; the network must be private.
   # KGM_RELAY_ALLOWED_NETWORK=10.50.0.0/24
   # KGM_RELAY_HOST=10.50.0.2
   # KGM_RELAY_PORT=8767
   ```

6. Install the unit, check it, then start it:

   ```bash
   cp /opt/kgm-relay/deploy/kgm-relay.service /etc/systemd/system/
   systemctl daemon-reload
   systemd-analyze verify /etc/systemd/system/kgm-relay.service
   systemd-analyze security kgm-relay
   systemctl enable --now kgm-relay.service
   ```

   A configuration error exits with code 2, and the unit does not restart it
   (`RestartPreventExitStatus=2`); the journal names the variable. A bind
   failure (for example `wg0` not up yet) exits with 1 and is retried every
   10 s.

7. Check it on the Georgian server:

   ```bash
   systemctl status kgm-relay --no-pager
   journalctl -u kgm-relay -n 20 --no-pager     # "KGM relay слушает 10.50.0.2:8767, клиенты из 10.50.0.0/24 (version 1, kgm_code ...)"
   ss -ltnp | grep 8767                        # bound to 10.50.0.2 only
   systemctl show kgm-relay -p MemoryCurrent
   systemctl status georgia-collector --no-pager   # unchanged
   ```

## 2. Check the connection from production

This needs the merged code on production. It calls only the relay's
`/health` and never contacts KGM.

1. Create the env file. Use only `KEY=value` lines: no quotes, no `export`,
   no comments after a value.

   ```bash
   install -d -m 0755 /etc/ai-lead-radar
   install -m 0600 -o root -g root /dev/null /etc/ai-lead-radar/turkeybot-kgm-relay.env
   ```

   ```
   TURKEY_KGM_ROUTE=relay
   TURKEY_KGM_RELAY_URL=http://10.50.0.2:8767
   TURKEY_KGM_RELAY_HMAC_SECRET=<the same value as KGM_RELAY_HMAC_SECRET>
   ```

   The URL must be `http://<IP>:<port>` with the IP inside
   `TURKEY_KGM_RELAY_ALLOWED_NETWORK` (default `10.50.0.0/24`). Host names and
   `https` are refused.

2. Run the check through `systemd-run`, so that systemd parses the env file
   exactly as it will for the bot and the check runs as the bot's user:

   ```bash
   systemd-run --quiet --wait --pipe --collect \
       -p User=leadradar -p Group=leadradar \
       -p WorkingDirectory=/home/leadradar/ai-lead-radar \
       -p EnvironmentFile=/etc/ai-lead-radar/turkeybot-kgm-relay.env \
       /home/leadradar/.venv/bin/python -m reader.turkey_bot.kgm.route
   ```

   Like the bot, the check loads `.env` without overriding what systemd set.
   Expected output:

   ```
   KGM route: relay via http://10.50.0.2:8767
   relay health: {"kgm_code": "...", "ok": true, "open_sessions": 0, "sessions_today": 0, "version": "1"}
   kgm_code: relay=<12 hex> local=<the same 12 hex>
   protocol version: relay=1 local=1
   OK
   ```

   - `MISMATCH`: the relay runs other KGM code or another protocol version.
     **Stop** and update the relay from production's commit ("Update the
     relay").
   - `FAIL` comes with one warning line that names the cause. A connection
     error means WireGuard or the relay is down. `HTTP 401` means the secrets
     differ, the clocks are more than 60 s apart, or production's address is
     outside `KGM_RELAY_ALLOWED_NETWORK`.

## 3. Switch the Turkey bot to the relay

The switch uses a systemd drop-in. It does not edit `.env`,
`config/config.yaml` or `deploy/ai-lead-radar-turkeybot.service`. Variables
set by systemd win over `.env`, because `load_dotenv()` does not override
variables that already exist.

1. Create the drop-in:

   ```bash
   install -d -m 0755 /etc/systemd/system/ai-lead-radar-turkeybot.service.d
   ```

   `/etc/systemd/system/ai-lead-radar-turkeybot.service.d/kgm-relay.conf`:

   ```
   [Service]
   EnvironmentFile=-/etc/ai-lead-radar/turkeybot-kgm-relay.env
   ```

   The leading `-` means a missing env file does not stop the bot from
   starting; the route then stays `direct`. That is why every switch and
   every rollback is confirmed by the route line in the journal.

2. Restart only the Turkey bot, outside 17:45–18:45 UTC (the scheduled
   monitoring runs at 21:00 Europe/Istanbul):

   ```bash
   systemctl daemon-reload
   systemctl restart ai-lead-radar-turkeybot
   journalctl -u ai-lead-radar-turkeybot -n 50 --no-pager | grep "KGM: маршрут"
   # KGM: маршрут relay через http://10.50.0.2:8767
   ```

   An `ERROR` line "KGM: маршрут relay не настроен — ..." means the env file
   is wrong. The bot keeps running, KGM checks end with `transport_error`,
   and nothing falls back to direct. "KGM: маршрут direct" means the env file
   was not read.

## Staged switch

1. Switch in the morning (UTC), outside 17:45–18:45 UTC.
2. Run **one** check with a test plate, and only after a separate go. During
   it, `journalctl -u kgm-relay -f` on the Georgian server shows
   `POST /internal/kgm/sessions -> 201`, `.../submit -> 200` and
   `DELETE ... -> 204`.
3. If the result is `unexpected` or an error, roll back **before 17:45 UTC**,
   so that the evening batch (about 90 checks) does not run from the
   Georgian IP with an old or broken form.
4. Then run several manual checks.
5. Then let the evening batch run.
6. After the first batch, check the relay's memory:
   `systemctl show kgm-relay -p MemoryCurrent`.

## Update the relay

Roll out every change to `kgm/session.py`, `parser.py` or `models.py`, and
every relay change, to the Georgian server with the commit production runs.

1. Copy the code of that commit next to the running one:

   ```bash
   git archive --format=tar <production-commit> \
       reader/__init__.py reader/turkey_bot/__init__.py reader/turkey_bot/kgm \
     | ssh root@<georgian-server> 'rm -rf /opt/kgm-relay/reader.new && mkdir /opt/kgm-relay/reader.new \
         && tar -x -C /opt/kgm-relay/reader.new'
   ```

2. On the Georgian server, replace the code and compile it:

   ```bash
   rm -rf /opt/kgm-relay/reader
   mv /opt/kgm-relay/reader.new/reader /opt/kgm-relay/reader
   rmdir /opt/kgm-relay/reader.new
   /opt/kgm-relay/.venv/bin/python -m compileall -q /opt/kgm-relay/reader
   ```

   If the commit changed `requirements-kgm-relay.txt` or
   `deploy/kgm-relay.service`, copy those too and repeat the `pip install`,
   `pip check` and freeze comparison, or the `systemd-analyze` checks and
   `systemctl daemon-reload`.

3. Restart the relay outside the monitoring window (a check that is running
   at that moment ends with `transport_error`):

   ```bash
   systemctl restart kgm-relay
   journalctl -u kgm-relay -n 5 --no-pager
   ```

4. Run the connection check from step 2 on production. It must print
   `kgm_code: relay=X local=X` and `OK`.

## Rollback

- **Production route:** remove the drop-in and restart the bot.

  ```bash
  rm /etc/systemd/system/ai-lead-radar-turkeybot.service.d/kgm-relay.conf
  systemctl daemon-reload
  systemctl restart ai-lead-radar-turkeybot
  journalctl -u ai-lead-radar-turkeybot -n 50 --no-pager | grep "KGM: маршрут"
  # KGM: маршрут direct (прямой запрос к KGM)
  ```

  Because of the `-` in the drop-in, a mistyped path or a missing file does
  not fail the restart. Confirm the rollback by the line "KGM: маршрут
  direct", not by the exit status. While KGM still drops production's
  traffic, KGM checks end with `transport_error`, as they did before the
  switch. Setting `TURKEY_KGM_ROUTE=direct` in the env file does the same.
  The code itself needs no rollback: without the env file it stays on
  `direct`.
- **Georgian server:** `systemctl disable --now kgm-relay`. To remove it
  completely:

  ```bash
  rm /etc/systemd/system/kgm-relay.service
  systemctl daemon-reload
  rm -rf /opt/kgm-relay /etc/kgm-relay
  userdel kgmrelay
  ```

  `georgia-collector` is not affected by either step.

## Rotating the secret

Generate a new value and write it to both env files. Restart `kgm-relay`,
then `ai-lead-radar-turkeybot`. Between the two restarts, KGM checks fail
with `401` and end as `transport_error`, so rotate outside the monitoring
window.

## Risks for the REDNET server

- **Shared resources.** The box has 1 vCPU and about 960 MB of RAM, shared
  with `georgia-collector` (about 22 MB). The unit caps the relay at
  `MemoryHigh=160M`, `MemoryMax=192M`, `CPUQuota=50%` and `TasksMax=64`, so
  it cannot starve the collector. At most two KGM sessions are open at a
  time.
- **Shared public IP.** KGM sees the Georgian server's address. The relay
  limits the load: one KGM operation at a time, one new session per 2 s
  (burst 3), at most 600 sessions per UTC day. A normal day (the 21:00
  monitoring batch, its retry pass and manual checks) stays well below the
  daily cap. If KGM ever blocked that address, only the relay would stop
  working. police.ge polling uses a different site and would not be
  affected.
- **Isolation.** The relay has its own system user, venv, unit, port and
  secret. It binds only to an address inside the WireGuard network, accepts
  requests only from that network, writes nothing to disk, and runs under
  `NoNewPrivileges`, `ProtectSystem=strict`, a system-call filter, an empty
  capability set and restricted address families. Every request must carry a
  valid HMAC signature with a fresh nonce.
- **Clocks.** Signatures are valid for ±60 s and not before the relay's
  start, so both servers need NTP. With clocks N seconds apart, requests fail
  for about N seconds after each relay restart.
- **Shared WireGuard tunnel.** Its configuration does not change. Each
  CAPTCHA attempt adds roughly 50 KB of traffic (KGM's answer is about 40 KB,
  plus the CAPTCHA image).
- **Failures.** A crash restarts after 10 s. If `wg0` is not up yet, the bind
  fails and systemd retries. Production then sees `transport_error`, the
  same as today.
- **Python upgrades.** An OS upgrade that replaces Python 3.12 would break
  the venv, exactly as for `georgia-collector`: recreate the venv from the
  pinned requirements. The relay needs Python 3.11 or newer.
