# MAX fines bots (Georgia and Turkey)

Two MAX bots on top of the existing fines monitoring. MAX is one more
client interface. There is no second monitoring system.

- **Georgia:** `python -m reader.max_bot.georgia.main`
- **Turkey:** `python -m reader.max_bot.turkey.main`

The Telegram bots and their processes, tables and behaviour are unchanged.
None of the existing processes imports or starts the MAX code.

## Architecture

```
                      ┌──────────── shared (unchanged) ────────────┐
 police.ge / REDNET ─▶│ ai-lead-radar: FineJob (operator+client_bot) │
                      │ fine_monitoring_tasks, detected_fines,       │
 Georgian Card ──────▶│ fine statuses, payable amounts + events      │
                      │ turkey_check_runs (GİB / Avrasya / KGM)      │
                      └───────▲───────────────────────────▲──────────┘
                              │                           │
          Telegram (unchanged)│                           │MAX (new)
   ┌──────────────────────────┴───┐       ┌───────────────┴──────────────┐
   │ geshtrafbot / turkeybot       │       │ max-ge / max-tr processes     │
   │ fine_monitoring_subscriptions │       │ max_ge_subscriptions          │
   │ client_fine_deliveries ...    │       │ max_ge_*_deliveries, max_tr_* │
   │ Telethon                      │       │ MAX Bot API (webhook)         │
   └───────────────────────────────┘       └───────────────────────────────┘
          operator chat notifications stay in ai-lead-radar (unchanged)
```

### Shared with Telegram

- **Georgia:**
  - the monitoring task: one active task per plate for both messengers. A
    car added in MAX joins the existing task or creates a `client_bot` task
    that the existing scheduled FineJob checks.
  - detected fines and payment statuses.
  - Georgian Card amounts and increase events.
  - the check path: the same `FineCheckService` and check-now provider
    (local or REDNET).
  - the texts.
- **Turkey:**
  - `turkey_check_runs`: MAX runs are saved there with
    `telegram_user_id = 0` and an initiator starting with `max_`.
  - the unified GİB/Avrasya/KGM check service.
  - change detection and the texts.

### Separate per messenger

- **MAX users and dialogs:** `max_bot_users` and `max_conversation_state`.
- **Webhook redelivery dedup:** `max_processed_updates`.
- **Georgia subscriptions and deliveries:** `max_ge_subscriptions`,
  `max_ge_fine_deliveries` and `max_ge_amount_deliveries`.
- **Turkey cars, monitoring and snapshots:** `max_tr_cars`,
  `max_tr_monitoring` and `max_tr_seen`.
- **Account links:** `max_link_codes`, `max_link_attempts` and
  `max_account_links`.

All MAX tables live in the same SQLite file (`settings.app.users_db_file`)
and are created on first start. The MAX processes read Telegram tables only
through a read-only SQLite connection (`reader/max_bot/telegram_readonly.py`).
They do not write to them or run their migrations.

A task created from MAX gets `telegram_chat_id = created_by_user_id = 0`.
Telegram code uses these fields as a client address (the "no fines" message
after a manual bootstrap). A real MAX id there could match an unrelated
Telegram user, while a send to 0 simply fails.

## Client functions

### Georgia

- **➕ Add car:** unlimited monitoring with an immediate check. Typing a
  plate from the menu also works, with a confirmation step.
- **📋 My cars:** a card per car with these buttons:
  - 🔎 check now;
  - 💰 to pay: the stored Georgian Card amount, or "to be confirmed";
  - 🟢 paid fines;
  - 🔕/🔔 notifications;
  - 🗑 delete.
- **🔎 Check fines:** the live check, with 🔴/🟢/⏳ statuses and amounts.
- **🔄 Refresh amount:** a Georgian Card session for 🔴/🟡 receipts. It uses
  the same atomic DB limits as Telegram. The per-user manual limit counts
  MAX users as `requested_by = -max_user_id`, which never collides with
  Telegram ids.
