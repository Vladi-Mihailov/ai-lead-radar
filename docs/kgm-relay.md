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
   anyway.

There is **no automatic fallback** to the direct route. If the relay is down
or misconfigured, KGM ends with `transport_error`, the overall result is
`PARTIAL`, and GİB/Avrasya work as usual.

## API contract

Every request carries three headers (`reader/turkey_bot/kgm/relay_auth.py`):

| Header | Value |
|---|---|
| `X-Kgm-Timestamp` | Unix time in seconds; accepted within ±60 s |
| `X-Kgm-Nonce` | 32 lowercase hex characters, new for every request |
| `X-Kgm-Signature` | `HMAC-SHA256(secret, "{timestamp}.{METHOD}.{path}.{nonce}.{sha256(body)}")`, hex |

A repeated nonce is rejected (replay). A nonce is remembered only after its
signature has been verified.

| Request | Success | Body |
|---|---|---|
| `POST /internal/kgm/sessions` | `201` | `{"session_id", "captcha_png_b64", "expires_in": 120}` |
| `POST /internal/kgm/sessions/{id}/captcha` | `200` | `{"captcha_png_b64"}` |
| `POST /internal/kgm/sessions/{id}/submit` with `{"plate", "captcha_code"}` | `200` | `{"response_text", "submit_seconds"}` |
| `DELETE /internal/kgm/sessions/{id}` | `204` | always, even for an unknown session |
| `GET /internal/kgm/health` | `200` | `{"ok": true, "open_sessions", "sessions_today", "version"}` |

`submit` accepts exactly two string fields: `plate` (1–20 characters of
`[A-Za-z0-9 ]`) and `captcha_code` (1–32 printable characters).

| Error | Status | Body |
|---|---|---|
| Bad or missing signature, stale timestamp, replay | `401` | `{"error": "unauthorized"}` |
| Malformed `submit` body | `400` | `{"error": "bad_request"}` |
| Unknown or expired session | `404` | `{"error": "session_not_found"}` |
| Body over 16 KiB | `413` | `{"error": "payload_too_large"}` |
| More than 1 new session per 2 s (burst 3), or 600 sessions in the UTC day | `429` | `{"error": "rate_limited"}` |
| Two sessions already open | `503` | `{"error": "busy"}` |
| KGM failed or took longer than 35 s | `502` | `{"error": "kgm_transport", "detail": "<exception type, HTTP status or errno>"}` |

Other limits: one KGM operation at a time (`asyncio.Semaphore(1)`). A session
is closed right after any KGM error, because its ASP.NET state is unknown and
production stops the KGM check after `transport_error` anyway.

The relay log has one line per request: method, route template (never the
session id), status, duration, response size and error category. The plate,
CAPTCHA code, cookies, ViewState, KGM's answer, session id, secret and
signature are never logged, and error responses never echo the request.

## 1. Install on the Georgian server

The server already has Python 3.12, `python3-venv`, WireGuard `wg0`
(10.50.0.2 ↔ production 10.50.0.1) and a UFW rule that allows traffic on
`wg0`. Nothing about `georgia-collector` changes: not its unit, user,
`/opt/georgia-collector`, venv, env file, port 8766, WireGuard or UFW.

1. Create a dedicated system user:

   ```bash
   useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin kgmrelay
   ```

2. Copy only the code the relay needs, from the merged commit, on a machine
   with the repository:

   ```bash
   git archive --format=tar <merged-commit> \
       reader/__init__.py reader/turkey_bot/__init__.py reader/turkey_bot/kgm \
       requirements-kgm-relay.txt deploy/kgm-relay.service \
     | ssh root@<georgian-server> 'mkdir -p /opt/kgm-relay && tar -x -C /opt/kgm-relay'
   ```

3. Install the minimal dependencies into the relay's own venv. Do **not**
   install `requirements.txt`: OCR and Telegram packages are not needed here.

   ```bash
   chown -R root:root /opt/kgm-relay
   python3 -m venv /opt/kgm-relay/.venv
   /opt/kgm-relay/.venv/bin/pip install -r /opt/kgm-relay/requirements-kgm-relay.txt
   # The unit makes the file system read-only, so compile the bytecode now.
   /opt/kgm-relay/.venv/bin/python -m compileall -q /opt/kgm-relay/reader
   ```

4. Generate a **new** secret. Never reuse `GEORGIA_COLLECTOR_HMAC_SECRET`.

   ```bash
   python3 -c "import secrets; print(secrets.token_hex(32))"
   install -d -m 0700 /etc/kgm-relay
   install -m 0600 /dev/null /etc/kgm-relay/kgm-relay.env
   ```

   Put the value into `/etc/kgm-relay/kgm-relay.env`, as plain `KEY=value`
   lines without quotes:

   ```
   KGM_RELAY_HMAC_SECRET=<64 hex characters>
   # Defaults; the relay refuses 0.0.0.0, :: and empty hosts.
   # KGM_RELAY_HOST=10.50.0.2
   # KGM_RELAY_PORT=8767
   ```

   systemd reads this file as root, so it stays `root:root 0600`.

