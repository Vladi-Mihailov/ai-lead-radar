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

## 8. Long outages: recovery over several cycles and alerts

A cycle reads at most `GEORGIA_COLLECTOR_MAX_PAGES_PER_CYCLE` pages (300 by
default, about 1.5 days of publications). After a longer outage the backlog
is read over several cycles. After every page a cycle saves what it has read
in order as *regions* (`georgia_public_feed_state.recovery_regions`, JSON):
for each region, the protocol numbers of its first 30 and last 600 rows and
its row count. No plates, no other fields. Each following cycle:

1. reads the new rows on top as usual;
2. at a row whose protocol number starts a region, jumps into the region's
   last rows (estimated from its row count);
3. reads forward until it finds one of those last rows by its number. Pages
   read while searching are not processed: they are read in order later;
4. jumps to the region's very last row (the distance inside a region is
   exact) and goes on in order from there;
5. on a sign of overshooting (an unread row, a row of a lower region, an
   empty page or a page of already processed rows), jumps back higher and
   searches again. If the search fails, the region is read in order from its
   first page;
6. goes on to the next region, and finally to the rows of the last
   successful cycle.

A jump is trusted only after the region's last rows are found by their
numbers, so nothing is skipped, even when publications arrive during the
cycle.

Assumption, the same one FAST and the DEEP horizon rely on (not a fact):
new rows are never inserted inside a region already read, so skipping the
region skips no new row. Production shows the order is not strictly by
publication time: in the 19.11 17:00 FAST cycle, a row seen earlier stood
above new rows; old receipts seem to be shown again near the top. That is
why a single seen row never counts as proof here; only an unread row means
"past the region's end". New rows inserted inside a region would be missed.

Protocol numbers are also assumed unique. As in every mode before regions,
a new row that re-uses a number already seen counts as processed. So that a
repeated number cannot stand in for a region, the traversal requires two
adjacent numbers:
- a region is accepted only when two adjacent rows match two consecutive
  numbers of its start; a match on the last row of a page waits for the next
  page;
- its end is accepted the same way during the search and after the jump.

An unread row inside an accepted region cancels the acceptance: its rows
are counted again and the region goes back on the list. A FAST cycle logs a
warning, without the number, when a new row sits below a row seen before
the cycle on the same page. Every cycle stays within the cap. Until the gap is closed the cycle
ends as "recovering". A cycle that is stopped mid-way (restart, OOM) keeps
the regions saved after its last page. A successful cycle clears them. The
column is added to the existing state database automatically when the
collector starts (`ALTER TABLE ... ADD COLUMN`; a second process doing the
same at the same moment is not an error); no existing value changes. The
same traversal (`reader/fines/public_feed_traversal.py`) serves the
in-process job.

After every feed poll the collector POSTs its outcome to production's
`/internal/georgia/health`: status, mode, page count, row counts, dates and
the time of the last complete cycle; no plates or protocol numbers. A cycle
skipped because the police.ge circuit breaker is open is reported too. The
production bot (`ai-lead-radar-geshtrafbot.service`) alerts the managers'
group (`public_bot.support_chat_id`):

- "recovering": at the first report;
- "error": at the second report in a row that is not "ok" (a single failure
  that the next cycle fixes alerts nobody);
- a reminder at most every 12 h while it lasts;
- ✅ when cycles complete again, only if the alert was delivered;
- an alert if no report arrives for more than 13 h, reminders every 12 h, and
  ✅ when reports resume. The watch starts with the first report ever
  received, so deploying the bot before the collector never raises a false
  alarm.

The bot keeps this state and the alert queue in its database (tables
`georgia_collector_health`, `georgia_collector_alert_outbox`), so a bot
restart neither forgets an outage nor repeats alerts. An alert counts as sent
only after Telegram accepts it. Otherwise it is retried every 1, 2, 4, …
minutes, at most every 30 minutes. Alerts are sent in order. An alert not
sent yet is updated by newer reports, and is dropped if the problem ends
first.

A report that fails is logged on REDNET and never affects the cycle.

## Rollback

Set `collector_api_enabled: false` on production and/or stop
`georgia-collector.service` on REDNET at any time — the existing in-process
`GeorgiaPublicFineFeedJob` (production's own scheduled polling) and the
manual-manager-bootstrap fallback both continue to work completely
independently of the collector; nothing about this architecture removes or
weakens either existing path.

Rolling the collector back to a version without regions (section 8)
leaves the `recovery_regions` column, and possibly saved regions, in
`collector.db`. Old code never reads them. If the new code is deployed
again later, it ignores regions written before the last successful cycle
and clears the column on its first success. The bot's two alert tables are
likewise ignored by old code.