- **Notifications:** new fines and confirmed amount increases.

### Turkey

Turkey mirrors the Telegram Turkey bot:

- add a car;
- check now (the unified GİB + Avrasya + KGM result, kept separate per
  provider);
- monitoring on or off;
- history and delete;
- change notifications from monitoring.

There is no paid/unpaid split because the Telegram Turkey bot has none.

### Both bots

- **🔗 Telegram:** links the account (see below) and copies the car list.
- **Managers** are listed by MAX user id in `MAX_*_MANAGER_USER_IDS`. They
  get MAX statistics and a plate search (Telegram/MAX subscriber counts and
  debt). Both screens only read the DB and never run live checks. Full
  manager tooling (debt list, refresh, stop for everyone, manual bootstrap,
  payment statuses) stays in the Telegram bots.

## Notifications

### Georgia (`reader/max_bot/georgia/delivery.py`)

The delivery poller runs every 3 minutes in the MAX process.

- **Pause and boundary:** the pause of Georgian notifications stops MAX
  delivery too. Receipts first seen before the notification boundary are
  excluded.
- **No history:** a fine is delivered only if the system first saw it after
  the subscription's `baseline_at` and after the plate's first full-history
  check (bootstrap) completed.
  - `baseline_at` is set at the end of the add-car check, so the fines shown
    in the add reply are history. While that check runs, the baseline is
    pending and the poller sends nothing.
  - While the bootstrap is pending, the decision waits. When it completes,
    everything known by then is history.
  - The resulting cutoff is fixed on the subscription once. Later
    "bootstrap completed" marks, written whenever another client adds the
    same plate, do not move it, so real new fines are never reclassified as
    history.
  - At startup any pending baseline is resolved, since no add can be in
    flight. During a run, a baseline still pending after 30 minutes is
    resolved too.
- **Dedup:**
  - one delivery per receipt (the canonical, earliest row), no ⏳ and no ⚪;
  - per-subscription rows, so a delivered fine is never resent;
  - the update dedup protects against webhook redelivery.
- **Retries:** the same backoff as Telegram (0/1m/5m/15m/1h/3h, 6
  attempts). A user who blocked the bot gets `blocked_at`.
- **Increases** follow the same event rules as Telegram
  (`_event_exclusion`):
  - the increase notifications flag;
  - the boundary;
  - the 3-day age limit;
  - the receipt must still be payable.

  The event must also be newer than the subscription baseline and the last
  on/off change. An increase shown in a "🔄 Refresh amount" reply is
  excluded for that subscription only.
- **Independence:** MAX and Telegram keep separate delivery rows and run in
  separate processes. Turning MAX notifications off does not touch Telegram,
  and deleting a MAX car does not stop the task for anyone else.

### Turkey

The MAX daily job runs once per day in a 22:30–23:59 Europe/Istanbul window,
after the Telegram 21:00 batch.

- **Waiting for Telegram:** while the Telegram batch is still writing
  results (its last `monitoring_*` run finished less than 10 minutes ago),
  the job waits, so the two processes never run KGM sessions at the same
  time. From 23:30 it runs without waiting.
- **Reuse:** for each plate with active MAX monitoring, it reuses every
  provider result the Telegram batch produced since 21:00. It runs one check
  per plate, only for the providers without a fresh result. It never runs
  one check per subscriber.
  A provider the Telegram batch already tried tonight and got ERROR is not
  checked again.
- **No initial snapshot:** each MAX subscriber has a per-provider snapshot
  (`max_tr_seen`). Turning monitoring on seeds it only from fresh results,
  obtained after the last 21:00 slot (the subscriber's own check or the
  nightly batch). Older results are not used, so a debt that appeared
  before the subscription never arrives as "new". A provider without a
  snapshot gets one silently on its first successful observation.