5. Install and start the unit:

   ```bash
   cp /opt/kgm-relay/deploy/kgm-relay.service /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable --now kgm-relay.service
   ```

6. Check it on the Georgian server:

   ```bash
   systemctl status kgm-relay --no-pager
   journalctl -u kgm-relay -n 20 --no-pager     # "KGM relay слушает 10.50.0.2:8767 (version 1)"
   ss -ltnp | grep 8767                        # bound to 10.50.0.2 only
   systemctl show kgm-relay -p MemoryCurrent
   systemctl status georgia-collector --no-pager   # unchanged
   ```

   No firewall change is needed: UFW already allows `wg0`, and port 8767 is
   not reachable on the public interface because the relay binds only
   10.50.0.2.

## 2. Check the connection from production

This needs the merged code on production. It calls only the relay's
`/health` and never contacts KGM.

1. Create `/etc/ai-lead-radar/turkeybot-kgm-relay.env`, owned
   `root:leadradar` with mode `0640` so the check below can read it:

   ```
   TURKEY_KGM_ROUTE=relay
   TURKEY_KGM_RELAY_URL=http://10.50.0.2:8767
   TURKEY_KGM_RELAY_HMAC_SECRET=<the same value as KGM_RELAY_HMAC_SECRET>
   ```

2. Run the check as the bot's user, from the repository directory:

   ```bash
   cd /home/leadradar/ai-lead-radar
   sudo -u leadradar -H /home/leadradar/.venv/bin/python -m reader.turkey_bot.kgm.route \
       --env-file /etc/ai-lead-radar/turkeybot-kgm-relay.env
   ```

   Expected output:

   ```
   KGM route: relay via http://10.50.0.2:8767
   OK: {"ok": true, "open_sessions": 0, "sessions_today": 0, "version": "1"}
   ```

   `FAIL` comes with one warning line that names the cause: a connection
   error means WireGuard or the relay is down; `HTTP 401` means the secrets
   differ or the clocks are more than 60 s apart.

## 3. Switch the Turkey bot to the relay

The switch uses a systemd drop-in. It does not edit `.env`,
`config/config.yaml` or `deploy/ai-lead-radar-turkeybot.service`. Variables
set by systemd win over `.env`, because `load_dotenv()` does not override
variables that already exist.

1. Create `/etc/systemd/system/ai-lead-radar-turkeybot.service.d/kgm-relay.conf`:

   ```
   [Service]
   EnvironmentFile=/etc/ai-lead-radar/turkeybot-kgm-relay.env
   ```

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
   and nothing falls back to direct.

3. The first real check needs an explicit go and one test plate. During it,
   `journalctl -u kgm-relay -f` on the Georgian server shows
   `POST /internal/kgm/sessions -> 201`, then `.../submit -> 200` and
   `DELETE ... -> 204`.

## Rollback

- **Production route:** remove the drop-in and restart the bot. The route
  goes back to `direct`, and the log shows "KGM: маршрут direct". While KGM
  still drops production's traffic, KGM checks end with `transport_error`,
  as they did before the switch.

  ```bash
  rm /etc/systemd/system/ai-lead-radar-turkeybot.service.d/kgm-relay.conf
  systemctl daemon-reload
  systemctl restart ai-lead-radar-turkeybot
  ```

  Setting `TURKEY_KGM_ROUTE=direct` in the env file does the same. The code
  itself needs no rollback: without the env file it stays on `direct`.
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
  `MemoryMax=192M`, `CPUQuota=50%` and `TasksMax=64`, so it cannot starve the
  collector. At most two KGM sessions are open at a time.
- **Shared public IP.** KGM sees the Georgian server's address. The relay
  limits the load: one KGM operation at a time, one new session per 2 s
  (burst 3), at most 600 sessions per UTC day. A normal day (the 21:00
  monitoring batch, its retry pass and manual checks) stays well below the
  daily cap. If KGM ever blocked that address, only the relay would stop
  working. police.ge polling uses a different site and would not be
  affected.
- **Isolation.** The relay has its own system user, venv, unit, port and
  secret. It binds only to the WireGuard address, writes nothing to disk,
  and runs under `NoNewPrivileges`, `ProtectSystem=strict`, an empty
  capability set and restricted address families. Every request must carry
  a valid HMAC signature with a fresh nonce.
- **Shared WireGuard tunnel.** Its configuration does not change. Each
  CAPTCHA attempt adds roughly 50 KB of traffic (KGM's answer is about 40 KB,
  plus the CAPTCHA image).
- **Failures.** A crash restarts after 10 s. If `wg0` is not up yet, the bind
  fails and systemd retries. Production then sees `transport_error`, the
  same as today.
- **Python upgrades.** An OS upgrade that replaces Python 3.12 would break
  the venv, exactly as for `georgia-collector`: recreate the venv. The relay
  needs Python 3.11 or newer.
