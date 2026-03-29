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
                await conn.execute("ALTER TABLE orders ADD COLUMN session_id TEXT");
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
                """, user_id, info['name'], info['phone'], info.get('email'), info['pickup'])
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
                seq = int(count) + 1
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
                    VALUES ($1, $2, $3, $4, $5, $6)
                """, order['order_id'], order['user_id'], order['item'], order['price'], order['status'], datetime.fromisoformat(order['time']), order.get('session_id'))
        else:
            self.data["orders"].append(order)
            self.save_json()

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

    async def get_all_orders(self):
        if self.is_pg:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch("SELECT * FROM orders ORDER BY created_at DESC")
                return [dict(row) for row in rows]
        else:
            return self.data["orders"]

    async def get_user_orders(self, user_id):
        if self.is_pg:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch("SELECT * FROM orders WHERE user_id = $1 ORDER BY created_at DESC", user_id)
                return [dict(row) for row in rows]
        else:
            return [o for o in self.data["orders"] if str(o["user_id"]) == str(user_id)]

# --- 拍賣核心狀態 ---
current_auction = {
    "active": False,
    "start_time": None,
    "end_time": None,
    "title": "",
    "photo_id": None,
    "base_price": 0,
    "current_price": 0,
    "pending_price": 0,
    "bin_price": 0,
    "pending_bidder": None,
    "pending_bidder_name": "無",
    "highest_bidder": None,
    "highest_bidder_name": "無",
    "message_id": None,
    "chat_id": None,
    "timer_task": None,
    "update_event": None,
    "session_id": None,
    "session_seq": 0,
    "bot_username": None
}

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
            
            # If registered, start bidding flow
            if not current_auction["active"]:
                await update.message.reply_text("❌ 當前沒有進行中的拍賣。")
                return ConversationHandler.END
                
            await update.message.reply_text(
                f"🔥 <b>正在拍賣：{html.escape(current_auction['title'])}</b>\n"
                f"💰 當前價格：${current_auction['current_price']}\n\n"
                f"請輸入您的 <b>出價金額</b> (純數字)：",
                parse_mode=ParseMode.HTML
            )
            return BIDDING_PRICE

        elif arg == 'bid_webapp':
            if not await store.is_registered(user.id):
                 await update.message.reply_text("⚠️ 請先完成註冊才能出價！\n請輸入您的 <b>稱呼 (Name)</b>：", parse_mode=ParseMode.HTML)
                 return NAME
                 
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

    # Define quick menu
    menu_keyboard = [['📜 拍賣規則', '👤 我的資料'], ['❓ 常見問題']]
    if user.id in ADMIN_IDS:
        menu_keyboard.append(['🔧 管理員選單'])
    
    reply_markup = ReplyKeyboardMarkup(menu_keyboard, resize_keyboard=True)

    if not is_edit and await store.is_registered(user.id):
        await update.message.reply_text(
            "✅ 您已經註冊過了，可以直接參與競拍！\n您可以點擊下方按鈕查看規則或個人資料。",
            reply_markup=reply_markup
        )
        return ConversationHandler.END
    
    msg_text = "👋 歡迎來到極速拍賣機器人！\n為了確保交易順利，請先完成簡單的登記。\n\n請輸入您的 <b>稱呼 (Name)</b>："
    if is_edit:
        msg_text = "✏️ <b>修改資料</b>\n\n請輸入您的新 <b>稱呼 (Name)</b>："

    if update.callback_query:
         await update.callback_query.message.reply_text(msg_text, parse_mode=ParseMode.HTML)
    else:
         await update.message.reply_text(msg_text, parse_mode=ParseMode.HTML)
         
    return NAME

async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['reg_name'] = update.message.text
    await update.message.reply_text("✅ 收到。請輸入您的 <b>電話號碼</b> (例如 91234567)：", parse_mode=ParseMode.HTML)
    return PHONE

async def get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['reg_phone'] = update.message.text
    # Modified: now we collect Email
    await update.message.reply_text("✅ 收到。請輸入您的 <b>Email</b> (用於得標通知)：", parse_mode=ParseMode.HTML)
    return EMAIL
    
async def get_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['reg_email'] = update.message.text
    keyboard = [['旺角店自取']]
    await update.message.reply_text(
        "✅ 收到。請選擇 <b>交收地點</b>：",
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )
    return PICKUP

