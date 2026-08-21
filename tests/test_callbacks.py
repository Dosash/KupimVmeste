from __future__ import annotations

import unittest

from telegram import InlineKeyboardMarkup

from app.main import INPUT_PROMPT_PREFIX, ShoppingListBot
from app.ui import Screen


class CallbackPayloadTestCase(unittest.TestCase):
    def test_item_and_page(self) -> None:
        self.assertEqual(ShoppingListBot._item_and_page("item:42:3"), (42, 3))
        self.assertEqual(ShoppingListBot._item_and_page("item:42"), (42, 0))
        self.assertEqual(ShoppingListBot._item_and_page("item:bad:3"), (0, 0))

    def test_item_page_revision(self) -> None:
        self.assertEqual(ShoppingListBot._item_page_revision("edit:42:3:9"), (42, 3, 9))
        self.assertEqual(ShoppingListBot._item_page_revision("edit:bad"), (0, 0, 0))

    def test_list_revision(self) -> None:
        self.assertEqual(ShoppingListBot._list_revision_payload("close_yes:7:11"), (7, 11))
        self.assertEqual(ShoppingListBot._list_revision_payload("close_yes:bad"), (0, 0))

    def test_state_payload_only_accepts_binary_state(self) -> None:
        self.assertEqual(ShoppingListBot._state_payload("state:42:1:3"), (42, True, 3))
        self.assertEqual(ShoppingListBot._state_payload("state:42:0:3"), (42, False, 3))
        self.assertEqual(ShoppingListBot._state_payload("state:42:2:3"), (0, False, 0))

    def test_archive_payload(self) -> None:
        self.assertEqual(ShoppingListBot._archive_payload("history_open:4:2:1"), (4, 2, 1))

    def test_stale_data_prompts_are_recognized(self) -> None:
        self.assertTrue(ShoppingListBot._is_stale_input_prompt(INPUT_PROMPT_PREFIX + "цена"))
        self.assertFalse(ShoppingListBot._is_stale_input_prompt("➕ Добавление покупок"))

    def test_flash_never_pushes_screen_over_telegram_limit(self) -> None:
        screen = Screen("Я" * 4050, InlineKeyboardMarkup([]))
        rendered = ShoppingListBot._with_flash(screen, "&" * 120)
        self.assertLessEqual(len(rendered.text.encode("utf-16-le")) // 2, 4096)


if __name__ == "__main__":
    unittest.main()
