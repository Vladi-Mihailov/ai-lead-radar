# Georgia fines: payment statuses and the daily evening re-check

A driver pays a Georgian fine, but police.ge keeps showing it, and the bot
keeps counting it as debt. This document explains why, what the bot can and
cannot know, and how payment statuses and the daily re-check work.

Georgia only. The Turkey bot and the KGM relay are not affected.

## What police.ge actually provides

The bot reads police.ge «Receipts of fines published», tab «Video fine»
(`https://police.ge/protocol/index.php?lang=en`). These are penalty notices
the Ministry failed to deliver by mail twice. A notice counts as delivered on
the 30th day after publication, and the fine is due 30 days after delivery.

- `protocols/searchByAuto` (one plate) and `protocols` (the public list, 30
  per page) return JSON with exactly 12 fields per receipt:
  `protocolNo`, `protocolAuto`, `protocolDate`, `violationDate`,
  `publishDate`, `activeDate`, `lastDate`, `protocolAmount`,
  `protocolPlace`, `protocolLaw`, `protocolLawDescription`,
  `remainingDays`.
- There is no payment status, no paid amount and no payment link. All 375
  receipts stored in production have exactly these 12 fields.
- Observed in production data:
  - `activeDate` = `publishDate` + 30 days;
  - `lastDate` = `activeDate` + 30–32 days;
  - `remainingDays` = max(0, `lastDate` − today);
  - the amount of a receipt never changed.
- Unpaid receipts stay listed for years. In production, 21 tasks had a
  receipt still listed more than 30 days after its due date, the longest 1307
  days. So a receipt disappearing from the list is practically the only
  signal that it was settled.
- The page shows «Date of update of information DD.MM.YYYY»: the data is
  updated in batches. The bot already downloads this page for the CSRF token
  and now reads the date (`parse_source_updated_on`) without an extra request.

## The payment page cannot prove a payment

The Visa/Mastercard button on police.ge is built by the page's JavaScript,
not by the JSON:

```
https://mpi.gc.ge/page1/?lang_code=ka&page_id=…&merch_id=…
  &back_url_s=https://police.ge/protocol/success.php
  &back_url_f=https://police.ge/protocol/fail.php?id=<receipt>
  &o.protocolId=<receipt>&o.vehicleNumber=<plate>&o.resourceAlias=1
```

The button is hidden when the amount exceeds 1000 GEL.