async def get_pickup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pickup = update.message.text
    if pickup not in ['旺角店自取']:
        await update.message.reply_text("⚠️ 請選擇有效的選項 (旺角店自取)。")
        return PICKUP
        
    context.user_data['reg_pickup'] = pickup
    
    # Save data
    user = update.effective_user
    info = {
        "name": context.user_data['reg_name'],
        "phone": context.user_data['reg_phone'],
        "email": context.user_data['reg_email'],
        "pickup": context.user_data['reg_pickup']
    }
    await store.register_user(user.id, info)
    
    # Restore main menu
    menu_keyboard = [['📜 拍賣規則', '👤 我的資料'], ['❓ 常見問題']]
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
        await update.message.reply_text("⛔ 權限不足", parse_mode=ParseMode.HTML)
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
        await update.message.reply_text("❌ 格式錯誤，請輸入純數字：", parse_mode=ParseMode.HTML)
        return WAITING_PRICE
    
    await update.message.reply_text("請輸入 <b>一口價 (Buy It Now)</b> 金額 (純數字，輸入 0 代表不設)：", parse_mode=ParseMode.HTML)
    return WAITING_BIN_PRICE

async def get_bin_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        bin_price = int(update.message.text)
        context.user_data['auc_bin_price'] = bin_price
    except ValueError:
        await update.message.reply_text("❌ 格式錯誤，請輸入純數字：", parse_mode=ParseMode.HTML)
        return WAITING_BIN_PRICE

    # Confirm publish
    photo_id = context.user_data.get('auc_photo')
    title = context.user_data.get('auc_title', '未知商品')
    price = context.user_data.get('auc_price', 0)
    safe_title = html.escape(title)
    
    bin_text = f"\n⚡️ 一口價：<b>${bin_price}</b>" if bin_price > 0 else ""
    
    keyboard = [
        [InlineKeyboardButton("🚀 發布到【客戶群】", callback_data="start_auction_prod")],
        [InlineKeyboardButton("🧪 發布到【測試群】", callback_data="start_auction_test")],
        [InlineKeyboardButton("📥 加入批次隊列【客戶群】", callback_data="queue_auction_prod")],
        [InlineKeyboardButton("📥 加入批次隊列【測試群】", callback_data="queue_auction_test")],
    ]
    await update.message.reply_photo(
        photo=photo_id,
        caption=f"📝 <b>預覽上架</b>\n\n📦 商品：{safe_title}\n💰 起標：${price}{bin_text}\n\n請選擇發布目標：",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ConversationHandler.END 

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
        # Fallback to old 'group_id'
        if not target_chat_id:
            target_chat_id = await store.get_config("group_id")

    if not target_chat_id:
        await query.edit_message_caption(f"❌ 尚未設定{target_type}群組！\n請先在目標群組輸入 /set_{'test_' if target_type == '測試' else 'prod_'}group")
        return

    # Initialize auction
    session_id, session_seq = await store.get_next_session()
    current_auction["active"] = True
    current_auction["title"] = title
    current_auction["base_price"] = price
    current_auction["current_price"] = price  # Public price starts as opening price
    current_auction["pending_price"] = price  # Pending price is same as current initially
    current_auction["bin_price"] = bin_price
    current_auction["photo_id"] = photo_id
    current_auction["pending_bidder"] = None
    current_auction["pending_bidder_name"] = "無"
    current_auction["highest_bidder"] = None
    current_auction["highest_bidder_name"] = "無"
    current_auction["start_time"] = datetime.now()
    current_auction["end_time"] = datetime.now().timestamp() + 25 
    current_auction["session_id"] = session_id
    current_auction["session_seq"] = session_seq 
    current_auction["chat_id"] = target_chat_id

    # Get bot username for deep linking
    try:
        me = await context.bot.get_me()
        current_auction["bot_username"] = me.username
    except Exception as e:
        logger.error(f"Failed to get bot info: {e}")

    # Initialize event in current loop
    if current_auction["update_event"] is None or current_auction["update_event"]._loop != asyncio.get_running_loop():
        current_auction["update_event"] = asyncio.Event()

    text = generate_auction_text(current_auction["end_time"] - datetime.now().timestamp())
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
            text=f"✅ 拍賣已發布到【{target_type}】！"
        )
    except Exception as e:
        logger.error(f"Failed to start auction: {e}")
        current_auction["active"] = False

async def queue_auction_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id not in ADMIN_IDS:
        return

    title = context.user_data.get('auc_title', '未知商品')
    price = int(context.user_data.get('auc_price', 0))
    bin_price = int(context.user_data.get('auc_bin_price', 0))
    photo_id = context.user_data.get('auc_photo')

    if not photo_id:
        await query.edit_message_caption("❌ 數據丟失，請重新上架。")
        return

    queue = await store.get_auction_queue()
    queue.append({
        "title": title,
        "price": price,
        "bin_price": bin_price,
        "photo_id": photo_id,
        "target_chat_id": target_chat_id
    })
    await store.set_auction_queue(queue)

    await query.edit_message_caption(
        f"✅ 已加入批次拍賣隊列（{target_type}群）\n目前隊列中共有 {len(queue)} 件拍賣品。"
    )

