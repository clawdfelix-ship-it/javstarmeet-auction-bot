"""Telegram bot entry point - handles setup and command registration."""
import logging
import os
import asyncio
from typing import Optional
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
)

from config import BOT_TOKEN, ADMIN_IDS

logger = logging.getLogger(__name__)


class TelegramAdapter:
    """Telegram bot adapter - wires telegram handlers to auction engine."""

    def __init__(self, token: str, admin_ids: list[int], engine, store):
        self.token = token
        self.admin_ids = admin_ids
        self.engine = engine
        self.store = store
        self.app: Optional[Application] = None

    async def start(self):
        """Start polling."""
        self.app = (
            Application.builder()
            .token(self.token)
            .read_timeout(30)
            .write_timeout(30)
            .build()
        )
        self._register_handlers()
        await self._set_commands()
        logger.info("Telegram bot starting...")
        await self.app.run_polling(drop_pending_updates=True)

    def _register_handlers(self):
        """Register all command and message handlers."""
        app = self.app
        if not app:
            raise RuntimeError("App not initialized")

        # Register handlers using the current main.py functions
        # These are imported lazily to avoid circular imports
        import main as main_module

        # Conversation: user registration
        app.add_handler(ConversationHandler(
            entry_points=[CommandHandler("start", main_module.start_register)],
            states={
                main_module.NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, main_module.get_name)],
                main_module.PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, main_module.get_phone)],
                main_module.EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, main_module.get_email)],
                main_module.PICKUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, main_module.get_pickup)],
            },
            fallbacks=[CommandHandler("cancel", main_module.cancel_register)],
        ))

        # Conversation: admin new auction
        app.add_handler(ConversationHandler(
            entry_points=[CommandHandler("new", main_module.new_auction_start, filters.User(ADMIN_IDS))],
            states={
                main_module.WAITING_PHOTO: [MessageHandler(filters.PHOTO, main_module.get_auction_photo)],
                main_module.WAITING_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, main_module.get_auction_title)],
                main_module.WAITING_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, main_module.get_auction_price)],
                main_module.WAITING_BIN_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, main_module.get_bin_price)],
            },
            fallbacks=[],
        ))

        # Batch commands (admin only)
        app.add_handler(CommandHandler("import_batch", main_module.import_batch_command, filters.User(ADMIN_IDS)))
        app.add_handler(CommandHandler("schedule", main_module.schedule_command, filters.User(ADMIN_IDS)))
        app.add_handler(CommandHandler("start_batch", main_module.start_batch_command, filters.User(ADMIN_IDS)))
        app.add_handler(CommandHandler("pause_batch", main_module.pause_batch_command, filters.User(ADMIN_IDS)))
        app.add_handler(CommandHandler("resume_batch", main_module.resume_batch_command, filters.User(ADMIN_IDS)))
        app.add_handler(CommandHandler("abort_batch", main_module.abort_batch_command, filters.User(ADMIN_IDS)))
        app.add_handler(CommandHandler("batch_status", main_module.batch_status_command, filters.User(ADMIN_IDS)))

        # Admin panel
        app.add_handler(CommandHandler("admin", main_module.admin_menu, filters.User(ADMIN_IDS)))
        app.add_handler(CallbackQueryHandler(main_module.admin_callback, user=ADMIN_IDS))

        # Batch admin panel callbacks
        for pattern in main_module.BATCH_CALLBACK_PATTERNS:
            app.add_handler(CallbackQueryHandler(main_module.handle_batch_callback, pattern=pattern, user_filter=ADMIN_IDS))

        # User commands
        app.add_handler(CommandHandler("help", main_module.help_command))
        app.add_handler(CommandHandler("myorders", main_module.my_orders_command))
        app.add_handler(CommandHandler("userinfo", main_module.user_info_command))

        # Ban/unban (admin only)
        app.add_handler(CommandHandler("ban", main_module.ban_command, filters.User(ADMIN_IDS)))
        app.add_handler(CommandHandler("unban", main_module.unban_command, filters.User(ADMIN_IDS)))

        # Group commands
        app.add_handler(CommandHandler("set_prod_group", main_module.set_prod_group_command, filters.User(ADMIN_IDS)))
        app.add_handler(CommandHandler("set_test_group", main_module.set_test_group_command, filters.User(ADMIN_IDS)))

        # Data export (admin only)
        app.add_handler(CommandHandler("export_data", main_module.export_data, filters.User(ADMIN_IDS)))

        # Broadcast (admin only)
        app.add_handler(CommandHandler("broadcast", main_module.broadcast_command, filters.User(ADMIN_IDS)))

        # Member import (admin only)
        app.add_handler(CommandHandler("import_members", main_module.import_members_start, filters.User(ADMIN_IDS)))
        app.add_handler(CommandHandler("cancel_import", main_module.cancel_import_members, filters.User(ADMIN_IDS)))

        # Web server + webapp
        app.add_handler(MessageHandler(filters.UpdateType.WEB_APP_DATA, main_module.handle_webapp_bid))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, main_module.handle_text_bid))

        # Numpad + bid callbacks
        app.add_handler(CallbackQueryHandler(main_module.handle_numpad_click, pattern="numpad_"))
        app.add_handler(CallbackQueryHandler(main_module.handle_bin_callback, pattern="bin_"))
        app.add_handler(CallbackQueryHandler(main_module.handle_bid_button, pattern="bid_"))
        app.add_handler(CallbackQueryHandler(main_module.handle_admin_order_action, pattern="order_"))

        # All other texts in private chat
        app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.UpdateType.MESSAGE & filters.ChatType.PRIVATE,
            main_module.handle_private_bid_text
        ))

    async def _set_commands(self):
        """Register bot commands with Telegram."""
        commands = [
            BotCommand("start", "開始 / 註冊"),
            BotCommand("help", "查看幫助"),
            BotCommand("myorders", "我的訂單"),
            BotCommand("userinfo", "我的資料"),
            BotCommand("admin", "管理員選單"),
            BotCommand("new", "上架拍賣品"),
            BotCommand("import_batch", "批次匯入"),
            BotCommand("schedule", "排程批次"),
            BotCommand("start_batch", "開始批次"),
            BotCommand("pause_batch", "暫停批次"),
            BotCommand("resume_batch", "恢復批次"),
            BotCommand("abort_batch", "終止批次"),
            BotCommand("batch_status", "批次狀態"),
            BotCommand("broadcast", "廣播訊息"),
            BotCommand("export_data", "匯出數據"),
            BotCommand("set_prod_group", "設定正式群組"),
            BotCommand("set_test_group", "設定測試群組"),
            BotCommand("import_members", "匯入會員"),
        ]
        await self.app.bot.set_my_commands(commands)
