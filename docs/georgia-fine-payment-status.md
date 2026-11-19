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

- Each plate gets one reference check across all its tasks. If the plate
  has a task with a positive saved total, the reference is the check that
  the old debt statistics already use (the freshest such task), so no car
  loses or changes its debt. Otherwise it is the latest successful check of
  any task of the plate.
- A receipt becomes 🔴 `listed` if police.ge returned it in the plate's
  latest successful check (any task) or the public feed found it later, or
  if it is part of the saved total the old statistics show: its row was in
  the last answer of the reference task. Nothing else brings back a receipt
  that a newer check, even one with total 0, no longer showed.
- Any other receipt becomes ⏳ `unverified`. So does every receipt of a
  plate none of whose tasks has a recorded successful check (tasks older
  than that field, or tasks that so far only had the REDNET bootstrap and
  public-feed matches): there is nothing to compare with, and the old
  statistics did not count them as debt. Their first full check turns
  them 🔴 or starts the way to ⚪.
  That answer came before the empty-answer protection and may have been
  falsely empty, so it confirms nothing:
  - not ⚪ and not confirmed debt; shown as a separate line in statistics
    («⏳ Ждут повторной проверки police.ge: … ₾ (N шт.)») and with ⏳ on the
    manager screen;
  - a manager mark wins: a ⏳ receipt marked 🟡 is counted as 🟡 debt, and
    one marked 🟢 as paid;
  - ⚪ only after two *new* successful complete checks without it, at least
    12 h apart;
  - one new check that finds it turns it 🔴 again;
  - errors, unconfirmed empty answers, incomplete answers and cached reads
    change nothing;
  - clients are notified about it exactly as before (it is not ⚪).
- A read-only dry run on a copy of the production database (2026-11-18)
  gave 67 receipts → 🔴 (33 cars) and 296 receipts → ⏳ (92 cars, 87 of
  them with ⏳ only; 14 800 ₾). No receipt becomes ⚪ at first start.
  All 29 cars of the old debt list keep their totals. 4 cars are added:
  receipts the public feed found after their plate's last check. 7 plates
  have no recorded check at all; their 7 receipts (400 ₾, all seen in the
  last 60 days) start as ⏳.
- Old tables (`detected_fines`, tasks, subscriptions, deliveries) are not
  changed. New tables are created with `CREATE TABLE IF NOT EXISTS`, and no
  existing table is altered.

Where statuses are shown:

- «🔎 Проверить сейчас»:
  - a «Статус: …» line under each fine;
  - a summary: «💰 К оплате» covers every fine that is neither 🟢, ⚪ nor
    ⏳ (see "Client delivery" below for ⏳). 🟡 is still debt («🟡 Из них
    оплата требует проверки»), and 🟢 is shown separately;
  - the «💳 Оплатить в рублях» button disappears when no fine is left in «К
    оплате».
- «📊 Статистика»:
  - confirmed debt (🔴), uncertain (🟡) and paid but still listed (🟢), with
    counts;
  - per-car lines: «💰 Задолженность» is 🔴 plus 🟡, then «🟡 Из них…» and
    «🟢 Оплачено» lines. A car whose receipts are all ⚪ is not shown, even if
    an old task still has a positive total;
  - a saved total that is newer than the plate's last full check with
    statuses and larger than the statuses' 🔴 + 🟡 is shown as before, as
    unpaid debt. This happens when a check ran without statuses (the
    operator process), writing statuses failed, or statuses were just seeded
    from history; errors must not hide debt. A public-feed match does not
    count as a full check, and 🟢 marks do not explain such a total (the
    paid receipt may already be gone and a new fine in its place), so until
    the next full check the car shows its saved total. The car's ⏳
    receipts are then not added on top, because the newer total already
    includes them;
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
- No such message for a ⏳ receipt either (history whose presence no new
  check has confirmed, whatever its manager mark). It goes out on the next
  tick after a check or the feed finds it again; after ⚪, never. The
  subscriber sees ⏳ on its own line instead: «⏳ Ждут повторной проверки
  police.ge: N ₾ (K шт.) — в сумму к оплате не входят», at the bottom of
  the add-car summary (owner and trusted variants) and of «🔎 Проверить
  сейчас», also under «штрафов не найдено». A ⏳ receipt that reaches
  «Проверить сейчас» from the cache gets its ⏳ label, stays out of «К
  оплате» and alone brings no payment buttons. Suppressed plates show no ⏳
  line.
  - For subscribers such a receipt is ⏳ even with a 🟡 mark: a refused
    payment is no proof of debt, and for a receipt police.ge did not even
    show it more likely means "already paid". Managers' statistics keep
    counting 🟡 as debt. A 🟢-marked one is paid and not on the ⏳ line (see
    the 🟢 rule below).
  - Not covered: a check that bypasses the statuses (the operator process,
    a failed status write) leaves the receipt ⏳ for subscribers until the
    next check with statuses, while statistics already show the saved total.
    The bootstrap and manual «штрафов не найдено» messages, the claim and
    «включить уведомления» say nothing about ⏳.
  - A receipt missing only once is still delivered, as before.
- **No such message for a 🟢 receipt**, whatever police.ge shows (so no
  payment buttons either). It stays in history and in the checks (the
  evening re-check keeps its plate until it is ⚪), and a check, the feed,
  a changed card on police.ge (a new row of the same receipt) or its return
  after ⚪ never make it a new fine: in the Georgian bot such a row is
  created as already notified, so the operator chat is not told about it,
  and it does not count in «найдено новых» of the add-car summary. «🔴 Снять
  отметку» makes it an ordinary receipt again (sent if the subscriber never
  got it). A status that cannot be read counts as unpaid: a fine is never
  lost because of it. Another receipt of the same plate is unaffected.
  - Not covered: a row created before the 🟢 mark and not yet sent to the
    operator chat (the operator process flushes pending rows without
    looking at statuses) still goes there; checks run by the operator
    process itself have no statuses. Managers' counters «🚨 Новых штрафов
    найдено сегодня» and the check-all «С задолженностью» still count such
    rows and plates. The trusted «🔎 Проверить сейчас» of a plate with an
    active task shows fines without status labels (it has no sum and no
    payment buttons).
