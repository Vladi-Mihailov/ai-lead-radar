# Georgia: notification pause, boundary and subscription restore

## Notification pause and boundary

One switch in `users.db` (`georgia_notification_control`) is read on every
tick by both senders, so pausing and resuming need no restart:

- the bot (`ai-lead-radar-geshtrafbot`): client delivery to owners and to the
  managers who created delegated subscriptions, «штрафов не найдено» after a
  bootstrap, and the manager-chat alerts (new user, car added);
- the main process (`ai-lead-radar`): the operator chat.

```
sudo -u leadradar -H /home/leadradar/.venv/bin/python -m scripts.georgia_notifications status
sudo -u leadradar -H /home/leadradar/.venv/bin/python -m scripts.georgia_notifications pause --note "<why>"
sudo -u leadradar -H /home/leadradar/.venv/bin/python -m scripts.georgia_notifications resume            # preview
sudo -u leadradar -H /home/leadradar/.venv/bin/python -m scripts.georgia_notifications resume --execute
```

- **Paused:** nothing is sent and nothing is marked. Checks, the public feed
  and payment statuses keep running.
- **Resume:** first, everything waiting to be sent is excluded explicitly,
  with reason `notification_boundary`:
  - client and manager queue: `client_fine_deliveries.excluded_at` /
    `exclusion_reason`;
  - operator chat: `fine_operator_notification_exclusions`.

  Nothing is marked as delivered. Then the pause is lifted and the boundary
  time is stored; the log is in `georgia_notification_control_log`.
- **After the boundary:** a "new fine" is a receipt first seen after it (the
  earliest `detected_fines` row of that plate and receipt, and
  `georgia_fine_status.first_listed_at`). A receipt known before the
  boundary is excluded on first sight, with reason
  `known_before_boundary`. That covers:
  - a re-detection under another task;
  - ⏳ history confirmed later;
  - a car switched back on;
  - a restored subscription.

  Delivery attempts that ran out of retries before the boundary are left as
  they are.
- **Never used** (no pause, no boundary): behaviour is unchanged.

Only processes running this code see the pause. Deploy the code and restart
both processes before the first `pause`.

## Restoring subscriptions that expired under the old term

`scripts/restore_expired_georgia_subscriptions.py` turns back on existing
subscriptions that `expire_elapsed` set to `expired` before the unlimited
migration (2026-11-17 18:49 UTC). It uses `SubscriptionService.turn_on_car`,
the client's own ▶️:

- the same row becomes unlimited;
- the task is reactivated, or the subscription moves to the plate's active
  task;
- no duplicate subscriptions or tasks are created;
- no police.ge request is made and no bootstrap is queued.

Every stored fine of the plate is excluded for that subscription, with
reason `subscription_restored`, so old receipts are never sent as new.

The following are not restored:

- an older duplicate of the same client and car;
- a client who already has a live subscription for the car;
- a later explicit OFF or delete of the car by the client;
- an explicitly stopped task;
- a denylisted plate;
- pending invitations (`pending_claim`).

Anything that cannot be decided reliably is reported as doubtful.

1. Make a backup of `data/users.db`.
2. `georgia_notifications pause`. `--execute` refuses to run without the
   pause.
3. `restore_expired_georgia_subscriptions` (preview), then `--execute`.
   Each row is re-read right before the write; rows changed since the
   preview are skipped. Running it again changes nothing.
4. Check the counts, then run `georgia_notifications resume`, then
   `resume --execute`.

The previous state of each subscription and task is kept in
`ge_subscription_restore_backup`. `--rollback` previews the rollback and
`--rollback --execute` applies it. A row is returned only if nobody changed
it after the restore. A task goes back only when no other live subscription
uses it.

## Rolling the code back

The previous code ignores exclusions and the pause: the old bot would send
excluded rows that have attempts left, and the old main process would send
excluded operator rows. So:

1. **Never roll back the main process** (`ai-lead-radar`). This code behaves
   exactly as before when the switch is unused, and the operator exclusions
   have no equivalent in the old code. The only way to make them inert for
   old code is to set `notification_sent_at = excluded_at` for those rows.
   That marks them as sent, so use it only as a last resort.
2. To roll back the bot:
   1. `systemctl stop ai-lead-radar-geshtrafbot`.
   2. Optionally, run `restore_expired_georgia_subscriptions --rollback
      --execute`. Exclusions alone already keep old receipts from being
      sent.
   3. Make the client exclusions inert for the old code, which treats rows
      with exhausted attempts as final:

      ```
      UPDATE client_fine_deliveries SET attempt_count = 6
      WHERE excluded_at IS NOT NULL AND delivered_at IS NULL;
      ```
   4. Check out the previous commit as `leadradar`, then start the bot.

## Notes

- Wait one delivery interval (about 3 minutes, plus a 30-second operator
  tick) after `pause` before changing data. A tick that started before the
  pause finishes with the list of subscriptions it read at its start.
- The boundary is global. After the first `resume`, a new subscription on a
  plate whose receipts are already known gets no "new fine" messages for
  those receipts; it sees them in the bot as before. Receipts never seen
  before still arrive.
- Plates switched back on by a restore are watched from the next collector
  cycle. Only new publications reach them through the feed. A later manual
  check or the nightly re-check sends only receipts never seen before.
