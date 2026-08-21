from __future__ import annotations

import argparse
import asyncio
import html
import logging
import sys
from dataclasses import replace
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from telegram import (
    Bot,
    BotCommand,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from .config import Config
from .database import CATEGORY_LABELS, UNIT_LABELS, Database, Item
from .ui import (
    HISTORY_PAGE_SIZE,
    MAX_BATCH_SIZE,
    MAX_ITEM_LENGTH,
    Screen,
    archived_screen,
    category_screen,
    dashboard_screen,
    favorites_screen,
    format_money,
    history_screen,
    item_screen,
    manage_screen,
    notification_settings_screen,
    parse_line_items,
    reminder_settings_screen,
    settings_screen,
    template_screen,
    templates_screen,
    unit_screen,
)

LOGGER = logging.getLogger("shopping_list_bot")
EDIT_PROMPT_PREFIX = "✏️ Отправьте новое название для"
INPUT_PROMPT_PREFIX = "🧩 Ввод данных\n"


HELP_TEXT = """🛒 <b>Как пользоваться ботом</b>

Одна строка — один товар. Можно отправить сразу целый список: каждая новая строка станет отдельной позицией. Запятые внутри строки сохраняются.

На главном экране нажмите всю строку товара, чтобы отметить покупку. Название теперь занимает всю ширину. Изменение названия, количества, единицы, категории и цен находится в «🛠 Управление товарами».

Также доступны избранные товары, шаблоны закупок, отмена последнего действия, бюджет и личные напоминания.

Когда закупка закончена, нажмите «🔒 Закрыть список». Он сохранится в истории, а следующий товар создаст новый список.

Команды:
/list — открыть список
/add — добавить покупки
/done — открыть кнопки отметки покупок
/new — создать список
/close — закрыть список
/history — история
/settings — личные настройки
/cancel — отменить ввод
/help — эта справка"""


class ShoppingListBot:
    def __init__(self, config: Config, database: Database) -> None:
        self.config = config
        self.database = database

    async def access_guard(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        query = update.callback_query
        if user is None or user.id not in self.config.allowed_user_ids:
            if query:
                await query.answer("⛔ У вас нет доступа к этому боту.", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text("⛔ У вас нет доступа к этому боту.")
            raise ApplicationHandlerStop

        chat = update.effective_chat
        if chat is None or chat.type != ChatType.PRIVATE:
            if query:
                await query.answer("Используйте бота в личных сообщениях.", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text(
                    "🔐 Используйте бота в личных сообщениях."
                )
            raise ApplicationHandlerStop

        self.database.upsert_user(user.id, chat.id, user.full_name)

    async def post_init(self, application: Application) -> None:
        await application.bot.delete_webhook(drop_pending_updates=False)
        await application.bot.set_my_commands(
            [
                BotCommand("start", "Открыть общий список"),
                BotCommand("list", "Показать список"),
                BotCommand("add", "Добавить товар или список"),
                BotCommand("done", "Открыть отметку покупок"),
                BotCommand("new", "Создать новый список"),
                BotCommand("close", "Закрыть текущий список"),
                BotCommand("history", "История списков"),
                BotCommand("settings", "Личные настройки"),
                BotCommand("cancel", "Отменить ввод"),
                BotCommand("help", "Как пользоваться"),
            ]
        )
        await application.bot.set_my_description(
            "Общий список покупок: товары, количество, цены, избранное, шаблоны и напоминания."
        )
        await application.bot.set_my_short_description("Удобный общий список покупок 🛒")
        me = await application.bot.get_me()
        if application.job_queue is None:
            raise RuntimeError("PTB JobQueue is required for shopping reminders")
        application.job_queue.run_repeating(
            self._reminder_job,
            interval=60,
            first=10,
            name="shopping-reminder-dispatcher",
            job_kwargs={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": 30,
            },
        )
        LOGGER.info("Bot @%s (%s) started", me.username, me.id)

    async def post_shutdown(self, application: Application) -> None:
        self.database.close()
        LOGGER.info("Bot stopped cleanly")

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        if isinstance(context.error, TelegramError):
            LOGGER.warning("Telegram error while handling update: %s", context.error)
        else:
            error = context.error
            LOGGER.error(
                "Unhandled error while handling update",
                exc_info=(type(error), error, error.__traceback__) if error else None,
            )

    def _home_screen(self, page: int = 0) -> Screen:
        current = self.database.get_open_list()
        items = self.database.list_items(current.id) if current else []
        return dashboard_screen(
            current,
            items,
            self.config.timezone,
            page,
            undo=self.database.latest_undo(),
        )

    @staticmethod
    def _with_flash(screen: Screen, flash: str | None) -> Screen:
        if not flash:
            return screen
        compact = " ".join(flash.split())
        escaped = html.escape(compact[:47] + ("…" if len(compact) > 48 else ""))
        combined = f"{escaped}\n\n{screen.text}"
        if len(combined.encode("utf-16-le")) // 2 > 4096:
            return screen
        return replace(screen, text=combined)

    @staticmethod
    def _is_stale_input_prompt(text: str) -> bool:
        return text.startswith((INPUT_PROMPT_PREFIX, EDIT_PROMPT_PREFIX))

    async def _render(
        self,
        update: Update,
        screen: Screen,
        *,
        mark_dashboard: bool = False,
    ) -> None:
        query = update.callback_query
        message = update.effective_message
        user = update.effective_user
        message_id: int | None = None

        if query and query.message:
            try:
                await query.edit_message_text(
                    screen.text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=screen.markup,
                    disable_web_page_preview=True,
                )
                message_id = query.message.message_id
            except (BadRequest, TypeError) as exc:
                error_text = str(exc).lower()
                if "message is not modified" in error_text:
                    message_id = query.message.message_id
                elif isinstance(exc, TypeError) or any(
                    phrase in error_text
                    for phrase in (
                        "message to edit not found",
                        "message can't be edited",
                        "message can not be edited",
                    )
                ):
                    LOGGER.info("Could not edit message; sending a new screen: %s", exc)
                    sent = await update.get_bot().send_message(
                        chat_id=update.effective_chat.id,
                        text=screen.text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=screen.markup,
                        disable_web_page_preview=True,
                    )
                    message_id = sent.message_id
                else:
                    LOGGER.warning("Telegram rejected a generated screen: %s", exc)
                    await update.get_bot().send_message(
                        chat_id=update.effective_chat.id,
                        text="Не удалось открыть этот экран. Вернитесь к /list.",
                    )
        elif message:
            sent = await message.reply_text(
                screen.text,
                parse_mode=ParseMode.HTML,
                reply_markup=screen.markup,
                disable_web_page_preview=True,
            )
            message_id = sent.message_id

        if mark_dashboard and user:
            self.database.set_dashboard(user.id, message_id)

    async def _show_home(
        self,
        update: Update,
        *,
        page: int = 0,
        flash: str | None = None,
    ) -> None:
        await self._render(
            update,
            self._with_flash(self._home_screen(page), flash),
            mark_dashboard=True,
        )

    async def _sync_dashboards(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        exclude_user_id: int | None = None,
    ) -> None:
        screen = self._home_screen(0)
        for dashboard in self.database.dashboards():
            if dashboard.dashboard_message_id is None or dashboard.user_id == exclude_user_id:
                continue
            try:
                await context.bot.edit_message_text(
                    chat_id=dashboard.chat_id,
                    message_id=dashboard.dashboard_message_id,
                    text=screen.text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=screen.markup,
                    disable_web_page_preview=True,
                )
            except BadRequest as exc:
                if "message is not modified" not in str(exc).lower():
                    LOGGER.info(
                        "Dashboard for user %s cannot be edited: %s",
                        dashboard.user_id,
                        exc,
                    )
                    try:
                        sent = await context.bot.send_message(
                            chat_id=dashboard.chat_id,
                            text=screen.text,
                            parse_mode=ParseMode.HTML,
                            reply_markup=screen.markup,
                            disable_web_page_preview=True,
                        )
                        self.database.set_dashboard(dashboard.user_id, sent.message_id)
                    except TelegramError as send_exc:
                        self.database.set_dashboard(dashboard.user_id, None)
                        LOGGER.warning(
                            "Could not replace dashboard for user %s: %s",
                            dashboard.user_id,
                            send_exc,
                        )
            except Forbidden:
                self.database.set_dashboard(dashboard.user_id, None)
                LOGGER.info("User %s blocked the bot", dashboard.user_id)
            except TelegramError as exc:
                LOGGER.warning(
                    "Could not sync dashboard for user %s: %s",
                    dashboard.user_id,
                    exc,
                )

    async def _notify_others(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        actor_id: int,
        text: str,
        *,
        important: bool = False,
    ) -> None:
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🛒 Открыть список", callback_data="home:0")]]
        )
        for dashboard in self.database.dashboards():
            if dashboard.user_id == actor_id or dashboard.notify_level == "off":
                continue
            if dashboard.notify_level == "important" and not important:
                continue
            try:
                await context.bot.send_message(
                    dashboard.chat_id,
                    text,
                    reply_markup=markup,
                )
            except Forbidden:
                self.database.set_dashboard(dashboard.user_id, None)
            except TelegramError as exc:
                LOGGER.warning("Could not notify user %s: %s", dashboard.user_id, exc)

    async def _reminder_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        now = datetime.now(self.config.timezone)
        for user in self.database.dashboards():
            if user.user_id not in self.config.allowed_user_ids:
                continue
            rule = user.reminder_rule
            due = (
                (rule == "daily18" and now.hour == 18 and now.minute < 15)
                or (rule == "fri18" and now.weekday() == 4 and now.hour == 18 and now.minute < 15)
                or (rule == "sat10" and now.weekday() == 5 and now.hour == 10 and now.minute < 15)
            )
            if not due:
                continue
            key = f"{rule}:{now.date().isoformat()}"
            if self.database.reminder_delivered(user.user_id, key):
                continue
            current = self.database.get_open_list()
            if current:
                active, done = self.database.item_counts(current.id)
                planned, _ = self.database.budget_totals(current.id)
                text = (
                    "⏰ Пора свериться со списком покупок.\n\n"
                    f"Осталось купить: {active} · Куплено: {done}"
                )
                if planned:
                    text += f"\nПлановая сумма: {format_money(planned)}"
            else:
                text = "⏰ Пора составить список покупок на ближайшую закупку."
            try:
                await context.bot.send_message(
                    chat_id=user.chat_id,
                    text=text,
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton("🛒 Открыть список", callback_data="home:0"),
                                InlineKeyboardButton("➕ Добавить", callback_data="add"),
                            ]
                        ]
                    ),
                )
                self.database.mark_reminder_delivered(user.user_id, key)
            except Forbidden:
                self.database.set_dashboard(user.user_id, None)
                self.database.mark_reminder_delivered(user.user_id, key)
            except TelegramError as exc:
                LOGGER.warning("Could not deliver reminder to user %s: %s", user.user_id, exc)

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.user_data.pop("mode", None)
        await self._show_home(update)

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.user_data.pop("mode", None)
        assert update.effective_message
        await update.effective_message.reply_text(
            HELP_TEXT,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🛒 Открыть список", callback_data="home:0")]]
            ),
        )

    async def cancel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        mode = context.user_data.pop("mode", None)
        flash = "Ввод отменён." if mode else "Сейчас нет незавершённого действия."
        await self._show_home(update, flash=flash)

    async def new_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.user_data.pop("mode", None)
        current = self.database.get_open_list()
        if current:
            await self._show_home(update, flash="Текущий список уже открыт.")
            return
        title = " ".join(context.args).strip()[:80] if context.args else None
        self.database.create_list(update.effective_user.id, title)
        await self._show_home(update, flash="Новый список создан.")
        await self._sync_dashboards(context, exclude_user_id=update.effective_user.id)

    async def _prompt_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.user_data["mode"] = {"kind": "add"}
        message = update.effective_message
        assert message
        await message.reply_text(
            "➕ <b>Добавление покупок</b>\n\n"
            "Отправьте один товар или целый список. Каждая новая строка станет "
            "отдельной позицией.\n\n"
            "Например:\n<code>Молоко 2 л\nХлеб\nСыр, 300 г</code>\n\n"
            "Для выхода используйте /cancel или кнопку «Готово».",
            parse_mode=ParseMode.HTML,
            reply_markup=ForceReply(
                selective=True,
                input_field_placeholder="Что нужно купить?",
            ),
        )

    async def add_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.user_data.pop("mode", None)
        if context.args:
            await self._add_from_text(update, context, " ".join(context.args))
        else:
            await self._prompt_add(update, context)

    async def done_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.user_data.pop("mode", None)
        await self._show_home(update, flash="Нажмите ☑️ у нужной позиции — так номер не устареет.")

    async def close_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.user_data.pop("mode", None)
        await self._show_close_confirmation(update)

    async def history_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.user_data.pop("mode", None)
        await self._show_history(update)

    async def settings_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.user_data.pop("mode", None)
        await self._show_settings(update)

    async def text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        assert update.effective_message and update.effective_message.text
        mode = context.user_data.get("mode") or {}
        prompt_modes = {
            "edit",
            "quantity",
            "planned_price",
            "actual_price",
            "budget",
            "template_name",
        }
        if mode.get("kind") in prompt_modes:
            reply = update.effective_message.reply_to_message
            if reply and reply.message_id == mode.get("prompt_message_id"):
                if mode.get("kind") == "edit":
                    await self._edit_from_text(update, context, mode)
                elif mode.get("kind") == "quantity":
                    await self._quantity_from_text(update, context, mode)
                elif mode.get("kind") in {"planned_price", "actual_price"}:
                    await self._price_from_text(update, context, mode)
                elif mode.get("kind") == "budget":
                    await self._budget_from_text(update, context, mode)
                else:
                    await self._template_name_from_text(update, context, mode)
            else:
                await update.effective_message.reply_text(
                    "Чтобы не изменить не то поле, ответьте именно на сообщение "
                    "с запросом или нажмите /cancel."
                )
        else:
            reply = update.effective_message.reply_to_message
            if (
                reply
                and reply.from_user
                and reply.from_user.is_bot
                and self._is_stale_input_prompt(reply.text or "")
            ):
                await update.effective_message.reply_text(
                    "Этот запрос уже устарел. Откройте «🛠 Управление товарами» "
                    "и выберите позицию ещё раз."
                )
            else:
                await self._add_from_text(update, context, update.effective_message.text)

    async def _add_from_text(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        text: str,
    ) -> None:
        item_texts = parse_line_items(text)
        if not item_texts:
            assert update.effective_message
            await update.effective_message.reply_text("Напишите хотя бы один товар.")
            return
        if len(item_texts) > MAX_BATCH_SIZE:
            assert update.effective_message
            await update.effective_message.reply_text(
                f"За один раз можно добавить до {MAX_BATCH_SIZE} позиций. "
                "Разделите список на несколько сообщений."
            )
            return
        too_long = next(
            (item_text for item_text in item_texts if len(item_text) > MAX_ITEM_LENGTH),
            None,
        )
        if too_long:
            assert update.effective_message
            await update.effective_message.reply_text(
                f"Название позиции должно быть не длиннее {MAX_ITEM_LENGTH} символов. "
                f"Сократите: «{html.escape(too_long[:45])}…»",
                parse_mode=ParseMode.HTML,
            )
            return

        continuous_mode = (context.user_data.get("mode") or {}).get("kind") == "add"
        result = self.database.add_items_with_undo(update.effective_user.id, item_texts)
        added = result.items
        if len(added) == 1:
            action_word = "Количество увеличено" if result.merged_count else "Добавлено"
            confirmation = f"✅ {action_word}: <b>{html.escape(added[0].text)}</b>"
            flash = f"✅ {action_word}: {added[0].text}"
            notification = (
                f"👤 {update.effective_user.full_name}: {action_word.lower()} — {added[0].text}"
            )
        else:
            details = f"Новых: {result.created_count}"
            if result.merged_count:
                details += f" · Объединено повторов: {result.merged_count}"
            confirmation = f"✅ Обработано строк: <b>{len(item_texts)}</b>\n{details}"
            flash = f"✅ Обработано строк: {len(item_texts)}. {details}"
            notification = (
                f"👤 {update.effective_user.full_name} обработал(а) {len(item_texts)} позиций"
            )
        if continuous_mode:
            assert update.effective_message
            await self._sync_dashboards(context)
            await update.effective_message.reply_text(
                f"{confirmation}\n\n"
                "Можно отправить ещё товар или список. Для завершения нажмите «Готово».",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("✅ Готово — открыть список", callback_data="home:0")]]
                ),
            )
        else:
            await self._show_home(update, flash=flash)
            await self._sync_dashboards(context, exclude_user_id=update.effective_user.id)
        await self._notify_others(context, update.effective_user.id, notification)

    async def _edit_from_text(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        mode: dict[str, object],
    ) -> None:
        assert update.effective_message and update.effective_message.text
        text = " ".join(update.effective_message.text.split())
        if not text:
            await update.effective_message.reply_text("Название не может быть пустым.")
            return
        if len(text) > MAX_ITEM_LENGTH:
            await update.effective_message.reply_text(
                f"Сократите название до {MAX_ITEM_LENGTH} символов."
            )
            return
        item_id = int(mode["item_id"])
        updated = self.database.edit_item(
            item_id,
            text,
            expected_revision=int(mode["list_revision"]),
            changed_by=update.effective_user.id,
        )
        context.user_data.pop("mode", None)
        flash = (
            "✏️ Позиция изменена."
            if updated
            else "Список уже изменился. Откройте позицию и повторите действие."
        )
        await self._show_home(update, flash=flash)
        await self._sync_dashboards(context, exclude_user_id=update.effective_user.id)
        if updated:
            await self._notify_others(
                context,
                update.effective_user.id,
                f"✏️ {update.effective_user.full_name} изменил(а): {updated.text}",
            )

    @staticmethod
    def _parse_quantity(value: str) -> int | None:
        cleaned = value.strip().lower().replace("шт.", "").replace("шт", "").replace(",", ".")
        try:
            number = Decimal(cleaned.strip())
        except InvalidOperation:
            return None
        if number <= 0 or number > 999_999:
            return None
        return int((number * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    @staticmethod
    def _parse_money(value: str) -> int | None | object:
        cleaned = value.strip().lower()
        if cleaned in {"нет", "убрать", "очистить", "—", "-"}:
            return ...
        cleaned = (
            cleaned.replace("₽", "").replace("рублей", "").replace("руб.", "").replace("руб", "")
        )
        cleaned = cleaned.replace(" ", "").replace(",", ".")
        try:
            number = Decimal(cleaned)
        except InvalidOperation:
            return None
        if number < 0 or number > 10_000_000:
            return None
        return int((number * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    async def _quantity_from_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, mode: dict[str, object]
    ) -> None:
        assert update.effective_message and update.effective_message.text
        quantity = self._parse_quantity(update.effective_message.text)
        if quantity is None:
            await update.effective_message.reply_text(
                "Введите положительное число, например <code>2</code> или <code>0,5</code>.",
                parse_mode=ParseMode.HTML,
            )
            return
        updated = self.database.update_item_details(
            int(mode["item_id"]),
            update.effective_user.id,
            int(mode["list_revision"]),
            quantity_milli=quantity,
        )
        context.user_data.pop("mode", None)
        flash = (
            "📏 Количество обновлено." if updated else "Список уже изменился. Повторите действие."
        )
        await self._show_home(update, flash=flash)
        await self._sync_dashboards(context, exclude_user_id=update.effective_user.id)
        if updated:
            await self._notify_others(
                context,
                update.effective_user.id,
                f"📏 {update.effective_user.full_name} изменил(а) количество: {updated.text}",
            )

    async def _price_from_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, mode: dict[str, object]
    ) -> None:
        assert update.effective_message and update.effective_message.text
        parsed = self._parse_money(update.effective_message.text)
        if parsed is None:
            await update.effective_message.reply_text(
                "Введите сумму в рублях, например <code>249,90</code>. Чтобы убрать цену, напишите <code>нет</code>.",
                parse_mode=ParseMode.HTML,
            )
            return
        price = None if parsed is ... else int(parsed)
        kwargs = (
            {"planned_price_minor": price}
            if mode["kind"] == "planned_price"
            else {"actual_price_minor": price}
        )
        updated = self.database.update_item_details(
            int(mode["item_id"]),
            update.effective_user.id,
            int(mode["list_revision"]),
            **kwargs,
        )
        context.user_data.pop("mode", None)
        flash = "💰 Цена обновлена." if updated else "Список уже изменился. Повторите действие."
        await self._show_home(update, flash=flash)
        await self._sync_dashboards(context, exclude_user_id=update.effective_user.id)
        if updated:
            await self._notify_others(
                context,
                update.effective_user.id,
                f"💰 {update.effective_user.full_name} обновил(а) цену: {updated.text}",
            )

    async def _budget_from_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, mode: dict[str, object]
    ) -> None:
        assert update.effective_message and update.effective_message.text
        parsed = self._parse_money(update.effective_message.text)
        if parsed is None:
            await update.effective_message.reply_text(
                "Введите бюджет в рублях, например <code>5000</code>, или напишите <code>нет</code>.",
                parse_mode=ParseMode.HTML,
            )
            return
        budget = None if parsed is ... else int(parsed)
        updated = self.database.set_budget(
            int(mode["list_id"]),
            int(mode["list_revision"]),
            budget,
            update.effective_user.id,
        )
        context.user_data.pop("mode", None)
        flash = "💰 Бюджет обновлён." if updated else "Список уже изменился. Повторите действие."
        await self._show_home(update, flash=flash)
        await self._sync_dashboards(context, exclude_user_id=update.effective_user.id)
        if updated:
            await self._notify_others(
                context,
                update.effective_user.id,
                f"💰 {update.effective_user.full_name} обновил(а) бюджет закупки",
            )

    async def _template_name_from_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, mode: dict[str, object]
    ) -> None:
        assert update.effective_message and update.effective_message.text
        name = " ".join(update.effective_message.text.split())
        if not name or len(name) > 60:
            await update.effective_message.reply_text(
                "Название должно содержать от 1 до 60 символов."
            )
            return
        saved = self.database.save_template(
            name,
            update.effective_user.id,
            int(mode["list_id"]),
            int(mode["list_revision"]),
        )
        context.user_data.pop("mode", None)
        if saved is None:
            await self._show_home(update, flash="Список уже изменился. Сохраните шаблон ещё раз.")
        else:
            await self._render(
                update,
                self._with_flash(
                    templates_screen(self.database.templates(), self.database.get_open_list()),
                    f"📦 Шаблон «{saved.name}» сохранён.",
                ),
            )

    async def callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        assert query and query.data
        data = query.data
        await query.answer()
        context.user_data.pop("mode", None)

        if data == "noop":
            return
        if data.startswith("home:"):
            await self._show_home(update, page=self._safe_int(data.split(":", 1)[1]))
            return
        if data == "new":
            if self.database.get_open_list():
                await self._show_home(update, flash="Текущий список уже открыт.")
            else:
                self.database.create_list(update.effective_user.id)
                await self._show_home(update, flash="Новый список создан.")
                await self._sync_dashboards(context, exclude_user_id=update.effective_user.id)
            return
        if data == "add":
            await self._prompt_add(update, context)
            return
        if data.startswith("manage:"):
            await self._show_manage(update, self._safe_int(data.split(":", 1)[1]))
            return
        if data.startswith("state:"):
            await self._state_callback(update, context, data)
            return
        if data.startswith("item:"):
            await self._item_callback(update, data)
            return
        if data.startswith("edit:"):
            await self._edit_prompt_callback(update, context, data)
            return
        if data.startswith("qty:"):
            await self._quantity_callback(update, context, data)
            return
        if data.startswith("qty_input:"):
            await self._detail_prompt_callback(update, context, data, "quantity")
            return
        if data.startswith("unit_menu:"):
            await self._unit_menu_callback(update, data)
            return
        if data.startswith("unit_set:"):
            await self._unit_set_callback(update, context, data)
            return
        if data.startswith("category_menu:"):
            await self._category_menu_callback(update, data)
            return
        if data.startswith("category_set:"):
            await self._category_set_callback(update, context, data)
            return
        if data.startswith("price_plan:"):
            await self._detail_prompt_callback(update, context, data, "planned_price")
            return
        if data.startswith("price_actual:"):
            await self._detail_prompt_callback(update, context, data, "actual_price")
            return
        if data.startswith("fav_save:"):
            await self._favorite_save_callback(update, data)
            return
        if data == "favorites":
            await self._render(update, favorites_screen(self.database.favorites()))
            return
        if data == "fav_manage":
            await self._render(update, favorites_screen(self.database.favorites(), manage=True))
            return
        if data.startswith("fav_add:"):
            await self._favorite_add_callback(update, context, data)
            return
        if data.startswith("fav_delete_ask:"):
            favorite_id = self._safe_int(data.rsplit(":", 1)[1])
            favorite = next(
                (item for item in self.database.favorites() if item.id == favorite_id), None
            )
            if favorite is None:
                await self._render(update, favorites_screen(self.database.favorites(), manage=True))
            else:
                await self._render(
                    update,
                    Screen(
                        f"🗑 <b>Удалить из общего избранного?</b>\n\n{html.escape(favorite.text)}",
                        InlineKeyboardMarkup(
                            [
                                [
                                    InlineKeyboardButton(
                                        "Да, удалить", callback_data=f"fav_delete_yes:{favorite_id}"
                                    ),
                                    InlineKeyboardButton("Отмена", callback_data="fav_manage"),
                                ]
                            ]
                        ),
                    ),
                )
            return
        if data.startswith("fav_delete_yes:"):
            favorite_id = self._safe_int(data.split(":", 1)[1])
            deleted = self.database.delete_favorite(favorite_id)
            screen = favorites_screen(self.database.favorites(), manage=True)
            await self._render(
                update,
                self._with_flash(
                    screen, "Удалено из избранного." if deleted else "Позиция уже удалена."
                ),
            )
            return
        if data == "templates":
            await self._show_templates(update)
            return
        if data.startswith("template_open:"):
            await self._show_template(update, self._safe_int(data.split(":", 1)[1]))
            return
        if data.startswith("template_apply:"):
            await self._template_apply_callback(update, context, data)
            return
        if data.startswith("template_save:"):
            await self._template_save_prompt(update, context, data)
            return
        if data.startswith("template_delete_ask:"):
            template_id = self._safe_int(data.rsplit(":", 1)[1])
            template = self.database.get_template(template_id)
            if template is None:
                await self._show_templates(update)
            else:
                await self._render(
                    update,
                    Screen(
                        f"🗑 <b>Удалить общий шаблон?</b>\n\n{html.escape(template.name)}",
                        InlineKeyboardMarkup(
                            [
                                [
                                    InlineKeyboardButton(
                                        "Да, удалить",
                                        callback_data=f"template_delete_yes:{template_id}",
                                    ),
                                    InlineKeyboardButton(
                                        "Отмена", callback_data=f"template_open:{template_id}"
                                    ),
                                ]
                            ]
                        ),
                    ),
                )
            return
        if data.startswith("template_delete_yes:"):
            template_id = self._safe_int(data.split(":", 1)[1])
            deleted = self.database.delete_template(template_id)
            await self._render(
                update,
                self._with_flash(
                    templates_screen(self.database.templates(), self.database.get_open_list()),
                    "Шаблон удалён." if deleted else "Шаблон уже удалён.",
                ),
            )
            return
        if data == "budget":
            await self._budget_prompt(update, context)
            return
        if data == "settings":
            await self._show_settings(update)
            return
        if data == "notify_settings":
            user = self.database.get_user(update.effective_user.id)
            if user:
                await self._render(update, notification_settings_screen(user.notify_level))
            return
        if data.startswith("notify_set:"):
            level = data.split(":", 1)[1]
            self.database.set_notify_level(update.effective_user.id, level)
            await self._show_settings(update, flash="Настройки уведомлений сохранены.")
            return
        if data == "reminder_settings":
            user = self.database.get_user(update.effective_user.id)
            if user:
                await self._render(
                    update, reminder_settings_screen(user.reminder_rule, self.config.timezone)
                )
            return
        if data.startswith("reminder_set:"):
            rule = data.split(":", 1)[1]
            self.database.set_reminder_rule(update.effective_user.id, rule)
            await self._show_settings(update, flash="Настройки напоминаний сохранены.")
            return
        if data.startswith("undo:"):
            action_id, revision = self._list_revision_payload(data)
            undone = self.database.undo_action(action_id, update.effective_user.id, revision)
            await self._show_home(
                update,
                flash="↩️ Последнее действие отменено."
                if undone
                else "Отмена уже недоступна: список успел измениться.",
            )
            if undone:
                await self._sync_dashboards(context, exclude_user_id=update.effective_user.id)
                await self._notify_others(
                    context,
                    update.effective_user.id,
                    f"↩️ {update.effective_user.full_name} отменил(а) последнее действие",
                )
            return
        if data.startswith("delete_ask:"):
            await self._delete_ask_callback(update, data)
            return
        if data.startswith("delete_yes:"):
            await self._delete_yes_callback(update, context, data)
            return
        if data == "clear_ask":
            await self._show_clear_confirmation(update)
            return
        if data.startswith("clear_yes:"):
            list_id, revision = self._list_revision_payload(data)
            count = self.database.clear_done(list_id, revision, update.effective_user.id)
            if count is None:
                flash = "Список уже изменился. Откройте очистку ещё раз."
            else:
                flash = f"🧹 Удалено купленных позиций: {count}"
            await self._show_home(update, flash=flash)
            await self._sync_dashboards(context, exclude_user_id=update.effective_user.id)
            if count:
                await self._notify_others(
                    context,
                    update.effective_user.id,
                    f"🧹 {update.effective_user.full_name} убрал(а) купленные позиции: {count}",
                )
            return
        if data == "close_ask":
            await self._show_close_confirmation(update)
            return
        if data.startswith("close_yes:"):
            list_id, revision = self._list_revision_payload(data)
            closed = self.database.close_open_list(update.effective_user.id, list_id, revision)
            if closed:
                await self._show_home(update, flash="🔒 Список закрыт и сохранён в истории.")
                await self._sync_dashboards(context, exclude_user_id=update.effective_user.id)
                await self._notify_others(
                    context,
                    update.effective_user.id,
                    f"🔒 {update.effective_user.full_name} закрыл(а) список покупок",
                    important=True,
                )
            else:
                await self._show_home(
                    update, flash="Список уже изменился. Подтвердите закрытие ещё раз."
                )
            return
        if data == "history":
            await self._show_history(update)
            return
        if data.startswith("history_page:"):
            await self._show_history(update, self._safe_int(data.split(":", 1)[1]))
            return
        if data.startswith("history_open:"):
            list_id, archive_page, history_page = self._archive_payload(data)
            await self._show_archived(
                update, list_id, archive_page=archive_page, history_page=history_page
            )
            return
        if data.startswith("reopen:"):
            await self._reopen_callback(update, context, data)
            return
        if data == "cancel":
            context.user_data.pop("mode", None)
            await self._show_home(update, flash="Действие отменено.")
            return

        LOGGER.info("Ignored unknown callback payload: %s", data)
        await self._show_home(update)

    @staticmethod
    def _safe_int(value: str, default: int = 0) -> int:
        try:
            return max(0, int(value))
        except ValueError:
            return default

    @staticmethod
    def _item_and_page(data: str) -> tuple[int, int]:
        parts = data.split(":")
        if len(parts) < 2:
            return 0, 0
        try:
            return int(parts[1]), max(0, int(parts[2])) if len(parts) > 2 else 0
        except ValueError:
            return 0, 0

    @staticmethod
    def _item_page_revision(data: str) -> tuple[int, int, int]:
        parts = data.split(":")
        try:
            return int(parts[1]), max(0, int(parts[2])), max(0, int(parts[3]))
        except (IndexError, ValueError):
            return 0, 0, 0

    @staticmethod
    def _list_revision_payload(data: str) -> tuple[int, int]:
        parts = data.split(":")
        try:
            return int(parts[1]), max(0, int(parts[2]))
        except (IndexError, ValueError):
            return 0, 0

    @staticmethod
    def _state_payload(data: str) -> tuple[int, bool, int]:
        parts = data.split(":")
        try:
            raw_state = int(parts[2])
            if raw_state not in (0, 1):
                raise ValueError
            return int(parts[1]), bool(raw_state), max(0, int(parts[3]))
        except (IndexError, ValueError):
            return 0, False, 0

    @staticmethod
    def _archive_payload(data: str) -> tuple[int, int, int]:
        parts = data.split(":")
        try:
            return int(parts[1]), max(0, int(parts[2])), max(0, int(parts[3]))
        except (IndexError, ValueError):
            return 0, 0, 0

    async def _show_manage(self, update: Update, page: int = 0) -> None:
        current = self.database.get_open_list()
        items = self.database.list_items(current.id) if current else []
        await self._render(update, manage_screen(current, items, page))

    async def _show_settings(self, update: Update, flash: str | None = None) -> None:
        user = self.database.get_user(update.effective_user.id)
        if user is None:
            await self._show_home(update)
            return
        await self._render(update, self._with_flash(settings_screen(user), flash))

    async def _show_templates(self, update: Update) -> None:
        await self._render(
            update,
            templates_screen(self.database.templates(), self.database.get_open_list()),
        )

    async def _show_template(self, update: Update, template_id: int) -> None:
        template = self.database.get_template(template_id)
        if template is None:
            await self._render(
                update,
                self._with_flash(
                    templates_screen(self.database.templates(), self.database.get_open_list()),
                    "Шаблон уже удалён.",
                ),
            )
            return
        await self._render(
            update, template_screen(template, self.database.template_items(template_id))
        )

    def _valid_item_revision(
        self, item_id: int, revision: int
    ) -> tuple[Item | None, object | None]:
        item = self.database.get_item(item_id)
        current = self.database.get_open_list()
        if (
            item is None
            or current is None
            or item.list_id != current.id
            or current.revision != revision
        ):
            return None, current
        return item, current

    async def _quantity_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, data: str
    ) -> None:
        parts = data.split(":")
        try:
            item_id, direction, page, revision = (
                int(parts[1]),
                parts[2],
                max(0, int(parts[3])),
                int(parts[4]),
            )
        except (IndexError, ValueError):
            await self._show_home(update)
            return
        item, _ = self._valid_item_revision(item_id, revision)
        if item is None or direction not in {"m", "p"}:
            await self._show_home(update, flash="Список уже изменился. Откройте товар снова.")
            return
        step = 500 if item.unit in {"kg", "l"} else 1000
        quantity = item.quantity_milli + (step if direction == "p" else -step)
        if quantity < 1:
            await self._item_callback(update, f"item:{item_id}:{page}")
            return
        updated = self.database.update_item_details(
            item_id,
            update.effective_user.id,
            revision,
            quantity_milli=quantity,
        )
        current = self.database.get_open_list()
        if updated and current:
            await self._render(
                update,
                self._with_flash(
                    item_screen(
                        updated,
                        self.database.user_names(),
                        self.config.timezone,
                        page,
                        current.revision,
                    ),
                    "Количество обновлено.",
                ),
            )
            await self._sync_dashboards(context, exclude_user_id=update.effective_user.id)
            await self._notify_others(
                context,
                update.effective_user.id,
                f"📏 {update.effective_user.full_name} изменил(а) количество: {updated.text}",
            )
        else:
            await self._show_home(update, flash="Список уже изменился. Повторите действие.")

    async def _detail_prompt_callback(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        data: str,
        kind: str,
    ) -> None:
        item_id, page, revision = self._item_page_revision(data)
        item, _ = self._valid_item_revision(item_id, revision)
        if item is None:
            await self._show_home(update, flash="Список уже изменился. Откройте товар снова.")
            return
        prompts = {
            "quantity": (
                f"📏 Отправьте точное количество для «{html.escape(item.text)}».\nНапример: <code>2</code> или <code>0,5</code>.",
                "Например: 2 или 0,5",
            ),
            "planned_price": (
                f"💰 Отправьте плановую цену всей позиции «{html.escape(item.text)}» в рублях.\nНапример: <code>249,90</code>. Чтобы убрать цену: <code>нет</code>.",
                "Плановая цена в рублях",
            ),
            "actual_price": (
                f"🧾 Отправьте фактическую цену всей позиции «{html.escape(item.text)}» в рублях.\nНапример: <code>239,50</code>. Чтобы убрать цену: <code>нет</code>.",
                "Цена по чеку",
            ),
        }
        prompt_text, placeholder = prompts[kind]
        assert update.effective_message
        prompt = await update.effective_message.reply_text(
            INPUT_PROMPT_PREFIX + prompt_text,
            parse_mode=ParseMode.HTML,
            reply_markup=ForceReply(selective=True, input_field_placeholder=placeholder),
        )
        context.user_data["mode"] = {
            "kind": kind,
            "item_id": item_id,
            "page": page,
            "list_revision": revision,
            "prompt_message_id": prompt.message_id,
        }

    async def _unit_menu_callback(self, update: Update, data: str) -> None:
        item_id, page, revision = self._item_page_revision(data)
        item, _ = self._valid_item_revision(item_id, revision)
        if item is None:
            await self._show_home(update, flash="Список уже изменился. Откройте товар снова.")
            return
        await self._render(update, unit_screen(item, page, revision))

    async def _unit_set_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, data: str
    ) -> None:
        parts = data.split(":")
        try:
            item_id, unit, page, revision = int(parts[1]), parts[2], int(parts[3]), int(parts[4])
        except (IndexError, ValueError):
            await self._show_home(update)
            return
        if unit not in UNIT_LABELS:
            await self._show_home(update)
            return
        updated = self.database.update_item_details(
            item_id, update.effective_user.id, revision, unit=unit
        )
        current = self.database.get_open_list()
        if updated and current:
            await self._render(
                update,
                self._with_flash(
                    item_screen(
                        updated,
                        self.database.user_names(),
                        self.config.timezone,
                        page,
                        current.revision,
                    ),
                    "Единица измерения обновлена.",
                ),
            )
            await self._sync_dashboards(context, exclude_user_id=update.effective_user.id)
            await self._notify_others(
                context,
                update.effective_user.id,
                f"📏 {update.effective_user.full_name} изменил(а) единицу: {updated.text}",
            )
        else:
            await self._show_home(update, flash="Список уже изменился. Повторите действие.")

    async def _category_menu_callback(self, update: Update, data: str) -> None:
        item_id, page, revision = self._item_page_revision(data)
        item, _ = self._valid_item_revision(item_id, revision)
        if item is None:
            await self._show_home(update, flash="Список уже изменился. Откройте товар снова.")
            return
        await self._render(update, category_screen(item, page, revision))

    async def _category_set_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, data: str
    ) -> None:
        parts = data.split(":")
        try:
            item_id, category, page, revision = (
                int(parts[1]),
                parts[2],
                int(parts[3]),
                int(parts[4]),
            )
        except (IndexError, ValueError):
            await self._show_home(update)
            return
        if category not in CATEGORY_LABELS:
            await self._show_home(update)
            return
        updated = self.database.update_item_details(
            item_id, update.effective_user.id, revision, category=category
        )
        current = self.database.get_open_list()
        if updated and current:
            await self._render(
                update,
                self._with_flash(
                    item_screen(
                        updated,
                        self.database.user_names(),
                        self.config.timezone,
                        page,
                        current.revision,
                    ),
                    "Категория обновлена.",
                ),
            )
            await self._sync_dashboards(context, exclude_user_id=update.effective_user.id)
            await self._notify_others(
                context,
                update.effective_user.id,
                f"🗂 {update.effective_user.full_name} изменил(а) категорию: {updated.text}",
            )
        else:
            await self._show_home(update, flash="Список уже изменился. Повторите действие.")

    async def _favorite_save_callback(self, update: Update, data: str) -> None:
        item_id, page, revision = self._item_page_revision(data)
        item, _ = self._valid_item_revision(item_id, revision)
        if item is None:
            await self._show_home(update, flash="Список уже изменился. Откройте товар снова.")
            return
        favorite = self.database.favorite_from_item(item_id, update.effective_user.id)
        current = self.database.get_open_list()
        if favorite and current:
            await self._render(
                update,
                self._with_flash(
                    item_screen(
                        item,
                        self.database.user_names(),
                        self.config.timezone,
                        page,
                        current.revision,
                    ),
                    "⭐ Товар сохранён в избранное.",
                ),
            )

    async def _favorite_add_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, data: str
    ) -> None:
        favorite_id = self._safe_int(data.split(":", 1)[1])
        result = self.database.add_favorite(favorite_id, update.effective_user.id)
        screen = favorites_screen(self.database.favorites())
        await self._render(
            update,
            self._with_flash(
                screen, "✅ Добавлено в список." if result else "Избранная позиция уже недоступна."
            ),
        )
        if result:
            await self._sync_dashboards(context, exclude_user_id=update.effective_user.id)
            await self._notify_others(
                context,
                update.effective_user.id,
                f"⭐ {update.effective_user.full_name} добавил(а) товар из избранного",
            )

    async def _template_apply_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, data: str
    ) -> None:
        template_id = self._safe_int(data.split(":", 1)[1])
        result = self.database.apply_template(template_id, update.effective_user.id)
        if result:
            await self._show_home(
                update, flash=f"📦 Из шаблона добавлено позиций: {len(result.items)}"
            )
            await self._sync_dashboards(context, exclude_user_id=update.effective_user.id)
            await self._notify_others(
                context,
                update.effective_user.id,
                f"👤 {update.effective_user.full_name} добавил(а) шаблон закупки ({len(result.items)} поз.)",
            )
        else:
            await self._show_templates(update)

    async def _template_save_prompt(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, data: str
    ) -> None:
        list_id, revision = self._list_revision_payload(data)
        current = self.database.get_open_list()
        if current is None or current.id != list_id or current.revision != revision:
            await self._show_home(update, flash="Список уже изменился. Откройте шаблоны снова.")
            return
        active, done = self.database.item_counts(current.id)
        if active + done == 0:
            await self._show_home(update, flash="Пустой список нельзя сохранить как шаблон.")
            return
        assert update.effective_message
        prompt = await update.effective_message.reply_text(
            INPUT_PROMPT_PREFIX
            + "📦 <b>Название шаблона</b>\n\nНапример: <code>Еженедельная закупка</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=ForceReply(selective=True, input_field_placeholder="Название шаблона"),
        )
        context.user_data["mode"] = {
            "kind": "template_name",
            "list_id": list_id,
            "list_revision": revision,
            "prompt_message_id": prompt.message_id,
        }

    async def _budget_prompt(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        current = self.database.get_open_list()
        if current is None:
            await self._show_home(update, flash="Сначала создайте список.")
            return
        planned, actual = self.database.budget_totals(current.id)
        assert update.effective_message
        prompt = await update.effective_message.reply_text(
            INPUT_PROMPT_PREFIX + "💰 <b>Бюджет закупки</b>\n\n"
            f"Сейчас: <b>{format_money(current.budget_minor)}</b>\n"
            f"План по товарам: <b>{format_money(planned)}</b>\n"
            f"Факт: <b>{format_money(actual)}</b>\n\n"
            "Отправьте сумму в рублях или <code>нет</code>, чтобы убрать бюджет.",
            parse_mode=ParseMode.HTML,
            reply_markup=ForceReply(selective=True, input_field_placeholder="Например: 5000"),
        )
        context.user_data["mode"] = {
            "kind": "budget",
            "list_id": current.id,
            "list_revision": current.revision,
            "prompt_message_id": prompt.message_id,
        }

    async def _state_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, data: str
    ) -> None:
        item_id, desired_state, page = self._state_payload(data)
        item, changed = self.database.set_item_done(
            item_id, desired_state, update.effective_user.id
        )
        if item is None:
            flash = "Позиция уже удалена или список закрыт."
        elif not changed:
            flash = "Состояние уже изменено — список обновлён."
        elif item.is_done:
            flash = f"✅ Куплено: {item.text}"
        else:
            flash = f"↩️ Возвращено: {item.text}"
        await self._show_home(update, page=page, flash=flash)
        await self._sync_dashboards(context, exclude_user_id=update.effective_user.id)
        if item is not None and changed:
            await self._notify_others(
                context,
                update.effective_user.id,
                f"{'✅' if item.is_done else '↩️'} {update.effective_user.full_name}: {item.text}",
            )

    async def _item_callback(self, update: Update, data: str) -> None:
        item_id, page = self._item_and_page(data)
        item = self.database.get_item(item_id)
        current = self.database.get_open_list()
        if item is None or current is None or item.list_id != current.id:
            await self._show_home(update, page=page, flash="Позиция уже недоступна.")
            return
        await self._render(
            update,
            item_screen(
                item,
                self.database.user_names(),
                self.config.timezone,
                page,
                current.revision,
            ),
        )

    async def _edit_prompt_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, data: str
    ) -> None:
        item_id, page, revision = self._item_page_revision(data)
        item = self.database.get_item(item_id)
        current = self.database.get_open_list()
        if (
            item is None
            or current is None
            or item.list_id != current.id
            or current.revision != revision
        ):
            await self._show_home(
                update, page=page, flash="Список уже изменился. Откройте позицию снова."
            )
            return
        assert update.effective_message
        prompt = await update.effective_message.reply_text(
            f"{INPUT_PROMPT_PREFIX}{EDIT_PROMPT_PREFIX} «{html.escape(item.text)}».",
            parse_mode=ParseMode.HTML,
            reply_markup=ForceReply(
                selective=True,
                input_field_placeholder="Новое название",
            ),
        )
        context.user_data["mode"] = {
            "kind": "edit",
            "item_id": item_id,
            "page": page,
            "list_revision": revision,
            "prompt_message_id": prompt.message_id,
        }

    async def _delete_ask_callback(self, update: Update, data: str) -> None:
        item_id, page, revision = self._item_page_revision(data)
        item = self.database.get_item(item_id)
        current = self.database.get_open_list()
        if (
            item is None
            or current is None
            or item.list_id != current.id
            or current.revision != revision
        ):
            await self._show_home(
                update, page=page, flash="Список уже изменился. Откройте позицию снова."
            )
            return
        screen = Screen(
            text=(
                "🗑 <b>Удалить позицию?</b>\n\n"
                f"{html.escape(item.text)}\n\nПосле удаления действие можно отменить кнопкой в списке."
            ),
            markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Да, удалить",
                            callback_data=f"delete_yes:{item_id}:{page}:{revision}",
                        ),
                        InlineKeyboardButton("Отмена", callback_data=f"item:{item_id}:{page}"),
                    ]
                ]
            ),
        )
        await self._render(update, screen)

    async def _delete_yes_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, data: str
    ) -> None:
        item_id, page, revision = self._item_page_revision(data)
        item = self.database.get_item(item_id)
        deleted = self.database.delete_item(item_id, revision, changed_by=update.effective_user.id)
        flash = (
            "🗑 Позиция удалена."
            if deleted
            else "Список уже изменился. Откройте позицию и повторите действие."
        )
        await self._show_home(update, page=page, flash=flash)
        await self._sync_dashboards(context, exclude_user_id=update.effective_user.id)
        if deleted and item:
            await self._notify_others(
                context,
                update.effective_user.id,
                f"🗑 {update.effective_user.full_name} удалил(а): {item.text}",
            )

    async def _show_clear_confirmation(self, update: Update) -> None:
        current = self.database.get_open_list()
        if current is None:
            await self._show_home(update, flash="Открытого списка нет.")
            return
        _, done = self.database.item_counts(current.id)
        if not done:
            await self._show_home(update, flash="Купленных позиций нет.")
            return
        screen = Screen(
            text=(
                f"🧹 <b>Удалить купленные позиции ({done})?</b>\n\n"
                "Они исчезнут из текущего списка. После очистки действие можно отменить."
            ),
            markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Да, убрать",
                            callback_data=f"clear_yes:{current.id}:{current.revision}",
                        ),
                        InlineKeyboardButton("Отмена", callback_data="home:0"),
                    ]
                ]
            ),
        )
        await self._render(update, screen)

    async def _show_close_confirmation(self, update: Update) -> None:
        current = self.database.get_open_list()
        if current is None:
            await self._show_home(update, flash="Открытого списка нет.")
            return
        active, done = self.database.item_counts(current.id)
        screen = Screen(
            text=(
                "🔒 <b>Закрыть текущий список?</b>\n\n"
                f"Осталось купить: <b>{active}</b>\n"
                f"Куплено: <b>{done}</b>\n\n"
                "Список сохранится в истории. Следующая добавленная покупка создаст новый."
            ),
            markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Да, закрыть",
                            callback_data=f"close_yes:{current.id}:{current.revision}",
                        ),
                        InlineKeyboardButton("Отмена", callback_data="home:0"),
                    ]
                ]
            ),
        )
        await self._render(update, screen)

    async def _show_history(self, update: Update, page: int = 0) -> None:
        total_count = self.database.closed_list_count()
        total_pages = max(1, (total_count + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))
        lists = self.database.closed_lists(HISTORY_PAGE_SIZE, offset=page * HISTORY_PAGE_SIZE)
        counts = {item.id: self.database.item_counts(item.id) for item in lists}
        await self._render(
            update,
            history_screen(
                lists,
                counts,
                self.config.timezone,
                page=page,
                total_count=total_count,
            ),
        )

    async def _show_archived(
        self,
        update: Update,
        list_id: int,
        *,
        archive_page: int = 0,
        history_page: int = 0,
    ) -> None:
        shopping_list = self.database.get_closed_list(list_id)
        if shopping_list is None:
            await self._show_history(update)
            return
        await self._render(
            update,
            archived_screen(
                shopping_list,
                self.database.list_items(shopping_list.id),
                self.config.timezone,
                page=archive_page,
                history_page=history_page,
            ),
        )

    async def _reopen_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, data: str
    ) -> None:
        list_id, revision = self._list_revision_payload(data)
        reopened = self.database.reopen_list(list_id, revision)
        if reopened:
            flash = "↩️ Список снова открыт."
            await self._sync_dashboards(context, exclude_user_id=update.effective_user.id)
            await self._notify_others(
                context,
                update.effective_user.id,
                f"↩️ {update.effective_user.full_name} снова открыл(а) список покупок",
                important=True,
            )
        elif self.database.get_open_list():
            flash = "Сначала закройте текущий список."
        else:
            flash = "Этот список уже недоступен."
        await self._show_home(update, flash=flash)

    async def unknown_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.user_data.pop("mode", None)
        assert update.effective_message
        await update.effective_message.reply_text(
            "Не знаю такую команду. Нажмите /help или откройте /list."
        )


