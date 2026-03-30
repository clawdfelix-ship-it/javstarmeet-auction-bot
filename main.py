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
    "highest_bidder": None,  # 上一個最高出價者 (for outbid notification)
    "highest_bidder_name": "無",
    "message_id": None,     # 拍賣訊息 ID (群組)
    "chat_id": None,        # 群組 ID
    "timer_task": None,
    "update_event": asyncio.Event(),
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
                f"💰 當前最高暗標價：${current_auction['pending_price']}\n\n"
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

    # 定義快捷選單
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
    # 跳過 Email，直接問交收
    # await update.message.reply_text("✅ 收到。請輸入您的 <b>Email</b>：", parse_mode=ParseMode.HTML)
    # return EMAIL
    
    # 修改：現在要收集 Email
    await update.message.reply_text("✅ 收到。請輸入您的 <b>Email</b> (用於得標通知)：", parse_mode=ParseMode.HTML)
    return EMAIL
    
    # 這裡直接設定 Email 為空，跳到 Pickup
    # context.user_data['reg_email'] = ""
    
    # keyboard = [['旺角店自取']]
    await update.message.reply_text(
        "✅ 收到。請選擇 <b>交收地點</b>：",
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )
    return PICKUP

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
    current_auction["bin_price"] = bin_price
    current_auction["photo_id"] = photo_id
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

    text = generate_auction_text(25)
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
    
    while True:
        try:
            now = datetime.now().timestamp()
            remaining = current_auction["end_time"] - now
            
            if remaining <= 0:
                await end_auction(bot)
                break

            # Limit update frequency
            # Increase interval to avoid rate limits (429 Too Many Requests)
            # Normal: 2.0s, Last 10s: 1.0s (was 1.0/0.5)
            limit = 1.0 if remaining < 10 else 2.0
            
            # Check if we should update
            if now - last_update_time >= limit:
                try:
                    await bot.edit_message_caption(
                        chat_id=current_auction["chat_id"],
                        message_id=current_auction["message_id"],
                        caption=generate_auction_text(remaining),
                        reply_markup=generate_bid_keyboard(current_auction["current_price"]),
                        parse_mode=ParseMode.HTML
                    )
                    last_update_time = datetime.now().timestamp()
                    now = last_update_time # Recalculate 'now' after update
                except Exception as e:
                    # Ignore "message is not modified" error
                    if "message is not modified" not in str(e):
                        logger.warning(f"Update message failed: {e}")
                        # Avoid tight loop on error
                        await asyncio.sleep(1)
                        last_update_time = datetime.now().timestamp()
            
            # Calculate dynamic wait time
            # Target time is last_update_time + limit
            target_time = last_update_time + limit
            wait_seconds = target_time - datetime.now().timestamp()
            
            # Ensure wait time is reasonable
            if wait_seconds < 0.1:
                wait_seconds = 0.1
                
            # Wait for event or timeout
            try:
                await asyncio.wait_for(event.wait(), timeout=wait_seconds)
                event.clear()
            except asyncio.TimeoutError:
                pass # Timeout means it's time to update (or check time)
                    
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

    if price <= current_auction["pending_price"]:
        await update.message.reply_text(
            f"❌ 出價必須高於當前最高暗標價 (${current_auction['pending_price']})。\n請重新輸入："
        )
        return BIDDING_PRICE
    
    # Check registration
    if not await store.is_registered(user.id):
        await update.message.reply_text(
            "⚠️ 請先完成註冊才能出價！\n請輸入您的 <b>稱呼</b>：",
            parse_mode=ParseMode.HTML
        )
        return NAME
        
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
        "   • <b>防狙擊機制</b>：若在最後 5 秒內有人出價，時間自動延長 5 秒。\n"
        "3️⃣ <b>得標結算</b>：\n"
        "   • 拍賣完結後，最高出價先至會公開。\n"
        "   • 系統會私訊得標者送出結算通知。\n"
        "   • 請於得標後盡快完成付款。\n"
        "4️⃣ <b>注意事項</b>：\n"
        "   • 棄標者將被列入黑名單，無法參與未來拍賣。\n"
        "   • 管理員擁有最終解釋權。\n\n"
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

