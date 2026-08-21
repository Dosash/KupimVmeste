# Deployment with systemd

The supplied units target a Linux host with systemd and install the application in
`/opt/shopping-list-bot`.

## 1. Install the application

```bash
sudo install -d -m 0755 /opt/shopping-list-bot
# Export only committed files, never a local .env, database, backup or cache.
git archive --format=tar HEAD | sudo tar -xf - -C /opt/shopping-list-bot
sudo python3 -m venv /opt/shopping-list-bot/.venv
sudo /opt/shopping-list-bot/.venv/bin/pip install /opt/shopping-list-bot
```

## 2. Create the environment file

Install the production example as `/etc/shopping-list-bot.env`, then replace its placeholder values:

```bash
sudo install -m 0600 deploy/shopping-list-bot.env.example /etc/shopping-list-bot.env
sudo editor /etc/shopping-list-bot.env
sudo chmod 0600 /etc/shopping-list-bot.env
```

## 3. Install and start the units

```bash
sudo install -m 0644 deploy/shopping-list-bot.service /etc/systemd/system/
sudo install -m 0644 deploy/shopping-list-bot-backup.service /etc/systemd/system/
sudo install -m 0644 deploy/shopping-list-bot-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now shopping-list-bot.service
sudo systemctl enable --now shopping-list-bot-backup.timer
```

The main service uses a dynamic user and a private state directory. Do not create or change the
database owner manually unless you understand systemd `StateDirectory` semantics.

## Operations

```bash
sudo systemctl status shopping-list-bot.service
sudo journalctl -u shopping-list-bot.service -n 100 --no-pager
sudo systemctl restart shopping-list-bot.service

# Create and verify a backup immediately
sudo systemctl start shopping-list-bot-backup.service
sudo journalctl -u shopping-list-bot-backup.service -n 30 --no-pager
```

Backups are stored in `/var/lib/shopping-list-bot/backups`. They are local snapshots on the same
host, not off-site backups. Copy them to separate storage for protection against disk or host loss.
The timer starts after 03:30 in `Europe/Moscow`, with a randomized delay of up to ten minutes.

## Upgrade

1. Create a backup and confirm that the backup unit succeeded.
2. Update files under `/opt/shopping-list-bot` without replacing `.venv` or runtime data.
3. Install updated requirements.
4. Run the test suite.
5. Restart the main service and inspect its logs.

Schema migrations run automatically when the application opens the database.

## Restore a local backup

Use a snapshot created by the same or an older application version. Stop writers, preserve the
current database, install the selected snapshot and let systemd restore ownership on startup:

```bash
sudo systemctl stop shopping-list-bot-backup.timer shopping-list-bot.service
sudo cp -a /var/lib/shopping-list-bot/shopping_list.sqlite3 /var/lib/shopping-list-bot/shopping_list.sqlite3.before-restore
sudo install -m 0600 /var/lib/shopping-list-bot/backups/SELECTED_BACKUP.sqlite3 /var/lib/shopping-list-bot/shopping_list.sqlite3
sudo find /var/lib/shopping-list-bot -maxdepth 1 -type f \( -name 'shopping_list.sqlite3-wal' -o -name 'shopping_list.sqlite3-shm' \) -delete
sudo systemctl start shopping-list-bot.service shopping-list-bot-backup.timer
sudo systemctl status shopping-list-bot.service --no-pager
```

Keep `shopping_list.sqlite3.before-restore` until the restored bot has been checked. Replace
`SELECTED_BACKUP.sqlite3` with an exact filename from the backup directory; do not use a wildcard.