def generate_auction_text(remaining_seconds):
    title = html.escape(current_auction["title"])
    price = current_auction["current_price"]
    # Always hide bidder in blind mode
    bidder = "㊙️ (匿名暗標)"
    
    seq = current_auction.get("session_seq", "?")
    
    bin_price = current_auction.get("bin_price", 0)
    bin_text = f"\n⚡️ 一口價：<b>${bin_price}</b>" if bin_price > 0 else ""
    
    if remaining_seconds <= 0:
        time_str = "00:00"
    else:
        mins, secs = divmod(int(remaining_seconds), 60)
        time_str = f"{int(mins):02}:{secs:02}"
    
    return (
        f"🔥 <b>正在拍賣：{title}</b> (第 {seq} 場 - 匿名暗標)\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 當前價格：<b>${price}</b>{bin_text}\n"
        f"👑 最高出價：{bidder}\n"
        f"⏱️ 剩餘時間：<b>{time_str}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"👇 點擊下方按鈕私訊出價："
    )

def generate_bid_keyboard(current_price):
    # 全暗標拍賣：所有出價都必須透過私訊，所以淨係得兩個button：
    # 1. 如果有一口價就顯示 -> 連去私訊
    # 2. 永遠顯示私訊出價入口
    buttons = []
    
    # Add BIN button if set
    bin_price = current_auction["bin_price"]
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
    
    # Ensure event is created
    if event is None or event._loop != asyncio.get_running_loop():
        event = asyncio.Event()
        current_auction["update_event"] = event
    
    while True:
        try:
            now = datetime.now().timestamp()
            remaining = current_auction["end_time"] - now
            
            if remaining <= 0:
                # Auction ended → update public price with pending price, reveal winner
                current_auction["current_price"] = current_auction["pending_price"]
                current_auction["highest_bidder"] = current_auction["pending_bidder"]
                current_auction["highest_bidder_name"] = current_auction["pending_bidder_name"]
                await end_auction(bot)
                break

            # Update more frequently when less than 10s remaining
            # Normal: update every 2s, last 10s → update every 1s to avoid 429
            update_limit = 1.0 if remaining < 10 else 2.0
            
            # Check if we need to update
            if now - last_update_time >= update_limit:
                try:
                    await bot.edit_message_caption(
                        chat_id=current_auction["chat_id"],
                        message_id=current_auction["message_id"],
                        caption=generate_auction_text(remaining),
                        reply_markup=generate_bid_keyboard(current_auction["current_price"]),
                        parse_mode=ParseMode.HTML
                    )
                    last_update_time = datetime.now().timestamp()
                except Exception as e:
                    # Ignore "message not modified" errors
                    if "message is not modified" not in str(e):
                        logger.warning(f"Failed to update auction message: {e}")
            # Calculate dynamic wait time
            target_time = last_update_time + update_limit
            wait_seconds = target_time - datetime.now().timestamp()
            
            # Ensure reasonable wait time
            if wait_seconds < 0.1:
                wait_seconds = 0.1
            
            try:
                await asyncio.wait_for(event.wait(), timeout=wait_seconds)
            except asyncio.TimeoutError:
                pass
            event.clear()
                    
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Timer loop error: {e}")
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

    if price <= current_auction["current_price"]:
        await update.message.reply_text(
            f"❌ 出價必須高於當前價格 (${current_auction['current_price']})。\n"
            f"請重新輸入："
        )
        return BIDDING_PRICE
        
    # Process bid in blind mode: we just accept the bid and notify, don't update public until end
    await process_blind_bid(user, price, update, context.bot)
    await update.message.reply_text(f"✅ 成功出價：${price}\n"
                                 f"出價已私密收下，如有更高出價您會收到通知！")
    return ConversationHandler.END

async def process_blind_bid(user, price, query, bot):
    # Blind auction: price not revealed publicly until end
    # We just validate and store it, don't update public display until timer ends
    
    if price <= current_auction["current_price"]:
        # already checked before, just return
        if query:
            await query.answer("❌ 出價必須高於當前價格 (${current_auction['current_price']})")
        return

    # Check Buy It Now
    bin_price = current_auction["bin_price"]
    if bin_price > 0 and price >= bin_price:
        # End immediately
        current_auction["pending_price"] = price
        current_auction["pending_bidder"] = user.id
        current_auction["pending_bidder_name"] = user.first_name
        current_auction["end_time"] = datetime.now().timestamp() # End immediately
        if current_auction["timer_task"]:
            current_auction["timer_task"].cancel()
        # end_auction will handle finalization
        return

    # Anti-sniping: if bid within last 5 seconds → extend by 5 seconds
    now = datetime.now().timestamp()
