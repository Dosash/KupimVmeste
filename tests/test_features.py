from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

from app.database import Database
from app.ui import dashboard_screen, item_screen


class FeatureDatabaseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.tempdir.name) / "features.sqlite3")

    def tearDown(self) -> None:
        self.database.close()
        self.tempdir.cleanup()

    def test_details_budget_and_durable_undo(self) -> None:
        item = self.database.add_items(1, ["Молоко"])[0]
        current = self.database.get_open_list()
        assert current
        self.assertEqual(item.category, "dairy")

        updated = self.database.update_item_details(
            item.id,
            1,
            current.revision,
            quantity_milli=2500,
            unit="l",
            category="drinks",
            planned_price_minor=34990,
            actual_price_minor=33950,
        )
        assert updated
        self.assertEqual((updated.quantity_milli, updated.unit), (2500, "l"))
        self.assertEqual(self.database.budget_totals(current.id), (34990, 33950))

        action = self.database.latest_undo()
        assert action
        self.assertTrue(self.database.undo_action(action.id, 2, action.result_revision))
        restored = self.database.get_item(item.id)
        assert restored
        self.assertEqual((restored.quantity_milli, restored.unit), (1000, "pcs"))
        self.assertIsNone(restored.planned_price_minor)

        current = self.database.get_open_list()
        assert current
        budgeted = self.database.set_budget(current.id, current.revision, 500000, 1)
        assert budgeted
        self.assertEqual(budgeted.budget_minor, 500000)
        action = self.database.latest_undo()
        assert action
        self.assertTrue(self.database.undo_action(action.id, 1, action.result_revision))
        self.assertIsNone(self.database.get_open_list().budget_minor)

    def test_unambiguous_quantity_suffix_is_parsed_without_changing_text(self) -> None:
        milk, cheese = self.database.add_items(1, ["Молоко 2 л", "Сыр, 300 г"])
        self.assertEqual((milk.text, milk.quantity_milli, milk.unit), ("Молоко 2 л", 2000, "l"))
        self.assertEqual(
            (cheese.text, cheese.quantity_milli, cheese.unit), ("Сыр, 300 г", 300000, "g")
        )

    def test_active_duplicates_merge_once_and_undo_restores_quantity(self) -> None:
        first = self.database.add_items(1, ["Молоко"])[0]
        before = self.database.get_open_list()
        assert before
        merged = self.database.add_items(2, ["  молоко  "])[0]
        after = self.database.get_open_list()
        assert after
        self.assertEqual(merged.id, first.id)
        self.assertEqual(merged.quantity_milli, 2000)
        self.assertEqual(after.revision, before.revision + 1)
        self.assertEqual(len(self.database.list_items(after.id)), 1)
        action = self.database.latest_undo()
        assert action
        self.assertTrue(self.database.undo_action(action.id, 2, action.result_revision))
        self.assertEqual(self.database.get_item(first.id).quantity_milli, 1000)

        eggs = self.database.add_items(1, ["Яйца 6 шт", "яйца 4 шт"])
        self.assertEqual(len(eggs), 1)
        self.assertEqual(eggs[0].quantity_milli, 10000)

    def test_delete_can_be_undone(self) -> None:
        item = self.database.add_items(1, ["Хлеб"])[0]
        current = self.database.get_open_list()
        assert current
        self.assertTrue(self.database.delete_item(item.id, current.revision, changed_by=1))
        self.assertIsNone(self.database.get_item(item.id))
        action = self.database.latest_undo()
        assert action
        self.assertTrue(self.database.undo_action(action.id, 2, action.result_revision))
        self.assertEqual(self.database.get_item(item.id).text, "Хлеб")

    def test_undo_survives_database_restart(self) -> None:
        item = self.database.add_items(1, ["Кофе"])[0]
        path = self.database.path
        self.database.close()
        self.database = Database(path)
        action = self.database.latest_undo()
        assert action
        self.assertTrue(self.database.undo_action(action.id, 1, action.result_revision))
        self.assertIsNone(self.database.get_item(item.id))

    def test_stale_template_prompt_cannot_snapshot_new_list(self) -> None:
        self.database.add_items(1, ["Старое"])
        first = self.database.get_open_list()
        assert first
        self.database.close_open_list(1, first.id, first.revision)
        self.database.add_items(2, ["Новое"])
        second = self.database.get_open_list()
        assert second
        self.assertEqual(first.revision, second.revision)
        self.assertIsNone(self.database.save_template("Устаревший", 1, first.id, first.revision))

    def test_favorites_templates_and_personal_settings(self) -> None:
        self.database.upsert_user(1, 1, "Первый")
        item = self.database.add_items(1, ["Яблоки"])[0]
        current = self.database.get_open_list()
        assert current
        item = self.database.update_item_details(
            item.id, 1, current.revision, planned_price_minor=10000
        )
        assert item
        favorite = self.database.favorite_from_item(item.id, 1)
        assert favorite
        result = self.database.add_favorite(favorite.id, 1)
        assert result
        self.assertEqual(len(result.items), 1)

        current = self.database.get_open_list()
        assert current
        template = self.database.save_template("Каждую неделю", 1, current.id, current.revision)
        assert template
        self.assertEqual(template.item_count, 1)
        self.assertEqual(len(self.database.template_items(template.id)), 1)
        self.assertEqual(self.database.list_items(current.id)[0].quantity_milli, 2000)
        self.assertEqual(self.database.list_items(current.id)[0].planned_price_minor, 20000)

        self.assertTrue(self.database.set_notify_level(1, "important"))
        self.assertTrue(self.database.set_reminder_rule(1, "fri18"))
        user = self.database.get_user(1)
        assert user
        self.assertEqual((user.notify_level, user.reminder_rule), ("important", "fri18"))
        self.database.mark_reminder_delivered(1, "fri18:2026-08-21")
        self.assertTrue(self.database.reminder_delivered(1, "fri18:2026-08-21"))


class FeatureUiTestCase(unittest.TestCase):
    def test_dashboard_has_one_full_width_button_per_item_and_no_dots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "ui.sqlite3")
            item = database.add_items(
                1, ["Очень длинное название товара, которое должно занимать всю строку"]
            )[0]
            current = database.get_open_list()
            assert current
            screen = dashboard_screen(current, [item], ZoneInfo("UTC"))
            state_rows = [
                row
                for row in screen.markup.inline_keyboard
                if row and (row[0].callback_data or "").startswith("state:")
            ]
            self.assertEqual(len(state_rows), 1)
            self.assertEqual(len(state_rows[0]), 1)
            self.assertNotIn("⋯", str(screen.markup.inline_keyboard))

            card = item_screen(item, {1: "Пользователь"}, ZoneInfo("UTC"), 0, current.revision)
            callbacks = [
                button.callback_data
                for row in card.markup.inline_keyboard
                for button in row
                if button.callback_data
            ]
            self.assertTrue(all(len(value.encode("utf-8")) <= 64 for value in callbacks))
            database.close()


if __name__ == "__main__":
    unittest.main()