def build_application(config: Config) -> Application:
    database = Database(config.database_path)
    bot = ShoppingListBot(config, database)
    application = (
        ApplicationBuilder()
        .token(config.token)
        .concurrent_updates(False)
        .post_init(bot.post_init)
        .post_shutdown(bot.post_shutdown)
        .build()
    )
    application.bot_data["service"] = bot

    application.add_handler(TypeHandler(Update, bot.access_guard), group=-1)
    application.add_handler(CommandHandler("start", bot.start_command))
    application.add_handler(CommandHandler("list", bot.start_command))
    application.add_handler(CommandHandler("help", bot.help_command))
    application.add_handler(CommandHandler("cancel", bot.cancel_command))
    application.add_handler(CommandHandler("new", bot.new_command))
    application.add_handler(CommandHandler("add", bot.add_command))
    application.add_handler(CommandHandler("done", bot.done_command))
    application.add_handler(CommandHandler("close", bot.close_command))
    application.add_handler(CommandHandler("history", bot.history_command))
    application.add_handler(CommandHandler("settings", bot.settings_command))
    application.add_handler(CallbackQueryHandler(bot.callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.text_message))
    application.add_handler(MessageHandler(filters.COMMAND, bot.unknown_command))
    application.add_error_handler(bot.error_handler)
    return application