- In the Georgian bot only, if the operator was already notified about a
  receipt through another task, the new task's row is created as already
  notified. The operator process (`reader/main.py`) keeps its old behaviour.
- Managers' «🔎 Поиск» and the trusted one-off check: «💰 Штрафы» leaves 🟢
  out and shows it next to it, «(🟢 оплачено: N ₾)». Both are live checks,
  so every other fine police.ge just returned counts (a ⚪ or ⏳ status there
  only means its write failed).
- Statistics at first start: seeding records the snapshot's time as the
  plate's last full check when every receipt of the snapshot check's answer
  is among the statuses still in the police base (with any payment mark)
  and they add up to the snapshot, so a 🟢 mark leaves «Задолженность» at
  once even on plates not checked since the transition. The match is by
  receipt, not by sum: on a later start, a newer snapshot from a check
  without statuses (its receipts ⏳ or without a status) or other receipts
  (feed finds, 🟢) never pass for it. In production all 29 cars of the old
  debt list are explained this way. Deliberate exception: a saved
  total that is newer than the statuses (a check without statuses) or not
  explained by them is still shown whole, possibly including a 🟢 receipt,
  until the next full check — so that a new fine that took the paid one's
  place is never hidden.

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
  - Order: least recently checked or attempted first. A failed attempt also
    sends the plate to the back of the queue, so with a cap smaller than the
    number of plates an always-failing plate does not return to the front
    every other night. Plates that failed in their latest run go at the end
    of their pass, so two always-failing plates cannot stop it before the
    others. A plate the latest run did not reach does not count as failed,
    so it is not kept at the end when passes keep ending early.
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
  with «🔄 Проверить авто» and `fine check-all`. Known limitation: the plates
  are chosen when the day's run is created. A pass that had to wait for a
  manual bulk check re-checks the plates that check has just covered. Those
  checks are less than 12 h apart, so they move nothing toward ⚪. Avoid manual
  bulk checks in the window.
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
| daily re-check: plates with known fines today | 33 |
| plus plates with ⏳ history only, during the first ~5 days after enabling | +87 |
| re-check requests, nights 1–2 | ≈ 120 `searchByAuto` + ≈ 87 zero confirmations (an empty answer for a plate with ⏳ receipts is always asked twice) + 1 page GET ≈ **205–215 (+40–50 %)** |
| re-check requests, nights 3–5 | ≈ 120 `searchByAuto` + 1 page GET ≈ **120–125 (+22–29 %)**: the ⏳ receipts are ⚪ by then, and their plates stay in the re-check for 3 days without a second request |
| re-check requests afterwards | ≈ **35–40 (+7–9 %)** |
| pass duration | ≈ 8 minutes steady state; ≈ 36 minutes on nights 1–2 (120 × 8 s + 11 × 60 s + 87 × 5 s), still inside the 45-minute window; a plate not reached by 23:45 goes first the next night |

The collector process uses 33 MB of RAM, and the server has 565 MB available
(1 vCPU, load 0.10). Each plate is one HTTPS request handled by the existing
check-now server, so memory does not change and CPU time stays negligible.
For comparison, a collector cycle fetches 100 pages back to back in 35–73 s
without blocking.

Found while measuring, not changed here: the collector's public-list poll
never completes a cycle (`last_successful_poll_at` is empty, last error
"safety cap reached before an empty page"). Every cycle therefore runs as a
first run of 100 pages, about 400 requests a day instead of a few dozen. Its
seen-receipts table is never cleaned (148 k rows). The fix is a separate
pull request (branch `fix/georgia-collector-feed-horizon`).

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
  daily_recheck_scope: all              # "debt": known-debt plates only (pilot)
  fine_disappearance_min_gap_hours: 12
  payment_page_already_paid_markers: []  # only an observed, distinct message
  # payment_page_failed_markers: built-in «ჯარიმის გადახდა ვერ მოხერხდა»
  payment_page_technical_error_markers: []
```

`daily_recheck_scope: debt` limits the selection to the plates the managers'
debt list shows: a positive saved total, or a 🔴/🟡/🟢 status (a receipt still
in the police base with any payment mark, or a ⏳ receipt a manager marked 🟡
or 🟢). Plates whose only receipts are unmarked ⏳ history are left out;
plates with a receipt that went ⚪ in the last 3 days stay in, for the
follow-up.

With a small `daily_recheck_max_plates` (e.g. 15) this is a pilot, but not a
fixed group: each night takes the least recently checked plates of that set,
so the set is covered in rotation. With about 35 plates, each one is checked
every 2–3 nights, and a paid receipt reaches ⚪ about 3–6 days after police.ge
drops it (two checks at least 12 h apart, one per round). A cap at least as
large as the set (about 40, about 8 minutes a night) checks every plate each
night. With the default `all` and a small cap, the first plates would be
⏳-history plates (they are the least recently checked).

Any other value is a configuration error, and every process that loads
`config/config.yaml` refuses to start, even with the re-check disabled. That
includes the Georgian bot, ai-lead-radar and the Turkey bot, so check the
edited file with `load_settings` before restarting anything. Code without
this key ignores it. A changed scope or cap applies from the next day's run:
a run already created that day keeps its plates.

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
