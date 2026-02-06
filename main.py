import os
import json
import logging
import asyncio
import datetime
import html
import random
import csv
import io
from datetime import datetime

# Telegram
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
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
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None

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
WAITING_PHOTO, WAITING_TITLE, WAITING_PRICE = range(4, 7)

# --- Store Class (Abstracts JSON / Postgres) ---
class Store:
    def __init__(self):
        self.is_pg = bool(DATABASE_URL)
        if self.is_pg:
            if not psycopg2:
                logger.error("DATABASE_URL present but psycopg2 not installed.")
                exit(1)
            self.init_pg()
        else:
            self.db_file = os.getenv("DATA_PATH", "data.json")
            self.data = {
                "users": {},
                "blacklist": [],
                "auctions": [],
                "orders": [],
                "config": {}
            }
            self.load_json()

    def get_pg_conn(self):
        return psycopg2.connect(DATABASE_URL)

    def init_pg(self):
        conn = self.get_pg_conn()
        cur = conn.cursor()
        
        # Users
        cur.execute("""
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
        cur.execute("""
            CREATE TABLE IF NOT EXISTS blacklist (
                user_id BIGINT PRIMARY KEY,
                reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Orders
        cur.execute("""
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
        cur.execute("""
            CREATE TABLE IF NOT EXISTS system_config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        
        conn.commit()
        conn.close()
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
    def register_user(self, user_id, info):
        if self.is_pg:
            conn = self.get_pg_conn()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO users (user_id, name, phone, email, pickup)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE 
                SET name=EXCLUDED.name, phone=EXCLUDED.phone, email=EXCLUDED.email, pickup=EXCLUDED.pickup
            """, (user_id, info['name'], info['phone'], info.get('email', ''), info['pickup']))
            conn.commit()
            conn.close()
        else:
            self.data["users"][str(user_id)] = info
            self.save_json()

    def get_user(self, user_id):
        if self.is_pg:
            conn = self.get_pg_conn()
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
            user = cur.fetchone()
            conn.close()
            return user
        else:
            return self.data["users"].get(str(user_id))

    def is_registered(self, user_id):
        return self.get_user(user_id) is not None

    # --- Blacklist Methods ---
    def add_blacklist(self, user_id):
        if self.is_pg:
            conn = self.get_pg_conn()
            cur = conn.cursor()
            cur.execute("INSERT INTO blacklist (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (user_id,))
            conn.commit()
            conn.close()
        else:
            if user_id not in self.data["blacklist"]:
                self.data["blacklist"].append(user_id)
                self.save_json()

    def remove_blacklist(self, user_id):
        if self.is_pg:
            conn = self.get_pg_conn()
            cur = conn.cursor()
            cur.execute("DELETE FROM blacklist WHERE user_id = %s", (user_id,))
            conn.commit()
            conn.close()
        else:
            if user_id in self.data["blacklist"]:
                self.data["blacklist"].remove(user_id)
                self.save_json()

    def is_blacklisted(self, user_id):
        if self.is_pg:
            conn = self.get_pg_conn()
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM blacklist WHERE user_id = %s", (user_id,))
            exists = cur.fetchone()
            conn.close()
            return bool(exists)
        else:
            return user_id in self.data["blacklist"]

    # --- Order Methods ---
    def add_order(self, order):
        if self.is_pg:
            conn = self.get_pg_conn()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO orders (order_id, user_id, item, price, status, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (order['order_id'], order['user_id'], order['item'], order['price'], order['status'], order['time']))
            conn.commit()
            conn.close()
        else:
            self.data["orders"].append(order)
            self.save_json()

    def get_all_orders(self):
        if self.is_pg:
            conn = self.get_pg_conn()
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT * FROM orders ORDER BY created_at DESC")
            rows = cur.fetchall()
            conn.close()
            # Convert back to dict format if needed or keep as is
            return rows
        else:
            return self.data["orders"]

    def get_all_users(self):
        if self.is_pg:
            conn = self.get_pg_conn()
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT * FROM users")
            rows = cur.fetchall()
            conn.close()
            return rows
        else:
            # Convert dict to list of dicts with user_id injected
            users = []
            for uid, info in self.data["users"].items():
                u = info.copy()
                u['user_id'] = uid
                users.append(u)
            return users

    # --- Config Methods ---
    def set_config(self, key, value):
        if self.is_pg:
            conn = self.get_pg_conn()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO system_config (key, value) VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """, (key, str(value)))
            conn.commit()
            conn.close()
        else:
            self.data["config"][key] = value
            self.save_json()

    def get_config(self, key):
        if self.is_pg:
            conn = self.get_pg_conn()
            cur = conn.cursor()
            cur.execute("SELECT value FROM system_config WHERE key = %s", (key,))
            row = cur.fetchone()
            conn.close()
            if row:
                # Try to cast to int if it looks like one (simple heuristic)
                val = row[0]
                if val.isdigit(): return int(val)
                return val
            return None
        else:
            return self.data["config"].get(key)

store = Store()

# 全局拍賣狀態
current_auction = {
    "active": False,
    "start_time": None,
    "end_time": None,
    "title": "",
    "photo_id": None,
    "base_price": 0,
    "current_price": 0,
    "highest_bidder": None, # user_id
    "highest_bidder_name": "無",
    "message_id": None,     # 拍賣訊息 ID (群組)
    "chat_id": None,        # 群組 ID
    "timer_task": None
}

# --- 註冊流程 ---
async def start_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    # 定義快捷選單
    menu_keyboard = [['📜 拍賣規則', '👤 我的資料'], ['❓ 常見問題']]
    if user.id in ADMIN_IDS:
        menu_keyboard.append(['🔧 管理員選單'])
    
    reply_markup = ReplyKeyboardMarkup(menu_keyboard, resize_keyboard=True)

    if store.is_registered(user.id):
        await update.message.reply_text(
            "✅ 您已經註冊過了，可以直接參與競拍！\n您可以點擊下方按鈕查看規則或個人資料。",
            reply_markup=reply_markup
        )
        return ConversationHandler.END
    
    await update.message.reply_text(
        "👋 歡迎來到極速拍賣機器人！\n為了確保交易順利，請先完成簡單的登記。\n\n請輸入您的 <b>稱呼 (Name)</b>：",
        parse_mode=ParseMode.HTML
    )
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
    
    # 這裡直接設定 Email 為空，跳到 Pickup
    context.user_data['reg_email'] = ""
    
    keyboard = [['旺角', '寄件']]
    await update.message.reply_text(
        "✅ 收到。請選擇 <b>交收地點</b>：",
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )
    return PICKUP

async def get_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['reg_email'] = update.message.text
    keyboard = [['旺角', '寄件']]
    await update.message.reply_text(
        "✅ 收到。請選擇 <b>交收地點</b>：",
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )
    return PICKUP

async def get_pickup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pickup = update.message.text
    if pickup not in ['旺角', '寄件']:
        await update.message.reply_text("⚠️ 請選擇有效的選項 (旺角/寄件)。")
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
    store.register_user(user.id, info)
    
    # 恢復主菜單
    menu_keyboard = [['📜 拍賣規則', '👤 我的資料'], ['❓ 常見問題']]
    if user.id in ADMIN_IDS:
        menu_keyboard.append(['🔧 管理員選單'])
    reply_markup = ReplyKeyboardMarkup(menu_keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        "🎉 <b>註冊成功！</b>\n現在您可以參與所有拍賣活動了。",
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
    
    # 確認上架
    photo_id = context.user_data['auc_photo']
    title = context.user_data['auc_title']
    safe_title = html.escape(title)
    
    keyboard = [[InlineKeyboardButton("🚀 立即開始拍賣", callback_data="start_auction_confirm")]]
    await update.message.reply_photo(
        photo=photo_id,
        caption=f"📝 <b>預覽上架</b>\n\n📦 商品：{safe_title}\n💰 起標：${price}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ConversationHandler.END 

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
    photo_id = context.user_data.get('auc_photo')
    
    if not photo_id:
        await query.edit_message_caption("❌ 數據丟失，請重新上架。")
        return

    # 初始化拍賣
    current_auction["active"] = True
    current_auction["title"] = title
    current_auction["base_price"] = price
    current_auction["current_price"] = price
    current_auction["photo_id"] = photo_id
    current_auction["highest_bidder"] = None
    current_auction["highest_bidder_name"] = "無"
    current_auction["start_time"] = datetime.now()
    current_auction["end_time"] = datetime.now().timestamp() + 25 
    
    target_chat_id = store.get_config("group_id")
    
    if not target_chat_id:
        if update.effective_chat.type in ["group", "supergroup"]:
            target_chat_id = update.effective_chat.id
        else:
            await query.edit_message_caption("❌ 尚未設定拍賣群組！\n請先在目標群組輸入 /set_group，或在該群組內直接操作。")
            return
            
    current_auction["chat_id"] = target_chat_id

    text = generate_auction_text(25)
    keyboard = generate_bid_keyboard(price)
    
    await query.delete_message()
    
    msg = await context.bot.send_photo(
        chat_id=target_chat_id,
        photo=photo_id,
        caption=text,
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML
    )
    current_auction["message_id"] = msg.message_id
    
    current_auction["timer_task"] = asyncio.create_task(auction_timer_loop(context.bot))

def generate_auction_text(remaining_seconds):
    title = html.escape(current_auction["title"])
    price = current_auction["current_price"]
    bidder = html.escape(current_auction["highest_bidder_name"])
    
    if remaining_seconds <= 0:
        time_str = "00:00"
    else:
        mins, secs = divmod(int(remaining_seconds), 60)
        time_str = f"{mins:02}:{secs:02}"
        
    return (
        f"🔥 <b>正在拍賣：{title}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 當前價格：<b>${price}</b>\n"
        f"👑 最高出價：{bidder}\n"
        f"⏱️ 剩餘時間：<b>{time_str}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"👇 點擊下方按鈕出價！"
    )

def generate_bid_keyboard(current_price):
    # 根據當前價格動態調整加價幅度
    if current_price < 100:
        increments = [10, 20, 50]
    elif current_price < 500:
        increments = [20, 50, 100]
    elif current_price < 1000:
        increments = [50, 100, 200]
    else:
        increments = [100, 200, 500]
        
    buttons = []
    row = []
    for inc in increments:
        row.append(InlineKeyboardButton(f"+${inc}", callback_data=f"bid_{inc}"))
    buttons.append(row)
    
    return InlineKeyboardMarkup(buttons)

async def auction_timer_loop(bot):
    last_update_time = 0
    
    while True:
        now = datetime.now().timestamp()
        remaining = current_auction["end_time"] - now
        
        if remaining <= 0:
            await end_auction(bot)
            break
            
        if now - last_update_time >= 2:
            try:
                await bot.edit_message_caption(
                    chat_id=current_auction["chat_id"],
                    message_id=current_auction["message_id"],
                    caption=generate_auction_text(remaining),
                    reply_markup=generate_bid_keyboard(current_auction["current_price"]),
                    parse_mode=ParseMode.HTML
                )
                last_update_time = now
            except Exception as e:
                logger.warning(f"Update message failed: {e}")
                
        await asyncio.sleep(0.5)

async def handle_bid_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    
    if not current_auction["active"]:
        await query.answer("❌ 拍賣已結束", show_alert=True)
        return

    if not store.is_registered(user.id):
        bot_username = context.bot.username
        url = f"https://t.me/{bot_username}?start=register"
        await query.answer("⚠️ 請先點此註冊！", url=url)
        return

    if store.is_blacklisted(user.id):
        await query.answer("⛔ 您已被禁止參與拍賣", show_alert=True)
        return

    data = query.data 
    add_amount = int(data.split("_")[1])
    new_price = current_auction["current_price"] + add_amount
    
    await process_bid(user, new_price, query)

# --- 拍賣規則 & Menu ---
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📜 <b>拍賣規則 & 使用指南</b>\n\n"
        "1️⃣ <b>參與資格</b>：首次使用需完成簡單登記 (稱呼、電話、交收地點)。\n"
        "2️⃣ <b>出價方式</b>：\n"
        "   • 點擊拍賣訊息下方的快捷按鈕 (例如 +$10)。\n"
        "   • 直接在群組輸入數字 (例如 1500) 進行出價。\n"
        "3️⃣ <b>拍賣時限</b>：\n"
        "   • 每場拍賣預設 25 秒倒數。\n"
        "   • <b>防狙擊機制</b>：若在最後 3 秒內有人出價，時間自動延長 3 秒。\n"
        "4️⃣ <b>得標與結算</b>：\n"
        "   • 拍賣結束後，系統會私訊得標者付款連結。\n"
        "   • 請於得標後盡快完成付款並上傳截圖。\n"
        "5️⃣ <b>注意事項</b>：\n"
        "   • 棄標者將被列入黑名單，無法參與未來拍賣。\n"
        "   • 管理員擁有最終解釋權。\n\n"
        "如有疑問，請聯繫管理員。"
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)

async def user_info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    info = store.get_user(user.id)
    
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
        f"如需修改，請重新輸入 /start 進行登記。"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return

    text = (
        "🔧 <b>管理員選單</b>\n\n"
        "請選擇要執行的操作："
    )
    
    keyboard = [
        [InlineKeyboardButton("🛑 強制結束拍賣", callback_data="admin_force_end")],
        [InlineKeyboardButton("📊 導出數據 (CSV)", callback_data="admin_export")],
        [InlineKeyboardButton("ℹ️ 系統狀態", callback_data="admin_status")]
    ]
    
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    
    if user.id not in ADMIN_IDS:
        await query.answer("⛔ 權限不足", show_alert=True)
        return

    await query.answer()

    if query.data == "admin_force_end":
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

    elif query.data == "admin_export":
        # Call existing export function, mocking update if needed or just ensuring it works
        # export_data uses update.message.reply_document
        # In callback, update.message is the message with buttons. Replying to it is fine.
        await export_data(update, context)

    elif query.data == "admin_status":
        status = "🟢 運行中" if current_auction["active"] else "⚪ 閒置"
        db_type = "PostgreSQL" if store.is_pg else "JSON (Local)"
        
        msg = (
            f"ℹ️ <b>系統狀態</b>\n"
            f"━━━━━━━━━━\n"
            f"🤖 Bot 狀態: {status}\n"
            f"💾 資料庫: {db_type}\n"
            f"👥 註冊用戶: {len(store.get_all_users())} 人"
        )
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

    text = msg.text.strip()
    if not text.isdigit():
        return 
        
    bid_price = int(text)
    user = msg.from_user

    if not store.is_registered(user.id):
        return 
        
    if bid_price <= current_auction["current_price"]:
        return

    await process_bid(user, bid_price, None)
    try:
        await msg.delete()
    except:
        pass

async def process_bid(user, price, query=None):
    if price <= current_auction["current_price"]:
        if query: await query.answer("❌ 出價已被超越", show_alert=True)
        return

    current_auction["current_price"] = price
    current_auction["highest_bidder"] = user.id
    current_auction["highest_bidder_name"] = user.first_name
    
    now = datetime.now().timestamp()
    remaining = current_auction["end_time"] - now
    if remaining < 3:
        current_auction["end_time"] += 3
        extended = True
    else:
        extended = False
        
    if query:
        await query.answer(f"✅ 出價成功！當前 ${price}")
    
    pass 

async def end_auction(bot):
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
    
    await bot.edit_message_caption(
        chat_id=current_auction["chat_id"],
        message_id=current_auction["message_id"],
        caption=final_text,
        reply_markup=None,
        parse_mode=ParseMode.HTML
    )
    
    if winner_id:
        order = {
            "order_id": f"ORD-{int(datetime.now().timestamp())}",
            "user_id": winner_id,
            "item": title,
            "price": price,
            "time": datetime.now().isoformat(),
            "status": "pending"
        }
        store.add_order(order)
        
        try:
            user_info = store.get_user(winner_id)
            pay_link = f"https://payme.hsbc/sample/{price}" 
            msg = (
                f"🎉 恭喜您標得 <b>{html.escape(title)}</b>！\n\n"
                f"金額：${price}\n"
                f"交收：{html.escape(user_info.get('pickup', '未定'))}\n\n"
                f"請點擊以下連結付款，並回傳截圖：\n{pay_link}"
            )
            await bot.send_message(chat_id=winner_id, text=msg, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"Failed to DM winner: {e}")
            await bot.send_message(
                chat_id=current_auction["chat_id"], 
                text=f"⚠️ 無法私聊得標者 (ID: {winner_id})，請主動聯繫管理員。"
            )

# --- CSV Export & Blacklist ---
async def export_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return

    # Export Users
    users = store.get_all_users()
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
    await update.message.reply_document(
        document=io.BytesIO(si.getvalue().encode('utf-8-sig')),
        filename="users.csv",
        caption="📊 用戶名單"
    )
    
    # Export Orders
    orders = store.get_all_orders()
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['order_id', 'user_id', 'item', 'price', 'status', 'time'])
    for o in orders:
        cw.writerow([o['order_id'], o['user_id'], o['item'], o['price'], o['status'], o.get('time', o.get('created_at'))])
        
    si.seek(0)
    await update.message.reply_document(
        document=io.BytesIO(si.getvalue().encode('utf-8-sig')),
        filename="orders.csv",
        caption="📊 訂單記錄"
    )

async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    try:
        target_id = int(context.args[0])
        store.add_blacklist(target_id)
        await update.message.reply_text(f"🚫 已封鎖用戶 {target_id}")
    except:
        await update.message.reply_text("用法: /ban <user_id>")

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    try:
        target_id = int(context.args[0])
        store.remove_blacklist(target_id)
        await update.message.reply_text(f"✅ 已解封用戶 {target_id}")
    except:
        await update.message.reply_text("用法: /unban <user_id>")

async def set_group_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    
    chat_id = update.effective_chat.id
    if update.effective_chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("❌ 請在群組內使用此指令。")
        return
        
    store.set_config("group_id", chat_id)
    await update.message.reply_text(f"✅ 已將此群組 ({chat_id}) 設定為拍賣群組。")

# --- Web Server (Zeabur Requirement) ---
async def web_handler(request):
    return aiohttp.web.Response(text="Bot is running")

async def run_web_server():
    app = aiohttp.web.Application()
    app.router.add_get('/', web_handler)
    app.router.add_get('/health', web_handler)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()
    logger.info("Web server started on port 8080")

# --- 主程式 ---
async def main():
    # 啟動 Web Server (為了 Zeabur 保持活躍)
    await run_web_server()

    # 設置 Bot
    application = Application.builder().token(TOKEN).build()

    # 註冊處理器
    reg_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start_register)],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_phone)],
            EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_email)],
            PICKUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_pickup)],
        },
        fallbacks=[CommandHandler("cancel", cancel_register)],
    )
    
    auction_handler = ConversationHandler(
        entry_points=[CommandHandler("new_auction", new_auction_start)],
        states={
            WAITING_PHOTO: [MessageHandler(filters.PHOTO, get_auction_photo)],
            WAITING_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_auction_title)],
            WAITING_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_auction_price)],
        },
        fallbacks=[CommandHandler("cancel", cancel_register)],
    )

    application.add_handler(reg_handler)
    application.add_handler(auction_handler)
    application.add_handler(CallbackQueryHandler(start_auction_action, pattern="^start_auction_confirm$"))
    application.add_handler(CallbackQueryHandler(handle_bid_button, pattern="^bid_"))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern="^admin_"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_text))
    application.add_handler(CommandHandler("export", export_data))
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("unban", unban_command))
    application.add_handler(CommandHandler("set_group", set_group_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("admin", admin_menu))
    application.add_handler(CommandHandler("force_end", force_end_command)) # Added command handler explicitly

    # 啟動 Bot
    # 使用 drop_pending_updates 防止舊消息干擾
    await application.initialize()

    # 設定 Bot 命令選單
    from telegram import BotCommand
    commands = [
        BotCommand("start", "開始 / 註冊"),
        BotCommand("help", "拍賣規則"),
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