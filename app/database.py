from __future__ import annotations

import json
import re
import sqlite3
import threading
import unicodedata
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

CATEGORIES: tuple[tuple[str, str, str], ...] = (
    ("produce", "🥬", "Овощи и фрукты"),
    ("bakery", "🥖", "Хлеб и выпечка"),
    ("dairy", "🥛", "Молочное и яйца"),
    ("meat", "🥩", "Мясо и рыба"),
    ("grocery", "🥫", "Бакалея"),
    ("drinks", "🥤", "Напитки"),
    ("frozen", "❄️", "Заморозка"),
    ("household", "🧹", "Для дома"),
    ("hygiene", "🧴", "Гигиена"),
    ("pets", "🐾", "Для питомцев"),
    ("other", "📦", "Другое"),
)
CATEGORY_LABELS = {code: label for code, _, label in CATEGORIES}
CATEGORY_ICONS = {code: icon for code, icon, _ in CATEGORIES}
CATEGORY_ORDER = {code: index for index, (code, _, _) in enumerate(CATEGORIES)}

UNITS: tuple[tuple[str, str], ...] = (
    ("pcs", "шт"),
    ("kg", "кг"),
    ("g", "г"),
    ("l", "л"),
    ("ml", "мл"),
    ("pack", "уп"),
    ("bottle", "бут"),
    ("can", "бан"),
)
UNIT_LABELS = {code: label for code, label in UNITS}
NOTIFY_LEVELS = {"all", "important", "off"}
REMINDER_RULES = {"off", "daily18", "fri18", "sat10"}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


_QUANTITY_RE = re.compile(
    r"(?:^|\s)(\d+(?:[.,]\d+)?)\s*(шт\.?|кг|г|л|мл|уп\.?|бут\.?|бан\.?)$",
    re.IGNORECASE,
)
_INPUT_UNITS = {
    "шт": "pcs",
    "шт.": "pcs",
    "кг": "kg",
    "г": "g",
    "л": "l",
    "мл": "ml",
    "уп": "pack",
    "уп.": "pack",
    "бут": "bottle",
    "бут.": "bottle",
    "бан": "can",
    "бан.": "can",
}


