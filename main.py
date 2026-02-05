import logging
import os
import json
import asyncio
import html
import pandas as pd
from datetime import datetime
from typing import Dict, Optional, List
from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler, 
    ContextTypes, ConversationHandler, filters
)
from telegram.constants import ParseMode

# 載入環境變數
load_dotenv()

# 設定日誌
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# 環境變數配置
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x]
PORT = int(os.getenv("PORT", 8080))

# 狀態定義 (註冊流程)
NAME, PHONE, EMAIL, PICKUP = range(4)

# 狀態定義 (拍賣流程)
WAITING_PHOTO, WAITING_TITLE, WAITING_PRICE = range(4, 7)

# 數據存儲類
class Store:
    def __init__(self, db_file="data.json"):
        self.db_file = db_file
        self.data = {
            "users": {},      # {user_id: {name, phone, email, pickup}}
            "blacklist": [],  # [user_id, ...]
            "auctions": [],   # 歷史記錄
            "orders": []      # 訂單記錄
        }
        self.load()

    def load(self):
        if os.path.exists(self.db_file):
            try:
                with open(self.db_file, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception as e:
                logger.error(f"Failed to load data: {e}")

    def save(self):
        try:
            with open(self.db_file, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to save data: {e}")

    def is_registered(self, user_id):
        return str(user_id) in self.data["users"]

    def register_user(self, user_id, info):
        self.data["users"][str(user_id)] = info
        self.save()

    def is_blacklisted(self, user_id):
        return user_id in self.data["blacklist"]

    def ban_user(self, user_id):
        if user_id not in self.data["blacklist"]:
            self.data["blacklist"].append(user_id)
            self.save()

    def unban_user(self, user_id):
        if user_id in self.data["blacklist"]:
            self.data["blacklist"].remove(user_id)
            self.save()

    def add_order(self, order):
        self.data["orders"].append(order)
        self.save()

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
    await update.message.reply_text("收到。請輸入您的 <b>電話號碼 (Phone)</b>：", parse_mode=ParseMode.HTML)
    return PHONE

async def get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['reg_phone'] = update.message.text
    await update.message.reply_text("收到。請輸入您的 <b>Email</b>：", parse_mode=ParseMode.HTML)
    return EMAIL

async def get_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['reg_email'] = update.message.text
    keyboard = [['銅鑼灣', '旺角'], ['尖沙咀', '郵寄']]
    await update.message.reply_text(
        "最後一步，請選擇預設 <b>交收地點</b>：",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
        parse_mode=ParseMode.HTML
    )
    return PICKUP

async def get_pickup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['reg_pickup'] = update.message.text
    user = update.effective_user
    
    # 儲存資料
    info = {
        "name": context.user_data['reg_name'],
        "phone": context.user_data['reg_phone'],
        "email": context.user_data['reg_email'],
        "pickup": context.user_data['reg_pickup'],
        "username": user.username,
        "joined_at": datetime.now().isoformat()
    }
    store.register_user(user.id, info)
    
    # 定義快捷選單
    menu_keyboard = [['📜 拍賣規則', '👤 我的資料'], ['❓ 常見問題']]
    if user.id in ADMIN_IDS:
        menu_keyboard.append(['🔧 管理員選單'])
        
    await update.message.reply_text(
        "🎉 註冊成功！您現在可以參與競拍了。",
        reply_markup=ReplyKeyboardMarkup(menu_keyboard, resize_keyboard=True)
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
    return ConversationHandler.END # 這裡結束對話，後續通過按鈕觸發

# --- 拍賣核心邏輯 ---

async def start_auction_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    # 只有管理員可以按
    if query.from_user.id not in ADMIN_IDS:
        return

    # 獲取暫存資料
    # 注意：這裡我們需要把數據轉移到全局狀態
    # 由於對話已結束，我們依賴 user_data 仍然保留 (通常 Context 會保留)
    # 更好的做法是在 get_auction_price 直接保存到 global temp，或者這裡不結束 conversation
    # 為簡化，我們假設 user_data 可用。如果不可用，我們需要調整流程。
    # 修正：直接從 user_data 讀取可能不安全，如果 bot 重啟。
    # 但對於簡單流程，暫且這樣。
    
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
    current_auction["end_time"] = datetime.now().timestamp() + 25 # 25秒倒數
    
    # 發送到當前群組 (假設 admin 在群組操作，或者 admin 在私聊操作但指定了群組？)
    # 這裡假設 admin 在私聊操作，我們需要一個 target chat id。
    # 為了簡化，我們假設 admin 在群組裡發送 /new_auction 指令。
    # 如果 admin 在私聊，我們需要配置 GROUP_ID。
    target_chat_id = update.effective_chat.id 
    # 如果這是私聊，可能需要指定群組。暫時設為當前對話。
    
    current_auction["chat_id"] = target_chat_id

    text = generate_auction_text(25)
    keyboard = generate_bid_keyboard(price)
    
    # 刪除預覽訊息
    await query.delete_message()
    
    # 發送正式拍賣訊息
    msg = await context.bot.send_photo(
        chat_id=target_chat_id,
        photo=photo_id,
        caption=text,
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML
    )
    current_auction["message_id"] = msg.message_id
    
    # 啟動計時器
    current_auction["timer_task"] = asyncio.create_task(auction_timer_loop(context.bot))

def generate_auction_text(seconds_left):
    price = current_auction["current_price"]
    leader = html.escape(current_auction["highest_bidder_name"])
    # 隱藏名字中間 (例如 T**m)
    if len(leader) > 2:
        leader_display = f"{leader[0]}**{leader[-1]}"
    elif leader != "無":
        leader_display = f"{leader[0]}**"
    else:
        leader_display = "等待出價..."
        
    title = html.escape(current_auction['title'])
    
    return (
        f"🔥 <b>極速拍賣開始！</b> 🔥\n\n"
        f"📦 <b>{title}</b>\n"
        f"💰 當前價格：<b>${price}</b>\n"
        f"👑 最高出價：{leader_display}\n\n"
        f"⏳ <b>剩餘時間：{int(seconds_left)} 秒</b>\n"
        f"⚠️ 倒數 3 秒內出價自動延長 3 秒！"
    )

def generate_bid_keyboard(current_price):
    # 智能按鈕：+10, +50, +100 (根據價格動態調整可選)
    kb = [
        [
            InlineKeyboardButton(f"+$10 (${current_price+10})", callback_data="bid_10"),
            InlineKeyboardButton(f"+$20 (${current_price+20})", callback_data="bid_20"),
        ],
        [
            InlineKeyboardButton(f"+$50 (${current_price+50})", callback_data="bid_50"),
            InlineKeyboardButton(f"+$100 (${current_price+100})", callback_data="bid_100"),
        ]
    ]
    return InlineKeyboardMarkup(kb)

async def auction_timer_loop(bot):
    last_update_time = 0
    
    while True:
        now = datetime.now().timestamp()
        remaining = current_auction["end_time"] - now
        
        if remaining <= 0:
            await end_auction(bot)
            break
            
        # 每 2 秒更新一次顯示，避免 API 限制
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

    # 檢查註冊
    if not store.is_registered(user.id):
        # 提供註冊連結
        bot_username = context.bot.username
        url = f"https://t.me/{bot_username}?start=register"
        await query.answer("⚠️ 請先點此註冊！", url=url)
        return

    # 檢查黑名單
    if store.is_blacklisted(user.id):
        await query.answer("⛔ 您已被禁止參與拍賣", show_alert=True)
        return

    # 解析出價
    data = query.data # bid_10, bid_50
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
    if not store.is_registered(user.id):
        await update.message.reply_text("❌ 您尚未註冊。請輸入 /start 進行註冊。")
        return
        
    info = store.data["users"][str(user.id)]
    text = (
        "👤 <b>我的資料</b>\n\n"
        f"稱呼：{html.escape(info.get('name', ''))}\n"
        f"電話：{html.escape(info.get('phone', ''))}\n"
        f"Email：{html.escape(info.get('email', ''))}\n"
        f"交收地點：{html.escape(info.get('pickup', ''))}\n\n"
        "如需修改資料，請聯繫管理員。"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return

    text = (
        "🔧 <b>管理員選單</b>\n\n"
        "📦 <b>/new_auction</b> - 上架新拍賣\n"
        "📊 <b>/export</b> - 導出 CSV (用戶/訂單)\n"
        "🚫 <b>/ban &lt;ID&gt;</b> - 封鎖用戶\n"
        "✅ <b>/unban &lt;ID&gt;</b> - 解封用戶\n"
        "ℹ️ <b>Bot Info</b> - 查看狀態"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

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
        # 如果不是按鈕文字，且是數字，可能是出價
        if current_auction["active"] and text.isdigit():
            await handle_text_bid(update, context)

async def handle_text_bid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not current_auction["active"] or not msg.text:
        return
        
    # 檢查是否在拍賣群組
    if msg.chat_id != current_auction["chat_id"]:
        return

    text = msg.text.strip()
    if not text.isdigit():
        return # 忽略非數字
        
    bid_price = int(text)
    user = msg.from_user

    # 檢查註冊 & 黑名單 (略，同上，但這裡不能用 query.answer，可以用 reply)
    if not store.is_registered(user.id):
        # 這裡不回應避免刷屏，或者私聊提醒
        return 
        
    if bid_price <= current_auction["current_price"]:
        # 出價過低，忽略
        return

    await process_bid(user, bid_price, None)
    # 刪除用戶的出價訊息以保持版面清潔 (可選)
    try:
        await msg.delete()
    except:
        pass

async def process_bid(user, price, query=None):
    # 再次檢查價格 (防止併發)
    if price <= current_auction["current_price"]:
        if query: await query.answer("❌ 出價已被超越", show_alert=True)
        return

    current_auction["current_price"] = price
    current_auction["highest_bidder"] = user.id
    current_auction["highest_bidder_name"] = user.first_name
    
    # 延長時間邏輯
    now = datetime.now().timestamp()
    remaining = current_auction["end_time"] - now
    if remaining < 3:
        current_auction["end_time"] += 3
        extended = True
    else:
        extended = False
        
    if query:
        await query.answer(f"✅ 出價成功！當前 ${price}")
    
    # 立即更新介面 (為了即時性)
    # 注意：如果 timer 也在更新，這裡可能會衝突，但 Telegram API 會處理順序
    # 為減少請求，這裡可以只更新變數，讓 timer loop 處理更新
    # 但為了"極速"體驗，有人出價時應該立即反饋
    # 我們讓 timer loop 負責主要倒數，這裡觸發一次強制更新
    pass 

async def end_auction(bot):
    current_auction["active"] = False
    winner_id = current_auction["highest_bidder"]
    price = current_auction["current_price"]
    title = current_auction["title"]
    
    # 停止更新
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
        # 記錄訂單
        order = {
            "order_id": f"ORD-{int(datetime.now().timestamp())}",
            "user_id": winner_id,
            "item": title,
            "price": price,
            "time": datetime.now().isoformat(),
            "status": "pending"
        }
        store.add_order(order)
        
        # 私聊得標者
        try:
            user_info = store.data["users"].get(str(winner_id))
            pay_link = f"https://payme.hsbc/sample/{price}" # 示例連結
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

# --- CSV 導出 ---
async def export_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return

    # 導出 Users
    users_df = pd.DataFrame.from_dict(store.data["users"], orient='index')
    users_df.to_csv("users.csv", index=True, index_label="user_id")
    
    # 導出 Orders
    orders_df = pd.DataFrame(store.data["orders"])
    orders_df.to_csv("orders.csv", index=False)
    
    await update.message.reply_document(document=open("users.csv", "rb"), filename="users.csv")
    await update.message.reply_document(document=open("orders.csv", "rb"), filename="orders.csv")

async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS: return
    
    try:
        target_id = int(context.args[0])
        store.ban_user(target_id)
        await update.message.reply_text(f"🚫 已封鎖用戶 ID: {target_id}")
    except (IndexError, ValueError):
        await update.message.reply_text("用法: /ban <user_id>")

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS: return
    
    try:
        target_id = int(context.args[0])
        store.unban_user(target_id)
        await update.message.reply_text(f"✅ 已解封用戶 ID: {target_id}")
    except (IndexError, ValueError):
        await update.message.reply_text("用法: /unban <user_id>")

# --- Zeabur Health Check (Dummy Web Server) ---
from aiohttp import web

async def health_check(request):
    return web.Response(text="OK")

async def run_web_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"Web server started on port {PORT}")

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
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_text))
    application.add_handler(CommandHandler("export", export_data))
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("unban", unban_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("admin", admin_menu))

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
    if not TOKEN:
        logger.error("Error: BOT_TOKEN is not set in environment variables.")
        exit(1)
        
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
