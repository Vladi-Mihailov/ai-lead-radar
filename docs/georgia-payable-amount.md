# Georgia fines: the amount to pay with surcharges (Georgian Card)

police.ge shows only a receipt's original amount (`protocolAmount`), and it
never changes. The amount with surcharges is known only to the Ministry (MIA).
Georgian Card receives it when the payment page opens. This feature reads that
amount, stores it, shows it, and tells the client when it grows.

Georgia only. The Turkey bot, KGM and the REDNET collector are not affected.
Everything is **off by default**.

## Where the amount comes from

police.ge builds the Visa/Mastercard link in JavaScript:

```
https://mpi.gc.ge/page1/?lang_code=ka&page_id=<MIA>&merch_id=<MIA>
  &back_url_s=https://police.ge/protocol/success.php
  &back_url_f=https://police.ge/protocol/fail.php?id=<receipt>
  &o.protocolId=<receipt>&o.vehicleNumber=<plate>&o.resourceAlias=1
```

Opening it makes Georgian Card create a transaction and send MIA a signed
"check". MIA answers with the amount in tetri, or refuses. The page then shows
the card form with the amount on the pay button, «გადახდა (<amount> ₾)». The
same gateway's page calls `POST /open/api//v4/{merchantId}/payment/{token}/start`
on load, which returns `amount`, `currency` and `state: "offer"` without any
card data.

**Every reading of the amount is therefore a technical payment session at
Georgian Card.** No card data is entered, nothing is authorised, nothing is
charged, and an abandoned session most likely ends as CLIENT_LOST. There is no
read-only "inquiry" call, and no sandbox can return MIA's amounts.

`reader/fines/georgian_card_client.py` repeats only what a browser does before
the card form:

1. `GET page1` with MIA's `merch_id`/`page_id` (configuration, taken from
   police.ge's JavaScript).
2. Redirects are followed only inside the Georgian Card host:
   - a redirect to `police.ge/protocol/fail.php` is a refusal and is not
     requested;
   - `success.php` or any other host is an error.
3. The amount is read from the pay button and/or the `.../start` answer.

Every request passes `_guard_request`, which allows only two kinds of request
and blocks everything else before it is sent:

- GET to the Georgian Card host;
- the single `POST .../start` with the body `{}`.

That blocks card fields, payment, authorisation, police.ge and any other host.
The host is pinned to `mpi.gc.ge` in the code (`ALLOWED_GC_HOSTS`), so a typo
in the configuration cannot send the plate and the receipt elsewhere. This is
tested.

httpx logs every request URL at INFO. A log filter installed by the module
keeps only the host of Georgian Card URLs (`https://mpi.gc.ge/<redacted>`), so
the plate, the receipt, MIA's ids and the session token never reach the
journal.

An amount is accepted only when all of these hold:

