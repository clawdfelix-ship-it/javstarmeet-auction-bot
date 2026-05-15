import logging
import os
import json
import csv
import io
import html
import asyncio
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from email_utils import send_email

# Telegram
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, ForceReply
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

# Database
try:
    import asyncpg
except ImportError:
    asyncpg = None

# Load environment variables from .env file
load_dotenv()

# Config
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "582328026").split(",")]
DATABASE_URL = os.getenv("DATABASE_URL")

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# States
NAME, PHONE, EMAIL, PICKUP = range(4)
WAITING_PHOTO, WAITING_TITLE, WAITING_PRICE, WAITING_BIN_PRICE = range(4, 8)
BIDDING_PRICE = 8

# --- Store Class (Async Postgres / JSON) ---
class Store:
    def __init__(self):
        self.is_pg = bool(DATABASE_URL)
        self.pool = None
        self.data = {
            "users": {},
            "blacklist": [],
            "auctions": [],
            "orders": [],
            "sessions": [],
            "config": {}
        }
        if not self.is_pg:
            self.db_file = os.getenv("DATA_PATH", "data.json")
            self.load_json()

    async def connect(self):
        if self.is_pg:
            if not asyncpg:
                logger.error("DATABASE_URL present but asyncpg not installed.")
                exit(1)
            
            # Retry connection logic
            retries = 5
            for i in range(retries):
                try:
                    logger.info(f"Connecting to DB... (Attempt {i+1}/{retries})")
                    self.pool = await asyncpg.create_pool(DATABASE_URL)
                    await self.init_pg()
                    logger.info("Connected to PostgreSQL (Async)")
                    return
                except Exception as e:
                    logger.error(f"Failed to connect to DB: {e}")
                    if i < retries - 1:
                        wait_time = 5
                        logger.info(f"Retrying in {wait_time} seconds...")
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error("Could not connect to database after multiple attempts.")
                        exit(1)

    async def init_pg(self):
        async with self.pool.acquire() as conn:
            # Users
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    name TEXT,
                    phone TEXT,
                    email TEXT,
                    pickup TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Blacklist
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS blacklist (
                    user_id BIGINT PRIMARY KEY,
                    reason TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Orders
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    user_id BIGINT,
                    item TEXT,
                    price INTEGER,
                    status TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Config
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS system_config (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            
            # Sessions (New)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    date TEXT,
                    seq_num INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Add session_id to orders if not exists
            try:
                await conn.execute("ALTER TABLE orders ADD COLUMN session_id TEXT")
            except Exception:
                pass

            logger.info("PostgreSQL tables initialized.")

    def load_json(self):
        if os.path.exists(self.db_file):
            try:
                with open(self.db_file, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception as e:
                logger.error(f"Failed to load data: {e}")

    def save_json(self):
        if not self.is_pg:
            try:
                with open(self.db_file, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error(f"Failed to save data: {e}")

    # --- User Methods ---
    async def register_user(self, user_id, info):
        if self.is_pg:
            async with self.pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO users (user_id, name, phone, email, pickup)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (user_id) DO UPDATE 
                    SET name=EXCLUDED.name, phone=EXCLUDED.phone, email=EXCLUDED.email, pickup=EXCLUDED.pickup
                """, user_id, info['name'], info['phone'], info.get('email', ''), info['pickup'])
        else:
            self.data["users"][str(user_id)] = info
            self.save_json()

    async def get_user(self, user_id):
        if self.is_pg:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
                if row: return dict(row)
                return None
        else:
            return self.data["users"].get(str(user_id))

    async def is_registered(self, user_id):
        user = await self.get_user(user_id)
        return user is not None

    # --- Blacklist Methods ---
    async def add_blacklist(self, user_id, reason="violation"):
        if self.is_pg:
            async with self.pool.acquire() as conn:
                await conn.execute("INSERT INTO blacklist (user_id, reason) VALUES ($1, $2) ON CONFLICT DO NOTHING", user_id, reason)
        else:
            if user_id not in self.data["blacklist"]:
                self.data["blacklist"].append(user_id)
                self.save_json()

    async def remove_blacklist(self, user_id):
        if self.is_pg:
            async with self.pool.acquire() as conn:
                await conn.execute("DELETE FROM blacklist WHERE user_id = $1", user_id)
        else:
            if user_id in self.data["blacklist"]:
                self.data["blacklist"].remove(user_id)
                self.save_json()

    async def is_blacklisted(self, user_id):
        if self.is_pg:
            async with self.pool.acquire() as conn:
                val = await conn.fetchval("SELECT 1 FROM blacklist WHERE user_id = $1", user_id)
                return val is not None
        else:
            return user_id in self.data["blacklist"]

    # --- Session Methods ---
    async def get_next_session(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if self.is_pg:
            async with self.pool.acquire() as conn:
                count = await conn.fetchval("SELECT COUNT(*) FROM sessions WHERE date = $1", today)
                seq = count + 1
                session_id = f"{today.replace('-','')}-{seq}"
                await conn.execute("INSERT INTO sessions (session_id, date, seq_num) VALUES ($1, $2, $3)", session_id, today, seq)
                return session_id, seq
        else:
            if "sessions" not in self.data: self.data["sessions"] = []
            count = len([s for s in self.data["sessions"] if s["date"] == today])
            seq = count + 1
            session_id = f"{today.replace('-','')}-{seq}"
            self.data["sessions"].append({"session_id": session_id, "date": today, "seq_num": seq})
            self.save_json()
            return session_id, seq

    # --- Order Methods ---
    async def add_order(self, order):
        if self.is_pg:
            async with self.pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO orders (order_id, user_id, item, price, status, created_at, session_id)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                """, order['order_id'], order['user_id'], order['item'], order['price'], order['status'], datetime.fromisoformat(order['time']), order.get('session_id'))
        else:
            self.data["orders"].append(order)
            self.save_json()

    async def get_all_orders(self):
        if self.is_pg:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch("SELECT * FROM orders ORDER BY created_at DESC")
                return [dict(row) for row in rows]
        else:
            return self.data["orders"]

    async def update_order_status(self, order_id, status):
        if self.is_pg:
            async with self.pool.acquire() as conn:
                await conn.execute("UPDATE orders SET status = $1 WHERE order_id = $2", status, order_id)
        else:
            for o in self.data["orders"]:
                if o['order_id'] == order_id:
                    o['status'] = status
                    self.save_json()
                    break

    async def get_user_orders(self, user_id):
        if self.is_pg:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch("SELECT * FROM orders WHERE user_id = $1 ORDER BY created_at DESC", user_id)
                return [dict(row) for row in rows]
        else:
            return [o for o in self.data["orders"] if str(o['user_id']) == str(user_id)]

    async def get_session_orders(self, session_id):
        if self.is_pg:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch("SELECT * FROM orders WHERE session_id = $1 ORDER BY user_id, created_at", session_id)
                return [dict(row) for row in rows]
        else:
            return [o for o in self.data["orders"] if o.get('session_id') == session_id]

    async def get_all_users(self):
        if self.is_pg:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch("SELECT * FROM users")
                return [dict(row) for row in rows]
        else:
            return list(self.data["users"].values())

    # --- Config Methods ---
    async def set_config(self, key, value):
        if self.is_pg:
            async with self.pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO system_config (key, value) VALUES ($1, $2)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """, key, str(value))
        else:
            self.data["config"][key] = value
            self.save_json()

    async def get_config(self, key):
        if self.is_pg:
            async with self.pool.acquire() as conn:
                val = await conn.fetchval("SELECT value FROM system_config WHERE key = $1", key)
                if val:
                    if val.isdigit(): return int(val)
                    return val
                return None
        else:
            return self.data["config"].get(key)

    async def get_auction_queue(self):
        raw = await self.get_config("auction_queue")
        if not raw:
            return []
        try:
            return json.loads(raw)
        except Exception:
            return []

    async def set_auction_queue(self, queue):
        await self.set_config("auction_queue", json.dumps(queue))

store = Store()

# Constant for custom bid prompt
CUSTOM_BID_PROMPT = "請回覆此訊息輸入您的出價金額 (純數字)："

# --- Batch Auction Constants ---
ITEM_DURATION = 25        # seconds per auction item
PAUSE_BETWEEN_ITEMS = 3  # seconds pause between items in batch mode

# --- Batch Admin Panel Message Tracking ---
BATCH_PANEL_MESSAGE_ID = None  # chat_id, message_id of the admin panel
BATCH_PANEL_CHAT_ID = None

# --- Batch Admin Panel State Machine ---
def get_batch_state():
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


def build_batch_admin_keyboard(state):
    """Build inline keyboard based on current batch state."""
    keyboard = []

    if state == "empty":
        # No queue - show nothing useful
        return InlineKeyboardMarkup(keyboard)

    if state == "idle":
        # Items imported but not started
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
        # Time set but not started
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
        # Batch is actively running
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
        # Batch is paused
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


def build_batch_admin_text(state):
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
        queue_len = len(current_auction.get("batch_queue", []))
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
    global BATCH_PANEL_MESSAGE_ID, BATCH_PANEL_CHAT_ID

    state = get_batch_state()
    text = build_batch_admin_text(state)
    keyboard = build_batch_admin_keyboard(state)

    admin_id = ADMIN_IDS[0] if ADMIN_IDS else None
    target_chat_id = chat_id or admin_id
    if not target_chat_id:
        return

    try:
        if update_existing and BATCH_PANEL_MESSAGE_ID and BATCH_PANEL_CHAT_ID:
            # Try to edit existing panel message
            try:
                await bot.edit_message_text(
                    chat_id=BATCH_PANEL_CHAT_ID,
                    message_id=BATCH_PANEL_MESSAGE_ID,
                    text=text,
                    reply_markup=keyboard,
                    parse_mode=ParseMode.HTML
                )
                return
            except Exception:
                # Message not found or can't be edited - send new one
                BATCH_PANEL_MESSAGE_ID = None
                BATCH_PANEL_CHAT_ID = None

        # Send new panel message
        msg = await bot.send_message(
            chat_id=target_chat_id,
            text=text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML
        )
        BATCH_PANEL_MESSAGE_ID = msg.message_id
        BATCH_PANEL_CHAT_ID = target_chat_id

    except Exception as e:
        logger.error(f"Failed to show batch admin panel: {e}")


# --- Batch Callback Handlers ---

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
        global BATCH_PANEL_MESSAGE_ID, BATCH_PANEL_CHAT_ID
        BATCH_PANEL_MESSAGE_ID = None
        BATCH_PANEL_CHAT_ID = None
        await query.message.edit_text(
            f"✅ 已清空隊列（{queue_len} 件已移除）。",
            parse_mode=ParseMode.HTML
        )

    elif data == "batch_status":
        # Show detailed status
        queue_len = len(current_auction.get("batch_queue", []))
        sched_time = current_auction.get("scheduled_start", "未設定")
        state = get_batch_state()

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
        state = get_batch_state()
        text = build_batch_admin_text(state)
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

# --- Auction State Persistence ---
AUCTION_STATE_FILE = "auction_state.json"

def save_auction_state():
    """Persist current_auction to JSON for crash recovery."""
    try:
        # Only save if auction was ever activated (has title/chat_id)
        if current_auction.get("title") and current_auction.get("chat_id"):
            with open(AUCTION_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(current_auction, f, ensure_ascii=False, indent=2, default=str)
            logger.info(f"Auction state saved: {current_auction.get('title')}")
        elif os.path.exists(AUCTION_STATE_FILE):
            # Clean up stale file if no active auction
            try:
                os.remove(AUCTION_STATE_FILE)
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Failed to save auction state: {e}")

def load_auction_state():
    """Load auction state from JSON. Returns True if a valid active auction was restored."""
    if not os.path.exists(AUCTION_STATE_FILE):
        return False
    try:
        with open(AUCTION_STATE_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if loaded.get("active") and loaded.get("title"):
            # Restore into current_auction (preserve Event/lock objects)
            preserved_keys = {"update_event": current_auction["update_event"], "timer_task": None}
            current_auction.update(loaded)
            current_auction["update_event"] = preserved_keys["update_event"]
            current_auction["timer_task"] = None
            current_auction["_ending"] = False
            # Recalculate end_time relative to now if it was stored as a timestamp
            if isinstance(current_auction.get("end_time"), (int, float)):
                saved_end = current_auction["end_time"]
                remaining = saved_end - datetime.now().timestamp()
                # If auction would have already expired, treat as finished
                if remaining < 5:
                    current_auction["active"] = False
                    logger.info(f"Loaded auction '{loaded.get('title')}' has expired; skipping resume.")
                    save_auction_state()  # will clean up
                    return False
                # else: still has time, can resume
            logger.info(f"Auction state restored: {loaded.get('title')}, remaining {remaining:.0f}s")
            return True
    except Exception as e:
        logger.error(f"Failed to load auction state: {e}")
    return False

# Lock to prevent race conditions when multiple users bid simultaneously
auction_lock = asyncio.Lock()

# --- 註冊流程 ---
async def start_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_edit = False
    
    # Check if this is from "edit_profile" callback
    if update.callback_query:
        await update.callback_query.answer()
        if update.callback_query.data == "edit_profile":
            is_edit = True
    
    # Check for deep linking parameters (bidding)
    if not is_edit and context.args:
        arg = context.args[0]
        
        if arg == 'bid':
            if not await store.is_registered(user.id):
                 await update.message.reply_text("⚠️ 請先完成註冊才能出價！\n請輸入您的 <b>稱呼 (Name)</b>：", parse_mode=ParseMode.HTML)
                 return NAME

            # Strict mode: check profile completeness
            user_info = await store.get_user(user.id)
            missing = []
            if not user_info.get('name'):
                missing.append('稱呼')
            if not user_info.get('phone'):
                missing.append('電話')
            if not user_info.get('email'):
                missing.append('Email')
            if not user_info.get('pickup'):
                missing.append('交收地點')

            if missing:
                await update.message.reply_text(
                    f"⚠️ 請先補全以下資料才能出價：\n" +
                    "\n".join(f"- {m}" for m in missing) +
                    "\n\n請點擊 /start 更新資料",
                    parse_mode=ParseMode.HTML
                )
                return ConversationHandler.END

            # If registered, start bidding flow
            if not current_auction["active"]:
                await update.message.reply_text("❌ 當前沒有進行中的拍賣。")
                return ConversationHandler.END
                
            await update.message.reply_text(
                f"🔥 <b>正在拍賣：{html.escape(current_auction['title'])}</b>\n"
                f"💰 當前最高暗標價：${current_auction['pending_price']}\n\n"
                f"請輸入您的 <b>出價金額</b> (純數字)：",
                parse_mode=ParseMode.HTML
            )
            return BIDDING_PRICE

        elif arg == 'bid_webapp':
            if not await store.is_registered(user.id):
                 await update.message.reply_text("⚠️ 請先完成註冊才能出價！\n請輸入您的 <b>稱呼 (Name)</b>：", parse_mode=ParseMode.HTML)
                 return NAME

            # Strict mode: check profile completeness
            user_info = await store.get_user(user.id)
            missing = []
            if not user_info.get('name'):
                missing.append('稱呼')
            if not user_info.get('phone'):
                missing.append('電話')
            if not user_info.get('email'):
                missing.append('Email')
            if not user_info.get('pickup'):
                missing.append('交收地點')

            if missing:
                await update.message.reply_text(
                    f"⚠️ 請先補全以下資料才能出價：\n" +
                    "\n".join(f"- {m}" for m in missing) +
                    "\n\n請點擊 /start 更新資料",
                    parse_mode=ParseMode.HTML
                )
                return ConversationHandler.END
                 
            if not current_auction["active"]:
                await update.message.reply_text("❌ 當前沒有進行中的拍賣。")
                return ConversationHandler.END

            webapp_url = os.getenv("WEBAPP_URL")
            if not webapp_url:
                 await update.message.reply_text("⚠️ 系統未配置 WebApp，請使用傳統出價方式。")
                 return ConversationHandler.END
                 
            if not webapp_url.startswith("https://"):
                webapp_url = f"https://{webapp_url}"
                
            from telegram import WebAppInfo
            keyboard = [[InlineKeyboardButton("✍️ 開啟出價頁面", web_app=WebAppInfo(url=webapp_url))]]
            await update.message.reply_text(
                "👇 請點擊下方按鈕開啟出價視窗：",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return ConversationHandler.END

    # 定義快捷選單
    menu_keyboard = [['📜 拍賣規則', '👤 我的資料'], ['📍 取貨地址']]
    if user.id in ADMIN_IDS:
        menu_keyboard.append(['🔧 管理員選單'])
    
    reply_markup = ReplyKeyboardMarkup(menu_keyboard, resize_keyboard=True)

    if not is_edit and await store.is_registered(user.id):
        await update.message.reply_text(
            "✅ 您已經註冊過了，可以直接參與競拍！\n您可以點擊下方按鈕查看規則或個人資料。",
            reply_markup=reply_markup
        )
        return ConversationHandler.END
    
    # Check completeness for returning users (not editing)
    if not is_edit and await store.is_registered(user.id):
        user_info = await store.get_user(user.id)
        missing = []
        if not user_info.get('name'):
            missing.append('稱呼')
        if not user_info.get('phone'):
            missing.append('電話')
        if not user_info.get('email'):
            missing.append('Email')
        if not user_info.get('pickup'):
            missing.append('交收地點')

        if missing:
            # Incomplete profile → force edit mode
            is_edit = True

    msg_text = "👋 歡迎來到極速拍賣機器人！\n為了確保交易順利，請先完成簡單的登記。\n\n請輸入您的 <b>稱呼 (Name)</b>："
    if is_edit:
        # Prefill with existing values
        existing_info = await store.get_user(user.id)
        existing_name = existing_info.get('name', '') if existing_info else ''
        existing_phone = existing_info.get('phone', '') if existing_info else ''
        existing_email = existing_info.get('email', '') if existing_info else ''
        existing_pickup = existing_info.get('pickup', '') if existing_info else ''

        # Store existing values for potential use
        context.user_data['reg_name'] = existing_name
        context.user_data['reg_phone'] = existing_phone
        context.user_data['reg_email'] = existing_email
        context.user_data['reg_pickup'] = existing_pickup

        prefilled_note = ""
        if existing_name or existing_phone or existing_email or existing_pickup:
            prefilled_note = f"\n\n📋 現有資料：\n" \
                f"稱呼：{html.escape(existing_name) or '未填'}\n" \
                f"電話：{html.escape(existing_phone) or '未填'}\n" \
                f"Email：{html.escape(existing_email) or '未填'}\n" \
                f"交收：{html.escape(existing_pickup) or '未填'}\n" \
                f"\n直接輸入新值可更新，或回覆「skip」保留現有值"

        msg_text = f"✏️ <b>補全 / 修改資料</b>{prefilled_note}\n\n請輸入您的 <b>稱呼 (Name)</b>："

    if update.callback_query:
         await update.callback_query.message.reply_text(msg_text, parse_mode=ParseMode.HTML)
    else:
         await update.message.reply_text(msg_text, parse_mode=ParseMode.HTML)
         
    return NAME

async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    # Support "skip" to keep existing value
    if text.lower() == 'skip' and context.user_data.get('reg_name'):
        pass  # keep existing
    else:
        context.user_data['reg_name'] = text
    await update.message.reply_text("✅ 收到。請輸入您的 <b>電話號碼</b> (例如 91234567)：", parse_mode=ParseMode.HTML)
    return PHONE

async def get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.lower() == 'skip' and context.user_data.get('reg_phone'):
        pass  # keep existing
    else:
        context.user_data['reg_phone'] = text
    await update.message.reply_text("✅ 收到。請輸入您的 <b>Email</b> (用於得標通知)：", parse_mode=ParseMode.HTML)
    return EMAIL

async def get_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import re
    text = update.message.text.strip()
    if text.lower() == 'skip' and context.user_data.get('reg_email'):
        pass  # keep existing
    else:
        # Validate email format
        email_pattern = r'^[\w\.\-]+@[\w\.\-]+\.\w+$'
        if not re.match(email_pattern, text):
            await update.message.reply_text(
                "⚠️ Email 格式不正確，請重新輸入：",
                parse_mode=ParseMode.HTML
            )
            return EMAIL
        context.user_data['reg_email'] = text

    keyboard = [['旺角店自取']]
    await update.message.reply_text(
        "✅ 收到。請選擇 <b>交收地點</b>：",
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )
    return PICKUP

async def get_pickup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    # Support "skip" to keep existing value
    if text.lower() == 'skip' and context.user_data.get('reg_pickup'):
        pass  # keep existing
    elif text in ['旺角店自取']:
        context.user_data['reg_pickup'] = text
    else:
        await update.message.reply_text("⚠️ 請選擇有效的選項 (旺角店自取)，或輸入「skip」保留現有值。")
        return PICKUP
    
    # 保存資料
    user = update.effective_user
    info = {
        "name": context.user_data['reg_name'],
        "phone": context.user_data['reg_phone'],
        "email": context.user_data['reg_email'],
        "pickup": context.user_data['reg_pickup']
    }
    await store.register_user(user.id, info)
    
    # 恢復主菜單
    menu_keyboard = [['📜 拍賣規則', '👤 我的資料'], ['📍 取貨地址']]
    if user.id in ADMIN_IDS:
        menu_keyboard.append(['🔧 管理員選單'])
    reply_markup = ReplyKeyboardMarkup(menu_keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        "門市地址：旺角西洋菜南街72號3樓（OK右手邊門口上）\n營業時間 :星期一 至 星期六\n星期日休息\n\n🎉 <b>註冊成功！</b>\n現在您可以參與所有拍賣活動了。",
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup
    )
    return ConversationHandler.END

async def cancel_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("註冊已取消。", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# --- 管理員上架流程 ---
async def new_auction_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ 權限不足")
        return ConversationHandler.END
    
    await update.message.reply_text("請發送拍賣品的 <b>圖片</b>：", parse_mode=ParseMode.HTML)
    return WAITING_PHOTO

async def get_auction_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1]
    context.user_data['auc_photo'] = photo.file_id
    await update.message.reply_text("收到圖片。請輸入 <b>商品標題/描述</b>：", parse_mode=ParseMode.HTML)
    return WAITING_TITLE

async def get_auction_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['auc_title'] = update.message.text
    await update.message.reply_text("請輸入 <b>起標價</b> (純數字)：", parse_mode=ParseMode.HTML)
    return WAITING_PRICE

async def get_auction_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        price = int(update.message.text)
        context.user_data['auc_price'] = price
    except ValueError:
        await update.message.reply_text("❌ 格式錯誤，請輸入純數字：")
        return WAITING_PRICE
    
    await update.message.reply_text("請輸入 <b>一口價 (Buy It Now)</b> 金額 (純數字，輸入 0 代表不設)：", parse_mode=ParseMode.HTML)
    return WAITING_BIN_PRICE

async def get_bin_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        bin_price = int(update.message.text)
        context.user_data['auc_bin_price'] = bin_price
    except ValueError:
        await update.message.reply_text("❌ 格式錯誤，請輸入純數字：")
        return WAITING_BIN_PRICE

    # 確認上架
    photo_id = context.user_data['auc_photo']
    title = context.user_data['auc_title']
    price = context.user_data['auc_price']
    safe_title = html.escape(title)
    
    bin_text = f"\n⚡️ 一口價：${bin_price}" if bin_price > 0 else ""
    
    keyboard = [
        [InlineKeyboardButton("🚀 發布到【客戶群】", callback_data="start_auction_prod")],
        [InlineKeyboardButton("🧪 發布到【測試群】", callback_data="start_auction_test")],
        [InlineKeyboardButton("📥 加入批次隊列【客戶群】", callback_data="queue_auction_prod")],
        [InlineKeyboardButton("📥 加入批次隊列【測試群】", callback_data="queue_auction_test")]
    ]
    await update.message.reply_photo(
        photo=photo_id,
        caption=f"📝 <b>預覽上架</b>\n\n📦 商品：{safe_title}\n💰 起標：${price}{bin_text}\n\n請選擇發布目標：",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ConversationHandler.END 

# --- 拍賣結算邏輯 ---

async def process_settlement_by_date(update: Update, context: ContextTypes.DEFAULT_TYPE, date_str: str):
    """結算指定日期的訂單"""
    query = update.callback_query
    await query.message.edit_text(f"⏳ 正在統計 {date_str} 的訂單並發送帳單...")
    
    orders = await store.get_all_orders()
    
    # Filter orders by date
    target_orders = []
    for o in orders:
        created_at = o.get('created_at')
        if isinstance(created_at, str):
            if created_at.startswith(date_str):
                target_orders.append(o)
        elif isinstance(created_at, datetime):
            if created_at.strftime('%Y-%m-%d') == date_str:
                target_orders.append(o)
                
    if not target_orders:
        await query.message.edit_text(f"❌ {date_str} 沒有任何訂單。")
        return

    # Group by User
    user_orders = {}
    for o in target_orders:
        uid = o['user_id']
        if uid not in user_orders:
            user_orders[uid] = []
        user_orders[uid].append(o)
        
    # Generate Bill & Send Message
    success_count = 0
    fail_count = 0
    
    for uid, u_orders in user_orders.items():
        try:
            user_info = await store.get_user(uid)
            if not user_info: continue
            
            total_amount = sum(o['price'] for o in u_orders)
            
            bill_text = (
                f"🎉 <b>拍賣結算單</b>\n"
                f"📅 日期：{date_str}\n"
                f"━━━━━━━━━━━━━━\n"
            )
            
            for idx, o in enumerate(u_orders, 1):
                bill_text += f"{idx}. {html.escape(o['item'])} - <b>${o['price']}</b>\n"
                
            bill_text += (
                f"━━━━━━━━━━━━━━\n"
                f"💰 <b>總金額：HKD ${total_amount}</b>\n\n"
                f"👤 <b>收件資料</b>：\n"
                f"• 名稱：{html.escape(user_info['name'])}\n"
                f"• 電話：{html.escape(user_info['phone'])}\n"
                f"• 交收：{html.escape(user_info['pickup'])}\n\n"
                f"請盡快完成付款並回傳截圖，謝謝！"
            )
            
            await context.bot.send_message(chat_id=uid, text=bill_text, parse_mode=ParseMode.HTML)
            success_count += 1
            
        except Exception as e:
            logger.error(f"Failed to send bill to {uid}: {e}")
            fail_count += 1
            
    # Final Report to Admin
    await query.message.edit_text(
        f"✅ <b>結算完成！</b>\n\n"
        f"📅 日期：{date_str}\n"
        f"• 總訂單數：{len(target_orders)}\n"
        f"• 中標人數：{len(user_orders)}\n"
        f"• 發送成功：{success_count}\n"
        f"• 發送失敗：{fail_count}",
        parse_mode=ParseMode.HTML
    )

async def process_daily_settlement(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.message.edit_text("⏳ 正在統計今日訂單並發送帳單...")
    
    # 1. Get today's orders (or specific session orders if session_id available)
    # For now, let's filter orders by today's date
    orders = await store.get_all_orders()
    today_str = datetime.now().strftime('%Y-%m-%d')
    
    # Filter orders created today
    today_orders = []
    for o in orders:
        created_at = o.get('created_at')
        if isinstance(created_at, str):
            if created_at.startswith(today_str):
                today_orders.append(o)
        elif isinstance(created_at, datetime):
            if created_at.strftime('%Y-%m-%d') == today_str:
                today_orders.append(o)
                
    if not today_orders:
        await query.message.edit_text("❌ 今日沒有任何訂單。")
        return

    # 2. Group by User
    user_orders = {}
    for o in today_orders:
        uid = o['user_id']
        if uid not in user_orders:
            user_orders[uid] = []
        user_orders[uid].append(o)
        
    # 3. Generate Bill & Send Message
    success_count = 0
    fail_count = 0
    
    for uid, u_orders in user_orders.items():
        try:
            user_info = await store.get_user(uid)
            if not user_info: continue
            
            total_amount = sum(o['price'] for o in u_orders)
            
            bill_text = (
                f"🎉 <b>恭喜中標！今日拍賣結算單</b>\n"
                f"📅 日期：{today_str}\n"
                f"━━━━━━━━━━━━━━\n"
            )
            
            for idx, o in enumerate(u_orders, 1):
                bill_text += f"{idx}. {html.escape(o['item'])} - <b>${o['price']}</b>\n"
                
            bill_text += (
                f"━━━━━━━━━━━━━━\n"
                f"💰 <b>總金額：HKD ${total_amount}</b>\n\n"
                f"👤 <b>收件資料</b>：\n"
                f"• 名稱：{html.escape(user_info['name'])}\n"
                f"• 電話：{html.escape(user_info['phone'])}\n"
                f"• 交收：{html.escape(user_info['pickup'])}\n\n"
                f"請盡快完成付款並回傳截圖，謝謝！"
            )
            
            await context.bot.send_message(chat_id=uid, text=bill_text, parse_mode=ParseMode.HTML)
            success_count += 1
            
        except Exception as e:
            logger.error(f"Failed to send bill to {uid}: {e}")
            fail_count += 1
            
    # 4. Final Report to Admin
    await query.message.edit_text(
        f"✅ <b>結算完成！</b>\n\n"
        f"• 總訂單數：{len(today_orders)}\n"
        f"• 中標人數：{len(user_orders)}\n"
        f"• 發送成功：{success_count}\n"
        f"• 發送失敗：{fail_count}",
        parse_mode=ParseMode.HTML
    )

# --- 拍賣核心邏輯 ---

async def start_auction_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.from_user.id not in ADMIN_IDS:
        return

    if current_auction["active"]:
        await query.edit_message_caption("❌ 已有拍賣進行中，請先結束。")
        return

    title = context.user_data.get('auc_title', '未知商品')
    price = context.user_data.get('auc_price', 0)
    bin_price = context.user_data.get('auc_bin_price', 0)
    photo_id = context.user_data.get('auc_photo')
    
    if not photo_id:
        await query.edit_message_caption("❌ 數據丟失，請重新上架。")
        return

    # Determine target group
    target_type = "正式"
    if query.data == "start_auction_test":
        target_chat_id = await store.get_config("test_group_id")
        target_type = "測試"
    else:
        target_chat_id = await store.get_config("prod_group_id")
        # Fallback to old 'group_id' if prod not set
        if not target_chat_id:
            target_chat_id = await store.get_config("group_id")
            
    if not target_chat_id:
        await query.edit_message_caption(f"❌ 尚未設定【{target_type}群組】！\n請先在目標群組輸入 /set_{'test_' if target_type=='測試' else 'prod_'}group")
        return

    # 初始化拍賣
    session_id, session_seq = await store.get_next_session()
    current_auction["active"] = True
    current_auction["title"] = title
    current_auction["base_price"] = price
    current_auction["current_price"] = price
    current_auction["pending_price"] = price   # 暗標：pending = base price initially
    current_auction["pending_bidder"] = None
    current_auction["pending_bidder_name"] = "無"
    current_auction["bidders"] = []
    current_auction["bin_price"] = bin_price
    current_auction["photo_id"] = photo_id
    current_auction["highest_bidder"] = None
    current_auction["highest_bidder_name"] = "無"
    current_auction["start_time"] = datetime.now()
    current_auction["end_time"] = datetime.now().timestamp() + ITEM_DURATION
    current_auction["session_id"] = session_id
    current_auction["session_seq"] = session_seq 
    current_auction["chat_id"] = target_chat_id

    # Get bot username for deep linking
    try:
        me = await context.bot.get_me()
        current_auction["bot_username"] = me.username
    except Exception as e:
        logger.error(f"Failed to get bot info: {e}")

    text = generate_auction_text(ITEM_DURATION)
    keyboard = generate_bid_keyboard(price)
    
    await query.delete_message()
    
    try:
        msg = await context.bot.send_photo(
            chat_id=target_chat_id,
            photo=photo_id,
            caption=text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML
        )
        current_auction["message_id"] = msg.message_id
        current_auction["timer_task"] = asyncio.create_task(auction_timer_loop(context.bot))
        
        # Admin feedback
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"✅ 拍賣已發布到【{target_type}群組】！"
        )
    except Exception as e:
        logger.error(f"Failed to start auction: {e}")
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"❌ 發布失敗：{e}\n請檢查機器人是否在該群組且有發言權限。"
        )
        current_auction["active"] = False

async def queue_auction_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id not in ADMIN_IDS:
        return

    title = context.user_data.get('auc_title', '未知商品')
    price = context.user_data.get('auc_price', 0)
    photo_id = context.user_data.get('auc_photo')

    if not photo_id:
        await query.edit_message_caption("❌ 數據丟失，請重新上架。")
        return

    target_type = "正式"
    if query.data == "queue_auction_test":
        target_chat_id = await store.get_config("test_group_id")
        target_type = "測試"
    else:
        target_chat_id = await store.get_config("prod_group_id")
        if not target_chat_id:
            target_chat_id = await store.get_config("group_id")

    if not target_chat_id:
        await query.edit_message_caption(f"❌ 尚未設定【{target_type}群組】！\n請先在目標群組輸入 /set_{'test_' if target_type=='測試' else 'prod_'}group")
        return

    queue = await store.get_auction_queue()
    
    bin_price = context.user_data.get('auc_bin_price', 0)
    
    queue.append({
        "title": title,
        "price": price,
        "bin_price": bin_price,
        "photo_id": photo_id,
        "target_chat_id": target_chat_id
    })
    await store.set_auction_queue(queue)

    await query.edit_message_caption(
        f"✅ 已加入批次拍賣隊列（{target_type}群）。\n目前隊列中共有 {len(queue)} 件拍賣品。"
    )

# --- Helper for Numpad Keyboard (Stateless Logic) ---
def generate_numpad_keyboard(current_value, user_id):
    # Layout:
    # 1 2 3
    # 4 5 6
    # 7 8 9
    # ⬅️ 0 ✅
    # ❌ Cancel
    
    # Pre-calculate possible next values to encode in buttons
    # This allows the client to send the *next* state directly, 
    # reducing server-side calculation dependency and race conditions slightly.
    # BUT standard callback buttons are still round-trip.
    # To truly optimize, we need to handle "clicks" fast.
    
    keyboard = []
    # Rows 1-3
    for i in range(0, 9, 3):
        row = []
        for j in range(1, 4):
            num = i + j
            # Logic: If current is "0", next is "num". Else "current" + "num"
            # We calculate the NEXT value here and put it in callback_data
            # format: numpad_{user_id}_{NEXT_VALUE}_set
            
            if current_value == "0":
                next_val = str(num)
            else:
                next_val = current_value + str(num)
                if len(next_val) > 9: next_val = current_value # Prevent overflow in button
            
            row.append(InlineKeyboardButton(str(num), callback_data=f"numpad_{user_id}_{next_val}_set"))
        keyboard.append(row)
        
    # Row 4
    # Back button logic
    if len(current_value) > 1:
        back_val = current_value[:-1]
    else:
        back_val = "0"
        
    # Zero button logic
    if current_value == "0":
        zero_val = "0"
    else:
        zero_val = current_value + "0"
        if len(zero_val) > 9: zero_val = current_value

    row4 = [
        InlineKeyboardButton("⬅️", callback_data=f"numpad_{user_id}_{back_val}_set"),
        InlineKeyboardButton("0", callback_data=f"numpad_{user_id}_{zero_val}_set"),
        InlineKeyboardButton("✅ 確認", callback_data=f"numpad_{user_id}_{current_value}_enter")
    ]
    keyboard.append(row4)
    
    # Row 5
    keyboard.append([InlineKeyboardButton("❌ 取消", callback_data=f"numpad_{user_id}_{current_value}_cancel")])
    
    return InlineKeyboardMarkup(keyboard)

async def handle_numpad_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    # Format: numpad_{user_id}_{NEXT_VALUE}_{action}
    # action: set, enter, cancel
    
    parts = data.split("_")
    # parts[0] = numpad
    target_user_id = int(parts[1])
    next_val_str = parts[2]
    action = parts[3]
    
    user = query.from_user
    
    # Check if the user clicking is the one who opened the numpad
    if user.id != target_user_id:
        await query.answer("⚠️這不是您的出價視窗，請點擊「自定義出價」開啟。", show_alert=True)
        return

    # Check auction active
    if not current_auction["active"]:
        await query.answer("❌ 拍賣已結束", show_alert=True)
        try:
            await query.message.delete()
        except: pass
        return

    # Optimistic UI: Answer immediately
    # We answer with the new value as a toast if it's a 'set' action?
    # Or just empty to stop spinner.
    # For 'set', we can show "Input: $123" in toast for instant feedback
    if action == "set":
        # Show toast feedback immediately
        await query.answer(f"已輸入: ${next_val_str}")
    else:
        await query.answer()

    if action == "set":
        # Update message with new value
        # We only edit if value is different (though logic usually implies it is, unless 0->0 or max len)
        # But we need to compare with *message content* to be sure, or just try edit.
        # Since we encoded NEXT value, we just use next_val_str directly.
        
        try:
            await query.message.edit_text(
                f"🔢 <b>{html.escape(user.first_name)} 請輸入出價金額：</b>\n\n"
                f"💰 目前輸入：<b>${next_val_str}</b>",
                reply_markup=generate_numpad_keyboard(next_val_str, target_user_id),
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            # Ignore "message is not modified"
            pass
            
    elif action == "cancel":
        try:
            await query.message.delete()
        except: pass
        return
        
    elif action == "enter":
        price = int(next_val_str)
        if price <= 0:
            # We already answered, so we need to send a message or just ignore?
            # Or send a new answer? (Can't answer twice)
            # Send temp message
            msg = await context.bot.send_message(chat_id=query.message.chat_id, text="❌ 金額必須大於 0")
            await asyncio.sleep(2)
            try: await msg.delete() 
            except: pass
            return
            
        # Submit bid
        # Delete numpad message first
        try:
            await query.message.delete()
        except: pass
        
        # Process bid
        await process_blind_bid(user, price, query=None, bot=context.bot)
        # Send confirmation in PM
        try:
            await context.bot.send_message(
                chat_id=user.id,
                text=f"✅ 成功出價：${price}！\n如有更高出價，您將收到通知。"
            )
        except Exception as e:
            logger.warning(f"Failed to send numpad bid confirmation: {e}")
        return

def generate_auction_text(remaining_seconds):
    title = html.escape(current_auction["title"])
    # 暗標模式：拍賣進行中顯示起標價，結標後顯示實際成交價
    if current_auction["active"]:
        # Auction in progress - show base/opening price (blind)
        price = current_auction["base_price"]
    else:
        # Auction ended - reveal actual price
        price = current_auction["current_price"]
    # 永遠隱藏出價者資訊 (暗標)
    bidder = "㊙️ (匿名暗標)"
    
    seq = current_auction.get("session_seq", "?")
    
    bin_price = current_auction.get("bin_price", 0)
    bin_text = f"\n⚡️ 一口價：<b>${bin_price}</b>" if bin_price > 0 else ""
    
    if remaining_seconds <= 0:
        time_str = "00:00"
    else:
        mins, secs = divmod(int(remaining_seconds), 60)
        time_str = f"{mins:02}:{secs:02}"
        
    return (
        f"🔥 <b>正在拍賣：{title}</b> (第 {seq} 場 - 匿名暗標)\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 當前價格：<b>${price}</b>{bin_text}\n"
        f"👑 最高出價：{bidder}\n"
        f"⏱️ 剩餘時間：<b>{time_str}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"👇 點擊下方按鈕私訊出價！"
    )

def generate_bid_keyboard(current_price):
    # 全暗標拍賣：所有出價必須透過私訊，按鈕只提供私訊入口
    buttons = []
    
    # Add BIN button if set (links to private chat for BIN purchase)
    bin_price = current_auction.get("bin_price", 0)
    if bin_price > 0:
        bot_username = current_auction.get("bot_username")
        if bot_username:
            url = f"https://t.me/{bot_username}?start=bid"
            buttons.append([InlineKeyboardButton(f"⚡️ 一口價 ${bin_price}", url=url)])
    
    # Always add private bid button
    bot_username = current_auction.get("bot_username")
    if bot_username:
        url = f"https://t.me/{bot_username}?start=bid"
        buttons.append([InlineKeyboardButton("✍️ 點擊私訊出價", url=url)])
    
    return InlineKeyboardMarkup(buttons)

async def auction_timer_loop(bot):
    last_update_time = 0
    event = current_auction["update_event"]
    
    # Countdown update points (seconds before end)
    UPDATE_POINTS = [25, 20, 15, 10, 5, 4, 3, 2, 1]
    last_updated_point = None
    
    while True:
        try:
            # Safety: if auction is not active, stop the loop
            if not current_auction["active"]:
                break

            now = datetime.now().timestamp()
            remaining = current_auction["end_time"] - now
            
            # Force end if time is up - safety net
            if remaining <= 0:
                try:
                    await end_auction(bot)
                except Exception as e:
                    logger.error(f"Failed to end auction in timer loop: {e}")
                break

            # Check if we need to update at this countdown point
            current_point = None
            for point in UPDATE_POINTS:
                if remaining <= point:
                    current_point = point
            
            # Update if we've crossed a countdown point
            should_update = (current_point is not None and last_updated_point != current_point)
            
            if should_update:
                try:
                    await bot.edit_message_caption(
                        chat_id=current_auction["chat_id"],
                        message_id=current_auction["message_id"],
                        caption=generate_auction_text(remaining),
                        reply_markup=generate_bid_keyboard(current_auction["current_price"]),
                        parse_mode=ParseMode.HTML
                    )
                    last_updated_point = current_point
                except Exception as e:
                    # Ignore "message is not modified" error
                    if "message is not modified" not in str(e):
                        logger.warning(f"Update message failed: {e}")
                        last_updated_point = current_point
            
            # Wait until next expected update point or 1 second, whichever comes first
            if current_point is not None:
                # Find next point after current
                next_point = None
                for point in UPDATE_POINTS:
                    if point < current_point:
                        next_point = point
                        break
                if next_point is not None:
                    wait_time = min(remaining - next_point, 1.0)
                else:
                    wait_time = 0.1
            else:
                wait_time = 1.0
            
            wait_time = max(0.1, min(wait_time, 1.0))
            
            # Wait for event or timeout
            try:
                await asyncio.wait_for(event.wait(), timeout=wait_time)
                event.clear()
            except asyncio.TimeoutError:
                pass
                    
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Timer loop error: {e}")
            # If error, check if auction should end
            remaining = current_auction["end_time"] - datetime.now().timestamp()
            if remaining <= 0:
                try:
                    await end_auction(bot)
                except Exception as e2:
                    logger.error(f"Failed to end auction after error: {e2}")
                break
            await asyncio.sleep(1)

async def handle_private_bid_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text
    
    if not text.isdigit():
        await update.message.reply_text("❌ 格式錯誤，請輸入純數字：")
        return BIDDING_PRICE

    price = int(text)
    
    if not current_auction["active"]:
        await update.message.reply_text("❌ 拍賣已結束。")
        return ConversationHandler.END

    # Check registration
    if not await store.is_registered(user.id):
        await update.message.reply_text(
            "⚠️ 請先完成註冊才能出價！\n請輸入您的 <b>稱呼</b>：",
            parse_mode=ParseMode.HTML
        )
        return NAME

    # Check profile completeness (strict mode: all fields required)
    user_info = await store.get_user(user.id)
    missing = []
    if not user_info.get('name'):
        missing.append('稱呼')
    if not user_info.get('phone'):
        missing.append('電話')
    if not user_info.get('email'):
        missing.append('Email')
    if not user_info.get('pickup'):
        missing.append('交收地點')

    if missing:
        await update.message.reply_text(
            f"⚠️ 請先補全以下資料才能出價：\n" +
            "\n".join(f"- {m}" for m in missing) +
            "\n\n請點擊 /start 更新資料",
            parse_mode=ParseMode.HTML
        )
        return ConversationHandler.END

    # Process the bid (blind mode - no public price reveal until end)
    await process_blind_bid(user, price, query=None, bot=context.bot)
    await update.message.reply_text(
        f"✅ 成功出價：${price}！\n"
        f"出價已私密收下，如有更高出價您會收到通知！"
    )
    return ConversationHandler.END

async def handle_bid_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # All inline bid buttons removed - redirect to private chat
    # Only bid_custom can reach here (URL buttons don't trigger callbacks)
    query = update.callback_query
    user = query.from_user
    
    if query.data != "bid_custom":
        # Unknown button, ignore
        return
    
    # Check if active
    if not current_auction["active"]:
        await query.answer("❌ 拍賣已結束", show_alert=True)
        return
    
    # Check if user has registered
    if not await store.is_registered(user.id):
        bot_username = current_auction.get("bot_username") or context.bot.username
        if not bot_username:
            try:
                me = await context.bot.get_me()
                bot_username = me.username
            except Exception:
                pass
        if bot_username:
            url = f"https://t.me/{bot_username}?start=register"
            await query.answer("⚠️ 請先點此註冊！", url=url)
        else:
            await query.answer("⚠️ 請先私訊機器人完成註冊", show_alert=True)
        return

    # Check profile completeness (strict mode: all fields required)
    user_info = await store.get_user(user.id)
    missing = []
    if not user_info.get('name'):
        missing.append('稱呼')
    if not user_info.get('phone'):
        missing.append('電話')
    if not user_info.get('email'):
        missing.append('Email')
    if not user_info.get('pickup'):
        missing.append('交收地點')

    if missing:
        await query.answer(
            f"⚠️ 請先補全資料：{'、'.join(missing)}",
            show_alert=True
        )
        return

    # Redirect to private chat for bidding
    bot_username = current_auction.get("bot_username") or context.bot.username
    if not bot_username:
        try:
            me = await context.bot.get_me()
            bot_username = me.username
        except Exception:
            pass

    if bot_username:
        url = f"https://t.me/{bot_username}?start=bid"
        await query.answer("👇 請點擊按鈕私訊出價", url=url)
    else:
        await query.answer("⚠️ 請私訊機器人輸入出價金額", show_alert=True)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📜 <b>拍賣規則 & 使用指南</b>\n\n"
        "1️⃣ <b>參與資格</b>：首次使用需完成簡單登記 (稱呼、電話、交收地點)。\n"
        "2️⃣ <b>出價方式</b>：\n"
        "   • 點擊拍賣訊息下方的 <b>私訊出價按鈕</b>。\n"
        "   • 所有出價都係 <b>匿名暗標</b>，其他人睇唔到你出幾多錢。\n"
        "   • <b>暗標制</b>：所有出價均為匿名，結果於拍賣結束後揭曉。\n"
        "3️⃣ <b>得標結算</b>：\n"
        "   • 拍賣完結後，最高出價先至會公開。\n"
        "   • 系統會私訊得標者送出結算通知。\n"
        "   • 請於得標後盡快完成付款。\n"
        "4️⃣ <b>注意事項</b>：\n"
        "   • 棄標者將被列入黑名單，無法參與未來拍賣。\n"
        "   • 管理員擁有最終解釋權。\n\n"
        "📍 <b>取貨地址</b>：\n"
        "   旺角西洋菜南街72號3樓\n"
        "   （OK右手邊門口上）\n"
        "   營業時間：星期一至星期六\n"
        "   星期日休息\n\n"
        "如有疑問，請聯繫管理員。"
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)

async def my_orders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    orders = await store.get_user_orders(user.id)
    
    if not orders:
        await update.message.reply_text("📭 您目前沒有任何中標記錄。")
        return
        
    text = "🛍️ <b>我的中標記錄</b>\n"
    
    # 按日期分組 (Group by Date)
    orders_by_date = {}
    for o in orders:
        date_str = o.get('created_at') or o.get('time')
        if isinstance(date_str, str):
            try:
                dt = datetime.fromisoformat(date_str)
                date_key = dt.strftime('%Y-%m-%d')
            except:
                date_key = "未知日期"
        elif isinstance(date_str, datetime):
            date_key = date_str.strftime('%Y-%m-%d')
        else:
            date_key = "未知日期"
            
        if date_key not in orders_by_date:
            orders_by_date[date_key] = []
        orders_by_date[date_key].append(o)
    
    # 日期倒序排列
    sorted_dates = sorted(orders_by_date.keys(), reverse=True)
    
    for d in sorted_dates[:5]: # Show last 5 days groups to avoid too long message
        text += f"\n📅 <b>{d}</b>\n━━━━━━━━━━\n"
        for o in orders_by_date[d]:
             status_icon = "✅" if o['status'] == 'won' else "❌"
             if o['status'] == 'paid': status_icon = "💰"
             elif o['status'] == 'shipped': status_icon = "🚚"
             elif o['status'] == 'pending': status_icon = "⏳"
             
             # 顯示商品與價格
             text += (
                f"📦 {html.escape(o['item'])} | 💰 ${o['price']} | {status_icon}\n"
            )
        
    if len(orders) > 20:
        text += f"\n<i>(僅顯示最近記錄，共 {len(orders)} 筆)</i>"
        
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def user_info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    info = await store.get_user(user.id)
    
    if not info:
        await update.message.reply_text("❌ 您尚未註冊。\n請輸入 /start 開始註冊。")
        return
        
    text = (
        f"👤 <b>我的資料</b>\n"
        f"━━━━━━━━━━\n"
        f"名稱：{html.escape(info['name'])}\n"
        f"電話：{html.escape(info['phone'])}\n"
        f"Email：{html.escape(info.get('email', '未填寫'))}\n"
        f"交收：{html.escape(info['pickup'])}\n\n"
    )
    # 新增按鈕
    keyboard = [[InlineKeyboardButton("✏️ 修改資料", callback_data="edit_profile")]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)

# --- Unified Admin Panel (Inline Keyboard) ---

def build_admin_keyboard():
    """Build the unified admin panel inline keyboard."""
    keyboard = [
        # 📦 Auction section
        [
            InlineKeyboardButton("➕ 新增單件", callback_data="admin_add_single"),
            InlineKeyboardButton("📥 批量匯入", callback_data="admin_import_batch"),
            InlineKeyboardButton("📋 查看隊列", callback_data="admin_view_queue"),
        ],
        # 🚀 Batch Control section
        [
            InlineKeyboardButton("🕐 排程", callback_data="admin_schedule"),
            InlineKeyboardButton("🚀 開始", callback_data="admin_start_batch"),
            InlineKeyboardButton("⏸ 暫停", callback_data="admin_pause"),
            InlineKeyboardButton("▶️ 繼續", callback_data="admin_resume"),
            InlineKeyboardButton("🛑 終止", callback_data="admin_abort"),
        ],
        # 📊 Status section
        [
            InlineKeyboardButton("📊 拍賣狀態", callback_data="admin_batch_status"),
            InlineKeyboardButton("📢 廣播通知", callback_data="admin_broadcast"),
            InlineKeyboardButton("📤 匯出訂單", callback_data="admin_export"),
            InlineKeyboardButton("👥 匯出會員", callback_data="export_members"),
        ],
        # ⚙️ Settings section
        [
            InlineKeyboardButton("📢 設定正式群", callback_data="admin_set_prod"),
            InlineKeyboardButton("🧪 設定測試群", callback_data="admin_set_test"),
        ],
        # 🛑 End Auction & Settlement
        [
            InlineKeyboardButton("🛑 結束拍賣", callback_data="admin_end_auction"),
            InlineKeyboardButton("📋 當日結算", callback_data="admin_end_session"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the unified admin panel inline keyboard."""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return

    text = (
        "🏠 <b>管理員面板</b>\n\n"
        "請選擇操作："
    )

    # Delete the command message if it's a /admin call to keep chat clean
    try:
        if update.message:
            await update.message.delete()
    except Exception:
        pass

    # Send the admin panel as a new message
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=text,
        reply_markup=build_admin_keyboard(),
        parse_mode=ParseMode.HTML
    )

# --- Admin Order Management ---
async def admin_order_mgmt_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 1):
    # 1. List users with recent orders (paginated)
    # Get all users who have orders? Or just all users? 
    # "要列出全部中標用戶" -> users who have orders.
    # But get_all_users() returns all registered users.
    # Let's filter users who have orders.
    
    all_orders = await store.get_all_orders()
    user_ids_with_orders = set(o['user_id'] for o in all_orders)
    
    all_users = await store.get_all_users()
    target_users = [u for u in all_users if u['user_id'] in user_ids_with_orders]
    
    # Sort by name or ID
    target_users.sort(key=lambda u: str(u.get('name', '') or u.get('user_id')))
    
    # Pagination
    PAGE_SIZE = 10
    total_users = len(target_users)
    total_pages = (total_users + PAGE_SIZE - 1) // PAGE_SIZE
    if total_pages == 0: total_pages = 1
    
    if page < 1: page = 1
    if page > total_pages: page = total_pages
    
    start_idx = (page - 1) * PAGE_SIZE
    end_idx = start_idx + PAGE_SIZE
    current_users = target_users[start_idx:end_idx]
    
    keyboard = []
    # Generate buttons for users
    for u in current_users:
        uid = u['user_id']
        name = u.get('name') or f"ID:{uid}"
        if len(name) > 20: name = name[:18] + "..."
        keyboard.append([InlineKeyboardButton(f"👤 {name}", callback_data=f"adm_ord_user_{uid}")])
    
    # Navigation buttons
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("⬅️", callback_data=f"admin_order_mgmt_{page-1}"))
    
    nav_row.append(InlineKeyboardButton(f"📄 {page}/{total_pages}", callback_data="noop"))
    
    if page < total_pages:
        nav_row.append(InlineKeyboardButton("➡️", callback_data=f"admin_order_mgmt_{page+1}"))
        
    if nav_row:
        keyboard.append(nav_row)
    
    # Add manual search button
    # keyboard.append([InlineKeyboardButton("🔍 搜尋其他用戶 ID", callback_data="adm_ord_search")])
    
    text = f"📝 **訂單管理 - 中標用戶列表**\n共 {total_users} 位有訂單的用戶"
    
    if update.callback_query:
        await update.callback_query.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)

