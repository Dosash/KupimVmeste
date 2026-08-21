# Architecture

KupimVmeste is a single-process asynchronous Telegram bot with a local SQLite database.

## Components

- `app/main.py` — Telegram commands, callbacks, access control, notifications and reminder jobs.
- `app/database.py` — schema migrations, transactions, optimistic revisions and durable undo data.
- `app/ui.py` — HTML messages and inline keyboards.
- `app/backup.py` — consistent hot local SQLite backups with integrity verification and retention.
- `deploy/` — hardened systemd service and daily backup timer.

## Request flow

1. A global guard rejects users outside `ALLOWED_USER_IDS` and non-private chats.
2. The handler reads or changes the shared open list in SQLite.
3. Mutations increment the list revision and, where applicable, store an undo snapshot in the same
   transaction.
4. The actor's screen and the saved dashboards of other users are refreshed.
5. Notification preferences determine whether an additional Telegram message is sent.

## Storage model

SQLite runs in WAL mode with foreign keys enabled. The primary entities are users, shopping lists,
items, favourites, templates, undo actions and reminder-delivery records. Only one shopping list can
be open at a time.

Quantities are stored as thousandths and money as integer minor units, avoiding floating-point
rounding errors. Repeated active products with the same normalized name and unit are merged.

## Scheduling and backups

The in-process PTB JobQueue checks reminder rules once per minute. A delivery ledger prevents normal
duplicate reminders after restarts.

Backups are intentionally separate from the bot process. At 03:30 in the configured timer timezone,
with a randomized delay of up to ten minutes, the systemd timer runs `app.backup`, which
uses SQLite's backup API, performs `PRAGMA quick_check`, atomically installs the snapshot and retains
the newest 30 local copies. For disaster recovery, copy snapshots to another host or storage service.