- MIA allowed payment (`state "offer"`, or the card page with the pay button);
- the currency is GEL (`GEL` or `981`; the pay button's ₾);
- the receipt number appears in the answer itself, not just in our own links
  (`back_url_f`, `o.protocolId`);
- the amount is positive and plausible, that is between half the original
  amount and six times it plus 300 ₾. This guards against a parse or unit
  mistake (tetri read as lari is 100 times the original, rejected for any
  receipt above about 3 ₾); it is not a surcharge calculation;
- if both the button and `.../start` carry an amount, they agree.

Anything else is an outcome without an amount:

| outcome | meaning |
|---|---|
| `refused` | MIA refused the check |
| `receipt_unconfirmed` | the answer does not name the receipt |
| `currency_mismatch` | the currency is not GEL |
| `invalid_response` | the answer has an unknown shape |
| `blocked` | HTTP 403/429 |
| `timeout` | the session timed out |
| `error` | any other failure |

Such an outcome stores nothing. **It is never a payment**: payment statuses
(🟢/🟡/🔴) are not touched, and a refusal does not mean "paid". Surcharges are
never calculated; the bot only compares actual Georgian Card amounts.

The page format for police.ge receipts is confirmed only for a refusal (the
2026-11-18 recording). The amount page is known from the OSAGO scenario on the
same gateway (`reader/checkout/payment_gateway.py`). Unknown answers are
therefore rejected with a reason code, and the format is checked by the
one-receipt pilot below.

## When the amount is read

`reader/fines/payable_amount.py`, `reader/public_bot/payable_amount_service.py`.

**Control periods.** `lastDate` is the last day of the original 30-day term
(30 days after delivery). It is not extended by another 30 days.

- Period 1 starts the day after `lastDate`.
- Periods 2–6 start every 30 days after that: 30, 60, 90, 120, 150 and 180
  days.
- Without `lastDate`, the end of the term is `activeDate` + 30 days, otherwise
  `publishDate` + 60 days.
- Without any of these dates there are no automatic checks.

**In each period:**

- at most one automatic check per day, only on the first 10 calendar days, and
  at most 10 checks in the period;
- an increase (also one found by a manual «Уточнить сумму» inside the window)
  closes the period; so does a refusal;
- 10 days without an increase also end the period;
- the next period starts a new cycle of at most 10 checks;
- there are no checks between windows, and none after the 6th period.

**First amount.** The first amount Georgian Card returns is stored silently,
even if it is larger than the original. If it is larger, it closes the current
period. Later amounts are compared with the stored Georgian Card amount:

- a confirmed increase is an event;
- the same amount is nothing;
- a decrease is stored silently.

**Which receipts are checked automatically:**

- 🔴 and 🟡 still in the police base (listed or missing once);
- never 🟢, ⏳ history or ⚪;
- only plates with a live client subscription (active, not expired, client
  known, not a manager's own subscription);
- plates hidden by the suppression list are never checked.

**Automatic pass.** Once a day in a window (default 13:00–14:00 Tbilisi):

- one session at a time, with a pause between sessions (30 s);
- the oldest periods go first;
- it stops for the day:
  - at the daily session cap (all triggers together, default 40);
  - after 2 errors in a row (a refusal is not an error);
  - on HTTP 403/429, which also suspends all sessions, manual ones included,
    for 24 h; the suspension survives restarts.
- A restart resumes the day: a receipt already checked that day is skipped.
- A day never runs twice.

**Manual «🔄 Уточнить сумму»** (client card):

- only reads and stores the amount;
- a receipt read (or tried) less than 12 h ago is not opened again, and the
  stored amount is shown instead;
- at most 5 sessions per user per day (checked in the same atomic insert);
- the same suspension and daily cap apply.

**Hard limits are in the database.** These limits are checked in the same
`INSERT` that starts a check (`PayableAmountRepository.begin_check`), so two
concurrent calls or two processes cannot exceed them:

- one session per receipt at a time;
- one automatic check per receipt per day;
- 10 per period;
- the daily cap.

A session interrupted by a restart is closed as `abandoned` and still counts.
A late answer never overwrites a fresher amount.

**Viewing the card or the statistics never opens a session.**

## Sessions: how many

Each check is one Georgian Card session.

- **Per receipt and period:**
  - 1 to 10 sessions;
  - 1 to 3 if the increase appears in the first days;
  - 10 if nothing changes;
  - 1 if MIA refuses.
- **Per receipt, lifetime:** at most 6 × 10 = 60 automatic sessions, plus manual
  presses.
- **Typical unpaid police receipt:** the law (Art. 125, note 3, matsne 642)
  adds one surcharge after the term, 2 × the fine, at most 500 ₾. Expected
  pattern:
  - period 1: 1–3 sessions (finds the increase);
  - periods 2–6: about 10 sessions each with no increase.

  That is roughly 50 sessions over half a year.
- **Per day:** the receipts whose window is open that day, at most the daily
  cap (40). Each receipt is in a window about 10 days out of 30, so roughly a
  third of the eligible receipts (unpaid, client linked, between 1 and 190 days
  past the term).
- `python -m scripts.georgia_payable_amount plan --date YYYY-MM-DD` prints the
  exact number for a day from the database, without any network.

The **daily cap** is the hard upper bound on load and on the gateway's
anti-fraud exposure. If the pilot confirms that a police receipt never grows
after the first surcharge, periods 2–6 could be shortened. That is a decision
for later; the code follows the agreed algorithm.

## What the client and the managers see

Shown only with `payable_amount_enabled: true`. Without it, every screen is
exactly as before.

**«🔎 Проверить сейчас»:** under each payable fine, «🧾 Штраф: 50 ₾» (the
original amount, instead of «💰 Сумма: 50 GEL») and «💰 К оплате: 150 ₾» or
«💰 К оплате: требуется уточнение». The summary:

```
🧾 Штраф: 50 ₾
💰 К оплате: 150 ₾
🕒 Сумма уточнена 21.11 в 15:20

[🔄 Уточнить сумму]
[💳 Оплатить в рублях] [🚗 ОСАГО Грузии]
```

- With unknown amounts: «💰 Предварительно к оплате» plus «⚠️ По N штрафам
  сумма требует уточнения».
- The refresh button goes to the owner and to the manager of a delegated
  subscription.
- The payment row is unchanged: owner only, and only when something is
  payable.

**«🔄 Уточнить сумму»:** the same card for all payable fines of the car,
plus notes:

- an increase («⚠️ Сумма к оплате по вашему штрафу увеличилась: 100 → 150 ₾»);
- unavailable;
- the daily limit was reached;
- the amount was read recently.

**«📊 Статистика»:**

- **Header:** «🔴 Неоплачено: N штрафов» (count only), then «💰 К оплате: X ₾»
  with one «🕒 Суммы уточнены …». That date is the earliest of the readings,
  and it is shown only when every payable receipt has a current amount.
  Otherwise «💰 Предварительно к оплате» and «⚠️ По N штрафам сумма требует
  уточнения».
- **Per car:** «🚗 Автомобиль», «🧾 Штрафы: 3 шт. — 150 ₾», «💰 К оплате»
  without a date, then the 🟡/🟢 lines and the users as before.
- A Georgian Card amount is current if it was read after the start of the
  latest control period that has begun.
- Paid fines are not in «К оплате». The original and the refined amount of one
  receipt are never added together (the refined one already includes the
  original). The preliminary total is the known amounts plus the original
  amounts of the rest, a lower bound.
- Hidden lines (⏳, subscription counts) stay hidden.

## Notifications

`reader/public_bot/amount_delivery_service.py` runs in the same poller as the
"new fine" delivery.

- One message per event and recipient. The owner gets «⚠️ Сумма к оплате по
  вашему штрафу увеличилась: 100 → 150 ₾» with the card and the buttons. The
  manager of a delegated subscription gets the manager variant without
  buttons.
- An event is created only by a confirmed increase over the stored Georgian
  Card amount. One event per new amount of a receipt, so getting the same
  amount again never notifies.
- Not sent:
  - while Georgian notifications are paused;
  - events created before the boundary: they are explicitly excluded
    (`notification_boundary`), not marked delivered;
  - to a chat that already got the event through another subscription;
  - to a subscription created after the event;
  - when the receipt is now 🟢, ⚪ or ⏳;
  - events older than 3 days (for example after a long pause): explicitly
    excluded (`event_too_old`);
  - an increase already shown in the reply to «🔄 Уточнить сумму» (excluded
    for that chat).
- The first amount, a migration or the initial filling never create events.

## Configuration

Keys under `fine_monitor:` in `config/config.yaml`, with their defaults:

```yaml
fine_monitor:
  payable_amount_enabled: false              # UI, «Уточнить сумму», increase notifications
  payable_amount_auto_checks_enabled: false  # daily automatic checks in control periods
  payable_amount_gc_base_url: "https://mpi.gc.ge"
  payable_amount_gc_merchant_id: ""          # MIA merch_id from police.ge JavaScript
  payable_amount_gc_page_id: ""              # MIA page_id from police.ge JavaScript
  payable_amount_gc_amount_unit: major       # "major" (lari) or "minor" (tetri) — confirm in the pilot
  payable_amount_request_timeout_seconds: 20
  payable_amount_auto_time: "13:00"          # fine_monitor.timezone
  payable_amount_auto_cutoff_time: "14:00"
  payable_amount_auto_delay_seconds: 30
  payable_amount_max_sessions_per_day: 40    # all triggers together, hard cap
  payable_amount_max_consecutive_errors: 2
  payable_amount_manual_min_interval_hours: 12
  payable_amount_manual_max_sessions_per_user_per_day: 5
  payable_amount_blocked_suspension_hours: 24
```

- Without `merch_id`/`page_id` no session is ever opened.
- Without `payable_amount_enabled`, nothing changes, and the new tables are
  not created by the bot.
- A typo in `payable_amount_gc_amount_unit`, a base URL other than
  `https://mpi.gc.ge`, a timeout outside 1–60 s or a limit below 1 stops every
  process that loads the config, so check the edited file with `load_settings`
  before restarting.

**Requests come from the Georgian bot's own server (DE).** They never go
through REDNET: a Georgian Card block must not cut the only path to police.ge.
Whether `mpi.gc.ge` answers from the DE IP is not yet known. That is step 1 of
the pilot.

New tables, all `CREATE TABLE IF NOT EXISTS`; no existing table is altered:

- `georgia_fine_payable_amounts`
- `georgia_fine_amount_checks`
- `georgia_fine_amount_events`
- `georgia_fine_amount_deliveries`
- `georgia_payable_amount_control`
- `georgia_payable_amount_runs`

## Operator tool

```
python -m scripts.georgia_payable_amount status
python -m scripts.georgia_payable_amount plan [--date YYYY-MM-DD]
python -m scripts.georgia_payable_amount fetch --car-number <plate> --protocol-no <receipt>            # dry run
python -m scripts.georgia_payable_amount fetch --car-number <plate> --protocol-no <receipt> --execute  # ONE session
python -m scripts.georgia_payable_amount clear-suspension
```

`fetch --execute`:

- opens exactly one session for one receipt;
- refuses a receipt that is not 🔴/🟡 in the police base;
- stores the amount silently (trigger `pilot`, never an event), even with the
  feature off in the bot;
- counts towards the daily cap and respects the suspension;
- prints the plate and the receipt masked.

## Pilot on one allowed receipt (needs a separate GO)

1. **Values.**
   - Take MIA's `merch_id`/`page_id` from police.ge's payment JavaScript. That
     is one page download via REDNET, or a value copied by hand from the
     browser.
   - Pick one receipt the owner allows: unpaid 🔴, preferably the owner's own
     or a consenting client's.
2. **Reachability from DE.**
   - One TLS handshake/HEAD to `https://mpi.gc.ge/`, which creates no session.
   - If it is blocked, stop: a Georgian path for Georgian Card is a separate
     decision, and REDNET is not used for it.
3. **Configuration.**
   - Put the two values into `config/config.yaml`; keep
     `payable_amount_enabled: false` and `payable_amount_auto_checks_enabled:
     false`.
   - Check the file with `load_settings`.
   - The bot does not need a restart, because the CLI reads the config itself.
4. **Dry run.** `fetch` without `--execute` shows the receipt is allowed and
   its control periods.
5. **One session.** Run `fetch --execute` once and record:
   - the outcome;
   - the amount and currency;
   - the receipt confirmation;
   - the time taken;
   - the reason code if something did not match.
6. **Compare.**
   - With the amount shown in a browser for the same receipt, stopping at the
     card form without entering anything, at about the same time.
   - Fix `payable_amount_gc_amount_unit` if the units differ, and tighten the
     receipt check to the field that carries it.
7. **Wait and check.**
   - At least 24 h; ask the owner whether MIA or the bank noticed anything.
   - No more sessions in the meantime.
8. **Then decide** step by step, each with its own GO:
   - `payable_amount_enabled: true` (UI, manual refresh, notifications);
   - after a few days, `payable_amount_auto_checks_enabled: true`, starting
     with a low `payable_amount_max_sessions_per_day` (for example 10) and
     `plan` checked before each step.

Rollback at any point:

- set `payable_amount_auto_checks_enabled: false` (and, if needed,
  `payable_amount_enabled: false`) and restart geshtrafbot;
- stored amounts and the journal stay;
- nothing else depends on them.

## Remaining limits

- Using MIA's merchant page outside a documented integration has an unknown
  contractual status. Georgian Card and BOG publish no rate limits or
  anti-fraud rules for abandoned sessions.
- The exact answer format for police.ge receipts, the amount units and the
  field naming the receipt are confirmed only by the pilot.
- CLIENT_LOST sessions are probably visible to MIA per receipt.
- The new-fine notification, the add-car summary and «📋 Мои авто» keep their
  layout (fine amount only). The card with «К оплате» is «🔎 Проверить
  сейчас», «🔄 Уточнить сумму» and the increase notification.
- The 20 % early-payment reduction (Art. 268¹) is not calculated. If MIA
  returns a reduced amount during the term, it is stored like any other
  amount, and the later rise to the full amount is a confirmed increase.