async def handle_admin_order_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    
    # adm_ord_user_{uid} -> Show user's orders
    if data.startswith("adm_ord_user_"):
        uid = int(data.split("_")[3])
        orders = await store.get_user_orders(uid)
        user_info = await store.get_user(uid)
        name = user_info['name'] if user_info else str(uid)
        
        if not orders:
            await query.message.edit_text(f"❌ 用戶 {name} 沒有訂單。")
            return

        keyboard = []
        # List last 10 orders
        for o in orders[:10]:
            oid = o['order_id']
            item = o['item']
            status = o['status']
            icon = "✅" if status == 'won' else ("💰" if status == 'paid' else ("🚚" if status == 'shipped' else status))
            btn_text = f"{icon} {item[:15]}..."
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"adm_ord_view_{oid}")])
        
        keyboard.append([InlineKeyboardButton("🔙 返回用戶列表", callback_data="admin_order_mgmt")])
        
        await query.message.edit_text(
            f"👤 **用戶：{name}**\n請選擇要管理的訂單：",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )

    # adm_ord_view_{oid} -> Show order details & actions
    elif data.startswith("adm_ord_view_"):
        oid = data.split("_")[3]
        # Need to find order details. Store doesn't have get_order(oid), iterate all (slow) or user's
        # Optimization: We assume we can find it in all orders or we implement get_order in Store
        # For now, let's just use get_all_orders and find (inefficient but works for MVP)
        all_orders = await store.get_all_orders()
        order = next((o for o in all_orders if o['order_id'] == oid), None)
        
        if not order:
            await query.answer("❌ 找不到訂單", show_alert=True)
            return
            
        # Display details
        status_map = {
            "won": "✅ 得標 (未付)",
            "paid": "💰 已付款",
            "shipped": "🚚 已發貨/完成",
            "pending": "⏳ 處理中",
            "cancelled": "❌ 已取消"
        }
        status_text = status_map.get(order['status'], order['status'])
        
        text = (
            f"📦 **訂單詳情**\n"
            f"🆔 `{order['order_id']}`\n"
            f"📌 商品：{order['item']}\n"
            f"💰 金額：${order['price']}\n"
            f"📅 時間：{order.get('created_at', order.get('time'))}\n"
            f"🔖 狀態：<b>{status_text}</b>"
        )
        
        # Action buttons
        keyboard = [
            [
                InlineKeyboardButton("💰 標記已付", callback_data=f"adm_ord_set_{oid}_paid"),
                InlineKeyboardButton("🚚 標記發貨", callback_data=f"adm_ord_set_{oid}_shipped")
            ],
            [
                InlineKeyboardButton("❌ 取消訂單", callback_data=f"adm_ord_set_{oid}_cancelled"),
                InlineKeyboardButton("↩️ 重置為得標", callback_data=f"adm_ord_set_{oid}_won")
            ],
            [InlineKeyboardButton("🔙 返回訂單列表", callback_data=f"adm_ord_user_{order['user_id']}")]
        ]
        
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)

    # adm_ord_set_{oid}_{status} -> Update status
    elif data.startswith("adm_ord_set_"):
        parts = data.split("_")
        oid = parts[3]
        new_status = parts[4]
        
        await store.update_order_status(oid, new_status)
        
        # Refresh view
        # We can just redirect to view
        # Re-construct data to call view logic? Or just duplicate simple logic
        
        await query.answer(f"✅ 狀態已更新為 {new_status}", show_alert=True)
        
        # Trigger view update directly
        # Recursive call logic (simulate click view)
        # Hacky but easy:
        new_data = f"adm_ord_view_{oid}"
        # Update query.data so we can re-call this handler? No, infinite recursion risk if not careful.
        # Just manually call the view logic part or simpler: re-send message
        
        # Let's just update the message content to reflect new status
        status_map = {
            "won": "✅ 得標 (未付)",
            "paid": "💰 已付款",
            "shipped": "🚚 已發貨/完成",
            "pending": "⏳ 處理中",
            "cancelled": "❌ 已取消"
        }
        
        # Re-fetch order to confirm (and get other details)
        all_orders = await store.get_all_orders()
        order = next((o for o in all_orders if o['order_id'] == oid), None)
        status_text = status_map.get(new_status, new_status) # Use new_status directly as DB might have lag? No, await is done.
        
        text = (
            f"📦 **訂單詳情**\n"
            f"🆔 `{order['order_id']}`\n"
            f"📌 商品：{order['item']}\n"
            f"💰 金額：${order['price']}\n"
            f"📅 時間：{order.get('created_at', order.get('time'))}\n"
            f"🔖 狀態：<b>{status_text}</b>"
        )
        
        # Keep same keyboard
        keyboard = [
            [
                InlineKeyboardButton("💰 標記已付", callback_data=f"adm_ord_set_{oid}_paid"),
                InlineKeyboardButton("🚚 標記發貨", callback_data=f"adm_ord_set_{oid}_shipped")
            ],
            [
                InlineKeyboardButton("❌ 取消訂單", callback_data=f"adm_ord_set_{oid}_cancelled"),
                InlineKeyboardButton("↩️ 重置為得標", callback_data=f"adm_ord_set_{oid}_won")
            ],
            [InlineKeyboardButton("🔙 返回訂單列表", callback_data=f"adm_ord_user_{order['user_id']}")]
        ]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user

    if user.id not in ADMIN_IDS:
        await query.answer("⛔ 權限不足", show_alert=True)
        return

    await query.answer()
    data = query.data

    # --- Unified Admin Panel callbacks ---
    if data == "admin_add_single":
        # Start the /new_auction flow (add single auction)
        await new_auction_start(update, context)
        return

    elif data == "admin_import_batch":
        # Show instructions for /import_batch
        await query.message.edit_text(
            "📥 <b>批次匯入格式：</b>\n\n"
            "<code>標題|起標價|一口價|圖片URL</code>\n\n"
            "範例：\n"
            "<code>JAV-001|100|500|https://example.com/1.jpg</code>\n\n"
            "請直接回覆此訊息，貼上您的拍賣品列表。",
            parse_mode=ParseMode.HTML
        )
        return

    elif data == "admin_view_queue":
        queue = current_auction.get("batch_queue", [])
        if not queue:
            await query.message.edit_text("📋 隊列是空的。使用【Import Batch】匯入拍賣品。")
        else:
            text = f"📋 <b>批次隊列</b>（{len(queue)} 件）\n\n"
            for i, item in enumerate(queue, 1):
                title = html.escape(item.get("title", "?")[:30])
                price = item.get("price", 0)
                text += f"{i}. {title}\n   💰 起標 ${price}\n"
            await query.message.edit_text(text, parse_mode=ParseMode.HTML)
        return

    elif data == "admin_schedule":
        # Prompt for datetime - show current schedule and instructions
        sched = current_auction.get("scheduled_start", "未設定")
        await query.message.edit_text(
            f"🕐 <b>排程設定</b>\n\n"
            f"當前排程：{sched}\n\n"
            "請使用指令設定：\n"
            "<code>/schedule 2026-04-02 20:00</code>",
            parse_mode=ParseMode.HTML
        )
        return

    elif data == "admin_start_batch":
        if not current_auction.get("batch_queue"):
            await query.message.edit_text("❌ 請先【Import Batch】匯入拍賣品。")
            return
        if current_auction.get("active"):
            await query.message.edit_text("❌ 已有拍賣正在進行中。")
            return
        await query.message.edit_text("🚀 正在啟動批次拍賣...")
        await start_batch_command(update, context)
        return

    elif data == "admin_pause":
        if not current_auction.get("batch_mode"):
            await query.message.edit_text("❌ 目前沒有正在進行的批次拍賣。")
            return
        if current_auction.get("batch_paused"):
            await query.message.edit_text("⚠️ 已經是暫停狀態。")
            return
        current_auction["batch_paused"] = True
        await query.message.edit_text("⏸ 批次拍賣已暫停。")
        return

    elif data == "admin_resume":
        if not current_auction.get("batch_mode"):
            await query.message.edit_text("❌ 目前沒有正在進行的批次拍賣。")
            return
        if not current_auction.get("batch_paused"):
            await query.message.edit_text("⚠️ 不是暫停狀態。")
            return
        current_auction["batch_paused"] = False
        await query.message.edit_text("▶️ 批次拍賣已恢復！")
        return

    elif data == "admin_abort":
        if not current_auction.get("batch_mode"):
            await query.message.edit_text("❌ 目前沒有正在進行的批次拍賣。")
            return
        current_auction["batch_abort"] = True
        current_auction["batch_paused"] = False
        global BATCH_PANEL_MESSAGE_ID, BATCH_PANEL_CHAT_ID
        BATCH_PANEL_MESSAGE_ID = None
        BATCH_PANEL_CHAT_ID = None
        await query.message.edit_text("🛑 批次拍賣已終止。")
        return

    elif data == "admin_batch_status":
        # Show batch status
        queue = current_auction.get("batch_queue", [])
        queue_len = len(queue)
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
                f"🕐 排程：{current_auction.get('scheduled_start', '無')}"
            )
        else:
            text = (
                f"📊 <b>批次狀態</b>\n\n"
                f"📦 隊列：{queue_len} 件\n"
                f"🕐 排程：{current_auction.get('scheduled_start', '未設定')}"
            )
        await query.message.edit_text(text, parse_mode=ParseMode.HTML)
        return

    elif data == "admin_broadcast":
        await query.message.edit_text(
            "📢 <b>廣播訊息</b>\n\n"
            "請使用指令發送：\n"
            "<code>/broadcast 今晚8點拍賣開始！</code>",
            parse_mode=ParseMode.HTML
        )
        return

    elif data == "admin_set_prod":
        prod_id = await store.get_config("prod_group_id")
        prod_id = prod_id or await store.get_config("group_id") or "未設定"
        await query.message.edit_text(
            "📢 <b>設定客戶群組</b>\n\n"
            f"當前客戶群組 ID：<code>{prod_id}</code>\n\n"
            "請在目標群組發送指令：\n"
            "<code>/set_prod_group</code>",
            parse_mode=ParseMode.HTML
        )
        return

    elif data == "admin_set_test":
        test_id = await store.get_config("test_group_id") or "未設定"
        await query.message.edit_text(
            "🧪 <b>設定測試群組</b>\n\n"
            f"當前測試群組 ID：<code>{test_id}</code>\n\n"
            "請在目標群組發送指令：\n"
            "<code>/set_test_group</code>",
            parse_mode=ParseMode.HTML
        )
        return

    elif data == "admin_end_auction":
        if not current_auction["active"]:
            await query.message.edit_text("❌ 當前沒有進行中的拍賣。")
            return
        if current_auction["timer_task"]:
            current_auction["timer_task"].cancel()
            current_auction["timer_task"] = None
        await end_auction(context.bot)
        await query.message.edit_text("✅ 已強制結束拍賣。")
        return

    # --- Legacy callbacks ---
    if query.data.startswith("admin_order_mgmt"):
        page = 1
        parts = query.data.split("_")
        if len(parts) >= 4 and parts[3].isdigit():
            page = int(parts[3])

        await admin_order_mgmt_menu(update, context, page)

    elif query.data.startswith("adm_ord_"):
        await handle_admin_order_action(update, context)

    elif query.data == "admin_force_end":
        if not current_auction["active"]:
            await query.message.reply_text("❌ 當前沒有進行中的拍賣。")
            return

        if current_auction["timer_task"]:
            current_auction["timer_task"].cancel()
            current_auction["timer_task"] = None

        await end_auction(context.bot)
        await query.message.reply_text("✅ 已強制結束拍賣。")

    elif query.data == "admin_end_session":
        if current_auction["active"]:
            await query.message.edit_text("❌ 請先結束當前進行中的拍賣，再進行結算。")
            return

        # Get date options
        today = datetime.now()
        yesterday = (today - timedelta(days=1)).strftime('%Y-%m-%d')
        two_days_ago = (today - timedelta(days=2)).strftime('%Y-%m-%d')
        today_str = today.strftime('%Y-%m-%d')

        keyboard = [
            [InlineKeyboardButton(f"📅 今日 ({today_str})", callback_data="settle_date_" + today_str)],
            [InlineKeyboardButton(f"📅 昨日 ({yesterday})", callback_data="settle_date_" + yesterday)],
            [InlineKeyboardButton(f"📅 前日 ({two_days_ago})", callback_data="settle_date_" + two_days_ago)],
            [InlineKeyboardButton("❌ 取消", callback_data="cancel_end_session")]
        ]
        await query.message.edit_text(
            "📅 <b>選擇結算日期</b>\n\n"
            "請選擇要結算的日期：",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
        return

    elif query.data.startswith("settle_date_"):
        date_str = query.data.replace("settle_date_", "")
        # Store date in user_data for processing
        context.user_data['settle_date'] = date_str
        
        # Show confirmation
        keyboard = [
            [InlineKeyboardButton("✅ 確認結算並發送帳單", callback_data="confirm_settle_date")],
            [InlineKeyboardButton("❌ 取消", callback_data="cancel_end_session")]
        ]
        await query.message.edit_text(
            f"⚠️ <b>確認結算 {date_str} 的訂單？</b>\n\n"
            "這將會：\n1. 統計該日所有中標訂單\n2. 按用戶合併訂單\n3. 自動私訊發送總帳單給每位中標者\n\n此操作不可撤銷。",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
        return

    elif query.data == "confirm_settle_date":
        date_str = context.user_data.get('settle_date', datetime.now().strftime('%Y-%m-%d'))
        await process_settlement_by_date(update, context, date_str)
        return

    elif query.data == "cancel_end_session":
        await query.message.edit_text("已取消結算操作。")
        return

    elif query.data == "admin_export":
        await export_data(update, context)
        return

    elif query.data == "export_members":
        await export_members(update, context)
        return

    elif query.data == "admin_batch_menu":
        await show_batch_admin_panel(context.bot, chat_id=query.message.chat_id)
        return

    elif query.data == "admin_back":
        await admin_menu(update, context)
        return

    elif query.data == "admin_status":
        import platform
        from datetime import timedelta, timezone

        status = "🟢 運行中" if current_auction["active"] else "⚪ 閒置"
        db_type = "PostgreSQL 🐘" if store.is_pg else "SQLite/JSON 📁 (本地)"
        db_conn_str = DATABASE_URL

        if db_conn_str:
            parts = db_conn_str.split("@")
            if len(parts) > 1:
                db_conn_str = f"...@{parts[1]}"
            else:
                db_conn_str = "********"
        else:
            db_conn_str = store.db_file

        sys_info = f"OS: {platform.system()} {platform.release()}\n"
        tz_offset = timedelta(hours=8)
        now_taipei = datetime.now(timezone(tz_offset)).strftime('%Y-%m-%d %H:%M')
        sys_info += f"Time: {now_taipei} (UTC+8)\n"

        all_users = await store.get_all_users()
        msg = (
            f"ℹ️ <b>系統狀態概覽</b>\n"
            f"━━━━━━━━━━\n"
            f"🤖 <b>Bot 狀態</b>: {status}\n"
            f"💾 <b>資料庫類型</b>: {db_type}\n"
            f"🔗 <b>連接字串</b>: {html.escape(db_conn_str)}\n"
            f"⚠️ <b>持久化狀態</b>: {'✅ 安全' if store.is_pg else '⚠️ 危險 (重啟丟失)'}\n\n"
            f"👥 <b>註冊用戶</b>: {len(all_users)} 人\n"
            f"🖥 <b>運行環境</b>:\n<pre>{sys_info}</pre>\n"
        )

        if not store.is_pg:
            msg += "\n🚨 <b>警告</b>: 當前使用本地文件。在 Zeabur 等雲環境下，每次部署/重啟都會清除數據！請務必配置 PostgreSQL 服務。"

        await query.message.edit_text(msg, parse_mode=ParseMode.HTML)
        return

    elif query.data.startswith("admin_order_mgmt"):
        page = 1
        parts = query.data.split("_")
        if len(parts) >= 4 and parts[3].isdigit():
            page = int(parts[3])
        await admin_order_mgmt_menu(update, context, page)
        return

    elif query.data.startswith("adm_ord_"):
        await handle_admin_order_action(update, context)
        return

    elif query.data == "admin_force_end":
        if not current_auction["active"]:
            await query.message.edit_text("❌ 當前沒有進行中的拍賣。")
            return
        if current_auction["timer_task"]:
            current_auction["timer_task"].cancel()
            current_auction["timer_task"] = None
        await end_auction(context.bot)
        await query.message.edit_text("✅ 已強制結束拍賣。")
        return

    # Show admin panel for any unmatched admin callbacks
    await admin_menu(update, context)


async def force_end_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return

    if not current_auction["active"]:
        await update.message.reply_text("❌ 當前沒有進行中的拍賣。")
        return

    # Cancel timer task if running
    if current_auction["timer_task"]:
        current_auction["timer_task"].cancel()
        current_auction["timer_task"] = None
    
    # Manually trigger end
    await end_auction(context.bot)
    await update.message.reply_text("✅ 已強制結束拍賣。")


async def handle_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📜 拍賣規則":
        await help_command(update, context)
    elif text == "👤 我的資料":
        await user_info_command(update, context)
    elif text == "📍 取貨地址":
        await update.message.reply_text(
            "📍 <b>取貨地址</b>\n\n"
            "旺角西洋菜南街72號3樓\n"
            "（OK右手邊門口上）\n\n"
            "營業時間：星期一至星期六\n"
            "星期日休息\n\n"
            "請於得標後聯絡管理員安排取貨時間。",
            parse_mode=ParseMode.HTML
        )
    elif text == "🔧 管理員選單":
        await admin_menu(update, context)
    else:
        if current_auction["active"] and text.isdigit():
            await handle_text_bid(update, context)

async def handle_text_bid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not current_auction["active"] or not msg.text:
        return
        
    if msg.chat_id != current_auction["chat_id"]:
        return

    # Check if this is a reply to the custom bid prompt
    is_valid_reply = False
    if msg.reply_to_message and msg.reply_to_message.from_user.is_bot:
        # 使用 endswith 以兼容帶有用戶名的提示訊息 (ForceReply 會引用該訊息)
        if msg.reply_to_message.text and msg.reply_to_message.text.endswith(CUSTOM_BID_PROMPT):
            is_valid_reply = True
            # Delete the prompt message to clean up
            try:
                await msg.reply_to_message.delete()
            except:
                pass

    # If user wants to DISABLE direct text bidding, we only allow valid replies
    if not is_valid_reply:
        return

    text = msg.text.strip()
    if not text.isdigit():
        if is_valid_reply:
             await msg.reply_text("❌ 請輸入純數字。")
        return 
        
    bid_price = int(text)
    user = msg.from_user

    if not await store.is_registered(user.id):
        # Optional: Prompt to register if they try to bid
        return 
        
    await process_blind_bid(user, bid_price, None, context.bot)
    try:
        await msg.delete()
    except:
        pass

async def process_blind_bid(user, price, query=None, bot=None):
    # Use lock to prevent race conditions
    async with auction_lock:
        # 暗標拍賣：唔會即時更新 public display，淨係儲存 pending bid
        # 每人只能出一次價，價錢任意，最後 reveal 時價高者得

        # Issue 1 fix: if auction is in the process of ending (end_auction running),
        # extend time by 2s to accept the bid rather than rejecting it outright.
        if current_auction.get("_ending"):
            current_auction["end_time"] = datetime.now().timestamp() + 2
            logger.info(f"Late bid accepted; auction extended by 2s (user {user.id})")

        existing_bids = current_auction.get("bidders", [])
        if any(b["id"] == user.id for b in existing_bids):
            if query: await query.answer("❌ 你已經出過價了", show_alert=True)
            return

        # Store as pending (not yet public) and track bidder
        current_auction["pending_price"] = price
        current_auction["pending_bidder"] = user.id
        current_auction["pending_bidder_name"] = user.first_name
        current_auction["bidders"].append({"id": user.id, "name": user.first_name, "price": price, "time": datetime.now().timestamp()})
        
        # Check Buy It Now
        bin_price = current_auction.get("bin_price", 0)
        if bin_price > 0 and price >= bin_price:
            # End auction immediately
            current_auction["end_time"] = datetime.now().timestamp()
            if current_auction.get("timer_task"):
                current_auction["timer_task"].cancel()
            target_bot = bot if bot else (query.bot if query else None)
            if target_bot:
                await end_auction(target_bot)
            if query:
                await query.answer(f"⚡️ 一口價成交！恭喜您！", show_alert=True)
            return
        
        # Anti-sniping disabled per user request

async def notify_previous_bidder(bot, previous_bidder_id, title, new_price, new_bidder_name):
    try:
        target_bot = bot
        if not target_bot:
            return
        
        if target_bot:
            notify_text = (
                f"⚠️ <b>您的出價已被超越！</b>\n\n"
                f"📦 商品：{html.escape(title)}\n"
                f"💰 當前暗標價：<b>${new_price}</b>\n"
                f"👑 最高出價者：{html.escape(new_bidder_name)}\n\n"
                f"👇 立即私訊機器人反擊！"
            )
            
            await target_bot.send_message(
                chat_id=previous_bidder_id,
                text=notify_text,
                parse_mode=ParseMode.HTML
            )
    except Exception as e:
        logger.warning(f"Failed to notify outbid user {previous_bidder_id}: {e}")

async def start_next_queued_auction(bot):
    queue = await store.get_auction_queue()
    if not queue:
        return
    item = queue.pop(0)
    await store.set_auction_queue(queue)

    await asyncio.sleep(10)

    if current_auction["active"]:
        queue.insert(0, item)
        await store.set_auction_queue(queue)
        return

    await start_auction_from_queue(bot, item)

async def start_auction_from_queue(bot, item):
    if current_auction["active"]:
        return

    title = item.get("title", "未知商品")
    price = int(item.get("price", 0))
    bin_price = int(item.get("bin_price", 0))
    photo_id = item.get("photo_id")
    target_chat_id = item.get("target_chat_id")

    if not photo_id or not target_chat_id:
        return

    session_id, session_seq = await store.get_next_session()
    current_auction["active"] = True
    current_auction["title"] = title
    current_auction["base_price"] = price
    current_auction["current_price"] = price
    current_auction["pending_price"] = price   # 暗標：pending = base price initially
    current_auction["pending_bidder"] = None
    current_auction["pending_bidder_name"] = "無"
    current_auction["bidders"] = []
    current_auction["bin_price"] = bin_price
    current_auction["photo_id"] = photo_id
    current_auction["highest_bidder"] = None
    current_auction["highest_bidder_name"] = "無"
    current_auction["start_time"] = datetime.now()
    current_auction["end_time"] = datetime.now().timestamp() + ITEM_DURATION
    current_auction["session_id"] = session_id
    current_auction["session_seq"] = session_seq
    current_auction["chat_id"] = target_chat_id
    if current_auction.get("update_event"):
        current_auction["update_event"].clear()

    # Get bot username for deep linking
    try:
        me = await bot.get_me()
        current_auction["bot_username"] = me.username
    except Exception as e:
        logger.error(f"Failed to get bot username: {e}")

    text = generate_auction_text(ITEM_DURATION)
    keyboard = generate_bid_keyboard(price)

    msg = await bot.send_photo(
        chat_id=target_chat_id,
        photo=photo_id,
        caption=text,
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML
    )
    current_auction["message_id"] = msg.message_id
    current_auction["timer_task"] = asyncio.create_task(auction_timer_loop(bot))

async def end_auction(bot):
    # Issue 1 fix: mark auction as "ending" before releasing lock so that
    # process_blind_bid can detect it and extend time instead of rejecting.
    current_auction["_ending"] = True

    bidders = current_auction.get("bidders", [])
    # Sort by price descending, then by time ascending (tie = earliest wins)
    sorted_bidders = sorted(bidders, key=lambda x: (-x["price"], x.get("time", 0)))

    # Determine winner: highest bidder (first in sorted list)
    if sorted_bidders:
        winner = sorted_bidders[0]
        winner_id = winner["id"]
        winner_name = winner["name"]
        price = winner["price"]
    else:
        winner_id = None
        winner_name = "無"
        price = 0

    current_auction["active"] = False
    current_auction["current_price"] = price
    current_auction["highest_bidder"] = winner_id
    current_auction["highest_bidder_name"] = winner_name
    title = current_auction["title"]

    # Build bidders list text
    if sorted_bidders:
        bidders_lines = "\n".join(
            f"  {i+1}. {html.escape(b['name'])} — <b>${b['price']}</b>"
            for i, b in enumerate(sorted_bidders)
        )
        bidders_text = f"\n📋 <b>投標記錄：</b>\n{bidders_lines}\n"
    else:
        bidders_text = "\n📋 沒有投標者"

    final_text = (
        f"🛑 <b>拍賣結束！</b> 🛑\n\n"
        f"📦 {html.escape(title)}\n"
        f"💰 最終成交價：<b>${price}</b>\n"
        f"🏆 得標者：{html.escape(winner_name)}\n"
        f"{bidders_text}\n"
        f"系統將自動發送結算連結給得標者。"
    )
    
    # Issue 3 fix: retry once on 429 (rate limit) after 5s
    edit_ok = False
    try:
        await bot.edit_message_caption(
            chat_id=current_auction["chat_id"],
            message_id=current_auction["message_id"],
            caption=final_text,
            reply_markup=None,
            parse_mode=ParseMode.HTML
        )
        edit_ok = True
    except Exception as e:
        err_str = str(e)
        logger.warning(f"Failed to edit auction message: {e}")
        # Retry once on rate limit
        if "429" in err_str:
            logger.info("Rate limited (429); waiting 5s before retry...")
            await asyncio.sleep(5)
            try:
                await bot.edit_message_caption(
                    chat_id=current_auction["chat_id"],
                    message_id=current_auction["message_id"],
                    caption=final_text,
                    reply_markup=None,
                    parse_mode=ParseMode.HTML
                )
                edit_ok = True
            except Exception as e2:
                logger.error(f"Retry also failed: {e2}")
        # If not a 429, also try fallback below
        # Fallback: Send a new message only if edit truly failed (not rate-limited and retried)
        if not edit_ok:
            try:
                await bot.send_message(
                    chat_id=current_auction["chat_id"],
                    text=final_text,
                    parse_mode=ParseMode.HTML
                )
            except Exception as e2:
                logger.error(f"Failed to send fallback message: {e2}")
    
    if winner_id:
        order = {
            "order_id": f"ORD-{int(datetime.now().timestamp())}",
            "user_id": winner_id,
            "item": title,
            "price": price,
            "time": datetime.now().isoformat(),
            "status": "pending",
            "session_id": current_auction.get("session_id")
        }
        await store.add_order(order)
        
        try:
            user_info = await store.get_user(winner_id)
            
            # Update winner message: No payment link, just email notification
            msg = (
                f"🎉 恭喜您標得 <b>{html.escape(title)}</b>！\n\n"
                f"金額：${price}\n"
                f"交收：{html.escape(user_info.get('pickup', '未定'))}\n\n"
                f"ℹ️ <b>付款安排</b>：\n"
                f"拍賣結束後，我們會另外再發送付款連結到您的 Email，請留意查收。"
            )
            await bot.send_message(chat_id=winner_id, text=msg, parse_mode=ParseMode.HTML)
            
            # 發送郵件通知 (如果用戶有提供 Email)
            user_email = user_info.get('email')
            if user_email:
                email_subject = f"得標通知：{title}"
                email_body = f"""
                恭喜您標得 {title}！
                
                成交價：${price}
                
                請等待我們發送正式付款連結。
                
                OpenClaw 拍賣系統
                """
                
                # Use asyncio.to_thread to prevent blocking the event loop
                asyncio.create_task(asyncio.to_thread(send_email, user_email, email_subject, email_body))

        except Exception as e:
            logger.error(f"Failed to DM winner: {e}")
            await bot.send_message(
                chat_id=current_auction["chat_id"], 
                text=f"⚠️ 無法私聊得標者 (ID: {winner_id})，請主動聯繫管理員。"
            )

    # Issue 2 fix: persist state and reset ending flag
    current_auction["_ending"] = False
    save_auction_state()
    
    # Check if batch mode is active and auto-advance to next item
    if current_auction.get("batch_mode") and not current_auction.get("batch_abort"):
        asyncio.create_task(run_batch_auction_loop(bot))
    else:
        await start_next_queued_auction(bot)


# ============================================================
# BATCH AUCTION SYSTEM
# ============================================================

async def download_image_to_file_id(bot, url: str) -> str:
    """Download an image from URL and send it to bot's own chat to get a file_id."""
    import urllib.request
    import tempfile
    
    try:
        # Download image
        with urllib.request.urlopen(url, timeout=10) as response:
            image_data = response.read()
        
        # Get file extension from content-type or url
        content_type = response.headers.get('Content-Type', 'image/jpeg')
        ext = '.jpg'
        if 'png' in content_type:
            ext = '.png'
        elif 'gif' in content_type:
            ext = '.gif'
        elif 'webp' in content_type:
            ext = '.webp'
        
        # Save to temp file
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(image_data)
            tmp_path = tmp.name
        
        # Send to bot's own chat to get file_id
        admin_id = ADMIN_IDS[0] if ADMIN_IDS else None
        if not admin_id:
            logger.error("No admin ID configured for photo download")
            return None
        
        with open(tmp_path, 'rb') as f:
            msg = await bot.send_photo(chat_id=admin_id, photo=f)
        
        # Clean up temp file
        os.unlink(tmp_path)
        
        return msg.photo[-1].file_id
        
    except Exception as e:
        logger.error(f"Failed to download image from {url}: {e}")
        return None

async def run_batch_auction_loop(bot):
    """Main loop for batch auction - runs after each item ends."""
    # Wait for pause between items
    await asyncio.sleep(PAUSE_BETWEEN_ITEMS)

    # Check if abort was requested while waiting
    if current_auction.get("batch_abort"):
        await notify_batch_aborted(bot)
        return

    # Check if paused
    if current_auction.get("batch_paused"):
        # Wait until resumed
        while current_auction.get("batch_paused") and not current_auction.get("batch_abort"):
            await asyncio.sleep(1)
        if current_auction.get("batch_abort"):
            await notify_batch_aborted(bot)
            return

    # Increment index for the item we're about to start (0-based to 1-based)
    current_auction["batch_current_index"] += 1
    
    if current_auction["batch_current_index"] > len(current_auction["batch_queue"]):
        # Batch complete
        await notify_batch_complete(bot)
        return

    item = current_auction["batch_queue"][current_auction["batch_current_index"] - 1]  # -1 to convert 1-based index back to 0-based
    await start_single_batch_item(bot, item)


async def start_single_batch_item(bot, item):
    """Start a single auction item from the batch queue."""
    if current_auction.get("batch_abort"):
        return

    title = item.get("title", "未知商品")
    price = int(item.get("price", 0))
    bin_price = int(item.get("bin_price", 0))
    photo_id = item.get("photo_id")
    target_chat_id = item.get("target_chat_id")

    if not photo_id or not target_chat_id:
        logger.error(f"Batch item missing photo_id or target_chat_id: {title}")
        # Error path: increment index then move to next
        current_auction["batch_current_index"] += 1
        asyncio.create_task(run_batch_auction_loop(bot))
        return

    # Get session
    session_id, session_seq = await store.get_next_session()

    # Reset auction state for new item
    current_auction["active"] = True
    current_auction["title"] = title
    current_auction["base_price"] = price
    current_auction["current_price"] = price
    current_auction["pending_price"] = price
    current_auction["pending_bidder"] = None
    current_auction["pending_bidder_name"] = "無"
    current_auction["bidders"] = []
    current_auction["bin_price"] = bin_price
    current_auction["photo_id"] = photo_id
    current_auction["highest_bidder"] = None
    current_auction["highest_bidder_name"] = "無"
    current_auction["start_time"] = datetime.now()
    current_auction["end_time"] = datetime.now().timestamp() + ITEM_DURATION
    current_auction["session_id"] = session_id
    current_auction["session_seq"] = session_seq
    current_auction["chat_id"] = target_chat_id
    current_auction["_ending"] = False
    if current_auction.get("update_event"):
        current_auction["update_event"].clear()

    # Get bot username for deep linking
    try:
        me = await bot.get_me()
        current_auction["bot_username"] = me.username
    except Exception as e:
        logger.error(f"Failed to get bot username: {e}")

    # Generate auction message
    text = generate_auction_text(ITEM_DURATION)
    keyboard = generate_bid_keyboard(price)

    try:
        msg = await bot.send_photo(
            chat_id=target_chat_id,
            photo=photo_id,
            caption=text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML
        )
        current_auction["message_id"] = msg.message_id
        
        # Cancel existing timer task if any
        if current_auction.get("timer_task"):
            current_auction["timer_task"].cancel()
        
        current_auction["timer_task"] = asyncio.create_task(auction_timer_loop(bot))
        
        # Mark this item as started (0-based index, will be shown as +1 in notifications)
        # Index will be incremented AFTER this item ends in run_batch_auction_loop
        
        # Notify batch progress to admin (shows current item number)
        await notify_batch_progress(bot)
        
    except Exception as e:
        logger.error(f"Failed to start batch item '{title}': {e}")
        # Move to next item on error
        current_auction["batch_current_index"] += 1
        asyncio.create_task(run_batch_auction_loop(bot))


async def notify_batch_progress(bot):
    """Notify admin of batch progress and update the admin panel."""
    # Update the admin panel
    await show_batch_admin_panel(bot)

    # Also send a detailed progress message
    queue_len = len(current_auction["batch_queue"])
    current_idx = current_auction["batch_current_index"] + 1  # 1-indexed for display
    title = current_auction.get("title", "?")
    
    # Try to find admin chat_id from config or use first admin
    admin_id = ADMIN_IDS[0] if ADMIN_IDS else None
    
    if admin_id:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=f"📦 <b>批次拍賣進度</b>\n\n"
                     f"項目：{current_idx}/{queue_len}\n"
                     f"當前：{html.escape(title)}\n"
                     f"模式：{'運行中' if not current_auction.get('batch_paused') else '⏸ 已暫停'}",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.warning(f"Failed to notify admin of batch progress: {e}")


async def notify_batch_complete(bot):
    """Notify when batch auction is complete."""
    total_items = len(current_auction["batch_queue"])
    admin_id = ADMIN_IDS[0] if ADMIN_IDS else None
    
    # Reset batch state
    current_auction["batch_mode"] = False
    current_auction["batch_queue"] = []
    current_auction["batch_current_index"] = 0
    current_auction["batch_paused"] = False
    current_auction["batch_abort"] = False
    current_auction["scheduled_start"] = None
    
    # Clear admin panel tracking
    global BATCH_PANEL_MESSAGE_ID, BATCH_PANEL_CHAT_ID
    BATCH_PANEL_MESSAGE_ID = None
    BATCH_PANEL_CHAT_ID = None
    
    if admin_id:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=f"✅ <b>批次拍賣完成！</b>\n\n共完成 {total_items} 件拍賣品",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.warning(f"Failed to notify admin of batch complete: {e}")


async def notify_batch_aborted(bot):
    """Notify when batch auction is aborted."""
    admin_id = ADMIN_IDS[0] if ADMIN_IDS else None
    
    # Reset batch state
    current_auction["batch_mode"] = False
    current_auction["batch_queue"] = []
    current_auction["batch_current_index"] = 0
    current_auction["batch_paused"] = False
    current_auction["batch_abort"] = False
    current_auction["scheduled_start"] = None
    
    if admin_id:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text="🛑 <b>批次拍賣已終止</b>\n\n隊列已清空。",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.warning(f"Failed to notify admin of batch abort: {e}")


# --- Batch Auction Commands ---

async def import_batch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /import_batch command - accepts CSV-style text input."""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ 權限不足")
        return

    # Get the text after the command or expect it as a reply
    text = update.message.text.strip()
    
    # If text starts with /import_batch alone, ask for input
    if text == "/import_batch" or text.startswith("/import_batch "):
        if text.startswith("/import_batch "):
            text = text[len("/import_batch "):].strip()
        else:
            # Show format instructions
            await update.message.reply_text(
                "📥 <b>批次匯入格式：</b>\n\n"
                "<code>標題|起標價|一口價|圖片URL</code>\n\n"
                "範例：\n"
                "<code>JAV-001|100|500|https://example.com/1.jpg</code>\n"
                "<code>JAV-002|100|500|https://example.com/2.jpg</code>\n\n"
                "請直接回覆此訊息，貼上您的拍賣品列表。",
                parse_mode=ParseMode.HTML
            )
            # Store state to expect next message... but for simplicity,
            # let's use a different approach: accept multi-line input directly
            # Or accept reply to this message
            return

    # If empty, ask for input
    if not text:
        await update.message.reply_text(
            "📥 <b>請輸入拍賣品列表：</b>\n\n"
            "格式：<code>標題|起標價|一口價|圖片URL</code>\n\n"
            "範例：\n"
            "<code>JAV-001|100|500|https://example.com/1.jpg</code>",
            parse_mode=ParseMode.HTML
        )
        return

    # Parse CSV-style input
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    parsed_items = []
    errors = []

    for i, line in enumerate(lines, 1):
        parts = line.split('|')
        if len(parts) != 4:
            errors.append(f"第 {i} 行：格式錯誤，應為 4 個欄位（標題|起標價|一口價|圖片URL）")
            continue
        
        title, price_str, bin_price_str, photo_url = parts
        title = title.strip()
        price_str = price_str.strip()
        bin_price_str = bin_price_str.strip()
        photo_url = photo_url.strip()

        try:
            price = int(price_str)
            bin_price = int(bin_price_str)
        except ValueError:
            errors.append(f"第 {i} 行：價格必須是數字")
            continue

        if price <= 0:
            errors.append(f"第 {i} 行：起標價必須大於 0")
            continue

        # Validate URL format (basic check)
        if not photo_url.startswith(('http://', 'https://')):
            errors.append(f"第 {i} 行：圖片URL格式不正確")
            continue

        # For batch import, we need to download the image and get file_id
        # This requires the photo URL to be accessible and downloaded
        # We'll store the URL and download it when starting the auction
        parsed_items.append({
            "title": title,
            "price": price,
            "bin_price": bin_price,
            "photo_url": photo_url,  # Store URL for download later
        })

    if errors:
        error_text = "\n".join(errors)
        await update.message.reply_text(
            f"⚠️ <b>匯入時發生錯誤：</b>\n\n{error_text}",
            parse_mode=ParseMode.HTML
        )
        return

    if not parsed_items:
        await update.message.reply_text("❌ 沒有有效的拍賣品資料。")
        return

    # Store in current_auction batch_queue (without photo_id yet - need to download)
    # For now, store the items - photo download will happen at start_batch time
    current_auction["batch_queue"] = parsed_items
    current_auction["batch_mode"] = False  # Will be set to True when started
    current_auction["batch_current_index"] = 0
    current_auction["batch_paused"] = False
    current_auction["batch_abort"] = False

    await update.message.reply_text(
        f"✅ <b>已匯入 {len(parsed_items)} 件拍賣品：</b>\n\n" +
        "\n".join(f"{i+1}. {html.escape(item['title'])} - 起標 ${item['price']}" for i, item in enumerate(parsed_items)),
        parse_mode=ParseMode.HTML
    )
    # Show admin batch control panel
    await show_batch_admin_panel(context.bot, chat_id=update.effective_chat.id)


async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /schedule command - set batch auction start time."""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ 權限不足")
        return

    if not current_auction.get("batch_queue"):
        await update.message.reply_text("❌ 請先使用 <code>/import_batch</code> 匯入拍賣品。", parse_mode=ParseMode.HTML)
        return

    args = context.args
    if not args:
        # Show current schedule or prompt for datetime
        if current_auction.get("scheduled_start"):
            sched_time = current_auction["scheduled_start"]
            await update.message.reply_text(
                f"📅 <b>已設定拍賣時間：</b>\n{sched_time}\n\n"
                f"使用 <code>/start_batch</code> 可立即開始（跳過排程）。",
                parse_mode=ParseMode.HTML
            )
        else:
            await update.message.reply_text(
                "📅 <b>請輸入拍賣開始時間：</b>\n\n"
                "格式：<code>/schedule 2026-04-02 20:00</code>",
                parse_mode=ParseMode.HTML
            )
        return

    # Parse datetime
    datetime_str = " ".join(args)
    try:
        # Try common formats
        for fmt in ["%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%d-%m-%Y %H:%M"]:
            try:
                scheduled_dt = datetime.strptime(datetime_str, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError("Unknown format")
    except ValueError:
        await update.message.reply_text(
            "❌ <b>時間格式錯誤</b>\n\n"
            "正確格式：<code>/schedule 2026-04-02 20:00</code>",
            parse_mode=ParseMode.HTML
        )
        return

    # Check if time is in the future
    now = datetime.now()
    if scheduled_dt <= now:
        await update.message.reply_text("❌ 開始時間必須是未來的時間。")
        return

    # Set scheduled time
    current_auction["scheduled_start"] = scheduled_dt.strftime("%Y-%m-%d %H:%M")

    # Calculate estimated end time
    queue_len = len(current_auction["batch_queue"])
    # Each item: ITEM_DURATION (25s) + PAUSE_BETWEEN_ITEMS (3s) = 28s
    # Last item doesn't need pause after
    total_duration_seconds = queue_len * ITEM_DURATION + (queue_len - 1) * PAUSE_BETWEEN_ITEMS
    estimated_end_dt = scheduled_dt + timedelta(seconds=total_duration_seconds)

    # Get target group info
    target_type = current_auction.get("batch_target_group", "正式")
    target_desc = f"【{target_type}群組】" if target_type else "未設定"

    await update.message.reply_text(
        f"✅ <b>拍賣時間已設定：</b>\n\n"
        f"📦 件數：{queue_len} 件\n"
        f"🕐 開始時間：{scheduled_dt.strftime('%Y-%m-%d %H:%M')}\n"
        f"🕐 預計結束：{estimated_end_dt.strftime('%Y-%m-%d %H:%M')}\n"
        f"📢 發佈群組：{target_desc}",
        parse_mode=ParseMode.HTML
    )
    # Show admin batch control panel with scheduled state
    await show_batch_admin_panel(context.bot, chat_id=update.effective_chat.id)


async def start_batch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start_batch command - start the batch auction immediately or at scheduled time."""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ 權限不足")
        return

    if not current_auction.get("batch_queue"):
        await update.message.reply_text("❌ 請先使用 <code>/import_batch</code> 匯入拍賣品。", parse_mode=ParseMode.HTML)
        return

    if current_auction.get("active"):
        await update.message.reply_text("❌ 已有拍賣正在進行中，請先結束後再試。")
        return

    # Check if there's a scheduled time and if it's reached
    scheduled_start = current_auction.get("scheduled_start")
    if scheduled_start:
        try:
            sched_dt = datetime.strptime(scheduled_start, "%Y-%m-%d %H:%M")
            now = datetime.now()
            if sched_dt > now:
                # Not yet time - calculate wait time
                wait_seconds = (sched_dt - now).total_seconds()
                await update.message.reply_text(
                    f"⏳ 拍賣已排程至 {scheduled_start}\n"
                    f"距離開始還有約 {int(wait_seconds/60)} 分鐘\n\n"
                    f"如要立即開始，請先使用 <code>/schedule</code> 清除排程，然後再次呼叫 <code>/start_batch</code>",
                    parse_mode=ParseMode.HTML
                )
                return
        except ValueError:
            pass  # Invalid format, proceed with immediate start

    # Determine target group
    target_type = current_auction.get("batch_target_group", "prod")
    if target_type == "test":
        target_chat_id = await store.get_config("test_group_id")
        target_desc = "測試群組"
    else:
        target_chat_id = await store.get_config("prod_group_id")
        if not target_chat_id:
            target_chat_id = await store.get_config("group_id")
        target_desc = "客戶群組"

    if not target_chat_id:
        await update.message.reply_text(f"❌ 尚未設定【{target_desc}】！\n請先在目標群組輸入 /set_{'test_' if target_type=='test' else 'prod_'}group")
        return

    # Set batch mode
    current_auction["batch_mode"] = True
    current_auction["batch_current_index"] = 0
    current_auction["batch_paused"] = False
    current_auction["batch_abort"] = False

    # Add target_chat_id to each item in queue
    for item in current_auction["batch_queue"]:
        item["target_chat_id"] = target_chat_id

    # Get bot instance for the batch loop
    bot = context.bot

    # Pre-download all images if they are URLs
    await update.message.reply_text("📥 正在下載圖片中...")
    for i, item in enumerate(current_auction["batch_queue"]):
        if item.get("photo_url") and not item.get("photo_id"):
            photo_id = await download_image_to_file_id(bot, item["photo_url"])
            if photo_id:
                item["photo_id"] = photo_id
                logger.info(f"Downloaded photo for: {item['title']}")
            else:
                # Use a placeholder or skip
                logger.error(f"Failed to download photo for: {item['title']}")
                await update.message.reply_text(
                    f"⚠️ 無法下載第 {i+1} 件的圖片：{item['title']}\n"
                    f"URL: {item['photo_url']}",
                    parse_mode=ParseMode.HTML
                )
        # Add target_type for reference
        item["target_type"] = target_type

    # Start the first item immediately
    queue_len = len(current_auction["batch_queue"])
    await update.message.reply_text(
        f"🚀 <b>批次拍賣開始！</b>\n\n"
        f"📦 件數：{queue_len} 件\n"
        f"📢 發佈群組：{target_desc}\n\n"
        f"第一件拍賣品即將開始...",
        parse_mode=ParseMode.HTML
    )

    # Reset admin panel tracking so it sends a new message
    global BATCH_PANEL_MESSAGE_ID, BATCH_PANEL_CHAT_ID
    BATCH_PANEL_MESSAGE_ID = None
    BATCH_PANEL_CHAT_ID = None

    # Show admin batch control panel
    await show_batch_admin_panel(bot, chat_id=update.effective_chat.id)

    # Start first item
    item = current_auction["batch_queue"][0]
    await start_single_batch_item(bot, item)


async def pause_batch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /pause_batch command - pause the batch auction."""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ 權限不足")
        return

    if not current_auction.get("batch_mode"):
        await update.message.reply_text("❌ 目前沒有正在進行的批次拍賣。")
        return

    if current_auction.get("batch_paused"):
        await update.message.reply_text("⚠️ 批次拍賣已經是暫停狀態。")
        return

    current_auction["batch_paused"] = True
    
    await update.message.reply_text(
        f"⏸ <b>批次拍賣已暫停</b>",
        parse_mode=ParseMode.HTML
    )
    # Update admin panel
    await show_batch_admin_panel(context.bot, chat_id=update.effective_chat.id)


async def resume_batch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /resume_batch command - resume the batch auction."""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ 權限不足")
        return

    if not current_auction.get("batch_mode"):
        await update.message.reply_text("❌ 目前沒有正在進行的批次拍賣。")
        return

    if not current_auction.get("batch_paused"):
        await update.message.reply_text("⚠️ 批次拍賣不是在暫停狀態。")
        return

    current_auction["batch_paused"] = False
    
    await update.message.reply_text(
        f"▶️ <b>批次拍賣已恢復！</b>",
        parse_mode=ParseMode.HTML
    )
    # Update admin panel
    await show_batch_admin_panel(context.bot, chat_id=update.effective_chat.id)


async def abort_batch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /abort_batch command - abort the entire batch auction."""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ 權限不足")
        return

    if not current_auction.get("batch_mode"):
        await update.message.reply_text("❌ 目前沒有正在進行的批次拍賣。")
        return

    current_auction["batch_abort"] = True
    current_auction["batch_paused"] = False  # Unpause so loop can exit

    await update.message.reply_text(
        f"🛑 <b>批次拍賣已終止</b>",
        parse_mode=ParseMode.HTML
    )
    # Clear admin panel
    global BATCH_PANEL_MESSAGE_ID, BATCH_PANEL_CHAT_ID
    BATCH_PANEL_MESSAGE_ID = None
    BATCH_PANEL_CHAT_ID = None


async def batch_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /batch_status command - show batch queue progress."""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ 權限不足")
        return

    if not current_auction.get("batch_mode"):
        # Show queue status even if not started
        queue = current_auction.get("batch_queue", [])
        if not queue:
            await update.message.reply_text("❌ 目前沒有任何批次拍賣品。\n使用 <code>/import_batch</code> 匯入拍賣品。", parse_mode=ParseMode.HTML)
            return
        
        queue_len = len(queue)
        await update.message.reply_text(
            f"📋 <b>批次拍賣狀態</b>\n\n"
            f"📦 隊列中的拍賣品：{queue_len} 件\n"
            f"🕐 排程時間：{current_auction.get('scheduled_start', '未設定')}\n"
            f"📢 發佈群組：{current_auction.get('batch_target_group', '正式')}\n\n"
            f"💡 使用 <code>/start_batch</code> 開始拍賣。",
            parse_mode=ParseMode.HTML
        )
        return

    queue_len = len(current_auction["batch_queue"])
    current_idx = current_auction["batch_current_index"] + 1  # 1-indexed
    current_title = current_auction.get("title", "?")
    status = "⏸ 已暫停" if current_auction.get("batch_paused") else "▶️ 運行中"
    
    remaining = queue_len - current_auction["batch_current_index"]
    
    await update.message.reply_text(
        f"📋 <b>批次拍賣狀態</b>\n\n"
        f"項目：Item {current_idx}/{queue_len}\n"
        f"當前：{html.escape(current_title)}\n"
        f"狀態：{status}\n"
        f"剩餘：{remaining} 件\n"
        f"🕐 排程：{current_auction.get('scheduled_start', '無')}",
        parse_mode=ParseMode.HTML
    )
    # Also show/update the admin panel with buttons
    await show_batch_admin_panel(context.bot, chat_id=update.effective_chat.id)


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /broadcast command - send notification to target group."""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ 權限不足")
        return

    if not context.args:
        await update.message.reply_text(
            "📢 <b>廣播訊息格式：</b>\n\n"
            "<code>/broadcast 今晚8點拍賣開始！150件，約70分鐘</code>",
            parse_mode=ParseMode.HTML
        )
        return

    message_text = " ".join(context.args)

    # Determine target group
    target_type = current_auction.get("batch_target_group", "prod")
    if target_type == "test":
        target_chat_id = await store.get_config("test_group_id")
        target_desc = "測試群組"
    else:
        target_chat_id = await store.get_config("prod_group_id")
        if not target_chat_id:
            target_chat_id = await store.get_config("group_id")
        target_desc = "客戶群組"

    if not target_chat_id:
        await update.message.reply_text(f"❌ 尚未設定【{target_desc}】！\n請先在目標群組輸入 /set_prod_group")
        return

    try:
        await context.bot.send_message(
            chat_id=target_chat_id,
            text=f"📢 <b>拍賣預告</b>\n\n{message_text}",
            parse_mode=ParseMode.HTML
        )
        await update.message.reply_text(
            f"✅ <b>廣播已發送至{target_desc}</b>\n\n"
            f"訊息：{message_text}",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Failed to send broadcast: {e}")
        await update.message.reply_text(f"❌ 廣播發送失敗：{e}")


# --- CSV Export & Blacklist ---

async def export_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """匯出會員資料"""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return

    message = update.effective_message
    
    users = await store.get_all_users()
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['user_id', 'name', 'phone', 'email', 'pickup', 'registered_at'])
    
    for u in users:
        cw.writerow([
            u.get('user_id'),
            u.get('name'),
            u.get('phone'),
            u.get('email'),
            u.get('pickup'),
            u.get('registered_at', 'N/A')
        ])
        
    si.seek(0)
    await message.reply_document(
        document=io.BytesIO(si.getvalue().encode('utf-8-sig')),
        filename="members.csv",
        caption=f"👥 會員名單（共 {len(users)} 人）"
    )

# --- CSV Export & Blacklist ---
async def export_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return

    message = update.effective_message
    
    # Export Users
    users = await store.get_all_users()
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['user_id', 'name', 'phone', 'email', 'pickup'])
    
    for u in users:
        # Check if u is dict (RealDictRow) or simple dict
        uid = u.get('user_id')
        name = u.get('name')
        phone = u.get('phone')
        email = u.get('email')
        pickup = u.get('pickup')
        cw.writerow([uid, name, phone, email, pickup])
        
    si.seek(0)
    await message.reply_document(
        document=io.BytesIO(si.getvalue().encode('utf-8-sig')),
        filename="users.csv",
        caption="📊 用戶名單"
    )
    
    # Export Orders
    orders = await store.get_all_orders()
    users_dict = {u['user_id']: u for u in users}  # Create a lookup dict for users
    
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['order_id', 'session_id', 'user_id', 'user_name', 'phone', 'email', 'pickup', 'item', 'price', 'status', 'time'])
    
    for o in orders:
        uid = o['user_id']
        user_info = users_dict.get(uid, {})
        
        cw.writerow([
            o['order_id'],
            o.get('session_id', 'N/A'),
            uid, 
            user_info.get('name', 'N/A'),
            user_info.get('phone', 'N/A'),
            user_info.get('email', 'N/A'),
            user_info.get('pickup', 'N/A'),
            o['item'], 
            o['price'], 
            o['status'], 
            o.get('time', o.get('created_at'))
        ])
        
    si.seek(0)
    await message.reply_document(
        document=io.BytesIO(si.getvalue().encode('utf-8-sig')),
        filename="orders.csv",
        caption="📊 訂單記錄 (含客戶資料)"
    )

async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    try:
        target_id = int(context.args[0])
        await store.add_blacklist(target_id)
        await update.message.reply_text(f"🚫 已封鎖用戶 {target_id}")
    except:
        await update.message.reply_text("用法: /ban <user_id>")

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    try:
        target_id = int(context.args[0])
        await store.remove_blacklist(target_id)
        await update.message.reply_text(f"✅ 已解封用戶 {target_id}")
    except:
        await update.message.reply_text("用法: /unban <user_id>")

async def set_prod_group_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    
    chat_id = update.effective_chat.id
    if update.effective_chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("❌ 請在群組內使用此指令。")
        return
        
    await store.set_config("prod_group_id", chat_id)
    await update.message.reply_text(f"✅ 已將此群組 ({chat_id}) 設定為 **客戶正式群組**。")

async def set_test_group_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    
    chat_id = update.effective_chat.id
    if update.effective_chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("❌ 請在群組內使用此指令。")
        return
        
    await store.set_config("test_group_id", chat_id)
    await update.message.reply_text(f"✅ 已將此群組 ({chat_id}) 設定為 **內部測試群組**。")

# --- Web Server (Zeabur Requirement & WebApp) ---
async def web_handler(request):
    return aiohttp.web.Response(text="Bot is running")

async def bid_webapp_handler(request):
    # Simple HTML for Bidding WebApp
    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <title>出價</title>
        <script src="https://telegram.org/js/telegram-web-app.js"></script>
        <style>
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
                background-color: var(--tg-theme-bg-color, #ffffff);
                color: var(--tg-theme-text-color, #000000);
                margin: 0;
                padding: 20px;
                display: flex;
                flex-direction: column;
                align-items: center;
                justify_content: center;
                height: 100vh;
                box-sizing: border-box;
            }
            h2 { margin-top: 0; }
            input {
                font-size: 24px;
                padding: 10px;
                width: 100%;
                border: 2px solid var(--tg-theme-button-color, #3390ec);
                border-radius: 8px;
                margin: 20px 0;
                text-align: center;
                box-sizing: border-box;
                -webkit-appearance: none;
            }
            button {
                background-color: var(--tg-theme-button-color, #3390ec);
                color: var(--tg-theme-button-text-color, #ffffff);
                font-size: 18px;
                padding: 15px;
                width: 100%;
                border: none;
                border-radius: 8px;
                cursor: pointer;
                font-weight: bold;
            }
            .hint {
                font-size: 14px;
                color: var(--tg-theme-hint-color, #999999);
                margin-top: 10px;
            }
        </style>
    </head>
    <body>
        <h2>💰 輸入出價金額</h2>
        <input type="number" id="price" placeholder="例如: 100" pattern="[0-9]*" inputmode="numeric" autofocus>
        <button onclick="submitBid()">確認出價</button>
        <div class="hint">請輸入大於當前價格的純數字</div>

        <script>
            const tg = window.Telegram.WebApp;
            tg.expand(); // Expand to full height if possible

            function submitBid() {
                const price = document.getElementById('price').value;
                if (!price || isNaN(price) || parseInt(price) <= 0) {
                    tg.showPopup({
                        title: '錯誤',
                        message: '請輸入有效的金額 (純數字)',
                        buttons: [{type: 'ok'}]
                    });
                    return;
                }
                tg.sendData(price);
            }
            
            // Auto focus on input
            document.getElementById('price').focus();
        </script>
    </body>
    </html>
    """
    return aiohttp.web.Response(text=html_content, content_type='text/html')

async def run_web_server():
    app = aiohttp.web.Application()
    app.router.add_get('/', web_handler)
    app.router.add_get('/health', web_handler)
    app.router.add_get('/bid_webapp', bid_webapp_handler) # Add WebApp route
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()
    logger.info("Web server started on port 8080")

async def handle_webapp_bid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Handle data received from WebApp
    if not current_auction["active"]:
        return

    data = update.effective_message.web_app_data.data
    user = update.effective_user
    
    if not data or not data.isdigit():
        return
        
    price = int(data)
    
    # Process bid directly (blind mode - validate against pending_price)
    if price <= current_auction["pending_price"]:
        # WebApp doesn't show alert easily unless we reply
        pass  # Silent ignore
    
    # Check registration
    if not await store.is_registered(user.id):
        # Maybe send a private message to register?
        return

    # Call process_bid
    await process_blind_bid(user, price, query=None, bot=context.bot)
    
    # Send confirmation message
    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=f"✅ 成功出價：${price}！\n如有更高出價，您將收到通知。"
        )
    except Exception as e:
        logger.warning(f"Failed to send webapp bid confirmation: {e}")
    
    # Optional: Send confirmation in chat? process_bid usually updates the main message.
    # But we might want to delete the "service message" that Telegram sends when WebApp data is received.
    try:
        await update.effective_message.delete()
    except:
        pass

# --- 主程式 ---
async def main():
    # 連接數據庫
    await store.connect()

    # Issue 2 fix: check for unfinished auction from previous run
    if load_auction_state():
        # Auction was active when bot crashed — auto-abort and notify admin on next start
        logger.warning("Unfinished auction found on startup; auto-aborting.")
        current_auction["active"] = False
        current_auction["_ending"] = False
        save_auction_state()  # clears the file

    # 啟動 Web Server (為了 Zeabur 保持活躍)
    await run_web_server()

    # 設置 Bot
    application = Application.builder().token(TOKEN).build()

    # 註冊處理器
    reg_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start_register),
            CallbackQueryHandler(start_register, pattern="^edit_profile$")
        ],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_phone)],
            EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_email)],
            PICKUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_pickup)],
            BIDDING_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_private_bid_text)],
        },
        fallbacks=[CommandHandler("cancel", cancel_register)],
    )
    
    auction_handler = ConversationHandler(
        entry_points=[
            CommandHandler("new_auction", new_auction_start),
            CallbackQueryHandler(new_auction_start, pattern="^admin_add_single$"),
        ],
        states={
            WAITING_PHOTO: [MessageHandler(filters.PHOTO, get_auction_photo)],
            WAITING_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_auction_title)],
            WAITING_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_auction_price)],
            WAITING_BIN_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_bin_price)],
        },
        fallbacks=[CommandHandler("cancel", cancel_register)],
    )

    application.add_handler(reg_handler)
    application.add_handler(auction_handler)
    application.add_handler(CallbackQueryHandler(start_auction_action, pattern="^start_auction_"))
    application.add_handler(CallbackQueryHandler(queue_auction_action, pattern="^queue_auction_"))
    application.add_handler(CallbackQueryHandler(handle_bid_button, pattern="^bid_"))
    application.add_handler(CallbackQueryHandler(handle_numpad_click, pattern="^numpad_"))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern="^admin_"))
    application.add_handler(CallbackQueryHandler(process_daily_settlement, pattern="^confirm_end_session$"))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern="^cancel_end_session$"))
    application.add_handler(CallbackQueryHandler(handle_batch_callback, pattern="^batch_"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_text))
    application.add_handler(CommandHandler("export", export_data))
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("unban", unban_command))
    application.add_handler(CommandHandler("set_prod_group", set_prod_group_command))
    application.add_handler(CommandHandler("set_test_group", set_test_group_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("my_orders", my_orders_command))
    application.add_handler(CommandHandler("admin", admin_menu))
    application.add_handler(CommandHandler("force_end", force_end_command))
    
    # Batch auction commands
    application.add_handler(CommandHandler("import_batch", import_batch_command))
    application.add_handler(CommandHandler("schedule", schedule_command))
    application.add_handler(CommandHandler("start_batch", start_batch_command))
    application.add_handler(CommandHandler("pause_batch", pause_batch_command))
    application.add_handler(CommandHandler("resume_batch", resume_batch_command))
    application.add_handler(CommandHandler("abort_batch", abort_batch_command))
    application.add_handler(CommandHandler("batch_status", batch_status_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    
    # WebApp Data Handler
    application.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_bid))

    # 啟動 Bot
    # 使用 drop_pending_updates 防止舊消息干擾
    await application.initialize()

    # 設定 Bot 命令選單（只顯示俾普通用戶）
    from telegram import BotCommand
    commands = [
        BotCommand("start", "開始 / 註冊"),
        BotCommand("help", "拍賣規則"),
        BotCommand("my_orders", "我的中標記錄"),
    ]
    await application.bot.set_my_commands(commands)

    await application.bot.delete_webhook(drop_pending_updates=True)
    await application.start()
    await application.updater.start_polling()
    
    # 保持運行
    stop_signal = asyncio.Event()
    await stop_signal.wait()

if __name__ == "__main__":
    import aiohttp.web # Import here to avoid circular or top-level issues if not installed
    
    if not TOKEN:
        logger.error("Error: BOT_TOKEN is not set in environment variables.")
        exit(1)
        
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass