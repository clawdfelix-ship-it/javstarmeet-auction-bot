"""Batch auction queue management + admin panel UI."""
import asyncio
import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

ITEM_DURATION = 25
PAUSE_BETWEEN_ITEMS = 3


# --- Batch Admin Panel State (replaces module-level globals) ---
class BatchState:
    """Thread-unsafe singleton for batch admin panel message tracking."""
    def __init__(self):
        self.panel_message_id: int | None = None
        self.panel_chat_id: int | None = None

    def clear(self):
        self.panel_message_id = None
        self.panel_chat_id = None

batch_state = BatchState()


def get_batch_state(current_auction: dict) -> str:
    """Determine current batch state for panel display."""
    if current_auction.get("batch_abort"):
        return "aborting"
    if current_auction.get("batch_mode"):
        if current_auction.get("batch_paused"):
            return "paused"
        return "running"
    if current_auction.get("scheduled_start"):
        return "scheduled"
    queue = current_auction.get("batch_queue", [])
    if queue:
        return "idle"
    return "empty"


def build_batch_admin_keyboard(state: str) -> InlineKeyboardMarkup:
    """Build inline keyboard based on current batch state."""
    keyboard = []

    if state == "empty":
        return InlineKeyboardMarkup(keyboard)

    if state == "idle":
        keyboard.append([
            InlineKeyboardButton("🚀 開始批次拍賣", callback_data="batch_start"),
        ])
        keyboard.append([
            InlineKeyboardButton("🗑️ 清空隊列", callback_data="batch_clear"),
        ])
        keyboard.append([
            InlineKeyboardButton("📊 狀態", callback_data="batch_status"),
        ])

    elif state == "scheduled":
        keyboard.append([
            InlineKeyboardButton("▶️ 立即開始", callback_data="batch_start_now"),
        ])
        keyboard.append([
            InlineKeyboardButton("❌ 取消排程", callback_data="batch_cancel_schedule"),
        ])
        keyboard.append([
            InlineKeyboardButton("📊 狀態", callback_data="batch_status"),
        ])

    elif state == "running":
        keyboard.append([
            InlineKeyboardButton("⏸ 暫停", callback_data="batch_pause"),
        ])
        keyboard.append([
            InlineKeyboardButton("🛑 終止", callback_data="batch_abort"),
        ])
        keyboard.append([
            InlineKeyboardButton("📊 狀態", callback_data="batch_status"),
        ])

    elif state == "paused":
        keyboard.append([
            InlineKeyboardButton("▶️ 恢復", callback_data="batch_resume"),
        ])
        keyboard.append([
            InlineKeyboardButton("🛑 終止", callback_data="batch_abort"),
        ])
        keyboard.append([
            InlineKeyboardButton("📊 狀態", callback_data="batch_status"),
        ])

    elif state == "aborting":
        keyboard.append([
            InlineKeyboardButton("📊 狀態", callback_data="batch_status"),
        ])

    return InlineKeyboardMarkup(keyboard)


def build_batch_admin_text(state: str, current_auction: dict) -> str:
    """Build admin panel text based on current batch state."""
    queue_len = len(current_auction.get("batch_queue", []))
    sched_time = current_auction.get("scheduled_start", "未設定")
    target_type = current_auction.get("batch_target_group", "prod")
    target_desc = "正式群組" if target_type != "test" else "測試群組"

    if state == "empty":
        return (
            "📋 <b>批次拍賣控制台</b>\n\n"
            "⚪ 目前沒有任何拍賣品在隊列中。\n"
            "使用 <code>/import_batch</code> 匯入拍賣品。"
        )

    if state == "idle":
        return (
            "📋 <b>批次拍賣控制台</b>\n\n"
            f"📦 隊列：{queue_len} 件\n"
            f"🕐 排程：{sched_time}\n"
            f"📢 目標：{target_desc}\n\n"
            f"▶️ <b>準備就緒</b> — 按下方的按鈕開始拍賣。"
        )

    if state == "scheduled":
        return (
            "📋 <b>批次拍賣控制台</b>\n\n"
            f"📦 隊列：{queue_len} 件\n"
            f"🕐 排程時間：{sched_time}\n"
            f"📢 目標：{target_desc}\n\n"
            f"⏳ <b>已排程，等待開始</b>"
        )

    if state == "running":
        idx = current_auction.get("batch_current_index", 0) + 1
        title = html.escape(current_auction.get("title", "?"))
        return (
            "📋 <b>批次拍賣控制台</b>\n\n"
            f"📦 進度：Item {idx}/{queue_len}\n"
            f"📌 當前：{title}\n"
            f"🕐 排程：{sched_time}\n"
            f"📢 目標：{target_desc}\n\n"
            f"▶️ <b>拍賣進行中...</b>"
        )

    if state == "paused":
        idx = current_auction.get("batch_current_index", 0) + 1
        title = html.escape(current_auction.get("title", "?"))
        return (
            "📋 <b>批次拍賣控制台</b>\n\n"
            f"📦 進度：Item {idx}/{queue_len}\n"
            f"📌 當前：{title}\n"
            f"🕐 排程：{sched_time}\n"
            f"📢 目標：{target_desc}\n\n"
            f"⏸ <b>已暫停</b>"
        )

    if state == "aborting":
        return (
            "📋 <b>批次拍賣控制台</b>\n\n"
            f"🛑 <b>正在終止...</b>\n\n"
            "請等待當前項目結束。"
        )

    return "📋 <b>批次拍賣控制台</b>"



