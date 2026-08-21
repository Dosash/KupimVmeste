from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.database import Database


class DatabaseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.tempdir.name) / "test.sqlite3")

    def tearDown(self) -> None:
        self.database.close()
        self.tempdir.cleanup()

    def test_complete_list_lifecycle(self) -> None:
        added = self.database.add_items(100_000_001, ["Молоко", "Хлеб", "Яблоки"])
        self.assertEqual([item.text for item in added], ["Молоко", "Хлеб", "Яблоки"])

        current = self.database.get_open_list()
        self.assertIsNotNone(current)
        assert current
        self.assertEqual(self.database.item_counts(current.id), (3, 0))

        item, changed = self.database.set_item_done(added[1].id, True, 5_000_000_001)
        self.assertTrue(changed)
        self.assertIsNotNone(item)
        assert item
        self.assertTrue(item.is_done)
        self.assertEqual(item.completed_by, 5_000_000_001)
        self.assertEqual(self.database.item_counts(current.id), (2, 1))

        item, changed = self.database.set_item_done(added[1].id, True, 100_000_001)
        self.assertFalse(changed)
        assert item
        self.assertTrue(item.is_done)

        item, changed = self.database.set_item_done(added[1].id, False, 100_000_001)
        self.assertTrue(changed)
        assert item
        self.assertFalse(item.is_done)
        self.assertIsNone(item.completed_by)

        edited = self.database.edit_item(added[0].id, "Молоко 2 л")
        self.assertIsNotNone(edited)
        self.assertEqual(edited.text, "Молоко 2 л")

        closed = self.database.close_open_list(100_000_001)
        self.assertIsNotNone(closed)
        self.assertIsNone(self.database.get_open_list())
        self.assertEqual(self.database.closed_lists()[0].id, current.id)
        self.assertIsNone(self.database.edit_item(added[0].id, "Нельзя"))

    def test_only_one_open_list_and_reopen_rules(self) -> None:
        first = self.database.create_list(1, "Первый")
        same = self.database.create_list(2, "Второй")
        self.assertEqual(first.id, same.id)
        self.assertEqual(same.title, "Первый")

        self.database.close_open_list(1)
        second = self.database.create_list(2, "Второй")
        self.assertNotEqual(first.id, second.id)
        self.assertIsNone(self.database.reopen_list(first.id))

        self.database.close_open_list(2)
        reopened = self.database.reopen_list(first.id)
        self.assertIsNotNone(reopened)
        assert reopened
        self.assertEqual(reopened.id, first.id)
        self.assertEqual(reopened.status, "open")

    def test_clear_done_and_delete(self) -> None:
        items = self.database.add_items(1, ["A", "B", "C"])
        self.database.set_item_done(items[0].id, True, 1)
        self.database.set_item_done(items[2].id, True, 1)
        self.assertEqual(self.database.clear_done(), 2)
        self.assertIsNone(self.database.get_item(items[0].id))
        self.assertTrue(self.database.delete_item(items[1].id))
        self.assertFalse(self.database.delete_item(items[1].id))

    def test_stale_confirmation_cannot_mutate_a_new_list(self) -> None:
        first_items = self.database.add_items(1, ["Старый товар"])
        first = self.database.get_open_list()
        assert first
        stale_revision = first.revision
        self.database.set_item_done(first_items[0].id, True, 1)
        self.database.close_open_list(1, first.id)

        second_items = self.database.add_items(2, ["Новый товар"])
        second = self.database.get_open_list()
        assert second
        self.database.set_item_done(second_items[0].id, True, 2)

        self.assertIsNone(self.database.clear_done(first.id, expected_revision=stale_revision))
        self.assertEqual(self.database.item_counts(second.id), (0, 1))
        self.assertIsNone(
            self.database.close_open_list(1, first.id, expected_revision=stale_revision)
        )
        self.assertEqual(self.database.get_open_list().id, second.id)

    def test_stale_confirmation_stays_invalid_after_reopen(self) -> None:
        items = self.database.add_items(1, ["A"])
        current = self.database.get_open_list()
        assert current
        self.database.set_item_done(items[0].id, True, 1)
        confirmation_revision = self.database.get_open_list().revision

        self.database.close_open_list(1, current.id, confirmation_revision)
        reopened = self.database.reopen_list(current.id)
        assert reopened
        newer = self.database.add_items(2, ["B"])[0]
        self.database.set_item_done(newer.id, True, 2)

        self.assertIsNone(self.database.clear_done(current.id, confirmation_revision))
        self.assertEqual(self.database.item_counts(current.id), (0, 2))
        self.assertIsNone(self.database.close_open_list(1, current.id, confirmation_revision))
        self.assertEqual(self.database.get_open_list().id, current.id)

    def test_stale_edit_and_delete_do_not_override_newer_change(self) -> None:
        item = self.database.add_items(1, ["Исходное"])[0]
        stale_revision = self.database.get_open_list().revision
        self.assertIsNotNone(self.database.edit_item(item.id, "Новое"))
        self.assertIsNone(self.database.edit_item(item.id, "Устаревшее", stale_revision))
        self.assertFalse(self.database.delete_item(item.id, stale_revision))
        self.assertEqual(self.database.get_item(item.id).text, "Новое")

    def test_stale_reopen_button_is_rejected(self) -> None:
        self.database.add_items(1, ["A"])
        current = self.database.get_open_list()
        assert current
        archived = self.database.close_open_list(1, current.id, current.revision)
        assert archived

        reopened = self.database.reopen_list(archived.id, archived.revision)
        assert reopened
        archived_again = self.database.close_open_list(1, reopened.id, reopened.revision)
        assert archived_again

        self.assertIsNone(self.database.reopen_list(archived.id, archived.revision))
        self.assertIsNone(self.database.get_open_list())

    def test_migrates_database_created_before_revisions(self) -> None:
        self.database.close()
        legacy_path = Path(self.tempdir.name) / "legacy.sqlite3"
        connection = sqlite3.connect(legacy_path)
        connection.execute(
            """
            CREATE TABLE shopping_lists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('open', 'closed')),
                created_at TEXT NOT NULL,
                created_by INTEGER NOT NULL,
                closed_at TEXT,
                closed_by INTEGER
            )
            """
        )
        connection.commit()
        connection.close()

        self.database = Database(legacy_path)
        created = self.database.create_list(1)
        self.assertEqual(created.revision, 0)

    def test_migrates_complete_legacy_schema_without_index_and_backfills_metadata(self) -> None:
        self.database.close()
        legacy_path = Path(self.tempdir.name) / "full-legacy.sqlite3"
        connection = sqlite3.connect(legacy_path)
        connection.executescript(
            """
            CREATE TABLE users (
                user_id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL,
                display_name TEXT NOT NULL, dashboard_message_id INTEGER,
                last_seen TEXT NOT NULL
            );
            CREATE TABLE shopping_lists (
                id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('open', 'closed')),
                created_at TEXT NOT NULL, created_by INTEGER NOT NULL,
                closed_at TEXT, closed_by INTEGER
            );
            CREATE TABLE items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                list_id INTEGER NOT NULL REFERENCES shopping_lists(id) ON DELETE CASCADE,
                text TEXT NOT NULL, is_done INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, created_by INTEGER NOT NULL,
                completed_at TEXT, completed_by INTEGER
            );
            INSERT INTO shopping_lists(id,title,status,created_at,created_by)
            VALUES (1,'Старый','open','2026-08-21T10:00:00+00:00',1);
            INSERT INTO items(list_id,text,created_at,created_by)
            VALUES (1,'Молоко 2 л','2026-08-21T10:00:00+00:00',1);
            """
        )
        connection.commit()
        connection.close()

        self.database = Database(legacy_path)
        item = self.database.list_items(1)[0]
        self.assertEqual((item.quantity_milli, item.unit, item.category), (2000, "l", "dairy"))
        self.assertEqual(item.normalized_text, "молоко")
        migrated = sqlite3.connect(legacy_path)
        try:
            self.assertEqual(migrated.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            indexes = {row[1] for row in migrated.execute("PRAGMA index_list(items)")}
            self.assertIn("items_by_list_v2", indexes)
        finally:
            migrated.close()

    def test_64_bit_user_ids_and_dashboards(self) -> None:
        self.database.upsert_user(5_000_000_001, 5_000_000_001, "Пользователь")
        self.database.set_dashboard(5_000_000_001, 123)
        dashboard = self.database.dashboards()[0]
        self.assertEqual(dashboard.user_id, 5_000_000_001)
        self.assertEqual(dashboard.dashboard_message_id, 123)


if __name__ == "__main__":
    unittest.main()
