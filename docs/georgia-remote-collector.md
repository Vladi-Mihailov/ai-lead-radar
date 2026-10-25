# Georgia remote collector (REDNET) — deployment guide

Two-server architecture for Georgia fines monitoring: a lightweight
collector process on a Georgian-network VPS (REDNET) talks to police.ge
(which source-filters non-Georgian IPs — see the project's police.ge
reachability research), and the existing production server stays the sole
source of truth for tasks, dedup, `detected_fines`, and notifications.

**Not deployed by this change.** This document describes the steps an
operator runs manually; nothing here is automated by this PR.

## 1. Provision REDNET

- A small VPS is enough: 1 vCPU / 1 GB RAM / 20 GB disk.
- Ubuntu/Debian with Python 3.11+ and `python3-venv`.
- Create a dedicated unprivileged user (e.g. `collector`) and a working
  directory, e.g. `/home/collector/georgia-collector`.

## 2. Deploy the collector code

The collector only needs the `reader/fines/` and `reader/georgia_collector/`
packages, not the whole repo's feature set — but the simplest approach is
still to clone/copy the whole repository (it's small) and install only the
**minimal** dependency set:

```bash
cd /home/collector/georgia-collector
python3 -m venv .venv
.venv/bin/pip install -r requirements-collector.txt
```

Do **not** `pip install -r requirements.txt` on REDNET — that pulls in
Turkey-bot-only CAPTCHA-solver dependencies (`playwright`, `opencv-python`,
`rapidocr`, `onnxruntime`, multi-hundred-MB) and `telethon`, none of which
the collector imports or needs (see `requirements-collector.txt`'s own
comment for the exact verification).

## 3. Configure the collector

```bash
cp .env.collector.example .env
```

Edit `.env`:

- `GEORGIA_COLLECTOR_PRODUCTION_BASE_URL` — production's internal API,
  reached through the reverse proxy set up in step 5 (e.g.
  `https://collector.yourdomain.example`).
- `GEORGIA_COLLECTOR_HMAC_SECRET` — generate once with
  `python -c "import secrets; print(secrets.token_hex(32))"` and copy the
  **same** value into production's own `.env` as
  `GEORGIA_COLLECTOR_HMAC_SECRET` (step 4). Never commit this value
  anywhere.
- `GEORGIA_COLLECTOR_DB_PATH` — a local path for the collector's own small
  SQLite state file (feed frontier + retry outbox only — never production
  data).

## 4. Enable the internal API on production

In production's `config/config.yaml`, under `fine_monitor:`:

```yaml
fine_monitor:
  public_feed_enabled: true   # must already be true — see settings.py
  collector_api_enabled: true
  collector_api_host: "127.0.0.1"   # default; do not bind 0.0.0.0 directly
  collector_api_port: 8765          # default; change if this port is taken
```

In production's `.env`:

```
GEORGIA_COLLECTOR_HMAC_SECRET=<the same value generated in step 3>
```

Restart the `ai-lead-radar-geshtrafbot.service` process (this is the
**public bot** process — the collector API runs there, not in
`ai-lead-radar.service`, so that the zero-fines "no fines found" customer
message can be sent via the same bot identity the customer already talks
to). **Do not restart this yet as part of merging this PR** — only when you
are actually ready to turn the collector on.

## 5. Put a TLS-terminating reverse proxy in front of the API

`collector_api_host` defaults to `127.0.0.1` — the API is **not** meant to
be directly internet-facing. Set up nginx, Caddy, or an equivalent reverse
proxy on production that:

- Terminates TLS (a real certificate, e.g. via Let's Encrypt).
- Proxies to `127.0.0.1:8765` (or whatever `collector_api_port` is set to).
- Ideally restricts access to REDNET's own IP at the proxy/firewall level,
  as defense in depth on top of the HMAC signing (the HMAC check alone is
  sufficient for authentication, but IP allowlisting costs little and
  narrows the attack surface further).

This step is **not automated by this PR** — it's a manual, one-time
production infrastructure change the operator makes when actually ready to
deploy.

## 6. Install and start the collector service

```bash
sudo cp deploy/georgia-collector.service /etc/systemd/system/
# edit User/Group/WorkingDirectory/ExecStart paths to match your actual
# REDNET deployment account — the shipped file uses placeholder paths
sudo systemctl daemon-reload
sudo systemctl enable --now georgia-collector.service
```

## 7. Verify end-to-end

From REDNET:

```bash
GEORGIA_COLLECTOR_PRODUCTION_BASE_URL=https://collector.yourdomain.example \
GEORGIA_COLLECTOR_HMAC_SECRET=<the shared secret> \
./scripts/check_georgia_collector_health.sh
```

A successful run prints `OK: HTTP 200, {"plates": [...]}`. Then watch
`journalctl -u georgia-collector.service -f` around the next scheduled slot
(09:00/15:00/21:00/23:55 Asia/Tbilisi, or whatever `GEORGIA_COLLECTOR_RUN_TIMES`
is set to) for a successful poll cycle.

## Rollback

Set `collector_api_enabled: false` on production and/or stop
`georgia-collector.service` on REDNET at any time — the existing in-process
`GeorgiaPublicFineFeedJob` (production's own scheduled polling) and the
manual-manager-bootstrap fallback both continue to work completely
independently of the collector; nothing about this architecture removes or
weakens either existing path.
