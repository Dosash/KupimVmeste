from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True, slots=True)
class Config:
    token: str
    allowed_user_ids: frozenset[int]
    database_path: Path
    timezone: ZoneInfo
    log_level: str

    @classmethod
    def from_env(cls) -> Config:
        token = os.environ.get("BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("BOT_TOKEN is required")

        raw_ids = os.environ.get("ALLOWED_USER_IDS", "").strip()
        try:
            allowed_ids = frozenset(
                int(part.strip()) for part in raw_ids.split(",") if part.strip()
            )
        except ValueError as exc:
            raise ValueError("ALLOWED_USER_IDS must be comma-separated integers") from exc
        if not allowed_ids:
            raise ValueError("ALLOWED_USER_IDS must contain at least one user ID")

        database_path = Path(
            os.environ.get("DATABASE_PATH", "/var/lib/shopping-list-bot/shopping_list.sqlite3")
        ).expanduser()
        timezone_name = os.environ.get("BOT_TIMEZONE", "Europe/Moscow").strip()
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown BOT_TIMEZONE: {timezone_name}") from exc

        log_level = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
        return cls(
            token=token,
            allowed_user_ids=allowed_ids,
            database_path=database_path,
            timezone=timezone,
            log_level=log_level,
        )