async def show_batch_admin_panel(bot, chat_id=None, message_id=None, update_existing=True):
    """Send or edit the admin batch control panel message."""
    state = get_batch_state(current_auction)
    text = build_batch_admin_text(state, current_auction)
    keyboard = build_batch_admin_keyboard(state)

    admin_id = ADMIN_IDS[0] if ADMIN_IDS else None
    target_chat_id = chat_id or admin_id
    if not target_chat_id:
        return

    try:
        if update_existing and batch_state.panel_message_id and batch_state.panel_chat_id:
            # Try to edit existing panel message
            try:
                await bot.edit_message_text(
                    chat_id=batch_state.panel_chat_id,
                    message_id=batch_state.panel_message_id,
                    text=text,
                    reply_markup=keyboard,
                    parse_mode=ParseMode.HTML
                )
                return
            except Exception:
                # Message not found or can't be edited - send new one
                batch_state.clear()

        # Send new panel message
        msg = await bot.send_message(
            chat_id=target_chat_id,
            text=text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML
        )
        batch_state.panel_message_id = msg.message_id
        batch_state.panel_chat_id = target_chat_id

    except Exception as e:
        logger.error(f"Failed to show batch admin panel: {e}")




async def handle_batch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all batch admin panel button clicks."""
    query = update.callback_query
    user = query.from_user

    if user.id not in ADMIN_IDS:
        await query.answer("⛔ 權限不足", show_alert=True)
        return

    await query.answer()
    data = query.data
    bot = context.bot

    if data == "batch_start":
        # Start the batch auction
        if not current_auction.get("batch_queue"):
            await query.message.edit_text("❌ 請先使用 /import_batch 匯入拍賣品。")
            return
        if current_auction.get("active"):
            await query.message.edit_text("❌ 已有拍賣正在進行中。")
            return
        # Trigger start - redirect by editing message and letting admin use command
        await query.message.edit_text(
            "🚀 正在啟動批次拍賣...\n\n"
            "使用 <code>/start_batch</code> 開始拍賣。",
            parse_mode=ParseMode.HTML
        )
        # Actually start it
        await start_batch_command(update, context)

    elif data == "batch_clear":
        # Clear the queue
        queue_len = len(current_auction.get("batch_queue", []))
        current_auction["batch_queue"] = []
        current_auction["batch_mode"] = False
        current_auction["scheduled_start"] = None
        current_auction["batch_current_index"] = 0
        current_auction["batch_paused"] = False
        current_auction["batch_abort"] = False
        batch_state.clear()
        await query.message.edit_text(
            f"✅ 已清空隊列（{queue_len} 件已移除）。",
            parse_mode=ParseMode.HTML
        )

    elif data == "batch_status":
        # Show detailed status
        queue_len = len(current_auction.get("batch_queue", []))
        sched_time = current_auction.get("scheduled_start", "未設定")
        state = get_batch_state(current_auction)

        if current_auction.get("batch_mode"):
            idx = current_auction.get("batch_current_index", 0) + 1
            title = html.escape(current_auction.get("title", "?"))
            status = "⏸ 已暫停" if current_auction.get("batch_paused") else "▶️ 運行中"
            text = (
                f"📊 <b>批次狀態</b>\n\n"
                f"📦 隊列：{queue_len} 件\n"
                f"📌 進度：Item {idx}/{queue_len}\n"
                f"📝 當前：{title}\n"
                f"🔘 狀態：{status}\n"
                f"🕐 排程：{sched_time}"
            )
        else:
            text = (
                f"📊 <b>批次狀態</b>\n\n"
                f"📦 隊列：{queue_len} 件\n"
                f"🕐 排程：{sched_time}"
            )

        keyboard = build_batch_admin_keyboard(state)
        await query.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)

    elif data == "batch_start_now":
        # Cancel schedule and start immediately
        current_auction["scheduled_start"] = None
        await query.message.edit_text("🚀 正在立即開始批次拍賣...", parse_mode=ParseMode.HTML)
        await start_batch_command(update, context)

    elif data == "batch_cancel_schedule":
        # Cancel the scheduled time
        current_auction["scheduled_start"] = None
        state = get_batch_state(current_auction)
        text = build_batch_admin_text(state, current_auction)
        keyboard = build_batch_admin_keyboard(state)
        await query.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        await query.message.reply_text("✅ 排程已取消。", parse_mode=ParseMode.HTML)

    elif data == "batch_pause":
        # Pause the batch
        if not current_auction.get("batch_mode"):
            await query.message.edit_text("❌ 目前沒有正在進行的批次拍賣。")
            return
        if current_auction.get("batch_paused"):
            await query.answer("已經是暫停狀態", show_alert=True)
            return
        current_auction["batch_paused"] = True
        await show_batch_admin_panel(bot, update_existing=True)

    elif data == "batch_resume":
        # Resume the batch
        if not current_auction.get("batch_mode"):
            await query.message.edit_text("❌ 目前沒有正在進行的批次拍賣。")
            return
        if not current_auction.get("batch_paused"):
            await query.answer("不是暫停狀態", show_alert=True)
            return
        current_auction["batch_paused"] = False
        await show_batch_admin_panel(bot, update_existing=True)

    elif data == "batch_abort":
        # Abort the batch
        if not current_auction.get("batch_mode"):
            await query.message.edit_text("❌ 目前沒有正在進行的批次拍賣。")
            return
        current_auction["batch_abort"] = True
        current_auction["batch_paused"] = False
        await show_batch_admin_panel(bot, update_existing=True)


# --- Batch Auction Callback Patterns (for dispatch) ---
BATCH_CALLBACK_PATTERNS = [
    "batch_start", "batch_clear", "batch_status",
    "batch_start_now", "batch_cancel_schedule",
    "batch_pause", "batch_resume", "batch_abort",
]

# 全局拍賣狀態
current_auction = {
    "active": False,
    "start_time": None,
    "end_time": None,
    "title": "",
    "photo_id": None,
    "base_price": 0,
    "current_price": 0,
    "bin_price": 0,         # 一口價 (0 = 不設)
    "pending_price": 0,     # 暗標：待 reveal 的價格
    "pending_bidder": None, # user_id
    "pending_bidder_name": "無",
    "bidders": [],          # list of {id, name, price, time} - all bidders
    "highest_bidder": None,  # 上一個最高出價者 (for outbid notification)
    "highest_bidder_name": "無",
    "message_id": None,     # 拍賣訊息 ID (群組)
    "chat_id": None,        # 群組 ID
    "timer_task": None,
    "update_event": asyncio.Event(),
    "session_id": None,
    "session_seq": 0,
    "bot_username": None,
    "_ending": False,  # Flag: auction is in the process of ending (used to accept late-arriving bids)
    "bin_confirm_user_id": None,
    "bin_confirm_expires_at": 0,

    # Batch auction state
    "batch_mode": False,           # True when running batch auction
    "batch_queue": [],             # list of items: [{title, price, bin_price, photo_id, target_chat_id, target_type}, ...]
    "batch_current_index": 0,       # current item index in batch
    "batch_paused": False,         # True when batch is paused
    "batch_abort": False,          # True when batch should be aborted
    "batch_target_group": None,    # "prod" or "test"
    "scheduled_start": None,       # datetime when scheduled batch should start
    "batch_timer_task": None,      # asyncio task for scheduled batch start
}

# auction_lock is defined in core/auction.py