- **Failed sends:** a provider's snapshot moves only after its change was
  sent, so a failed delivery is detected and sent again on the next run.
  Changes that were already delivered are not repeated.
- **No CAPTCHA retry:** there is no 5-minute retry for CAPTCHA failures, as
  the job runs once a day. A provider that failed is simply not compared.

## Account linking

1. The MAX user presses 🔗 Telegram → 🔑 Get code and receives a one-time
   code.
2. The user sends `/max_link CODE` to the Telegram bot of the same country.
   The bot shows the name of the MAX account, and only the "✅ Привязать"
   button links it. A user tricked into sending someone else's command sees
   the stranger's name before anything happens. The code is stored as
   SHA-256, lives 15 minutes, works once, and is bound to its bot. Telegram
   users get 5 attempts per hour.
3. After linking, the MAX user can copy the car list. The copied cars get a
   fresh baseline, so no history is sent, and Telegram subscriptions are not
   changed.

Matching phone, name or plate is never used.

The Telegram side (`reader/max_bot/telegram_link.py`) is not registered in
the Telegram bots in this build. Enabling it is one line per bot (see the
module docstring) plus a bot restart, and needs a separate go.

## Configuration

These values come from environment variables only (`.env`). Never commit,
log or print them.

| Variable | Meaning |
|---|---|
| `MAX_GE_BOT_TOKEN`, `MAX_TR_BOT_TOKEN` | Bot tokens (business.max.ru → Чат-боты → bot → Настройки) |
| `MAX_GE_WEBHOOK_SECRET`, `MAX_TR_WEBHOOK_SECRET` | `X-Max-Bot-Api-Secret`, 5–256 chars `[A-Za-z0-9_-]` |
| `MAX_*_MODE` | `webhook` (production) or `polling` (local development only) |
| `MAX_*_WEBHOOK_HOST/PORT/PATH` | Local listener; defaults `127.0.0.1`, GE `8781` `/max/ge/webhook`, TR `8782` `/max/tr/webhook` |
| `MAX_*_MANAGER_USER_IDS` | MAX user ids of managers, comma-separated (not Telegram ids) |
| `MAX_API_BASE_URL` | `https://platform-api2.max.ru` |
| `MAX_CA_BUNDLE` | Optional CA bundle with the Минцифры root, if TLS to MAX fails |

Everything else (DB, police.ge, REDNET, Georgian Card flags, GİB/KGM) comes
from `config/config.yaml`. The flags behave as follows:

- `fine_monitor.payable_amount_enabled` switches the amount buttons.
- `payable_amount_increase_notifications_enabled` switches increase
  notifications.

## Services and network

- **Services:** `deploy/ai-lead-radar-max-ge.service` and
  `deploy/ai-lead-radar-max-tr.service` are templates and are not installed.
  The TR unit carries the same KGM relay environment as the turkeybot
  drop-in (`docs/kgm-relay.md`). Without it, KGM would go direct from the DE
  server, which is blocked.
- **Inbound:** a public domain with HTTPS on port 443 and a trusted
  certificate. MAX rejects self-signed certificates and other ports, and
  needs one endpoint per bot. The reverse proxy template is
  `deploy/max-bots.nginx.conf`, with two paths for the two bots. The bots
  listen on 127.0.0.1 and verify the secret header.
- **Outbound:** `platform-api2.max.ru:443`. Keep about 30 requests per
  second per bot and 2 messages per second per dialog. The client paces
  both.
- **Memory:** two more Python processes, about 80–120 MB each. Check
  headroom on the DE server before enabling.
- **Admin calls at release:**
  - `scripts/max_bot_admin.py {ge|tr} me` checks the token;
  - `... subscriptions`, `... subscribe https://<domain>/max/ge/webhook`,
    `... set-commands` configure the bot.

## Registering the two MAX bots (manual)