async def check_bot(config: Config, prepare: bool = False) -> None:
    async with Bot(config.token) as bot:
        me = await bot.get_me()
        webhook = await bot.get_webhook_info()
        if prepare:
            await bot.delete_webhook(drop_pending_updates=True)
            webhook_state = "removed; pending updates cleared"
        else:
            webhook_state = webhook.url or "none"
        print(
            f"OK: @{me.username} (id={me.id}), webhook={webhook_state}, "
            f"allowed_users={len(config.allowed_user_ids)}"
        )


async def notify_ready(config: Config) -> int:
    delivered = 0
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🛒 Открыть список", callback_data="home:0")]]
    )
    async with Bot(config.token) as bot:
        for user_id in sorted(config.allowed_user_ids):
            try:
                await bot.send_message(
                    chat_id=user_id,
                    text=(
                        "✅ KupimVmeste запущен и готов к работе.\n\n"
                        "Общий список покупок доступен по кнопке ниже."
                    ),
                    reply_markup=markup,
                )
                print(f"READY_NOTIFICATION_OK user_id={user_id}")
                delivered += 1
            except Forbidden:
                print(
                    f"READY_NOTIFICATION_SKIPPED user_id={user_id} "
                    "reason=user_has_not_started_or_blocked_bot"
                )
            except TelegramError as exc:
                print(f"READY_NOTIFICATION_FAILED user_id={user_id} reason={type(exc).__name__}")
    return delivered


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="KupimVmeste Telegram shopping-list bot")
    parser.add_argument(
        "--check", action="store_true", help="Validate token and Telegram connectivity"
    )
    parser.add_argument(
        "--prepare",
        action="store_true",
        help="Validate token, remove webhook and clear old updates before first launch",
    )
    parser.add_argument(
        "--notify-ready",
        action="store_true",
        help="Send a one-time ready message to all allowed users",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = Config.from_env()
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.check or args.prepare:
        asyncio.run(check_bot(config, prepare=args.prepare))
        return 0
    if args.notify_ready:
        asyncio.run(notify_ready(config))
        return 0

    application = build_application(config)
    LOGGER.info("Database: %s", Path(config.database_path))
    application.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
