from __future__ import annotations

import argparse
import os
import re
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

BACKUP_RE = re.compile(r"^shopping-list-\d{8}T\d{6}(?:\d{6})?Z\.sqlite3$")


def create_backup(database: Path, backup_dir: Path, keep: int = 30) -> Path:
    if keep < 1:
        raise ValueError("keep must be at least 1")
    if not database.is_file():
        raise FileNotFoundError(database)
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    final_path = backup_dir / f"shopping-list-{timestamp}.sqlite3"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".shopping-list-", suffix=".tmp", dir=backup_dir
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        source_uri = database.resolve().as_uri() + "?mode=ro"
        source = sqlite3.connect(source_uri, uri=True)
        destination = sqlite3.connect(temporary_path)
        try:
            source.execute("PRAGMA busy_timeout = 10000")
            source.backup(destination)
            result = destination.execute("PRAGMA quick_check").fetchone()
            if result is None or result[0] != "ok":
                raise RuntimeError(f"backup integrity check failed: {result!r}")
            destination.commit()
        finally:
            destination.close()
            source.close()
        os.chmod(temporary_path, 0o600)
        with temporary_path.open("rb") as backup_file:
            os.fsync(backup_file.fileno())
        os.replace(temporary_path, final_path)
        directory_fd = os.open(backup_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary_path.unlink(missing_ok=True)

    backups = sorted(
        (
            path
            for path in backup_dir.iterdir()
            if path.is_file() and BACKUP_RE.fullmatch(path.name)
        ),
        key=lambda path: path.name,
        reverse=True,
    )
    for old_backup in backups[keep:]:
        old_backup.unlink()
    return final_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a verified hot local SQLite backup")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--backup-dir", required=True, type=Path)
    parser.add_argument("--keep", type=int, default=30)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    path = create_backup(args.database, args.backup_dir, args.keep)
    print(f"BACKUP_OK path={path} size={path.stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
