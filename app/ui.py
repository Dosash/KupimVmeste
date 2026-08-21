from __future__ import annotations

import html
import math
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .database import (
    CATEGORIES,
    CATEGORY_ICONS,
    CATEGORY_LABELS,
    CATEGORY_ORDER,
    UNIT_LABELS,
    UNITS,
    Favorite,
    Item,
    SavedTemplate,
    ShoppingList,
    TemplateItem,
    UndoAction,
    UserDashboard,
)

PAGE_SIZE = 8
ARCHIVE_PAGE_SIZE = 15
HISTORY_PAGE_SIZE = 8
MAX_ITEM_LENGTH = 120
MAX_BATCH_SIZE = 50


@dataclass(frozen=True, slots=True)
class Screen:
    text: str
    markup: InlineKeyboardMarkup


def shorten(value: str, limit: int = 48) -> str:
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def parse_line_items(text: str) -> list[str]:
    """Every non-empty line is exactly one shopping-list position."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return [" ".join(line.split()) for line in normalized.split("\n") if line.strip()]


def format_quantity(quantity_milli: int, unit: str) -> str:
    if quantity_milli % 1000 == 0:
        number = str(quantity_milli // 1000)
    else:
        number = f"{quantity_milli / 1000:.3f}".rstrip("0").rstrip(".").replace(".", ",")
    return f"{number} {UNIT_LABELS.get(unit, 'шт')}"


def format_money(value: int | None) -> str:
    if value is None:
        return "—"
    rubles, kopecks = divmod(value, 100)
    grouped = f"{rubles:,}".replace(",", " ")
    return f"{grouped} ₽" if not kopecks else f"{grouped},{kopecks:02d} ₽"


def _format_time(value: str | None, timezone: ZoneInfo) -> str:
    if not value:
        return "—"
    return datetime.fromisoformat(value).astimezone(timezone).strftime("%d.%m.%Y в %H:%M")


def ordered_items(items: list[Item]) -> list[Item]:
    return sorted(
        items,
        key=lambda item: (
            int(item.is_done),
            CATEGORY_ORDER.get(item.category, 999),
            item.id if not item.is_done else -item.id,
        ),
    )


def _page(items: list[Item], page: int, size: int = PAGE_SIZE) -> tuple[list[Item], int, int]:
    total_pages = max(1, math.ceil(len(items) / size))
    page = max(0, min(page, total_pages - 1))
    start = page * size
    return items[start : start + size], page, total_pages


def _item_label(item: Item, number: int, *, action: bool = False) -> str:
    prefix = ("↩️" if item.is_done else "☑️") if action else "⚙️"
    label = f"{prefix} {number}. {shorten(item.text, 43)}"
    quantity = format_quantity(item.quantity_milli, item.unit)
    normalized_name = " ".join(item.text.lower().replace(".", ",").split())
    normalized_quantity = quantity.lower().replace(".", ",")
    if not normalized_name.endswith(normalized_quantity):
        label += f" · {quantity}"
    if item.planned_price_minor is not None:
        label += f" · {format_money(item.planned_price_minor)}"
    return label


def dashboard_screen(
    current: ShoppingList | None,
    items: list[Item],
    timezone: ZoneInfo,
    page: int = 0,
    undo: UndoAction | None = None,
) -> Screen:
    if current is None:
        return Screen(
            "🛒 <b>Общий список покупок</b>\n\n"
            "Открытого списка пока нет. Создайте новый, выберите шаблон или просто отправьте товар.",
            InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🆕 Создать список", callback_data="new")],
                    [
                        InlineKeyboardButton("⭐ Избранное", callback_data="favorites"),
                        InlineKeyboardButton("📦 Шаблоны", callback_data="templates"),
                    ],
                    [
                        InlineKeyboardButton("🕘 История", callback_data="history"),
                        InlineKeyboardButton("⚙️ Настройки", callback_data="settings"),
                    ],
                ]
            ),
        )

    sorted_items = ordered_items(items)
    active_count = sum(not item.is_done for item in sorted_items)
    done_count = len(sorted_items) - active_count
    visible, page, total_pages = _page(sorted_items, page)
    planned = sum(item.planned_price_minor or 0 for item in sorted_items)
    actual = sum(item.actual_price_minor or 0 for item in sorted_items)

    header = (
        f"🛒 <b>{html.escape(current.title)}</b>\n"
        f"Осталось: <b>{active_count}</b> · Куплено: <b>{done_count}</b>"
    )
    if current.budget_minor is not None or planned or actual:
        budget = (
            format_money(current.budget_minor) if current.budget_minor is not None else "не задан"
        )
        header += f"\n💰 План: <b>{format_money(planned)}</b> · Факт: <b>{format_money(actual)}</b> · Бюджет: <b>{budget}</b>"

    if not sorted_items:
        body = "Список пуст. Добавьте первую покупку 👇"
    else:
        lines: list[str] = []
        if active_count == 0:
            lines.extend(["🎉 <b>Всё куплено!</b>", ""])
        last_group: str | None = None
        start = page * PAGE_SIZE + 1
        for number, item in enumerate(visible, start=start):
            group = "done" if item.is_done else item.category
            if group != last_group:
                if group == "done":
                    lines.append("✅ <b>Куплено</b>")
                else:
                    lines.append(
                        f"{CATEGORY_ICONS.get(group, '📦')} <b>{html.escape(CATEGORY_LABELS.get(group, 'Другое'))}</b>"
                    )
                last_group = group
            icon = "✅" if item.is_done else "⬜"
            safe_name = html.escape(shorten(item.text, 70))
            suffix = format_quantity(item.quantity_milli, item.unit)
            normalized_name = " ".join(item.text.lower().replace(".", ",").split())
            show_quantity = not normalized_name.endswith(suffix.lower().replace(".", ","))
            details: list[str] = [suffix] if show_quantity else []
            if item.planned_price_minor is not None:
                details.append(f"~{format_money(item.planned_price_minor)}")
            detail_text = f" · {' · '.join(details)}" if details else ""
            lines.append(f"{number}. {icon} {safe_name}{detail_text}")
        body = "\n".join(lines)

    text = f"{header}\n\n{body}"
    if total_pages > 1:
        text += f"\n\nСтраница {page + 1} из {total_pages}"

    keyboard: list[list[InlineKeyboardButton]] = []
    start = page * PAGE_SIZE + 1
    for number, item in enumerate(visible, start=start):
        # Exactly one full-width button: no adjacent menu button can cover the name.
        keyboard.append(
            [
                InlineKeyboardButton(
                    _item_label(item, number, action=True),
                    callback_data=f"state:{item.id}:{int(not item.is_done)}:{page}",
                )
            ]
        )
    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️", callback_data=f"home:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
        if page + 1 < total_pages:
            nav.append(InlineKeyboardButton("▶️", callback_data=f"home:{page + 1}"))
        keyboard.append(nav)
    if undo is not None:
        keyboard.append(
            [
                InlineKeyboardButton(
                    "↩️ Отменить последнее действие",
                    callback_data=f"undo:{undo.id}:{undo.result_revision}",
                )
            ]
        )
    keyboard.extend(
        [
            [
                InlineKeyboardButton("➕ Добавить", callback_data="add"),
                InlineKeyboardButton("🔄 Обновить", callback_data=f"home:{page}"),
            ],
            [InlineKeyboardButton("🛠 Управление товарами", callback_data="manage:0")],
            [
                InlineKeyboardButton("⭐ Избранное", callback_data="favorites"),
                InlineKeyboardButton("📦 Шаблоны", callback_data="templates"),
            ],
            [
                InlineKeyboardButton("💰 Бюджет", callback_data="budget"),
                InlineKeyboardButton("⚙️ Настройки", callback_data="settings"),
            ],
        ]
    )
    if done_count:
        keyboard.append([InlineKeyboardButton("🧹 Убрать купленное", callback_data="clear_ask")])
    keyboard.append(
        [
            InlineKeyboardButton("🕘 История", callback_data="history"),
            InlineKeyboardButton("🔒 Закрыть", callback_data="close_ask"),
        ]
    )
    return Screen(text, InlineKeyboardMarkup(keyboard))


def manage_screen(current: ShoppingList | None, items: list[Item], page: int = 0) -> Screen:
    if current is None:
        return Screen(
            "🛠 <b>Управление товарами</b>\n\nОткрытого списка нет.",
            InlineKeyboardMarkup([[InlineKeyboardButton("← К списку", callback_data="home:0")]]),
        )
    sorted_items = ordered_items(items)
    visible, page, total_pages = _page(sorted_items, page)
    text = (
        "🛠 <b>Управление товарами</b>\n\n"
        "Здесь можно изменить название, количество, единицу, категорию и цены. Выберите позицию:"
    )
    keyboard: list[list[InlineKeyboardButton]] = []
    start = page * PAGE_SIZE + 1
    for number, item in enumerate(visible, start=start):
        keyboard.append(
            [
                InlineKeyboardButton(
                    _item_label(item, number), callback_data=f"item:{item.id}:{page}"
                )
            ]
        )
    if not visible:
        text += "\n\nСписок пока пуст."
    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️", callback_data=f"manage:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
        if page + 1 < total_pages:
            nav.append(InlineKeyboardButton("▶️", callback_data=f"manage:{page + 1}"))
        keyboard.append(nav)
    keyboard.extend(
        [
            [InlineKeyboardButton("➕ Добавить товар", callback_data="add")],
            [InlineKeyboardButton("← К списку", callback_data="home:0")],
        ]
    )
    return Screen(text, InlineKeyboardMarkup(keyboard))


def item_screen(
    item: Item,
    names: dict[int, str],
    timezone: ZoneInfo,
    page: int,
    list_revision: int,
) -> Screen:
    creator = html.escape(names.get(item.created_by, "пользователь"))
    category = CATEGORY_LABELS.get(item.category, "Другое")
    status = "✅ Куплено" if item.is_done else "⬜ Нужно купить"
    lines = [
        f"<b>{html.escape(item.text)}</b>",
        "",
        status,
        f"🗂 Категория: <b>{html.escape(category)}</b>",
        f"📏 Количество: <b>{format_quantity(item.quantity_milli, item.unit)}</b>",
        f"💰 Плановая цена позиции: <b>{format_money(item.planned_price_minor)}</b>",
        f"🧾 Фактическая цена: <b>{format_money(item.actual_price_minor)}</b>",
        "",
        f"Добавил: {creator}",
        f"Создано: {_format_time(item.created_at, timezone)}",
    ]
    if item.is_done:
        buyer = html.escape(names.get(item.completed_by or 0, "пользователь"))
        lines.extend([f"Купил: {buyer}", f"Отмечено: {_format_time(item.completed_at, timezone)}"])
    toggle = "↩️ Вернуть в покупки" if item.is_done else "☑️ Отметить купленным"
    markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("➖", callback_data=f"qty:{item.id}:m:{page}:{list_revision}"),
                InlineKeyboardButton(
                    format_quantity(item.quantity_milli, item.unit),
                    callback_data=f"qty_input:{item.id}:{page}:{list_revision}",
                ),
                InlineKeyboardButton("➕", callback_data=f"qty:{item.id}:p:{page}:{list_revision}"),
            ],
            [
                InlineKeyboardButton(
                    "📏 Единица", callback_data=f"unit_menu:{item.id}:{page}:{list_revision}"
                ),
                InlineKeyboardButton(
                    "🗂 Категория", callback_data=f"category_menu:{item.id}:{page}:{list_revision}"
                ),
            ],
            [
                InlineKeyboardButton(
                    "💰 План", callback_data=f"price_plan:{item.id}:{page}:{list_revision}"
                ),
                InlineKeyboardButton(
                    "🧾 Факт", callback_data=f"price_actual:{item.id}:{page}:{list_revision}"
                ),
            ],
            [
                InlineKeyboardButton(
                    "⭐ В избранное", callback_data=f"fav_save:{item.id}:{page}:{list_revision}"
                ),
                InlineKeyboardButton(
                    "✏️ Название", callback_data=f"edit:{item.id}:{page}:{list_revision}"
                ),
            ],
            [
                InlineKeyboardButton(
                    toggle, callback_data=f"state:{item.id}:{int(not item.is_done)}:{page}"
                )
            ],
            [
                InlineKeyboardButton(
                    "🗑 Удалить", callback_data=f"delete_ask:{item.id}:{page}:{list_revision}"
                )
            ],
            [
                InlineKeyboardButton("← К товарам", callback_data=f"manage:{page}"),
                InlineKeyboardButton("🏠 Список", callback_data="home:0"),
            ],
        ]
    )
    return Screen("\n".join(lines), markup)


def unit_screen(item: Item, page: int, revision: int) -> Screen:
    rows: list[list[InlineKeyboardButton]] = []
    for index in range(0, len(UNITS), 2):
        row: list[InlineKeyboardButton] = []
        for code, label in UNITS[index : index + 2]:
            mark = "✓ " if item.unit == code else ""
            row.append(
                InlineKeyboardButton(
                    mark + label, callback_data=f"unit_set:{item.id}:{code}:{page}:{revision}"
                )
            )
        rows.append(row)
    rows.append([InlineKeyboardButton("← К товару", callback_data=f"item:{item.id}:{page}")])
    return Screen(
        f"📏 <b>Единица измерения</b>\n\n{html.escape(item.text)}", InlineKeyboardMarkup(rows)
    )


def category_screen(item: Item, page: int, revision: int) -> Screen:
    rows: list[list[InlineKeyboardButton]] = []
    for index in range(0, len(CATEGORIES), 2):
        row: list[InlineKeyboardButton] = []
        for code, icon, label in CATEGORIES[index : index + 2]:
            mark = "✓ " if item.category == code else ""
            row.append(
                InlineKeyboardButton(
                    f"{mark}{icon} {label}",
                    callback_data=f"category_set:{item.id}:{code}:{page}:{revision}",
                )
            )
        rows.append(row)
    rows.append([InlineKeyboardButton("← К товару", callback_data=f"item:{item.id}:{page}")])
    return Screen(f"🗂 <b>Категория</b>\n\n{html.escape(item.text)}", InlineKeyboardMarkup(rows))


def favorites_screen(favorites: list[Favorite], *, manage: bool = False) -> Screen:
    title = "🗑 <b>Управление избранным</b>" if manage else "⭐ <b>Избранные товары</b>"
    hint = (
        "Нажмите, чтобы удалить позицию."
        if manage
        else "Нажмите товар — он сразу добавится в текущий список."
    )
    text = f"{title}\n\n{hint}"
    keyboard: list[list[InlineKeyboardButton]] = []
    for favorite in favorites[:40]:
        action = "🗑" if manage else "➕"
        callback = f"fav_delete_ask:{favorite.id}" if manage else f"fav_add:{favorite.id}"
        label = f"{action} {shorten(favorite.text, 43)} · {format_quantity(favorite.quantity_milli, favorite.unit)}"
        keyboard.append([InlineKeyboardButton(label, callback_data=callback)])
    if not favorites:
        text += "\n\nПока пусто. Добавляйте товары в избранное из карточки позиции."
    if favorites:
        keyboard.append(
            [
                InlineKeyboardButton("⭐ К избранному", callback_data="favorites")
                if manage
                else InlineKeyboardButton("🗑 Управление", callback_data="fav_manage")
            ]
        )
    keyboard.append([InlineKeyboardButton("← К списку", callback_data="home:0")])
    return Screen(text, InlineKeyboardMarkup(keyboard))


def templates_screen(templates: list[SavedTemplate], current: ShoppingList | None) -> Screen:
    text = (
        "📦 <b>Шаблоны закупок</b>\n\nСохраните обычную закупку один раз и добавляйте её целиком."
    )
    keyboard: list[list[InlineKeyboardButton]] = []
    for template in templates[:30]:
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"📋 {shorten(template.name, 42)} · {template.item_count} поз.",
                    callback_data=f"template_open:{template.id}",
                )
            ]
        )
    if not templates:
        text += "\n\nШаблонов пока нет."
    if current is not None:
        keyboard.append(
            [
                InlineKeyboardButton(
                    "💾 Сохранить текущий список",
                    callback_data=f"template_save:{current.id}:{current.revision}",
                )
            ]
        )
    keyboard.append([InlineKeyboardButton("← К списку", callback_data="home:0")])
    return Screen(text, InlineKeyboardMarkup(keyboard))


def template_screen(template: SavedTemplate, items: list[TemplateItem]) -> Screen:
    lines = [f"📋 <b>{html.escape(template.name)}</b>", f"Позиций: <b>{len(items)}</b>", ""]
    for number, item in enumerate(items[:20], start=1):
        lines.append(
            f"{number}. {html.escape(shorten(item.text, 35))} · {format_quantity(item.quantity_milli, item.unit)}"
        )
    if len(items) > 20:
        lines.append(f"…и ещё {len(items) - 20}")
    return Screen(
        "\n".join(lines),
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "➕ Добавить весь шаблон", callback_data=f"template_apply:{template.id}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🗑 Удалить шаблон", callback_data=f"template_delete_ask:{template.id}"
                    )
                ],
                [InlineKeyboardButton("← К шаблонам", callback_data="templates")],
            ]
        ),
    )


def settings_screen(user: UserDashboard) -> Screen:
    notify = {"all": "все изменения списка", "important": "только важные", "off": "выключены"}.get(
        user.notify_level, "все изменения списка"
    )
    reminder = {
        "off": "выключены",
        "daily18": "каждый день в 18:00",
        "fri18": "по пятницам в 18:00",
        "sat10": "по субботам в 10:00",
    }.get(user.reminder_rule, "выключены")
    return Screen(
        "⚙️ <b>Личные настройки</b>\n\n"
        f"🔔 Уведомления: <b>{notify}</b>\n"
        f"⏰ Напоминания: <b>{reminder}</b>\n\n"
        "Эти параметры действуют только для вас.",
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔔 Уведомления", callback_data="notify_settings")],
                [InlineKeyboardButton("⏰ Напоминания", callback_data="reminder_settings")],
                [InlineKeyboardButton("← К списку", callback_data="home:0")],
            ]
        ),
    )


def notification_settings_screen(level: str) -> Screen:
    options = (
        ("all", "Все изменения списка"),
        ("important", "Только закрытие и важное"),
        ("off", "Не присылать"),
    )
    rows = [
        [
            InlineKeyboardButton(
                ("✓ " if level == code else "") + label, callback_data=f"notify_set:{code}"
            )
        ]
        for code, label in options
    ]
    rows.append([InlineKeyboardButton("← К настройкам", callback_data="settings")])
    return Screen(
        "🔔 <b>Уведомления</b>\n\nСинхронизация общего списка продолжит работать при любом выборе.",
        InlineKeyboardMarkup(rows),
    )


def reminder_settings_screen(rule: str, timezone: ZoneInfo) -> Screen:
    options = (
        ("off", "Выключить"),
        ("daily18", "Каждый день · 18:00"),
        ("fri18", "Пятница · 18:00"),
        ("sat10", "Суббота · 10:00"),
    )
    rows = [
        [
            InlineKeyboardButton(
                ("✓ " if rule == code else "") + label, callback_data=f"reminder_set:{code}"
            )
        ]
        for code, label in options
    ]
    rows.append([InlineKeyboardButton("← К настройкам", callback_data="settings")])
    return Screen(
        f"⏰ <b>Напоминания</b>\n\nВремя указано в часовом поясе <code>{html.escape(str(timezone))}</code>.",
        InlineKeyboardMarkup(rows),
    )


def history_screen(
    lists: list[ShoppingList],
    counts: dict[int, tuple[int, int]],
    timezone: ZoneInfo,
    page: int = 0,
    total_count: int | None = None,
) -> Screen:
    total_count = len(lists) if total_count is None else total_count
    total_pages = max(1, math.ceil(total_count / HISTORY_PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    text = (
        "🕘 <b>История списков</b>\n\nВыберите список."
        if lists
        else "🕘 <b>История</b>\n\nЗакрытых списков пока нет."
    )
    keyboard: list[list[InlineKeyboardButton]] = []
    for shopping_list in lists:
        active, done = counts[shopping_list.id]
        date = _format_time(shopping_list.closed_at, timezone).split(" в ")[0]
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"📦 {shorten(shopping_list.title, 18)} · {date} · ✅ {done}/{active + done}",
                    callback_data=f"history_open:{shopping_list.id}:0:{page}",
                )
            ]
        )
    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️", callback_data=f"history_page:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
        if page + 1 < total_pages:
            nav.append(InlineKeyboardButton("▶️", callback_data=f"history_page:{page + 1}"))
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("← К списку", callback_data="home:0")])
    return Screen(text, InlineKeyboardMarkup(keyboard))


def archived_screen(
    shopping_list: ShoppingList,
    items: list[Item],
    timezone: ZoneInfo,
    page: int = 0,
    history_page: int = 0,
) -> Screen:
    sorted_items = ordered_items(items)
    visible, page, total_pages = _page(sorted_items, page, ARCHIVE_PAGE_SIZE)
    start = page * ARCHIVE_PAGE_SIZE
    lines = [
        f"📦 <b>{html.escape(shopping_list.title)}</b> · архив #{shopping_list.id}",
        f"Закрыт: {_format_time(shopping_list.closed_at, timezone)}",
    ]
    planned = sum(item.planned_price_minor or 0 for item in sorted_items)
    actual = sum(item.actual_price_minor or 0 for item in sorted_items)
    if shopping_list.budget_minor is not None or planned or actual:
        budget = (
            format_money(shopping_list.budget_minor)
            if shopping_list.budget_minor is not None
            else "не задан"
        )
        lines.append(
            f"💰 Бюджет: <b>{budget}</b> · План: <b>{format_money(planned)}</b> · Факт: <b>{format_money(actual)}</b>"
        )
    lines.append("")
    for number, item in enumerate(visible, start=start + 1):
        icon = "✅" if item.is_done else "⬜"
        lines.append(
            f"{number}. {icon} {html.escape(shorten(item.text, 40))} · {format_quantity(item.quantity_milli, item.unit)}"
        )
    if not visible:
        lines.append("Список был пуст.")
    if total_pages > 1:
        lines.extend(["", f"Страница {page + 1} из {total_pages}"])
    keyboard: list[list[InlineKeyboardButton]] = []
    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(
                InlineKeyboardButton(
                    "◀️", callback_data=f"history_open:{shopping_list.id}:{page - 1}:{history_page}"
                )
            )
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
        if page + 1 < total_pages:
            nav.append(
                InlineKeyboardButton(
                    "▶️", callback_data=f"history_open:{shopping_list.id}:{page + 1}:{history_page}"
                )
            )
        keyboard.append(nav)
    keyboard.extend(
        [
            [
                InlineKeyboardButton(
                    "↩️ Открыть снова",
                    callback_data=f"reopen:{shopping_list.id}:{shopping_list.revision}",
                )
            ],
            [InlineKeyboardButton("← К истории", callback_data=f"history_page:{history_page}")],
            [InlineKeyboardButton("🏠 Текущий список", callback_data="home:0")],
        ]
    )
    return Screen("\n".join(lines), InlineKeyboardMarkup(keyboard))
