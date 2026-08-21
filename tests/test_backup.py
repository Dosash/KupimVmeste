from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.backup import create_backup
from app.database import Database


class BackupTestCase(unittest.TestCase):
    def test_online_backup_is_complete_and_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "live.sqlite3"
            database = Database(database_path)
            database.add_items(1, ["Молоко", "Хлеб"])

            backup = create_backup(database_path, root / "backups", keep=2)
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            connection = sqlite3.connect(backup)
            try:
                self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM items").fetchone()[0], 2)
            finally:
                connection.close()
                database.close()


if __name__ == "__main__":
    unittest.main()