async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return

    text = (
        "🔧 <b>管理員選單</b>\n\n"
        "請選擇要執行的操作："
    )
    
    keyboard = [
        [InlineKeyboardButton("📦 訂單管理", callback_data="admin_order_mgmt")],
        [InlineKeyboardButton("🛑 強制結束拍賣", callback_data="admin_force_end")],
        [InlineKeyboardButton("🏁 當日拍賣會結束 (結算)", callback_data="admin_end_session")],
        [InlineKeyboardButton("📊 導出數據 (CSV)", callback_data="admin_export")],
        [InlineKeyboardButton("ℹ️ 系統狀態", callback_data="admin_status")]
    ]
    
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)

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

    if query.data.startswith("admin_order_mgmt"):
        page = 1
        parts = query.data.split("_")
        # admin_order_mgmt (len 3) -> page 1
        # admin_order_mgmt_2 (len 4) -> page 2
        if len(parts) >= 4 and parts[3].isdigit():
            page = int(parts[3])
            
        await admin_order_mgmt_menu(update, context, page)

    elif query.data.startswith("adm_ord_"):
        await handle_admin_order_action(update, context)

    elif query.data == "admin_force_end":
        if not current_auction["active"]:
            await query.message.reply_text("❌ 當前沒有進行中的拍賣。")
            return
            
        # Cancel timer task if running
        if current_auction["timer_task"]:
            current_auction["timer_task"].cancel()
            current_auction["timer_task"] = None
        
        # Manually trigger end
        await end_auction(context.bot)
        await query.message.reply_text("✅ 已強制結束拍賣。")

    elif query.data == "admin_end_session":
        # Check active auction
        if current_auction["active"]:
             await query.message.reply_text("❌ 請先結束當前進行中的拍賣，再進行當日結算。")
             return
             
        # Ask for confirmation
        keyboard = [
            [InlineKeyboardButton("✅ 確認結算並發送帳單", callback_data="confirm_end_session")],
            [InlineKeyboardButton("❌ 取消", callback_data="cancel_end_session")]
        ]
        await query.message.reply_text(
            "⚠️ **確認結束當日拍賣會？**\n\n這將會：\n1. 統計今日所有中標訂單\n2. 按用戶合併訂單\n3. 自動私訊發送總帳單給每位中標者\n\n此操作不可撤銷。",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    elif query.data == "confirm_end_session":
        await process_daily_settlement(update, context)
        
    elif query.data == "cancel_end_session":
        await query.message.edit_text("已取消結算操作。")

    elif query.data == "admin_export":
        # Call existing export function, mocking update if needed or just ensuring it works
        # export_data uses update.message.reply_document
        # In callback, update.message is the message with buttons. Replying to it is fine.
        await export_data(update, context)

    elif query.data == "admin_status":
        import platform
        from datetime import timedelta, timezone
        
        status = "🟢 運行中" if current_auction["active"] else "⚪ 閒置"
        db_type = "PostgreSQL 🐘" if store.is_pg else "SQLite/JSON 📁 (本地)"
        db_conn_str = DATABASE_URL
        
        # Mask sensitive info
        if db_conn_str:
            parts = db_conn_str.split("@")
            if len(parts) > 1:
                db_conn_str = f"...@{parts[1]}"
            else:
                db_conn_str = "********"
        else:
            db_conn_str = store.db_file
            
        # System Info
        sys_info = f"OS: {platform.system()} {platform.release()}\n"
        
        # Time (UTC+8)
        tz_offset = timedelta(hours=8)
        # datetime is shadowed by class, so use datetime.now(tz) directly if timezone imported
        # But datetime class is imported as datetime
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

        await query.message.reply_text(msg, parse_mode=ParseMode.HTML)

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
    elif text == "❓ 常見問題":
         await update.message.reply_text("常見問題功能建設中...")
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
        
    if bid_price <= current_auction["pending_price"]:
        if is_valid_reply:
            await msg.reply_text(f"❌ 出價必須高於當前最高暗標價 (${current_auction['pending_price']}")
        return

    await process_blind_bid(user, bid_price, None, context.bot)
    try:
        await msg.delete()
    except:
        pass

async def process_blind_bid(user, price, query=None, bot=None):
    # 暗標拍賣：唔會即時更新 public display，淨係儲存 pending bid
    # pending_price 係暗標用來校驗新舊出價，public 顯示跟 current_price
    if price <= current_auction["pending_price"]:
        if query: await query.answer(f"❌ 出價必須高於 ${current_auction['pending_price']}", show_alert=True)
        return

    # Track previous bidder for notification
    previous_bidder_id = current_auction["pending_bidder"]
    
    # Store as pending (not yet public)
    current_auction["pending_price"] = price
    current_auction["pending_bidder"] = user.id
    current_auction["pending_bidder_name"] = user.first_name
    
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
    
    # Anti-sniping: extend timer if bid within last 5 seconds
    now = datetime.now().timestamp()
    remaining = current_auction["end_time"] - now
    if remaining < 5:
        current_auction["end_time"] += 5
        if current_auction.get("update_event"):
            current_auction["update_event"].set()
    
    # Notify previous highest bidder
    if previous_bidder_id and previous_bidder_id != user.id:
        target_bot = bot if bot else (query.bot if query else None)
        if target_bot:
            asyncio.create_task(notify_previous_bidder(target_bot, previous_bidder_id, current_auction["title"], price, user.first_name))

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
    current_auction["bin_price"] = bin_price
    current_auction["photo_id"] = photo_id
    current_auction["highest_bidder"] = None
    current_auction["highest_bidder_name"] = "無"
    current_auction["start_time"] = datetime.now()
    current_auction["end_time"] = datetime.now().timestamp() + 25
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

    text = generate_auction_text(25)
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
    # Reveal pending price and bidder (暗標揭曉)
    current_auction["current_price"] = current_auction["pending_price"]
    current_auction["highest_bidder"] = current_auction["pending_bidder"]
    current_auction["highest_bidder_name"] = current_auction["pending_bidder_name"]
    
    current_auction["active"] = False
    winner_id = current_auction["highest_bidder"]
    price = current_auction["current_price"]
    title = current_auction["title"]
    
    final_text = (
        f"🛑 <b>拍賣結束！</b> 🛑\n\n"
        f"📦 {html.escape(title)}\n"
        f"💰 最終成交價：<b>${price}</b>\n"
        f"🏆 得標者：{html.escape(current_auction['highest_bidder_name'])}\n\n"
        f"系統將自動發送結算連結給得標者。"
    )
    
    try:
        await bot.edit_message_caption(
            chat_id=current_auction["chat_id"],
            message_id=current_auction["message_id"],
            caption=final_text,
            reply_markup=None,
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Failed to edit auction message: {e}")
        # Fallback: Send a new message if edit fails (e.g. message deleted or rate limit)
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

    await start_next_queued_auction(bot)

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
    cw.writerow(['order_id', 'session_id', 'user_id', 'user_name', 'phone', 'pickup', 'item', 'price', 'status', 'time'])
    
    for o in orders:
        uid = o['user_id']
        user_info = users_dict.get(uid, {})
        
        cw.writerow([
            o['order_id'],
            o.get('session_id', 'N/A'),
            uid, 
            user_info.get('name', 'N/A'),
            user_info.get('phone', 'N/A'),
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
        entry_points=[CommandHandler("new_auction", new_auction_start)],
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
    
    # WebApp Data Handler
    application.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_bid))

    # 啟動 Bot
    # 使用 drop_pending_updates 防止舊消息干擾
    await application.initialize()

    # 設定 Bot 命令選單
    from telegram import BotCommand
    commands = [
        BotCommand("start", "開始 / 註冊"),
        BotCommand("help", "拍賣規則"),
        BotCommand("my_orders", "我的中標記錄"),
        BotCommand("new_auction", "上架拍賣 (Admin)"),
        BotCommand("admin", "管理選單 (Admin)"),
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