Georgian Card (Bank of Georgia's processing) then asks the merchant, the
Ministry, whether this order can be paid ("check"). The merchant answers
`<payment-avail-response>` with `code 1` (payable, with amount and
description) or `code 2` with a free-text `desc`. This is the protocol used by
open-source integrations such as
[php-geopayment](https://github.com/akalongman/php-geopayment). There is no
separate "already paid" code. The answer travels between servers; the payer's
browser does not receive it.

What a payer sees when the check refuses, from a real recording on
2026-11-18:

1. police.ge → Visa/Mastercard → `mpi.gc.ge`;
2. a Bank of Georgia page: «მიმდინარეობს ოპერაციის დამუშავება. გთხოვთ, დაელოდოთ.»;
3. no card form, an automatic return to police.ge;
4. «ჯარიმის გადახდა ვერ მოხერხდა!» ("the fine could not be paid").

That text is rendered by police.ge's `fail.php`, the generic failure return
URL. Every unsuccessful outcome leads there:

- already paid;
- receipt not payable, cancelled, under enforcement or surcharged;
- plate passed in a different form;
- a Georgian Card or Ministry outage.

So the message is never treated as a payment. It maps to 🟡 "payment needs a
check".

Automatic requests to the payment gateway are not implemented. Its rules for
automated use are unknown. The refusal reason is not visible. And a block
would hit the REDNET IP, the only path the bot has to police.ge. An "already
paid" marker can be configured once a real, distinct message has been
observed (see below).

## Statuses

`reader/fines/payment_status.py`. Each receipt is identified by its plate and
receipt number (not by monitoring task). It has two independent dimensions:

| | meaning |
|---|---|
| `payment_status = unpaid` | 🔴 Не оплачен (default) |
| `payment_status = paid` | 🟢 Оплачен — a manager confirmed it with a receipt or an explicit official message |
| `payment_status = needs_review` | 🟡 Оплата требует проверки — payment failed for an unknown reason |
| `presence = listed` | in the last successful full check |
| `presence = missing` | absent once, waiting for confirmation (keeps its status and stays in debt) |
| `presence = gone` | ⚪ Больше не отображается в базе полиции — absent in two successful full checks at least `fine_disappearance_min_gap_hours` apart |
| `presence = unverified` | ⏳ Ждёт повторной проверки police.ge — a receipt from history that was absent from police.ge's last answer *before* this change; see "First start" |

Rules:

- Statuses change only after a successful full search of the plate. Errors,
  circuit-breaker reads from the database, and single public-feed matches
  never mark anything as missing.
- A response whose `count` differs from the number of records is incomplete.
  Receipts absent from it are not counted as missing.
- An empty result counts only if a second request 5 s later is empty too
  (the false-zero guard). That guard now always fires for a plate that still
  has non-⚪ receipts, even when the task's saved total is 0. So a ⚪ that
  rests on empty results takes four empty answers on two different days.
- A plate stays in the evening re-check for 3 days after a receipt went ⚪.
  If the receipt shows up again it returns to its previous status, without a
  "new fine" message.
- Every change is written to `georgia_fine_status_events`: what changed, the
  source, the manager's Telegram id and the reason.

## First start

On its first start the bot creates statuses from the existing
`detected_fines` (`seed_from_detected_fines`, idempotent, once):

- A receipt that police.ge returned in the last successful check of any task
  of the plate, or that the public feed found later, becomes 🔴 `listed`.
- A receipt that was absent from that last check becomes ⏳ `unverified`.
  That answer came before the empty-answer protection and may have been
  falsely empty, so it confirms nothing:
  - not ⚪ and not confirmed debt; shown as a separate line in statistics
    («⏳ Ждут повторной проверки police.ge: … ₾ (N шт.)») and with ⏳ on the
    manager screen;
  - ⚪ only after two *new* successful complete checks without it, at least
    12 h apart;
  - one new check that finds it turns it 🔴 again;
  - errors, unconfirmed empty answers, incomplete answers and cached reads
    change nothing;
  - clients are notified about it exactly as before (it is not ⚪).
- In production on 2026-11-18 this was 74 receipts → 🔴 (34 cars) and 289
  receipts → ⏳ (91 cars, 14 400 ₾; most last seen 30–89 days ago). No
  receipt becomes ⚪ at first start.
- Old tables (`detected_fines`, tasks, subscriptions, deliveries) are not
  changed. New tables are created with `CREATE TABLE IF NOT EXISTS`, and no
  existing table is altered.

Where statuses are shown:

- «🔎 Проверить сейчас»:
  - a «Статус: …» line under each fine;
  - a summary: «💰 К оплате» covers everything not confirmed as paid. 🟡 is
    still debt («🟡 Из них оплата требует проверки»), and 🟢 is shown
    separately;
  - the «💳 Оплатить в рублях» button disappears only when every fine is 🟢.
- «📊 Статистика»:
  - confirmed debt (🔴), uncertain (🟡) and paid but still listed (🟢), with
    counts;
  - per-car lines: «💰 Задолженность» is 🔴 plus 🟡, then «🟡 Из них…» and
    «🟢 Оплачено» lines. A car whose receipts are all ⚪ is not shown, even if
    an old task still has a positive total;
  - «База police.ge обновлена»;
  - the last evening re-check.
- «🧾 Статусы оплаты» (trusted managers): enter a plate → list → a receipt →
  one of these actions:
  - **«🟢 Оплачен — есть квитанция»**: a reason (receipt or operation number)
    is required.
  - **«🟡 Оплата не прошла на сайте МВД»**: always 🟡, whatever the markers
    say.
  - **«📝 Текст со страницы оплаты»**: the text is classified. An explicit
    "already paid" marker gives 🟢. A technical error marker changes nothing
    and is only logged. The generic failure or unknown text gives 🟡.
  - **«🔴 Снять отметку»**.

  Every action needs a second «✅ Подтвердить». Each confirmation screen
  carries a one-time token. A button from an older screen, another manager's
  press, or a status changed in the meantime changes nothing.

Client delivery:

- Only one «🚨 Обнаружен новый штраф» per receipt per recipient. Before
  this, a receipt stored under two tasks of a plate reached the same
  subscription twice (5 cases in production).
- No such message for a ⚪ receipt (2 cases in production).
  - A receipt missing only once, or ⏳ from history, is still delivered, as
    before.
- In the Georgian bot only, if the operator was already notified about a
  receipt through another task, the new task's row is created as already
  notified. The operator process (`reader/main.py`) keeps its old behaviour.

## Daily evening re-check

`reader/public_bot/daily_recheck.py`. The REDNET collector only looks for
newly published receipts. Known fines were re-checked only by hand: in
production, 508 of 595 active tasks were last searched 14–29 days ago.

The re-check, once a day:

- **Window:** 23:00–23:45 Asia/Tbilisi, between the collector's 21:00 and
  23:55 cycles and after the Turkey batch (22:00 Tbilisi).
- **Plates:** those with a positive debt snapshot, those with non-⚪
  statuses (feed-found, paid-but-listed and ⏳ history included), and those
  with a receipt that went ⚪ in the last 3 days.
  - Order: least recently checked first; plates the previous pass failed on
    go last, so two always-failing plates cannot stop every pass.
  - At most `daily_recheck_max_plates`.
  - Fine rows and notifications are created only for the plate's active
    tasks; with no active task, only for its freshest task with a positive
    total. Other tasks with a positive total only get their total refreshed,
    so a new fine reaches the operator once.
- **How:** the same `FineCheckService.check_plate_for_tasks` as «🔄 Проверить
  авто со штрафами»: `searchByAuto` through REDNET, the false-zero guard,
  normal handling of new fines, no repeat notifications for known ones.
  Only the first plate of a pass asks REDNET to re-read the police.ge page
  (`refresh_page`), which is one GET a day.
- **Pacing:** strictly sequential:
  - 8 s between plates;
  - 60 s after every 10 plates.
- **Stops for the day:**
  - police.ge 403/429 (REDNET reports `{"error": "blocked", "http_status": …}`);
  - REDNET's circuit breaker open (HTTP 503);
  - `daily_recheck_max_consecutive_errors` errors in a row (default 2).
- **Postponed:** when the window ends, or when a manual bulk check is still
  running at 23:45. One bulk pass per process (`BatchCheckGate`) is shared
  with «🔄 Проверить авто» and `fine check-all`.
- **Progress:** stored in `georgia_daily_recheck_runs` and
  `georgia_daily_recheck_items`.
  - After a restart, the same day resumes with the plates not yet checked.
  - A day never runs twice.
  - Plates not reached are first in line the next day.
  - A run left "running" by a restart outside the window is closed as
    interrupted.
- **Logging:** the pass itself logs counts only. The existing per-check line
  in `FineCheckService` ("Georgia fine check plate=…") still writes the plate,
  as it does for every manual check. Removing plates from logs is part of the
  separate plate-leak cleanup.

### Load

Measured on 2026-11-18 from the Georgian server's journal, 7 days, all
requests HTTP 200 (no 403/429/5xx):

| | per day |
|---|---|
| public list pages (4 cycles × 100) | ~400 |
| page GETs (CSRF) | 6–12 |
| `searchByAuto` (manual checks, refresh, check-all) | 16–133 |
| **total today** | **~430–550** |
| daily re-check: plates with known fines today | 34 |
| plus plates with ⏳ history, during the first ~5 days after enabling | +91 |
| re-check requests, first ~5 days | ≈ 125 `searchByAuto` + zero confirmations + 1 page GET ≈ **130–140 (+25–30 %)** |
| re-check requests afterwards | ≈ **35–40 (+7–9 %)** |
| pass duration | ≈ 8 minutes steady state; ≈ 30 minutes in the first nights (fits the 45-minute window) |

The collector process uses 33 MB of RAM, and the server has 565 MB available
(1 vCPU, load 0.10). Each plate is one HTTPS request handled by the existing
check-now server, so memory does not change and CPU time stays negligible.
For comparison, a collector cycle fetches 100 pages back to back in 35–73 s
without blocking.

Found while measuring, not changed here: the collector's public-list poll
never completes a cycle (`last_successful_poll_at` is empty, last error
"safety cap reached before an empty page"). Every cycle therefore runs as a
first run of 100 pages, about 400 requests a day instead of a few dozen. Its
seen-receipts table is never cleaned (148 k rows). Fixing it would cut the
largest share of police.ge traffic and needs a separate decision.

### Blocking risks

- police.ge filters by source IP (only Georgian networks reach it). REDNET is
  the only path, so a block of its IP stops the collector, Check Now and the
  re-check together.
- The re-check adds about 8 % of requests at a low rate, but at a fixed time.
  A 403/429 stops it at once, and REDNET's circuit breaker opens after 3
  transport failures in a row.
- The biggest existing load is the collector's list poll (above).

## Configuration

Nothing changes in production until `daily_recheck_enabled` is turned on.
Statuses and screens work with the defaults.

The one exception: a manual or manager check that gets an empty answer for a
plate with known (non-⚪) receipts now makes the 5-second confirmation
request. Plates with a positive total already did this. Keys under `fine_monitor:` in
`config/config.yaml`, with their defaults:

```yaml
fine_monitor:
  daily_recheck_enabled: false          # turning it on adds police.ge requests
  daily_recheck_time: "23:00"           # fine_monitor.timezone
  daily_recheck_cutoff_time: "23:45"
  daily_recheck_inter_car_delay_seconds: 8
  daily_recheck_batch_size: 10
  daily_recheck_batch_pause_seconds: 60
  daily_recheck_max_plates: 200
  daily_recheck_max_consecutive_errors: 2
  fine_disappearance_min_gap_hours: 12
  payment_page_already_paid_markers: []  # only an observed, distinct message
  # payment_page_failed_markers: built-in «ჯარიმის გადახდა ვერ მოხერხდა»
  payment_page_technical_error_markers: []
```

Markers must be YAML lists of at least 6 characters. A marker that is part of
the generic MIA failure message is rejected at start-up, so that message can
never become "paid" or "technical error".

REDNET side (optional, backward compatible): `reader/georgia_collector/
check_now_server.py` now returns `count`, `source_updated_on`, accepts
`refresh_page`, and reports 403/429 as `"error": "blocked"`. Production works
with the old collector too. Then `count`, the base date and the immediate
403/429 stop are missing, and the stop falls back to "two errors in a row".

## Data to collect at the next voluntary payment test

Use browser DevTools → Network with "Preserve log" on. Never enter card data,
and close the tab if the card form appears.

1. On police.ge before clicking: the receipt is listed, its amount, and «Date
   of update of information».
2. For every request during the ~10 s: URL (the receipt number and plate may
   be masked), method, status code, every redirect's `Location`.
3. On `mpi.gc.ge`: the response bodies of XHR/fetch calls (look for
   `state`, `status`, `code`, `reason`, `message`, `desc`) and the HTML of the
   processing page.
4. The final redirect to `police.ge/protocol/fail.php`: the full query string
   (does Georgian Card add a code or transaction id?) and the page source.
5. The exact time of the attempt.
6. If possible, the same capture for a receipt known to be unpaid (stop at the
   card form) and for one known to be paid (with its receipt).

Export as "HAR (sanitized)", or remove the `Cookie`, `Set-Cookie` and
`Authorization` headers. Share it privately, never through this repository.