1. **Partner profile:** create and verify a MAX partner profile at
   business.max.ru. It must belong to a Russian legal entity, an individual
   entrepreneur (ИП) or a self-employed person.
2. **Bots:** create two bots under Чат-боты → Создать, for example «Штрафы
   Грузии» and «Штрафы Турции».
   - Name: 1–59 characters, no emoji.
   - Logo: 500×500, up to 5 MB.
   - Description: up to 200 characters.
   - The nickname is generated automatically.
3. **Moderation:** wait for it to finish, up to 48 working hours. Editing a
   published bot sends it back to moderation.
4. **Credentials:** copy each token and choose two webhook secrets. Pass
   them only through the server's `.env`.
5. **Managers:** collect the MAX user ids of managers. To get one, the
   manager writes to the bot, and the id appears in `max_bot_users`.

## Release plan

Each step needs a separate go.

1. **Merge the PR.** Nothing runs yet.
2. **Prepare the server:**
   - back up `users.db` and `config.yaml`;
   - set up DNS and the certificate;
   - install the nginx site from the template;
   - fill the `.env` entries.
3. **Start Georgia in a closed test:**
   - set `MAX_GE_MANAGER_USER_IDS` to the team;
   - install the unit and start only `ai-lead-radar-max-ge`;
   - run `max_bot_admin.py ge me`, then `ge subscribe ...`.

   The team adds the control car and checks the following:
   - the add reply;
   - check now;
   - the amount card;
   - that no history notifications arrive;
   - that delivery works with a synthetic fine on a DB copy.
4. **Start Turkey the same way.** Confirm the KGM relay route in the log
   (`KGM: маршрут relay`).
5. **Open the bots publicly:** publish the bot links. Watch delivery errors,
   `blocked_at` and memory for a week.
6. **Optional follow-ups:**
   - register `/max_link` in the Telegram bots;
   - include MAX-only Georgian cars in the daily automatic amount checks
     (see Limitations).

**Rollback:** stop the unit and delete the webhook subscription. MAX tables
can stay: no other process reads them.

## Limitations and MAX API differences

### MAX API

- **No reply keyboard:** MAX has none, so the main menu is an inline
  keyboard. Text commands `/start`, `/help` and `/cancel`, and typing a
  plate, still work.
- **Callbacks:** a press is answered through `POST /answers`, which edits
  the message in place. Slow work shows "⏳" and then edits the result in.
  If the edit fails, the result is sent as a new message.
- **Messages:** text is limited to 4000 characters, so long replies are
  split. Formatting is sent as HTML: MAX markdown treats `__x__` as bold,
  and its escaping is undocumented.
- **No long polling in production:** MAX limits it, and it does not work
  while a webhook is set.
- **Bot identity:** bot nicknames are generated, and deep links need recent
  MAX clients.

### Bot behaviour

- **Automatic amount checks:** the daily automatic Georgian Card checks
  (geshtrafbot) only consider cars linked to Telegram clients. A car with
  only MAX subscribers gets increases only through "🔄 Refresh amount" until
  the follow-up change lands. That change is small, in geshtrafbot, and
  needs a go.
- **"No fines" message:** after a manual or collector bootstrap, the "✅
  Штрафов нет" message goes to the Telegram chat of the task's creator, as
  before. For a task created from MAX (chat 0) nobody receives it. MAX
  clients see the result through "🔎 Check fines".
- **Turkey statistics and history:** Telegram Turkey statistics and "📜
  История" count and show all `turkey_check_runs` of a plate, MAX runs
  included. Partial MAX monitoring runs, with only some providers, look like
  the existing Telegram retry runs. Per-subscriber baselines are not
  affected because they filter by Telegram user id.
- **Circuit breaker:** the MAX Georgia process has its own in-memory
  police.ge circuit breaker. Today each process (ai-lead-radar, geshtrafbot)
  already has one, and they are not shared.
- **Car list:** a list shows at most 25 cars, with a note when there are
  more.