def infer_item_metadata(text: str) -> tuple[int, str, str]:
    """Return quantity, unit and a normalized product name for safe merging."""
    match = _QUANTITY_RE.search(text.strip())
    if match is None:
        return 1000, "pcs", normalize(text)
    try:
        number = Decimal(match.group(1).replace(",", "."))
    except InvalidOperation:
        return 1000, "pcs", normalize(text)
    if number <= 0 or number > 999_999:
        return 1000, "pcs", normalize(text)
    quantity = int((number * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    base_name = text.strip()[: match.start()].rstrip(" ,;:-")
    normalized_name = normalize(base_name) or normalize(text)
    return quantity, _INPUT_UNITS[match.group(2).casefold()], normalized_name


def infer_quantity_unit(text: str) -> tuple[int, str]:
    quantity, unit, _ = infer_item_metadata(text)
    return quantity, unit


def guess_category(text: str) -> str:
    value = normalize(text)
    groups: tuple[tuple[str, tuple[str, ...]], ...] = (
        (
            "produce",
            (
                "яблок",
                "банан",
                "груш",
                "томат",
                "помидор",
                "огур",
                "карто",
                "морков",
                "лук",
                "чеснок",
                "капуст",
                "зелень",
                "авокадо",
                "лимон",
                "апельсин",
                "овощ",
                "фрукт",
                "ягод",
                "виноград",
                "перец",
            ),
        ),
        ("bakery", ("хлеб", "батон", "булоч", "лаваш", "выпеч", "багет")),
        (
            "dairy",
            ("молок", "кефир", "сыр", "йогурт", "творог", "сметан", "сливк", "яйц", "масло слив"),
        ),
        (
            "meat",
            (
                "мяс",
                "куриц",
                "индей",
                "фарш",
                "колбас",
                "сосиск",
                "рыб",
                "лосос",
                "тунец",
                "кревет",
            ),
        ),
        ("drinks", ("вода", "сок", "кофе", "чай", "лимонад", "газиров", "напит", "вино", "пиво")),
        ("frozen", ("заморож", "морожен", "пельмен", "вареник")),
        (
            "household",
            ("порошок", "губк", "пакет", "фольг", "бумаг", "салфет", "средство для", "мусор"),
        ),
        ("hygiene", ("шампун", "мыло", "паст", "щетк", "дезодорант", "гель", "крем", "ватн")),
        ("pets", ("корм", "наполнитель", "лакомство для", "кошк", "собак")),
        (
            "grocery",
            (
                "макарон",
                "рис",
                "греч",
                "мука",
                "сахар",
                "соль",
                "масло",
                "соус",
                "консерв",
                "круп",
                "специ",
            ),
        ),
    )
    for code, words in groups:
        if any(word in value for word in words):
            return code
    return "other"


@dataclass(frozen=True, slots=True)
class ShoppingList:
    id: int
    title: str
    status: str
    revision: int
    created_at: str
    created_by: int
    closed_at: str | None
    closed_by: int | None
    budget_minor: int | None = None


@dataclass(frozen=True, slots=True)
class Item:
    id: int
    list_id: int
    text: str
    is_done: bool
    created_at: str
    created_by: int
    completed_at: str | None
    completed_by: int | None
    quantity_milli: int = 1000
    unit: str = "pcs"
    category: str = "other"
    planned_price_minor: int | None = None
    actual_price_minor: int | None = None
    normalized_text: str = ""


@dataclass(frozen=True, slots=True)
class UserDashboard:
    user_id: int
    chat_id: int
    display_name: str
    dashboard_message_id: int | None
    notify_level: str = "all"
    reminder_rule: str = "off"


@dataclass(frozen=True, slots=True)
class Favorite:
    id: int
    text: str
    normalized_text: str
    quantity_milli: int
    unit: str
    category: str
    planned_price_minor: int | None
    created_at: str
    created_by: int
    updated_at: str
    updated_by: int
    use_count: int


@dataclass(frozen=True, slots=True)
class SavedTemplate:
    id: int
    name: str
    normalized_name: str
    created_at: str
    created_by: int
    updated_at: str
    updated_by: int
    item_count: int = 0


@dataclass(frozen=True, slots=True)
class TemplateItem:
    id: int
    template_id: int
    position: int
    text: str
    normalized_text: str
    quantity_milli: int
    unit: str
    category: str
    planned_price_minor: int | None


@dataclass(frozen=True, slots=True)
class UndoAction:
    id: int
    list_id: int
    actor_id: int
    kind: str
    result_revision: int
    created_at: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class MutationResult:
    items: list[Item]
    undo_id: int | None
    revision: int
    created_count: int = 0
    merged_count: int = 0


class Database:
    """Thread-safe SQLite store with atomic, revision-checked mutations."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = NORMAL")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @contextmanager
    def _transaction(self):
        """Acquire the writer lock before any read used by a mutation."""
        with self._lock:
            if self._connection.in_transaction:
                raise RuntimeError("nested database transaction")
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def _columns(self, table: str) -> set[str]:
        return {str(row["name"]) for row in self._connection.execute(f"PRAGMA table_info({table})")}

    def _add_columns(self, table: str, definitions: dict[str, str]) -> None:
        existing = self._columns(table)
        for name, definition in definitions.items():
            if name not in existing:
                self._connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def _migrate(self) -> None:
        previous_item_columns = self._columns("items")
        schema = """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL,
            display_name TEXT NOT NULL, dashboard_message_id INTEGER,
            last_seen TEXT NOT NULL, notify_level TEXT NOT NULL DEFAULT 'all',
            reminder_rule TEXT NOT NULL DEFAULT 'off'
        );
        CREATE TABLE IF NOT EXISTS shopping_lists (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('open', 'closed')),
            revision INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
            created_by INTEGER NOT NULL, closed_at TEXT, closed_by INTEGER,
            budget_minor INTEGER
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_open_list ON shopping_lists(status) WHERE status = 'open';
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            list_id INTEGER NOT NULL REFERENCES shopping_lists(id) ON DELETE CASCADE,
            text TEXT NOT NULL, is_done INTEGER NOT NULL DEFAULT 0 CHECK (is_done IN (0, 1)),
            created_at TEXT NOT NULL, created_by INTEGER NOT NULL,
            completed_at TEXT, completed_by INTEGER,
            quantity_milli INTEGER NOT NULL DEFAULT 1000,
            unit TEXT NOT NULL DEFAULT 'pcs', category TEXT NOT NULL DEFAULT 'other',
            planned_price_minor INTEGER, actual_price_minor INTEGER,
            normalized_text TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS favorites (
            id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL,
            normalized_text TEXT NOT NULL UNIQUE, quantity_milli INTEGER NOT NULL DEFAULT 1000,
            unit TEXT NOT NULL DEFAULT 'pcs', category TEXT NOT NULL DEFAULT 'other',
            planned_price_minor INTEGER, created_at TEXT NOT NULL, created_by INTEGER NOT NULL,
            updated_at TEXT NOT NULL, updated_by INTEGER NOT NULL, use_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            normalized_name TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
            created_by INTEGER NOT NULL, updated_at TEXT NOT NULL, updated_by INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS template_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            template_id INTEGER NOT NULL REFERENCES templates(id) ON DELETE CASCADE,
            position INTEGER NOT NULL, text TEXT NOT NULL, normalized_text TEXT NOT NULL,
            quantity_milli INTEGER NOT NULL DEFAULT 1000, unit TEXT NOT NULL DEFAULT 'pcs',
            category TEXT NOT NULL DEFAULT 'other', planned_price_minor INTEGER,
            UNIQUE(template_id, position)
        );
        CREATE INDEX IF NOT EXISTS template_items_order ON template_items(template_id, position);
        CREATE TABLE IF NOT EXISTS undo_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            list_id INTEGER NOT NULL REFERENCES shopping_lists(id) ON DELETE CASCADE,
            actor_id INTEGER NOT NULL, kind TEXT NOT NULL, base_revision INTEGER NOT NULL,
            result_revision INTEGER NOT NULL, payload_json TEXT NOT NULL,
            payload_version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
            undone_at TEXT, undone_by INTEGER, undo_revision INTEGER
        );
        CREATE INDEX IF NOT EXISTS undo_latest ON undo_actions(list_id, result_revision, undone_at, id DESC);
        CREATE TABLE IF NOT EXISTS reminder_deliveries (
            user_id INTEGER NOT NULL, reminder_key TEXT NOT NULL,
            delivered_at TEXT NOT NULL, PRIMARY KEY(user_id, reminder_key)
        );
        """
        with self._lock, self._connection:
            self._connection.executescript(schema)
            self._add_columns(
                "users",
                {
                    "notify_level": "TEXT NOT NULL DEFAULT 'all'",
                    "reminder_rule": "TEXT NOT NULL DEFAULT 'off'",
                },
            )
            self._add_columns(
                "shopping_lists",
                {"revision": "INTEGER NOT NULL DEFAULT 0", "budget_minor": "INTEGER"},
            )
            self._add_columns(
                "items",
                {
                    "quantity_milli": "INTEGER NOT NULL DEFAULT 1000",
                    "unit": "TEXT NOT NULL DEFAULT 'pcs'",
                    "category": "TEXT NOT NULL DEFAULT 'other'",
                    "planned_price_minor": "INTEGER",
                    "actual_price_minor": "INTEGER",
                    "normalized_text": "TEXT NOT NULL DEFAULT ''",
                },
            )
            self._add_columns("undo_actions", {"payload_version": "INTEGER NOT NULL DEFAULT 1"})
            if previous_item_columns and "quantity_milli" not in previous_item_columns:
                legacy_rows = self._connection.execute("SELECT id,text FROM items").fetchall()
                for legacy_row in legacy_rows:
                    text = str(legacy_row["text"])
                    quantity, unit, normalized_name = infer_item_metadata(text)
                    self._connection.execute(
                        """UPDATE items SET quantity_milli=?,unit=?,category=?,normalized_text=?
                           WHERE id=?""",
                        (
                            quantity,
                            unit,
                            guess_category(text),
                            normalized_name,
                            int(legacy_row["id"]),
                        ),
                    )
            rows = self._connection.execute(
                "SELECT id, text FROM items WHERE normalized_text = ''"
            ).fetchall()
            self._connection.executemany(
                "UPDATE items SET normalized_text = ? WHERE id = ?",
                [(normalize(str(row["text"])), int(row["id"])) for row in rows],
            )
            self._connection.execute(
                """CREATE INDEX IF NOT EXISTS items_by_list_v2
                   ON items(list_id,is_done,category,normalized_text,unit,id)"""
            )

    @staticmethod
    def _list_from_row(row: sqlite3.Row | None) -> ShoppingList | None:
        return ShoppingList(**dict(row)) if row is not None else None

    @staticmethod
    def _item_from_row(row: sqlite3.Row | None) -> Item | None:
        if row is None:
            return None
        values = dict(row)
        values["is_done"] = bool(values["is_done"])
        return Item(**values)

    @staticmethod
    def _favorite_from_row(row: sqlite3.Row) -> Favorite:
        return Favorite(**dict(row))

    @staticmethod
    def _template_from_row(row: sqlite3.Row) -> SavedTemplate:
        return SavedTemplate(**dict(row))

    @staticmethod
    def _item_snapshot(item: Item) -> dict[str, Any]:
        return asdict(item)

    def _insert_snapshot(self, s: dict[str, Any]) -> None:
        self._connection.execute(
            """INSERT INTO items(id,list_id,text,is_done,created_at,created_by,completed_at,
               completed_by,quantity_milli,unit,category,planned_price_minor,actual_price_minor,normalized_text)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                s["id"],
                s["list_id"],
                s["text"],
                int(s["is_done"]),
                s["created_at"],
                s["created_by"],
                s["completed_at"],
                s["completed_by"],
                s["quantity_milli"],
                s["unit"],
                s["category"],
                s["planned_price_minor"],
                s["actual_price_minor"],
                s["normalized_text"],
            ),
        )

    def _restore_snapshot(self, s: dict[str, Any]) -> None:
        self._connection.execute(
            """UPDATE items SET text=?,is_done=?,created_at=?,created_by=?,completed_at=?,
               completed_by=?,quantity_milli=?,unit=?,category=?,planned_price_minor=?,
               actual_price_minor=?,normalized_text=? WHERE id=? AND list_id=?""",
            (
                s["text"],
                int(s["is_done"]),
                s["created_at"],
                s["created_by"],
                s["completed_at"],
                s["completed_by"],
                s["quantity_milli"],
                s["unit"],
                s["category"],
                s["planned_price_minor"],
                s["actual_price_minor"],
                s["normalized_text"],
                s["id"],
                s["list_id"],
            ),
        )

    def _record_undo(
        self, list_id: int, actor_id: int, kind: str, revision: int, payload: dict[str, Any]
    ) -> int:
        created = datetime.now(UTC)
        cursor = self._connection.execute(
            """INSERT INTO undo_actions(list_id,actor_id,kind,base_revision,result_revision,
               payload_json,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?)""",
            (
                list_id,
                actor_id,
                kind,
                revision,
                revision + 1,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                created.isoformat(timespec="seconds"),
                (created + timedelta(minutes=30)).isoformat(timespec="seconds"),
            ),
        )
        return int(cursor.lastrowid)

    def _current_open_revision_for_item(self, item_id: int) -> tuple[Item, int] | None:
        row = self._connection.execute(
            """SELECT i.*, l.revision AS _revision FROM items i
               JOIN shopping_lists l ON l.id=i.list_id WHERE i.id=? AND l.status='open'""",
            (item_id,),
        ).fetchone()
        if row is None:
            return None
        values = dict(row)
        revision = int(values.pop("_revision"))
        values["is_done"] = bool(values["is_done"])
        return Item(**values), revision

    def _bump_revision(self, list_id: int, revision: int) -> bool:
        cursor = self._connection.execute(
            "UPDATE shopping_lists SET revision=revision+1 WHERE id=? AND status='open' AND revision=?",
            (list_id, revision),
        )
        return cursor.rowcount == 1

    def upsert_user(self, user_id: int, chat_id: int, display_name: str) -> None:
        now = utc_now()
        with self._transaction():
            self._connection.execute(
                """INSERT INTO users(user_id,chat_id,display_name,last_seen) VALUES (?,?,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id,
                   display_name=excluded.display_name,last_seen=excluded.last_seen""",
                (user_id, chat_id, display_name, now),
            )

    def set_dashboard(self, user_id: int, message_id: int | None) -> None:
        with self._transaction():
            self._connection.execute(
                "UPDATE users SET dashboard_message_id=? WHERE user_id=?", (message_id, user_id)
            )

    def dashboards(self) -> list[UserDashboard]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT user_id,chat_id,display_name,dashboard_message_id,notify_level,reminder_rule FROM users ORDER BY user_id"
            ).fetchall()
        return [UserDashboard(**dict(row)) for row in rows]

    def get_user(self, user_id: int) -> UserDashboard | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT user_id,chat_id,display_name,dashboard_message_id,notify_level,reminder_rule FROM users WHERE user_id=?",
                (user_id,),
            ).fetchone()
        return UserDashboard(**dict(row)) if row is not None else None

    def set_notify_level(self, user_id: int, level: str) -> bool:
        if level not in NOTIFY_LEVELS:
            return False
        with self._transaction():
            cursor = self._connection.execute(
                "UPDATE users SET notify_level=? WHERE user_id=?", (level, user_id)
            )
        return cursor.rowcount == 1

    def set_reminder_rule(self, user_id: int, rule: str) -> bool:
        if rule not in REMINDER_RULES:
            return False
        with self._transaction():
            cursor = self._connection.execute(
                "UPDATE users SET reminder_rule=? WHERE user_id=?", (rule, user_id)
            )
        return cursor.rowcount == 1

    def user_names(self) -> dict[int, str]:
        with self._lock:
            rows = self._connection.execute("SELECT user_id,display_name FROM users").fetchall()
        return {int(row["user_id"]): str(row["display_name"]) for row in rows}

    def get_open_list(self) -> ShoppingList | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM shopping_lists WHERE status='open' LIMIT 1"
            ).fetchone()
        return self._list_from_row(row)

    def _ensure_open_list(self, created_by: int, title: str | None = None) -> int:
        row = self._connection.execute(
            "SELECT id FROM shopping_lists WHERE status='open' LIMIT 1"
        ).fetchone()
        if row:
            return int(row["id"])
        cursor = self._connection.execute(
            "INSERT INTO shopping_lists(title,status,created_at,created_by) VALUES (?,'open',?,?)",
            ((title or "Общий список")[:80], utc_now(), created_by),
        )
        return int(cursor.lastrowid)

    def create_list(self, created_by: int, title: str | None = None) -> ShoppingList:
        with self._transaction():
            list_id = self._ensure_open_list(created_by, title)
            row = self._connection.execute(
                "SELECT * FROM shopping_lists WHERE id=?", (list_id,)
            ).fetchone()
        result = self._list_from_row(row)
        assert result is not None
        return result

    def _apply_specs_in_transaction(
        self,
        list_id: int,
        user_id: int,
        specs: list[dict[str, Any]],
        now: str,
    ) -> tuple[list[Item], dict[str, Any], int, int]:
        """Insert or merge active positions; caller owns an IMMEDIATE transaction."""
        created_ids: list[int] = []
        affected_ids: list[int] = []
        merged_before: dict[int, dict[str, Any]] = {}
        merge_count = 0
        for spec in specs:
            text = " ".join(str(spec["text"]).split())
            quantity = int(spec.get("quantity_milli", 1000))
            unit = str(spec.get("unit", "pcs"))
            category = str(spec.get("category", guess_category(text)))
            planned_price = spec.get("planned_price_minor")
            normalized_name = str(spec.get("normalized_text") or infer_item_metadata(text)[2])
            if quantity < 1 or quantity > 999_999_000:
                raise ValueError("invalid quantity")
            if unit not in UNIT_LABELS or category not in CATEGORY_LABELS:
                raise ValueError("invalid unit or category")
            if planned_price is not None and not 0 <= int(planned_price) <= 1_000_000_000:
                raise ValueError("invalid planned price")
            row = self._connection.execute(
                """SELECT * FROM items WHERE list_id=? AND is_done=0
                   AND normalized_text=? AND unit=? ORDER BY id LIMIT 1""",
                (list_id, normalized_name, unit),
            ).fetchone()
            existing = self._item_from_row(row)
            if existing is None:
                cursor = self._connection.execute(
                    """INSERT INTO items(list_id,text,created_at,created_by,normalized_text,
                       quantity_milli,unit,category,planned_price_minor) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        list_id,
                        text,
                        now,
                        user_id,
                        normalized_name,
                        quantity,
                        unit,
                        category,
                        planned_price,
                    ),
                )
                item_id = int(cursor.lastrowid)
                created_ids.append(item_id)
            else:
                merge_count += 1
                item_id = existing.id
                if item_id not in created_ids and item_id not in merged_before:
                    merged_before[item_id] = self._item_snapshot(existing)
                new_quantity = existing.quantity_milli + quantity
                if new_quantity > 999_999_000:
                    raise ValueError("merged quantity is too large")
                if existing.planned_price_minor is None:
                    new_price = int(planned_price) if planned_price is not None else None
                elif planned_price is None:
                    new_price = existing.planned_price_minor
                else:
                    new_price = existing.planned_price_minor + int(planned_price)
                    if new_price > 1_000_000_000:
                        raise ValueError("merged planned price is too large")
                self._connection.execute(
                    """UPDATE items SET quantity_milli=?,
                       planned_price_minor=? WHERE id=?""",
                    (new_quantity, new_price, item_id),
                )
            if item_id not in affected_ids:
                affected_ids.append(item_id)
        placeholders = ",".join("?" for _ in affected_ids)
        rows = self._connection.execute(
            f"SELECT * FROM items WHERE id IN ({placeholders}) ORDER BY id", affected_ids
        ).fetchall()
        items = [item for row in rows if (item := self._item_from_row(row)) is not None]
        created_snapshots = [self._item_snapshot(item) for item in items if item.id in created_ids]
        payload = {
            "items": created_snapshots,
            "merged_before": list(merged_before.values()),
        }
        return items, payload, len(created_ids), merge_count

    def add_items_with_undo(self, created_by: int, texts: Iterable[str]) -> MutationResult:
        clean_texts = [" ".join(text.split()) for text in texts if text.strip()]
        if not clean_texts:
            current = self.get_open_list()
            return MutationResult([], None, current.revision if current else 0)
        now = utc_now()
        with self._transaction():
            list_id = self._ensure_open_list(created_by)
            row = self._connection.execute(
                "SELECT revision FROM shopping_lists WHERE id=?", (list_id,)
            ).fetchone()
            revision = int(row["revision"])
            specs: list[dict[str, Any]] = []
            for text in clean_texts:
                quantity_milli, unit, normalized_name = infer_item_metadata(text)
                specs.append(
                    {
                        "text": text,
                        "quantity_milli": quantity_milli,
                        "unit": unit,
                        "category": guess_category(text),
                        "planned_price_minor": None,
                        "normalized_text": normalized_name,
                    }
                )
            items, payload, created_count, merged_count = self._apply_specs_in_transaction(
                list_id, created_by, specs, now
            )
            if not self._bump_revision(list_id, revision):
                raise sqlite3.OperationalError("shopping list changed while adding")
            undo_id = self._record_undo(list_id, created_by, "add", revision, payload)
        return MutationResult(items, undo_id, revision + 1, created_count, merged_count)

    def add_items(self, created_by: int, texts: Iterable[str]) -> list[Item]:
        return self.add_items_with_undo(created_by, texts).items

    def get_item(self, item_id: int) -> Item | None:
        with self._lock:
            result = self._current_open_revision_for_item(item_id)
        return result[0] if result else None

    def list_items(self, list_id: int) -> list[Item]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM items WHERE list_id=? ORDER BY is_done,id", (list_id,)
            ).fetchall()
        return [item for row in rows if (item := self._item_from_row(row)) is not None]

    def set_item_done(
        self, item_id: int, is_done: bool, changed_by: int
    ) -> tuple[Item | None, bool]:
        with self._transaction():
            result = self._current_open_revision_for_item(item_id)
            if result is None:
                return None, False
            item, revision = result
            if item.is_done == is_done:
                return item, False
            before = self._item_snapshot(item)
            if is_done:
                self._connection.execute(
                    "UPDATE items SET is_done=1,completed_at=?,completed_by=? WHERE id=? AND is_done=0",
                    (utc_now(), changed_by, item_id),
                )
            else:
                self._connection.execute(
                    "UPDATE items SET is_done=0,completed_at=NULL,completed_by=NULL WHERE id=? AND is_done=1",
                    (item_id,),
                )
            if not self._bump_revision(item.list_id, revision):
                raise sqlite3.OperationalError("shopping list changed while toggling")
            row = self._connection.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            updated = self._item_from_row(row)
            assert updated is not None
            self._record_undo(
                item.list_id,
                changed_by,
                "item",
                revision,
                {"before": before, "after": self._item_snapshot(updated)},
            )
        return updated, True

    def edit_item(
        self,
        item_id: int,
        text: str,
        expected_revision: int | None = None,
        changed_by: int = 0,
    ) -> Item | None:
        clean = " ".join(text.split())
        if not clean:
            return None
        with self._transaction():
            result = self._current_open_revision_for_item(item_id)
            if result is None:
                return None
            item, revision = result
            if expected_revision is not None and revision != expected_revision:
                return None
            before = self._item_snapshot(item)
            self._connection.execute(
                "UPDATE items SET text=?,normalized_text=? WHERE id=?",
                (clean, infer_item_metadata(clean)[2], item_id),
            )
            if not self._bump_revision(item.list_id, revision):
                return None
            row = self._connection.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            updated = self._item_from_row(row)
            assert updated is not None
            self._record_undo(
                item.list_id,
                changed_by,
                "item",
                revision,
                {"before": before, "after": self._item_snapshot(updated)},
            )
        return updated

    def update_item_details(
        self,
        item_id: int,
        changed_by: int,
        expected_revision: int,
        *,
        quantity_milli: int | None = None,
        unit: str | None = None,
        category: str | None = None,
        planned_price_minor: int | None | object = ...,
        actual_price_minor: int | None | object = ...,
    ) -> Item | None:
        with self._transaction():
            result = self._current_open_revision_for_item(item_id)
            if result is None:
                return None
            item, revision = result
            if revision != expected_revision:
                return None
            new_quantity = item.quantity_milli if quantity_milli is None else quantity_milli
            new_unit = item.unit if unit is None else unit
            new_category = item.category if category is None else category
            new_planned = (
                item.planned_price_minor if planned_price_minor is ... else planned_price_minor
            )
            new_actual = (
                item.actual_price_minor if actual_price_minor is ... else actual_price_minor
            )
            if new_quantity < 1 or new_quantity > 999_999_000:
                return None
            if new_unit not in UNIT_LABELS or new_category not in CATEGORY_LABELS:
                return None
            if new_planned is not None and not 0 <= int(new_planned) <= 1_000_000_000:
                return None
            if new_actual is not None and not 0 <= int(new_actual) <= 1_000_000_000:
                return None
            before = self._item_snapshot(item)
            self._connection.execute(
                """UPDATE items SET quantity_milli=?,unit=?,category=?,
                   planned_price_minor=?,actual_price_minor=? WHERE id=?""",
                (new_quantity, new_unit, new_category, new_planned, new_actual, item_id),
            )
            if not self._bump_revision(item.list_id, revision):
                return None
            row = self._connection.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            updated = self._item_from_row(row)
            assert updated is not None
            self._record_undo(
                item.list_id,
                changed_by,
                "item",
                revision,
                {"before": before, "after": self._item_snapshot(updated)},
            )
        return updated

    def delete_item(
        self, item_id: int, expected_revision: int | None = None, changed_by: int = 0
    ) -> bool:
        with self._transaction():
            result = self._current_open_revision_for_item(item_id)
            if result is None:
                return False
            item, revision = result
            if expected_revision is not None and revision != expected_revision:
                return False
            snapshot = self._item_snapshot(item)
            cursor = self._connection.execute("DELETE FROM items WHERE id=?", (item_id,))
            if cursor.rowcount != 1 or not self._bump_revision(item.list_id, revision):
                return False
            self._record_undo(item.list_id, changed_by, "delete", revision, {"items": [snapshot]})
        return True

    def clear_done(
        self,
        list_id: int | None = None,
        expected_revision: int | None = None,
        changed_by: int = 0,
    ) -> int | None:
        with self._transaction():
            if list_id is None:
                list_row = self._connection.execute(
                    "SELECT id,revision FROM shopping_lists WHERE status='open' LIMIT 1"
                ).fetchone()
            else:
                list_row = self._connection.execute(
                    "SELECT id,revision FROM shopping_lists WHERE id=? AND status='open'",
                    (list_id,),
                ).fetchone()
            if list_row is None:
                return None
            list_id = int(list_row["id"])
            revision = int(list_row["revision"])
            if expected_revision is not None and revision != expected_revision:
                return None
            rows = self._connection.execute(
                "SELECT * FROM items WHERE list_id=? AND is_done=1", (list_id,)
            ).fetchall()
            snapshots = [
                self._item_snapshot(item)
                for row in rows
                if (item := self._item_from_row(row)) is not None
            ]
            if not snapshots:
                return 0
            self._connection.execute("DELETE FROM items WHERE list_id=? AND is_done=1", (list_id,))
            if not self._bump_revision(list_id, revision):
                return None
            if snapshots:
                self._record_undo(list_id, changed_by, "delete", revision, {"items": snapshots})
        return len(snapshots)

    def set_budget(
        self, list_id: int, expected_revision: int, budget_minor: int | None, changed_by: int
    ) -> ShoppingList | None:
        if budget_minor is not None and not 0 <= budget_minor <= 10_000_000_000:
            return None
        with self._transaction():
            row = self._connection.execute(
                "SELECT * FROM shopping_lists WHERE id=? AND status='open' AND revision=?",
                (list_id, expected_revision),
            ).fetchone()
            current = self._list_from_row(row)
            if current is None:
                return None
            self._connection.execute(
                "UPDATE shopping_lists SET budget_minor=? WHERE id=?", (budget_minor, list_id)
            )
            if not self._bump_revision(list_id, expected_revision):
                return None
            self._record_undo(
                list_id,
                changed_by,
                "budget",
                expected_revision,
                {"before": current.budget_minor, "after": budget_minor},
            )
            updated = self._connection.execute(
                "SELECT * FROM shopping_lists WHERE id=?", (list_id,)
            ).fetchone()
        return self._list_from_row(updated)

    def budget_totals(self, list_id: int) -> tuple[int, int]:
        with self._lock:
            row = self._connection.execute(
                """SELECT COALESCE(SUM(planned_price_minor),0) planned,
                   COALESCE(SUM(actual_price_minor),0) actual FROM items WHERE list_id=?""",
                (list_id,),
            ).fetchone()
        return int(row["planned"]), int(row["actual"])

    def latest_undo(self) -> UndoAction | None:
        with self._lock:
            row = self._connection.execute(
                """SELECT u.id,u.list_id,u.actor_id,u.kind,u.result_revision,u.created_at,u.expires_at
                   FROM undo_actions u JOIN shopping_lists l ON l.id=u.list_id
                   WHERE l.status='open' AND l.revision=u.result_revision
                   AND u.undone_at IS NULL AND u.expires_at>=? ORDER BY u.id DESC LIMIT 1""",
                (utc_now(),),
            ).fetchone()
        return UndoAction(**dict(row)) if row is not None else None

    def undo_action(self, action_id: int, undone_by: int, expected_revision: int) -> bool:
        with self._transaction():
            row = self._connection.execute(
                """SELECT u.*,l.status,l.revision AS current_revision FROM undo_actions u
                   JOIN shopping_lists l ON l.id=u.list_id WHERE u.id=? AND u.undone_at IS NULL""",
                (action_id,),
            ).fetchone()
            if (
                row is None
                or row["status"] != "open"
                or int(row["current_revision"]) != expected_revision
                or int(row["result_revision"]) != expected_revision
                or str(row["expires_at"]) < utc_now()
            ):
                return False
            payload = json.loads(str(row["payload_json"]))
            kind = str(row["kind"])
            if kind == "add":
                ids = [int(item["id"]) for item in payload["items"]]
                if ids:
                    placeholders = ",".join("?" for _ in ids)
                    self._connection.execute(
                        f"DELETE FROM items WHERE list_id=? AND id IN ({placeholders})",
                        [int(row["list_id"]), *ids],
                    )
                for snapshot in payload.get("merged_before", []):
                    self._restore_snapshot(snapshot)
            elif kind == "item":
                self._restore_snapshot(payload["before"])
            elif kind == "delete":
                for snapshot in payload["items"]:
                    self._insert_snapshot(snapshot)
            elif kind == "budget":
                self._connection.execute(
                    "UPDATE shopping_lists SET budget_minor=? WHERE id=?",
                    (payload["before"], int(row["list_id"])),
                )
            else:
                return False
            if not self._bump_revision(int(row["list_id"]), expected_revision):
                return False
            self._connection.execute(
                """UPDATE undo_actions SET undone_at=?,undone_by=?,undo_revision=?
                   WHERE id=? AND undone_at IS NULL""",
                (utc_now(), undone_by, expected_revision + 1, action_id),
            )
        return True

    def favorite_from_item(self, item_id: int, user_id: int) -> Favorite | None:
        item = self.get_item(item_id)
        if item is None:
            return None
        now = utc_now()
        with self._transaction():
            self._connection.execute(
                """INSERT INTO favorites(text,normalized_text,quantity_milli,unit,category,
                   planned_price_minor,created_at,created_by,updated_at,updated_by)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(normalized_text) DO UPDATE SET text=excluded.text,
                   quantity_milli=excluded.quantity_milli,unit=excluded.unit,
                   category=excluded.category,planned_price_minor=excluded.planned_price_minor,
                   updated_at=excluded.updated_at,updated_by=excluded.updated_by""",
                (
                    item.text,
                    item.normalized_text,
                    item.quantity_milli,
                    item.unit,
                    item.category,
                    item.planned_price_minor,
                    now,
                    user_id,
                    now,
                    user_id,
                ),
            )
            row = self._connection.execute(
                "SELECT * FROM favorites WHERE normalized_text=?", (item.normalized_text,)
            ).fetchone()
        return self._favorite_from_row(row) if row is not None else None

    def favorites(self) -> list[Favorite]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM favorites ORDER BY use_count DESC,updated_at DESC,id DESC"
            ).fetchall()
        return [self._favorite_from_row(row) for row in rows]

    def delete_favorite(self, favorite_id: int) -> bool:
        with self._transaction():
            cursor = self._connection.execute("DELETE FROM favorites WHERE id=?", (favorite_id,))
        return cursor.rowcount == 1

    def _insert_specs(self, user_id: int, specs: list[dict[str, Any]]) -> MutationResult:
        now = utc_now()
        with self._transaction():
            list_id = self._ensure_open_list(user_id)
            row = self._connection.execute(
                "SELECT revision FROM shopping_lists WHERE id=?", (list_id,)
            ).fetchone()
            revision = int(row["revision"])
            items, payload, created_count, merged_count = self._apply_specs_in_transaction(
                list_id, user_id, specs, now
            )
            if not self._bump_revision(list_id, revision):
                raise sqlite3.OperationalError("shopping list changed while applying")
            undo_id = self._record_undo(list_id, user_id, "add", revision, payload)
        return MutationResult(items, undo_id, revision + 1, created_count, merged_count)

    def add_favorite(self, favorite_id: int, user_id: int) -> MutationResult | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM favorites WHERE id=?", (favorite_id,)
            ).fetchone()
        if row is None:
            return None
        favorite = self._favorite_from_row(row)
        result = self._insert_specs(
            user_id,
            [
                {
                    "text": favorite.text,
                    "quantity_milli": favorite.quantity_milli,
                    "unit": favorite.unit,
                    "category": favorite.category,
                    "planned_price_minor": favorite.planned_price_minor,
                    "normalized_text": favorite.normalized_text,
                }
            ],
        )
        with self._transaction():
            self._connection.execute(
                "UPDATE favorites SET use_count=use_count+1 WHERE id=?", (favorite_id,)
            )
        return result

    def save_template(
        self,
        name: str,
        user_id: int,
        list_id: int,
        expected_revision: int,
    ) -> SavedTemplate | None:
        clean_name = " ".join(name.split())[:60]
        if not clean_name:
            return None
        now = utc_now()
        with self._transaction():
            current = self._connection.execute(
                "SELECT id FROM shopping_lists WHERE id=? AND status='open' AND revision=?",
                (list_id, expected_revision),
            ).fetchone()
            if current is None:
                return None
            items = self._connection.execute(
                "SELECT * FROM items WHERE list_id=? ORDER BY id", (int(current["id"]),)
            ).fetchall()
            normalized_name = normalize(clean_name)
            existing = self._connection.execute(
                "SELECT id FROM templates WHERE normalized_name=?", (normalized_name,)
            ).fetchone()
            if existing:
                template_id = int(existing["id"])
                self._connection.execute(
                    "UPDATE templates SET name=?,updated_at=?,updated_by=? WHERE id=?",
                    (clean_name, now, user_id, template_id),
                )
                self._connection.execute(
                    "DELETE FROM template_items WHERE template_id=?", (template_id,)
                )
            else:
                cursor = self._connection.execute(
                    """INSERT INTO templates(name,normalized_name,created_at,created_by,updated_at,updated_by)
                       VALUES (?,?,?,?,?,?)""",
                    (clean_name, normalized_name, now, user_id, now, user_id),
                )
                template_id = int(cursor.lastrowid)
            for position, item_row in enumerate(items):
                item = self._item_from_row(item_row)
                assert item is not None
                self._connection.execute(
                    """INSERT INTO template_items(template_id,position,text,normalized_text,
                       quantity_milli,unit,category,planned_price_minor) VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        template_id,
                        position,
                        item.text,
                        item.normalized_text,
                        item.quantity_milli,
                        item.unit,
                        item.category,
                        item.planned_price_minor,
                    ),
                )
            row = self._connection.execute(
                """SELECT t.*,COUNT(ti.id) item_count FROM templates t
                   LEFT JOIN template_items ti ON ti.template_id=t.id
                   WHERE t.id=? GROUP BY t.id""",
                (template_id,),
            ).fetchone()
        return self._template_from_row(row) if row is not None else None

    def templates(self) -> list[SavedTemplate]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT t.*,COUNT(ti.id) item_count FROM templates t
                   LEFT JOIN template_items ti ON ti.template_id=t.id
                   GROUP BY t.id ORDER BY t.updated_at DESC,t.id DESC"""
            ).fetchall()
        return [self._template_from_row(row) for row in rows]

    def get_template(self, template_id: int) -> SavedTemplate | None:
        with self._lock:
            row = self._connection.execute(
                """SELECT t.*,COUNT(ti.id) item_count FROM templates t
                   LEFT JOIN template_items ti ON ti.template_id=t.id
                   WHERE t.id=? GROUP BY t.id""",
                (template_id,),
            ).fetchone()
        return self._template_from_row(row) if row is not None else None

    def template_items(self, template_id: int) -> list[TemplateItem]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM template_items WHERE template_id=? ORDER BY position",
                (template_id,),
            ).fetchall()
        return [TemplateItem(**dict(row)) for row in rows]

    def apply_template(self, template_id: int, user_id: int) -> MutationResult | None:
        items = self.template_items(template_id)
        if not items:
            return None
        return self._insert_specs(
            user_id,
            [
                {
                    "text": item.text,
                    "quantity_milli": item.quantity_milli,
                    "unit": item.unit,
                    "category": item.category,
                    "planned_price_minor": item.planned_price_minor,
                    "normalized_text": item.normalized_text,
                }
                for item in items
            ],
        )

    def delete_template(self, template_id: int) -> bool:
        with self._transaction():
            cursor = self._connection.execute("DELETE FROM templates WHERE id=?", (template_id,))
        return cursor.rowcount == 1

    def reminder_delivered(self, user_id: int, key: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM reminder_deliveries WHERE user_id=? AND reminder_key=?",
                (user_id, key),
            ).fetchone()
        return row is not None

    def mark_reminder_delivered(self, user_id: int, key: str) -> None:
        with self._transaction():
            self._connection.execute(
                "INSERT OR IGNORE INTO reminder_deliveries(user_id,reminder_key,delivered_at) VALUES (?,?,?)",
                (user_id, key, utc_now()),
            )

    def close_open_list(
        self,
        closed_by: int,
        list_id: int | None = None,
        expected_revision: int | None = None,
    ) -> ShoppingList | None:
        with self._transaction():
            if list_id is None:
                row = self._connection.execute(
                    "SELECT * FROM shopping_lists WHERE status='open' LIMIT 1"
                ).fetchone()
            else:
                row = self._connection.execute(
                    "SELECT * FROM shopping_lists WHERE id=? AND status='open'", (list_id,)
                ).fetchone()
            current = self._list_from_row(row)
            if current is None:
                return None
            revision = current.revision if expected_revision is None else expected_revision
            cursor = self._connection.execute(
                """UPDATE shopping_lists SET status='closed',closed_at=?,closed_by=?,revision=revision+1
                   WHERE id=? AND status='open' AND revision=?""",
                (utc_now(), closed_by, current.id, revision),
            )
            if cursor.rowcount != 1:
                return None
            updated = self._connection.execute(
                "SELECT * FROM shopping_lists WHERE id=?", (current.id,)
            ).fetchone()
        return self._list_from_row(updated)

    def reopen_list(
        self, list_id: int, expected_revision: int | None = None
    ) -> ShoppingList | None:
        with self._transaction():
            if self._connection.execute(
                "SELECT 1 FROM shopping_lists WHERE status='open'"
            ).fetchone():
                return None
            row = self._connection.execute(
                "SELECT * FROM shopping_lists WHERE id=? AND status='closed'", (list_id,)
            ).fetchone()
            current = self._list_from_row(row)
            if current is None:
                return None
            revision = current.revision if expected_revision is None else expected_revision
            cursor = self._connection.execute(
                """UPDATE shopping_lists SET status='open',closed_at=NULL,closed_by=NULL,revision=revision+1
                   WHERE id=? AND status='closed' AND revision=?""",
                (list_id, revision),
            )
            if cursor.rowcount != 1:
                return None
            updated = self._connection.execute(
                "SELECT * FROM shopping_lists WHERE id=?", (list_id,)
            ).fetchone()
        return self._list_from_row(updated)

    def closed_lists(self, limit: int = 10, offset: int = 0) -> list[ShoppingList]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM shopping_lists WHERE status='closed' ORDER BY closed_at DESC,id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [item for row in rows if (item := self._list_from_row(row)) is not None]

    def closed_list_count(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) count FROM shopping_lists WHERE status='closed'"
            ).fetchone()
        return int(row["count"])

    def get_closed_list(self, list_id: int) -> ShoppingList | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM shopping_lists WHERE id=? AND status='closed'", (list_id,)
            ).fetchone()
        return self._list_from_row(row)

    def item_counts(self, list_id: int) -> tuple[int, int]:
        with self._lock:
            row = self._connection.execute(
                """SELECT COALESCE(SUM(CASE WHEN is_done=0 THEN 1 ELSE 0 END),0) active,
                   COALESCE(SUM(CASE WHEN is_done=1 THEN 1 ELSE 0 END),0) done
                   FROM items WHERE list_id=?""",
                (list_id,),
            ).fetchone()
        return int(row["active"]), int(row["done"])
