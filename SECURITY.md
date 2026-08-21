# Security policy

## Supported version

Security fixes are applied to the latest version on the `main` branch.

## Reporting a vulnerability

Do not publish credentials, Telegram updates, database files or exploit details in a public issue.
Use GitHub's private vulnerability reporting feature if it is enabled for the repository. Otherwise,
contact the repository owner privately.

Repository maintainers should enable **Settings → Security → Private vulnerability reporting**
before making the repository public.

## Secrets and private data

- Never commit `.env`, `/etc/shopping-list-bot.env`, SQLite databases or backups.
- Treat `BOT_TOKEN` as a password. If it is exposed, revoke it with BotFather immediately.
- Keep `ALLOWED_USER_IDS` restricted to trusted Telegram accounts.
- Use `LOG_LEVEL=INFO` in production; debug logs may contain additional operational context.
- The bot stores list contents, display names and Telegram IDs locally in SQLite. Telegram still
  processes messages sent to the bot.

The repository's `.gitignore` excludes common secret and runtime-data files, but it is not a
substitute for reviewing staged files before every commit.
