from __future__ import annotations

import unittest
from zoneinfo import ZoneInfo

from app.database import Item, ShoppingList
from app.ui import PAGE_SIZE, archived_screen, dashboard_screen, parse_line_items


def make_list() -> ShoppingList:
    return ShoppingList(
        id=1,
        title="Общий <список>",
        status="open",
        revision=0,
        created_at="2026-08-21T10:00:00+00:00",
        created_by=1,
        closed_at=None,
        closed_by=None,
    )


def make_item(number: int, *, done: bool = False, text: str | None = None) -> Item:
    return Item(
        id=number,
        list_id=1,
        text=text or f"Товар {number}",
        is_done=done,
        created_at="2026-08-21T10:00:00+00:00",
        created_by=1,
        completed_at="2026-08-21T11:00:00+00:00" if done else None,
        completed_by=2 if done else None,
    )


class UiTestCase(unittest.TestCase):
    def test_parse_line_items(self) -> None:
        self.assertEqual(parse_line_items(" яблоки   1 кг "), ["яблоки 1 кг"])
        self.assertEqual(parse_line_items("сыр, 300 г"), ["сыр, 300 г"])
        self.assertEqual(
            parse_line_items("молоко\n\n хлеб\nсыр; нарезной"),
            ["молоко", "хлеб", "сыр; нарезной"],
        )
        self.assertEqual(parse_line_items("   "), [])

    def test_dashboard_escapes_text_and_has_compact_callbacks(self) -> None:
        screen = dashboard_screen(
            make_list(),
            [make_item(42, text="Молоко & <сыр>")],
            ZoneInfo("UTC"),
        )
        self.assertIn("Общий &lt;список&gt;", screen.text)
        self.assertIn("Молоко &amp; &lt;сыр&gt;", screen.text)
        callbacks = [
            button.callback_data
            for row in screen.markup.inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertIn("state:42:1:0", callbacks)
        self.assertTrue(all(len(value.encode()) <= 64 for value in callbacks))

    def test_dashboard_paginates(self) -> None:
        items = [make_item(number) for number in range(1, PAGE_SIZE + 3)]
        screen = dashboard_screen(make_list(), items, ZoneInfo("UTC"), page=1)
        self.assertIn("Страница 2 из 2", screen.text)
        self.assertIn(f"{PAGE_SIZE + 1}. ⬜ Товар {PAGE_SIZE + 1}", screen.text)
        self.assertNotIn("1. ⬜ Товар 1\n", screen.text)

    def test_everything_done_message(self) -> None:
        screen = dashboard_screen(make_list(), [make_item(1, done=True)], ZoneInfo("UTC"))
        self.assertIn("Всё куплено", screen.text)

    def test_archive_is_paginated_below_telegram_limit(self) -> None:
        archived = ShoppingList(
            id=7,
            title="Большая закупка",
            status="closed",
            revision=3,
            created_at="2026-08-21T10:00:00+00:00",
            created_by=1,
            closed_at="2026-08-21T11:00:00+00:00",
            closed_by=2,
        )
        items = [make_item(number, text="Я" * 120) for number in range(1, 101)]
        screen = archived_screen(archived, items, ZoneInfo("UTC"))
        utf16_units = len(screen.text.encode("utf-16-le")) // 2
        self.assertLessEqual(utf16_units, 4096)
        self.assertIn("Страница 1 из 7", screen.text)
        callbacks = [
            button.callback_data
            for row in screen.markup.inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertIn("history_open:7:1:0", callbacks)


if __name__ == "__main__":
    unittest.main()
