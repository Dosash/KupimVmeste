# Contributing

Thanks for improving KupimVmeste.

## Development setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Run the complete local check before opening a pull request:

```bash
.venv/bin/ruff check app tests
.venv/bin/ruff format --check app tests
.venv/bin/python -m unittest discover -s tests -v
```

## Pull requests

- Keep changes focused and include tests for changed behaviour.
- Do not include Telegram tokens, real user IDs, databases, backups or production logs.
- Preserve backward-compatible SQLite migrations.
- Keep Telegram callback payloads at or below 64 UTF-8 bytes and message text at or below 4096
  UTF-16 code units.
- Update README or deployment documentation when configuration changes.

## Commit messages

Use short imperative messages, for example: `Add template pagination` or `Fix stale callback guard`.
