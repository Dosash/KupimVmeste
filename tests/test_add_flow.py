from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from app.config import Config
from app.database import Database
from app.main import ShoppingListBot


class AddFlowTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.tempdir.name) / "test.sqlite3")
        config = Config(
            token="test-token",
            allowed_user_ids=frozenset({1, 2}),
            database_path=Path(self.tempdir.name) / "test.sqlite3",
            timezone=ZoneInfo("UTC"),
            log_level="INFO",
        )
        self.service = ShoppingListBot(config, self.database)
        self.service._show_home = AsyncMock()
        self.service._sync_dashboards = AsyncMock()
        self.service._notify_others = AsyncMock()

    async def asyncTearDown(self) -> None:
        self.database.close()
        self.tempdir.cleanup()

    @staticmethod
    def make_update(text: str) -> SimpleNamespace:
        message = SimpleNamespace(text=text, reply_text=AsyncMock())
        user = SimpleNamespace(id=1, full_name="Анна")
        return SimpleNamespace(effective_message=message, effective_user=user)

    async def test_continuous_mode_adds_one_and_stays_open(self) -> None:
        update = self.make_update("Молоко 2 л")
        context = SimpleNamespace(user_data={"mode": {"kind": "add"}})

        await self.service._add_from_text(update, context, update.effective_message.text)

        current = self.database.get_open_list()
        assert current
        self.assertEqual(
            [item.text for item in self.database.list_items(current.id)],
            ["Молоко 2 л"],
        )
        self.assertEqual(context.user_data["mode"]["kind"], "add")
        update.effective_message.reply_text.assert_awaited_once()
        self.service._show_home.assert_not_awaited()
        self.service._sync_dashboards.assert_awaited_once()

    async def test_comma_stays_inside_one_item(self) -> None:
        update = self.make_update("Сыр, 300 г")
        context = SimpleNamespace(user_data={})

        await self.service._add_from_text(update, context, update.effective_message.text)

        current = self.database.get_open_list()
        assert current
        self.assertEqual(self.database.list_items(current.id)[0].text, "Сыр, 300 г")
        self.service._show_home.assert_awaited_once()

    async def test_multiline_message_adds_one_item_per_line(self) -> None:
        update = self.make_update("Молоко\nХлеб")
        context = SimpleNamespace(user_data={"mode": {"kind": "add"}})

        await self.service._add_from_text(update, context, update.effective_message.text)

        current = self.database.get_open_list()
        assert current
        self.assertEqual(
            [item.text for item in self.database.list_items(current.id)],
            ["Молоко", "Хлеб"],
        )
        self.assertEqual(context.user_data["mode"]["kind"], "add")
        update.effective_message.reply_text.assert_awaited_once()
        self.service._sync_dashboards.assert_awaited_once()

    async def test_oversized_batch_is_rejected_atomically(self) -> None:
        update = self.make_update("\n".join(f"Товар {number}" for number in range(51)))
        context = SimpleNamespace(user_data={"mode": {"kind": "add"}})

        await self.service._add_from_text(update, context, update.effective_message.text)

        self.assertIsNone(self.database.get_open_list())
        update.effective_message.reply_text.assert_awaited_once()
        self.service._sync_dashboards.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
