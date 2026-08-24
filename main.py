import logging
import os
import json
import csv
import io
import html
import asyncio
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from store import create_store
import asyncio
from core.batch import get_batch_state, build_batch_admin_keyboard, build_batch_admin_text, ITEM_DURATION, PAUSE_BETWEEN_ITEMS, batch_state
from core.handlers import build_registration_handlers
from core.admin import build_admin_handlers
from core.settlement import process_settlement_by_date as _settle_by_date
from core.auction import AuctionEngine
from core.text import generate_auction_text, build_bin_confirm_keyboard, generate_bid_keyboard, truncate_name_prefix, generate_numpad_keyboard

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
WAITING_MEMBERS_CSV = 9

# --- Constants ---


# AuctionEngine instance (initialized in main(), used by module-level handlers)
auction_engine: "AuctionEngine" = None  # type: ignore

# --- 拍賣核心邏輯 ---

async def start_auction_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id not in ADMIN_IDS:
        return

    if auction_engine.state.active:
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
        if not target_chat_id:
            target_chat_id = await store.get_config("group_id")

    if not target_chat_id:
        await query.edit_message_caption(f"❌ 尚未設定【{target_type}群組】！\n請先在目標群組輸入 /set_{'test_' if target_type=='測試' else 'prod_'}group")
        return

    # Delegate state init to AuctionEngine
    await auction_engine.start_auction(title, photo_id, price, bin_price, int(target_chat_id))

    # Sync engine state to current_auction for display/handlers
    state = auction_engine.state
    auction_engine.state.active = state.active
    auction_engine.state.title = state.title
    auction_engine.state.base_price = state.base_price
    auction_engine.state.current_price = state.current_price
    auction_engine.state.pending_price = state.pending_price
    auction_engine.state.pending_bidder = state.pending_bidder
    auction_engine.state.pending_bidder_name = state.pending_bidder_name
    auction_engine.state.bidders = state.bidders
    auction_engine.state.bin_price = state.bin_price
    auction_engine.state.bin_confirm_user_id = state.bin_confirm_user_id
    auction_engine.state.bin_confirm_expires_at = state.bin_confirm_expires_at
    auction_engine.state.highest_bidder = state.highest_bidder
    auction_engine.state.highest_bidder_name = state.highest_bidder_name
    auction_engine.state.start_time = state.start_time
    auction_engine.state.end_time = state.end_time
    auction_engine.state.session_id = state.session_id
    auction_engine.state.session_seq = state.session_seq
    auction_engine.state.chat_id = state.chat_id
    auction_engine.state.update_event = state.update_event

    # Get bot username for deep linking
    try:
        me = await context.bot.get_me()
        auction_engine.state.bot_username = me.username
        state.bot_username = me.username
    except telegram.error.TelegramError as e:
        logger.error(f"Failed to get bot info: {e}")

    text = generate_auction_text(auction_engine.state, auction_engine.get_item_duration())
    keyboard = generate_bid_keyboard(price)

    await query.delete_message()

    try:
        msg = await context.bot.send_photo(
            chat_id=target_chat_id,
            photo=photo_id,
            caption=text,
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        auction_engine.state.message_id = msg.message_id
        state.message_id = msg.message_id
        timer_task = asyncio.create_task(auction_timer_loop(context.bot))
        auction_engine.state.timer_task = timer_task
        state.timer_task = timer_task
        auction_engine.set_timer_task(timer_task)

        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"✅ 拍賣已發布到【{target_type}群組】！"
        )
    except telegram.error.TelegramError as e:
        logger.error(f"Failed to start auction: {e}")
        auction_engine.state.active = False
        state.active = False
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"❌ 發布失敗：{e}\n請檢查機器人是否在該群組且有發言權限。"
        )


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


def _safe_create_task(coro, name: str = "anonymous"):
    """Create a task that logs exceptions instead of silently swallowing them."""
    task = asyncio.create_task(coro)
    async def _log_on_error(t):
        try:
            await t
        except asyncio.CancelledError:
            pass  # Expected on shutdown
        except Exception as e:
            logger.error(f"Task '{name}' raised: {e}")
    asyncio.create_task(_log_on_error(task))
    return task


# --- Helper for Numpad Keyboard (Stateless Logic) ---
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
    if not auction_engine.state.active:
        await query.answer("❌ 拍賣已結束", show_alert=True)
        try:
            await query.message.delete()
        except telegram.error.TelegramError:
            logger.exception("Failed to delete message for ended auction")
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
        except telegram.error.TelegramError as e:
            logger.warning("Numpad edit failed: %s", e)
            
    elif action == "cancel":
        try:
            await query.message.delete()
        except telegram.error.TelegramError:
            logger.exception("Failed to delete cancel message")
        return
        
    elif action == "enter":
        price = int(next_val_str)
        if price <= 0:
            # We already answered, so we need to send a message or just ignore?
            # Or send a new answer? (Can't answer twice)
            # Send temp message
            msg = await context.bot.send_message(chat_id=query.message.chat_id, text="❌ 金額必須大於 0")
            await asyncio.sleep(2)
            try:
                await msg.delete()
            except telegram.error.TelegramError:
                logger.exception("Failed to delete error message")
            return
            
        # Submit bid
        # Delete numpad message first
        try:
            await query.message.delete()
        except telegram.error.TelegramError:
            logger.exception("Failed to delete numpad message on bid submit")
        
        # Process bid
        await process_blind_bid(user, price, query=None, bot=context.bot)
        # Send confirmation in PM
        try:
            await context.bot.send_message(
                chat_id=user.id,
                text=f"✅ 成功出價：${price}！\n如有更高出價，您將收到通知。"
            )
        except telegram.error.TelegramError as e:
            logger.warning(f"Failed to send numpad bid confirmation: {e}")
        return

async def auction_timer_loop(bot):
    """Delegate to AuctionEngine.timer_loop — single source of truth for countdown."""
    await auction_engine.timer_loop(bot)


async def handle_private_bid_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text

    if not text.isdigit():
        await update.message.reply_text("❌ 格式錯誤，請輸入純數字：")
        return BIDDING_PRICE

    price = int(text)

    if not auction_engine.state.active:
        await update.message.reply_text("❌ 拍賣已結束。")
        return ConversationHandler.END

    # 🔴 Block blacklisted users
    if await store.is_blacklisted(user.id):
        await update.message.reply_text("🚫 您已被禁止參與拍賣。")
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

async def handle_bin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    data = query.data

    if not auction_engine.state.active:
        await query.answer("❌ 拍賣已結束", show_alert=True)
        return

    try:
        auction_chat_id = int(auction_engine.state.chat_id)
        auction_message_id = int(auction_engine.state.message_id)
    except (ValueError, TypeError):
        await query.answer("⚠️ 拍賣狀態已重置，請管理員重新開拍賣", show_alert=True)
        return

    if query.message:
        if query.message.chat.id != auction_chat_id or query.message.message_id != auction_message_id:
            await query.answer("⚠️ 呢個按鈕已過期", show_alert=True)
            return

    bin_price = int(auction_engine.state.bin_price)
    if bin_price <= 0:
        await query.answer("❌ 此拍賣未設定一口價", show_alert=True)
        return

    if data == "bin_confirm":
        if not await store.is_registered(user.id):
            await query.answer("⚠️ 請先私訊機器人 /start 完成註冊", show_alert=True)
            return

        await query.answer(f"⚡️ 一口價 ${bin_price}，請確認", show_alert=True)
        auction_engine.state.bin_confirm_user_id = user.id
        auction_engine.state.bin_confirm_expires_at = datetime.now().timestamp() + 30
        try:
            await query.message.edit_reply_markup(reply_markup=build_bin_confirm_keyboard(bin_price, user.id))
        except telegram.error.TelegramError as e:
            logger.warning(f"Failed to show bin confirm keyboard: {e}")
        return

    if data.startswith("bin_cancel_"):
        try:
            confirm_uid = int(data.split("_", 2)[2])
        except (ValueError, IndexError):
            await query.answer("❌ 無效操作", show_alert=True)
            return

        if confirm_uid != user.id:
            await query.answer("⚠️ 呢個確認唔係你開嘅", show_alert=True)
            return

        await query.answer("已取消", show_alert=False)
        auction_engine.state.bin_confirm_user_id = None
        auction_engine.state.bin_confirm_expires_at = 0
        try:
            await query.message.edit_reply_markup(reply_markup=generate_bid_keyboard(auction_engine.state.current_price))
        except telegram.error.TelegramError as e:
            logger.warning(f"Failed to restore bid keyboard: {e}")
        return

    if data.startswith("bin_execute_"):
        try:
            confirm_uid = int(data.split("_", 2)[2])
        except (ValueError, IndexError):
            await query.answer("❌ 無效操作", show_alert=True)
            return

        if confirm_uid != user.id:
            await query.answer("⚠️ 呢個確認唔係你開嘅", show_alert=True)
            return

        # 🟡 Fix: check if bin confirm has expired
        now = datetime.now().timestamp()
        confirm_expires_at = auction_engine.state.bin_confirm_expires_at
        if confirm_expires_at and now >= confirm_expires_at:
            auction_engine.state.bin_confirm_user_id = None
            auction_engine.state.bin_confirm_expires_at = 0
            await query.answer("⚠️ 確認已過期，請重新點擊一口價", show_alert=True)
            return

        if not await store.is_registered(user.id):
            await query.answer("⚠️ 請先私訊機器人 /start 完成註冊", show_alert=True)
            return

        if not auction_engine.state.active:
            await query.answer("❌ 拍賣已結束", show_alert=True)
            return

        auction_engine.state.bin_confirm_user_id = None
        auction_engine.state.bin_confirm_expires_at = 0

        user_info = await store.get_user(user.id)
        winner_name = (user_info or {}).get("name") or user.first_name or ""

        bidders = auction_engine.state.bidders
        updated = False
        for b in bidders:
            if b.get("id") == user.id:
                b["name"] = winner_name
                b["price"] = bin_price
                b["time"] = datetime.now().timestamp()
                updated = True
                break
        if not updated:
            bidders.append({"id": user.id, "name": winner_name, "price": bin_price, "time": datetime.now().timestamp()})
        auction_engine.state.bidders = bidders

        await end_auction_buyout(context.bot, user.id, winner_name, bin_price)

        await query.answer("⚡️ 買斷成功！", show_alert=True)
        return

    await query.answer("❌ 無效操作", show_alert=True)


async def handle_bid_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # All inline bid buttons removed - redirect to private chat
    # Only bid_custom can reach here (URL buttons don't trigger callbacks)
    query = update.callback_query
    user = query.from_user
    
    if query.data != "bid_custom":
        # Unknown button, ignore
        return
    
    # Check if active
    if not auction_engine.state.active:
        await query.answer("❌ 拍賣已結束", show_alert=True)
        return

    # 🔴 Block blacklisted users
    if await store.is_blacklisted(user.id):
        await query.answer("🚫 您已被禁止參與拍賣。", show_alert=True)
        return

    # Check if user has registered
    if not await store.is_registered(user.id):
        bot_username = auction_engine.state.bot_username or context.bot.username
        if not bot_username:
            try:
                me = await context.bot.get_me()
                bot_username = me.username
            except telegram.error.TelegramError:
                logger.warning("Failed to get bot username for register link")
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
    bot_username = auction_engine.state.bot_username or context.bot.username
    if not bot_username:
        try:
            me = await context.bot.get_me()
            bot_username = me.username
        except telegram.error.TelegramError:
            logger.warning("Failed to get bot username for bid link")

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
            except ValueError:
                logger.exception("Failed to parse order date")
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
            InlineKeyboardButton("👥 匯出會員", callback_data="admin_export_members"),
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
    except telegram.error.TelegramError:
        logger.exception("Failed to delete admin command message")

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
        queue = auction_engine.state.batch_queue or []
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
        sched = auction_engine.state.scheduled_start or "未設定"
        await query.message.edit_text(
            f"🕐 <b>排程設定</b>\n\n"
            f"當前排程：{sched}\n\n"
            "請使用指令設定：\n"
            "<code>/schedule 2026-04-02 20:00</code>",
            parse_mode=ParseMode.HTML
        )
        return

    elif data == "admin_start_batch":
        if not auction_engine.state.batch_queue:
            await query.message.edit_text("❌ 請先【Import Batch】匯入拍賣品。")
            return
        if auction_engine.state.active:
            await query.message.edit_text("❌ 已有拍賣正在進行中。")
            return
        await query.message.edit_text("🚀 正在啟動批次拍賣...")
        await start_batch_command(update, context)
        return

    elif data == "admin_pause":
        if not auction_engine.state.batch_mode:
            await query.message.edit_text("❌ 目前沒有正在進行的批次拍賣。")
            return
        if auction_engine.state.batch_paused:
            await query.message.edit_text("⚠️ 已經是暫停狀態。")
            return
        auction_engine.state.batch_paused = True
        await query.message.edit_text("⏸ 批次拍賣已暫停。")
        return

    elif data == "admin_resume":
        if not auction_engine.state.batch_mode:
            await query.message.edit_text("❌ 目前沒有正在進行的批次拍賣。")
            return
        if not auction_engine.state.batch_paused:
            await query.message.edit_text("⚠️ 不是暫停狀態。")
            return
        auction_engine.state.batch_paused = False
        await query.message.edit_text("▶️ 批次拍賣已恢復！")
        return

    elif data == "admin_abort":
        if not auction_engine.state.batch_mode:
            await query.message.edit_text("❌ 目前沒有正在進行的批次拍賣。")
            return
        auction_engine.state.batch_abort = True
        auction_engine.state.batch_paused = False
        batch_state.clear()
        await query.message.edit_text("🛑 批次拍賣已終止。")
        return

    elif data == "admin_batch_status":
        # Show batch status
        queue = auction_engine.state.batch_queue
        queue_len = len(queue)
        if auction_engine.state.batch_mode:
            idx = auction_engine.state.batch_current_index + 1
            title = html.escape(auction_engine.state.title or "?")
            status = "⏸ 已暫停" if auction_engine.state.batch_paused else "▶️ 運行中"
            text = (
                f"📊 <b>批次狀態</b>\n\n"
                f"📦 隊列：{queue_len} 件\n"
                f"📌 進度：Item {idx}/{queue_len}\n"
                f"📝 當前：{title}\n"
                f"🔘 狀態：{status}\n"
                f"🕐 排程：{auction_engine.state.scheduled_start or '無'}"
            )
        else:
            text = (
                f"📊 <b>批次狀態</b>\n\n"
                f"📦 隊列：{queue_len} 件\n"
                f"🕐 排程：{auction_engine.state.scheduled_start or '未設定'}"
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
        if not auction_engine.state.active:
            await query.message.edit_text("❌ 當前沒有進行中的拍賣。")
            return
        if auction_engine.state.timer_task:
            auction_engine.state.timer_task.cancel()
            auction_engine.state.timer_task = None
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
        if not auction_engine.state.active:
            await query.message.reply_text("❌ 當前沒有進行中的拍賣。")
            return

        if auction_engine.state.timer_task:
            auction_engine.state.timer_task.cancel()
            auction_engine.state.timer_task = None

        await end_auction(context.bot)
        await query.message.reply_text("✅ 已強制結束拍賣。")

    elif query.data == "admin_end_session":
        if auction_engine.state.active:
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
        await _settle_by_date(store, query, date_str, context.bot)
        return

    elif query.data == "cancel_end_session":
        await query.message.edit_text("已取消結算操作。")
        return

    elif query.data == "admin_export":
        await query.answer()  # Acknowledge callback immediately
        await export_data(update, context)
        return

    elif query.data == "admin_export_members":
        await query.answer()  # Acknowledge callback immediately
        await export_members(update, context)
        return

    elif query.data == "admin_batch_menu":
        await show_batch_admin_panel(auction_engine, context.bot, chat_id=query.message.chat_id)
        return

    elif query.data == "admin_back":
        await admin_menu(update, context)
        return

    elif query.data == "admin_status":
        import platform
        from datetime import timedelta, timezone

        status = "🟢 運行中" if auction_engine.state.active else "⚪ 閒置"
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
        if not auction_engine.state.active:
            await query.message.edit_text("❌ 當前沒有進行中的拍賣。")
            return
        if auction_engine.state.timer_task:
            auction_engine.state.timer_task.cancel()
            auction_engine.state.timer_task = None
        await end_auction(context.bot)
        await query.message.edit_text("✅ 已強制結束拍賣。")
        return

    # Show admin panel for any unmatched admin callbacks
    await admin_menu(update, context)


async def force_end_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return

    if not auction_engine.state.active:
        await update.message.reply_text("❌ 當前沒有進行中的拍賣。")
        return

    # Cancel timer task if running
    if auction_engine.state.timer_task:
        auction_engine.state.timer_task.cancel()
        auction_engine.state.timer_task = None
    
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
        if auction_engine.state.active and text.isdigit():
            await handle_text_bid(update, context)

async def handle_text_bid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not auction_engine.state.active or not msg.text:
        return
        
    if msg.chat_id != auction_engine.state.chat_id:
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
            except telegram.error.TelegramError:
                logger.exception("Failed to delete bid prompt message")

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
    except telegram.error.TelegramError:
        logger.exception("Failed to delete bid text message")

async def process_blind_bid(user, price, query=None, bot=None):
    """Delegate to AuctionEngine.process_bid() — the single source of truth for bid logic."""
    # 🔴 Block blacklisted users
    if await store.is_blacklisted(user.id):
        if query:
            await query.answer("🚫 您已被禁止參與拍賣。", show_alert=True)
        return

    result = await auction_engine.process_bid(user.id, price, user.first_name, bot)

    if result["action"] == "error":
        if query:
            await query.answer(result["message"], show_alert=True)
        return

    if result["action"] == "buyout":
        target_bot = bot if bot else (query.bot if query else None)
        if target_bot:
            await end_auction(target_bot)
        if query:
            await query.answer("⚡️ 一口價成交！恭喜您！", show_alert=True)
        return

    # accepted — sync engine state back to current_auction for display callers
    state = auction_engine.state
    auction_engine.state.pending_price = state.pending_price
    auction_engine.state.pending_bidder = state.pending_bidder
    auction_engine.state.pending_bidder_name = state.pending_bidder_name
    auction_engine.state.bidders = state.bidders

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
    except telegram.error.TelegramError as e:
        logger.warning(f"Failed to notify outbid user {previous_bidder_id}: {e}")

async def start_next_queued_auction(bot):
    queue = await store.get_auction_queue()
    if not queue:
        return
    item = queue.pop(0)
    await store.set_auction_queue(queue)

    await asyncio.sleep(10)

    if auction_engine.state.active:
        queue.insert(0, item)
        await store.set_auction_queue(queue)
        return

    await start_auction_from_queue(bot, item)

async def start_auction_from_queue(bot, item):
    if auction_engine.state.active:
        return

    title = item.get("title", "未知商品")
    price = int(item.get("price", 0))
    bin_price = int(item.get("bin_price", 0))
    photo_id = item.get("photo_id")
    target_chat_id = item.get("target_chat_id")

    if not photo_id or not target_chat_id:
        return

    session_id, session_seq = await store.get_next_session()
    target_chat_id = int(target_chat_id)
    auction_engine.state.active = True
    auction_engine.state.title = title
    auction_engine.state.base_price = price
    auction_engine.state.current_price = price
    auction_engine.state.pending_price = price   # 暗標：pending = base price initially
    auction_engine.state.pending_bidder = None
    auction_engine.state.pending_bidder_name = "無"
    auction_engine.state.bidders = []
    auction_engine.state.bin_price = bin_price
    auction_engine.state.bin_confirm_user_id = None
    auction_engine.state.bin_confirm_expires_at = 0
    auction_engine.state.highest_bidder = None
    auction_engine.state.highest_bidder_name = "無"
    auction_engine.state.start_time = datetime.now()
    auction_engine.state.end_time = datetime.now().timestamp() + ITEM_DURATION
    auction_engine.state.session_id = session_id
    auction_engine.state.session_seq = session_seq
    auction_engine.state.chat_id = target_chat_id
    if auction_engine.state.update_event:
        auction_engine.state.update_event.clear()

    # Get bot username for deep linking
    try:
        me = await bot.get_me()
        auction_engine.state.bot_username = me.username
    except telegram.error.TelegramError as e:
        logger.error(f"Failed to get bot username: {e}")

    text = generate_auction_text(auction_engine.state, ITEM_DURATION)
    keyboard = generate_bid_keyboard(price)

    msg = await bot.send_photo(
        chat_id=target_chat_id,
        photo=photo_id,
        caption=text,
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML
    )
    auction_engine.state.message_id = msg.message_id
    auction_engine.state.timer_task = asyncio.create_task(auction_timer_loop(bot))

async def end_auction_buyout(bot, winner_id: int, winner_name: str, price: int):
    """Execute buyout — delegate state management to AuctionEngine."""
    await auction_engine.confirm_buyout(winner_id, winner_name, price)

    # Sync engine state back to current_auction
    state = auction_engine.state
    auction_engine.state._ending = state._ending
    auction_engine.state.active = state.active
    auction_engine.state.current_price = state.current_price
    auction_engine.state.highest_bidder = state.highest_bidder
    auction_engine.state.highest_bidder_name = state.highest_bidder_name
    auction_engine.state.bin_confirm_user_id = None
    auction_engine.state.bin_confirm_expires_at = 0

    timer_task = auction_engine.state.timer_task
    if timer_task:
        try:
            timer_task.cancel()
        except asyncio.CancelledError:
            pass  # Already cancelled or done
        except Exception:
            logger.exception("Failed to cancel timer task")

    title = state.title
    winner_prefix = html.escape(truncate_name_prefix(winner_name, 4))
    final_text = (
        f"✅ <b>已成交</b>\n"
        f"⚡️ <b>${price}</b>\n"
        f"🏆 得標：{winner_prefix}"
    )

    try:
        await bot.edit_message_caption(
            chat_id=auction_engine.state.chat_id,
            message_id=auction_engine.state.message_id,
            caption=final_text,
            reply_markup=None,
            parse_mode="HTML"
        )
    except telegram.error.TelegramError as e:
        err_str = str(e)
        logger.warning(f"Failed to edit auction message (buyout): {e}")
        if "429" in err_str:
            await asyncio.sleep(5)
            try:
                await bot.edit_message_caption(
                    chat_id=auction_engine.state.chat_id,
                    message_id=auction_engine.state.message_id,
                    caption=final_text,
                    reply_markup=None,
                    parse_mode="HTML"
                )
            except telegram.error.TelegramError as e2:
                logger.error(f"Retry also failed (buyout): {e2}")

    order = {
        "order_id": f"ORD-{int(datetime.now().timestamp())}",
        "user_id": winner_id,
        "item": title,
        "price": price,
        "time": datetime.now().isoformat(),
        "status": "pending",
        "session_id": auction_engine.state.session_id
    }
    await store.add_order(order)

    try:
        user_info = await store.get_user(winner_id)
        msg = (
            f"🎉 <b>買斷成功</b>\n\n"
            f"商品：<b>{html.escape(title)}</b>\n"
            f"金額：<b>${price}</b>\n"
            f"交收：{html.escape(user_info.get('pickup', '未定'))}\n\n"
            f"拍賣系統會另外再發送付款連結到您的 Email，請留意查收。"
        )
        await bot.send_message(chat_id=winner_id, text=msg, parse_mode="HTML")
    except telegram.error.TelegramError as e:
        logger.error(f"Failed to DM winner (buyout): {e}")

    auction_engine.state._ending = False

    if auction_engine.state.batch_mode and not auction_engine.state.batch_abort:
        _safe_create_task(run_batch_auction_loop(bot), "run_batch_loop")
    else:
        await start_next_queued_auction(bot)


async def end_auction(bot):
    """Delegate to AuctionEngine.end_auction() for state, then handle UI + orders."""
    result = await auction_engine.end_auction(bot)

    winner_id = result["winner_id"]
    winner_name = result["winner_name"]
    price = result["price"]
    sorted_bidders = result["sorted_bidders"]
    state = auction_engine.state
    title = state.title

    # Sync engine state back to current_auction for display
    auction_engine.state.active = state.active
    auction_engine.state.current_price = state.current_price
    auction_engine.state.highest_bidder = state.highest_bidder
    auction_engine.state.highest_bidder_name = state.highest_bidder_name
    auction_engine.state._ending = state._ending

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

    # Retry once on 429 (rate limit) after 5s
    edit_ok = False
    try:
        await bot.edit_message_caption(
            chat_id=auction_engine.state.chat_id,
            message_id=auction_engine.state.message_id,
            caption=final_text,
            reply_markup=None,
            parse_mode="HTML"
        )
        edit_ok = True
    except telegram.error.TelegramError as e:
        err_str = str(e)
        logger.warning(f"Failed to edit auction message: {e}")
        if "429" in err_str:
            logger.info("Rate limited (429); waiting 5s before retry...")
            await asyncio.sleep(5)
            try:
                await bot.edit_message_caption(
                    chat_id=auction_engine.state.chat_id,
                    message_id=auction_engine.state.message_id,
                    caption=final_text,
                    reply_markup=None,
                    parse_mode="HTML"
                )
                edit_ok = True
            except telegram.error.TelegramError as e2:
                logger.error(f"Retry also failed: {e2}")
        if not edit_ok:
            try:
                await bot.send_message(
                    chat_id=auction_engine.state.chat_id,
                    text=final_text,
                    parse_mode="HTML"
                )
            except telegram.error.TelegramError as e2:
                logger.error(f"Failed to send fallback message: {e2}")

    if winner_id:
        order = {
            "order_id": f"ORD-{int(datetime.now().timestamp())}",
            "user_id": winner_id,
            "item": title,
            "price": price,
            "time": datetime.now().isoformat(),
            "status": "pending",
            "session_id": auction_engine.state.session_id
        }
        await store.add_order(order)

        try:
            user_info = await store.get_user(winner_id)
            msg = (
                f"🎉 恭喜您標得 <b>{html.escape(title)}</b>！\n\n"
                f"金額：${price}\n"
                f"交收：{html.escape(user_info.get('pickup', '未定'))}\n\n"
                f"ℹ️ <b>付款安排</b>：\n"
                f"拍賣結束後，我們會另外再發送付款連結到您的 Email，請留意查收。"
            )
            await bot.send_message(chat_id=winner_id, text=msg, parse_mode="HTML")
        except telegram.error.TelegramError as e:
            logger.error(f"Failed to DM winner: {e}")
            await bot.send_message(
                chat_id=auction_engine.state.chat_id,
                text=f"⚠️ 無法私聊得標者 (ID: {winner_id})，請主動聯繫管理員。"
            )

    # Reset ending flag
    auction_engine.state._ending = False

    # Check if batch mode is active and auto-advance to next item
    if auction_engine.state.batch_mode and not auction_engine.state.batch_abort:
        _safe_create_task(run_batch_auction_loop(bot), "run_batch_loop")
    else:
        await start_next_queued_auction(bot)


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
        
    except (OSError, asyncio.TimeoutError) as e:
        logger.error(f"Failed to download image from {url}: {e}")
        return None
    except telegram.error.TelegramError as e:
        logger.error(f"Failed to upload image to bot from {url}: {e}")
        return None

async def run_batch_auction_loop(bot):
    """Main loop for batch auction - runs after each item ends."""
    # Wait for pause between items
    await asyncio.sleep(PAUSE_BETWEEN_ITEMS)

    # Check if abort was requested while waiting
    if auction_engine.state.batch_abort:
        await notify_batch_aborted(bot)
        return

    # Check if paused
    if auction_engine.state.batch_paused:
        # Wait until resumed
        while auction_engine.state.batch_paused and not auction_engine.state.batch_abort:
            await asyncio.sleep(1)
        if auction_engine.state.batch_abort:
            await notify_batch_aborted(bot)
            return

    # Increment index for the item we're about to start (0-based to 1-based)
    auction_engine.state.batch_current_index += 1
    
    if auction_engine.state.batch_current_index > len(auction_engine.state.batch_queue):
        # Batch complete
        await notify_batch_complete(bot)
        return

    item = auction_engine.state.batch_queue[auction_engine.state.batch_current_index - 1]  # -1 to convert 1-based index back to 0-based
    await start_single_batch_item(bot, item)


async def start_single_batch_item(bot, item):
    """Start a single auction item from the batch queue."""
    if auction_engine.state.batch_abort:
        return

    title = item.get("title", "未知商品")
    price = int(item.get("price", 0))
    bin_price = int(item.get("bin_price", 0))
    photo_id = item.get("photo_id")
    target_chat_id = item.get("target_chat_id")

    if not photo_id or not target_chat_id:
        logger.error(f"Batch item missing photo_id or target_chat_id: {title}")
        auction_engine.state.batch_current_index += 1
        _safe_create_task(run_batch_auction_loop(bot), "run_batch_loop")
        return

    target_chat_id = int(target_chat_id)

    # Delegate to AuctionEngine
    await auction_engine.start_auction(title, photo_id, price, bin_price, target_chat_id)

    # Sync engine state to current_auction for display
    state = auction_engine.state
    auction_engine.state.active = state.active
    auction_engine.state.title = state.title
    auction_engine.state.base_price = state.base_price
    auction_engine.state.current_price = state.current_price
    auction_engine.state.pending_price = state.pending_price
    auction_engine.state.pending_bidder = state.pending_bidder
    auction_engine.state.pending_bidder_name = state.pending_bidder_name
    auction_engine.state.bidders = state.bidders
    auction_engine.state.bin_price = state.bin_price
    auction_engine.state.bin_confirm_user_id = state.bin_confirm_user_id
    auction_engine.state.bin_confirm_expires_at = state.bin_confirm_expires_at
    auction_engine.state.highest_bidder = state.highest_bidder
    auction_engine.state.highest_bidder_name = state.highest_bidder_name
    auction_engine.state.start_time = state.start_time
    auction_engine.state.end_time = state.end_time
    auction_engine.state.session_id = state.session_id
    auction_engine.state.session_seq = state.session_seq
    auction_engine.state.chat_id = state.chat_id
    auction_engine.state._ending = state._ending
    if auction_engine.state.update_event:
        auction_engine.state.update_event.clear()

    try:
        me = await bot.get_me()
        auction_engine.state.bot_username = me.username
        state.bot_username = me.username
    except telegram.error.TelegramError as e:
        logger.error(f"Failed to get bot username: {e}")

    text = generate_auction_text(auction_engine.state, auction_engine.get_item_duration())
    keyboard = generate_bid_keyboard(price)

    try:
        msg = await bot.send_photo(
            chat_id=target_chat_id,
            photo=photo_id,
            caption=text,
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        auction_engine.state.message_id = msg.message_id
        state.message_id = msg.message_id

        if auction_engine.state.timer_task:
            auction_engine.state.timer_task.cancel()

        timer_task = asyncio.create_task(auction_timer_loop(bot))
        auction_engine.state.timer_task = timer_task
        state.timer_task = timer_task
        auction_engine.set_timer_task(timer_task)
    except telegram.error.TelegramError as e:
        logger.error(f"Failed to start batch item: {e}")
        auction_engine.state.active = False
        state.active = False


async def notify_batch_progress(bot):
    """Notify admin of batch progress and update the admin panel."""
    # Update the admin panel
    await show_batch_admin_panel(auction_engine, bot)

    # Also send a detailed progress message
    queue_len = len(auction_engine.state.batch_queue)
    current_idx = auction_engine.state.batch_current_index + 1  # 1-indexed for display
    title = auction_engine.state.title or "?"
    
    # Try to find admin chat_id from config or use first admin
    admin_id = ADMIN_IDS[0] if ADMIN_IDS else None
    
    if admin_id:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=f"📦 <b>批次拍賣進度</b>\n\n"
                     f"項目：{current_idx}/{queue_len}\n"
                     f"當前：{html.escape(title)}\n"
                     f"模式：{'運行中' if not auction_engine.state.batch_paused else '⏸ 已暫停'}",
                parse_mode=ParseMode.HTML
            )
        except telegram.error.TelegramError as e:
            logger.warning(f"Failed to notify admin of batch progress: {e}")


async def notify_batch_complete(bot):
    """Notify when batch auction is complete."""
    total_items = len(auction_engine.state.batch_queue)
    admin_id = ADMIN_IDS[0] if ADMIN_IDS else None
    
    # Reset batch state
    auction_engine.state.batch_mode = False
    auction_engine.state.batch_queue = []
    auction_engine.state.batch_current_index = 0
    auction_engine.state.batch_paused = False
    auction_engine.state.batch_abort = False
    auction_engine.state.scheduled_start = None
    
    batch_state.clear()

    if admin_id:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=f"✅ <b>批次拍賣完成！</b>\n\n共完成 {total_items} 件拍賣品",
                parse_mode=ParseMode.HTML
            )
        except telegram.error.TelegramError as e:
            logger.warning(f"Failed to notify admin of batch complete: {e}")


async def notify_batch_aborted(bot):
    """Notify when batch auction is aborted."""
    admin_id = ADMIN_IDS[0] if ADMIN_IDS else None
    
    # Reset batch state
    auction_engine.state.batch_mode = False
    auction_engine.state.batch_queue = []
    auction_engine.state.batch_current_index = 0
    auction_engine.state.batch_paused = False
    auction_engine.state.batch_abort = False
    auction_engine.state.scheduled_start = None
    
    if admin_id:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text="🛑 <b>批次拍賣已終止</b>\n\n隊列已清空。",
                parse_mode=ParseMode.HTML
            )
        except telegram.error.TelegramError as e:
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
    auction_engine.state.batch_queue = parsed_items
    auction_engine.state.batch_mode = False  # Will be set to True when started
    auction_engine.state.batch_current_index = 0
    auction_engine.state.batch_paused = False
    auction_engine.state.batch_abort = False

    await update.message.reply_text(
        f"✅ <b>已匯入 {len(parsed_items)} 件拍賣品：</b>\n\n" +
        "\n".join(f"{i+1}. {html.escape(item['title'])} - 起標 ${item['price']}" for i, item in enumerate(parsed_items)),
        parse_mode=ParseMode.HTML
    )
    # Show admin batch control panel
    await show_batch_admin_panel(auction_engine, context.bot, chat_id=update.effective_chat.id)


async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /schedule command - set batch auction start time."""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ 權限不足")
        return

    if not auction_engine.state.batch_queue:
        await update.message.reply_text("❌ 請先使用 <code>/import_batch</code> 匯入拍賣品。", parse_mode=ParseMode.HTML)
        return

    args = context.args
    if not args:
        # Show current schedule or prompt for datetime
        if auction_engine.state.scheduled_start:
            sched_time = auction_engine.state.scheduled_start
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
    auction_engine.state.scheduled_start = scheduled_dt.strftime("%Y-%m-%d %H:%M")

    # Calculate estimated end time
    queue_len = len(auction_engine.state.batch_queue)
    # Each item: ITEM_DURATION (25s) + PAUSE_BETWEEN_ITEMS (3s) = 28s
    # Last item doesn't need pause after
    total_duration_seconds = queue_len * ITEM_DURATION + (queue_len - 1) * PAUSE_BETWEEN_ITEMS
    estimated_end_dt = scheduled_dt + timedelta(seconds=total_duration_seconds)

    # Get target group info
    target_type = auction_engine.state.batch_target_group
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
    await show_batch_admin_panel(auction_engine, context.bot, chat_id=update.effective_chat.id)


async def start_batch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start_batch command - start the batch auction immediately or at scheduled time."""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ 權限不足")
        return

    if not auction_engine.state.batch_queue:
        await update.message.reply_text("❌ 請先使用 <code>/import_batch</code> 匯入拍賣品。", parse_mode=ParseMode.HTML)
        return

    if auction_engine.state.active:
        await update.message.reply_text("❌ 已有拍賣正在進行中，請先結束後再試。")
        return

    # Check if there's a scheduled time and if it's reached
    scheduled_start = auction_engine.state.scheduled_start
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
            logger.exception("Invalid schedule format, proceeding with immediate start")

    # Determine target group
    target_type = auction_engine.state.batch_target_group
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
    auction_engine.state.batch_mode = True
    auction_engine.state.batch_current_index = 0
    auction_engine.state.batch_paused = False
    auction_engine.state.batch_abort = False

    # Add target_chat_id to each item in queue
    for item in auction_engine.state.batch_queue:
        item["target_chat_id"] = target_chat_id

    # Get bot instance for the batch loop
    bot = context.bot

    # Pre-download all images if they are URLs
    await update.message.reply_text("📥 正在下載圖片中...")
    for i, item in enumerate(auction_engine.state.batch_queue):
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
    queue_len = len(auction_engine.state.batch_queue)
    await update.message.reply_text(
        f"🚀 <b>批次拍賣開始！</b>\n\n"
        f"📦 件數：{queue_len} 件\n"
        f"📢 發佈群組：{target_desc}\n\n"
        f"第一件拍賣品即將開始...",
        parse_mode=ParseMode.HTML
    )

    # Reset admin panel tracking so it sends a new message
    batch_state.clear()

    # Show admin batch control panel
    await show_batch_admin_panel(bot, chat_id=update.effective_chat.id)

    # Start first item
    item = auction_engine.state.batch_queue[0]
    await start_single_batch_item(bot, item)


async def pause_batch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /pause_batch command - pause the batch auction."""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ 權限不足")
        return

    if not auction_engine.state.batch_mode:
        await update.message.reply_text("❌ 目前沒有正在進行的批次拍賣。")
        return

    if auction_engine.state.batch_paused:
        await update.message.reply_text("⚠️ 批次拍賣已經是暫停狀態。")
        return

    auction_engine.state.batch_paused = True
    
    await update.message.reply_text(
        f"⏸ <b>批次拍賣已暫停</b>",
        parse_mode=ParseMode.HTML
    )
    # Update admin panel
    await show_batch_admin_panel(auction_engine, context.bot, chat_id=update.effective_chat.id)


async def resume_batch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /resume_batch command - resume the batch auction."""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ 權限不足")
        return

    if not auction_engine.state.batch_mode:
        await update.message.reply_text("❌ 目前沒有正在進行的批次拍賣。")
        return

    if not auction_engine.state.batch_paused:
        await update.message.reply_text("⚠️ 批次拍賣不是在暫停狀態。")
        return

    auction_engine.state.batch_paused = False
    
    await update.message.reply_text(
        f"▶️ <b>批次拍賣已恢復！</b>",
        parse_mode=ParseMode.HTML
    )
    # Update admin panel
    await show_batch_admin_panel(auction_engine, context.bot, chat_id=update.effective_chat.id)


async def abort_batch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /abort_batch command - abort the entire batch auction."""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ 權限不足")
        return

    if not auction_engine.state.batch_mode:
        await update.message.reply_text("❌ 目前沒有正在進行的批次拍賣。")
        return

    auction_engine.state.batch_abort = True
    auction_engine.state.batch_paused = False  # Unpause so loop can exit

    await update.message.reply_text(
        f"🛑 <b>批次拍賣已終止</b>",
        parse_mode=ParseMode.HTML
    )
    batch_state.clear()


async def batch_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /batch_status command - show batch queue progress."""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ 權限不足")
        return

    if not auction_engine.state.batch_mode:
        # Show queue status even if not started
        queue = auction_engine.state.batch_queue
        if not queue:
            await update.message.reply_text("❌ 目前沒有任何批次拍賣品。\n使用 <code>/import_batch</code> 匯入拍賣品。", parse_mode=ParseMode.HTML)
            return
        
        queue_len = len(queue)
        await update.message.reply_text(
            f"📋 <b>批次拍賣狀態</b>\n\n"
            f"📦 隊列中的拍賣品：{queue_len} 件\n"
            f"🕐 排程時間：{auction_engine.state.scheduled_start or '未設定'}\n"
            f"📢 發佈群組：{auction_engine.state.batch_target_group}\n\n"
            f"💡 使用 <code>/start_batch</code> 開始拍賣。",
            parse_mode=ParseMode.HTML
        )
        return

    queue_len = len(auction_engine.state.batch_queue)
    current_idx = auction_engine.state.batch_current_index + 1  # 1-indexed
    current_title = auction_engine.state.title or "?"
    status = "⏸ 已暫停" if auction_engine.state.batch_paused else "▶️ 運行中"
    
    remaining = queue_len - auction_engine.state.batch_current_index
    
    await update.message.reply_text(
        f"📋 <b>批次拍賣狀態</b>\n\n"
        f"項目：Item {current_idx}/{queue_len}\n"
        f"當前：{html.escape(current_title)}\n"
        f"狀態：{status}\n"
        f"剩餘：{remaining} 件\n"
        f"🕐 排程：{auction_engine.state.scheduled_start or '無'}",
        parse_mode=ParseMode.HTML
    )
    # Also show/update the admin panel with buttons
    await show_batch_admin_panel(auction_engine, context.bot, chat_id=update.effective_chat.id)


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
    target_type = auction_engine.state.batch_target_group
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
    except telegram.error.TelegramError as e:
        logger.error(f"Failed to send broadcast: {e}")
        await update.message.reply_text(f"❌ 廣播發送失敗：{e}")


# --- CSV Export & Blacklist ---

async def export_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """匯出會員資料"""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return

    message = update.effective_message or update.message
    
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

async def cancel_import_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    await message.reply_text("已取消匯入會員。")
    return ConversationHandler.END


async def import_members_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    if user.id not in ADMIN_IDS:
        return ConversationHandler.END
    if not DATABASE_URL:
        await message.reply_text("❌ 未設定 DATABASE_URL，無法匯入到資料庫。")
        return ConversationHandler.END

    await message.reply_text(
        "請上傳 CSV 檔（欄位必須係：user_id,name,phone,email,pickup）。\n"
        "如要取消，輸入 /cancel"
    )
    return WAITING_MEMBERS_CSV


async def import_members_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    if user.id not in ADMIN_IDS:
        return ConversationHandler.END

    doc = update.message.document if update.message else None
    if not doc or not (doc.file_name or "").lower().endswith(".csv"):
        await message.reply_text("❌ 請上傳 .csv 檔案。")
        return WAITING_MEMBERS_CSV

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        data = await tg_file.download_as_bytearray()
    except telegram.error.TelegramError as e:
        await message.reply_text(f"❌ 下載檔案失敗：{e}")
        return ConversationHandler.END
    except OSError as e:
        await message.reply_text(f"❌ 下載檔案失敗：{e}")
        return ConversationHandler.END

    try:
        text = bytes(data).decode("utf-8-sig")
    except UnicodeDecodeError:
        text = bytes(data).decode("utf-8", errors="replace")

    reader = csv.DictReader(io.StringIO(text))
    required = {"user_id", "name", "phone", "email", "pickup"}
    if not reader.fieldnames or not required.issubset(set(h.strip() for h in reader.fieldnames if h)):
        await message.reply_text("❌ CSV 欄位唔正確，必須包含：user_id,name,phone,email,pickup")
        return ConversationHandler.END

    ok = 0
    skipped = 0
    bad_rows = []

    for idx, row in enumerate(reader, start=2):
        uid_raw = (row.get("user_id") or "").strip()
        name = (row.get("name") or "").strip()
        phone = (row.get("phone") or "").strip()
        email = (row.get("email") or "").strip()
        pickup = (row.get("pickup") or "").strip() or "旺角店自取"

        try:
            uid = int(uid_raw)
        except ValueError:
            skipped += 1
            bad_rows.append((idx, "invalid user_id"))
            continue

        if not name or not phone:
            skipped += 1
            bad_rows.append((idx, "missing name/phone"))
            continue

        info = {"name": name, "phone": phone, "email": email, "pickup": pickup}
        try:
            await store.register_user(uid, info)
            ok += 1
        except Exception as e:
            # store.register_user can raise various DB-specific exceptions
            skipped += 1
            bad_rows.append((idx, f"db error: {e}"))

    summary = f"✅ 匯入完成：{ok} 筆\n❌ 略過：{skipped} 筆"
    if bad_rows:
        preview = "\n".join(f"第 {ln} 行：{reason}" for ln, reason in bad_rows[:15])
        if len(bad_rows) > 15:
            preview += f"\n… 另外仲有 {len(bad_rows)-15} 筆"
        summary += f"\n\n{preview}"

    await message.reply_text(summary)
    return ConversationHandler.END

# --- CSV Export & Blacklist ---
async def export_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return

    message = update.effective_message or update.message
    
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
    except (ValueError, IndexError):
        logger.exception("Failed to parse /ban arguments")
        await update.message.reply_text("用法: /ban <user_id>")

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    try:
        target_id = int(context.args[0])
        await store.remove_blacklist(target_id)
        await update.message.reply_text(f"✅ 已解封用戶 {target_id}")
    except (ValueError, IndexError):
        logger.exception("Failed to parse /unban arguments")
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
    port = int(os.environ.get('PORT', 8080))
    site = aiohttp.web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info("Web server started on port 8080")

async def handle_webapp_bid(update: Update, context: ContextTypes.DEFAULT_TYPE, _store):
    # Handle data received from WebApp
    if not auction_engine.state.active:
        return

    data = update.effective_message.web_app_data.data
    user = update.effective_user
    
    if not data or not data.isdigit():
        return
        
    price = int(data)
    
    # Process bid directly (blind mode - validate against pending_price)
    if price <= auction_engine.state.pending_price:
        # WebApp doesn't show alert easily unless we reply
        pass  # Silent ignore
    
    # Check registration
    if not await _store.is_registered(user.id):
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
    except telegram.error.TelegramError as e:
        logger.warning(f"Failed to send webapp bid confirmation: {e}")
    
    # Optional: Send confirmation in chat? process_bid usually updates the main message.
    # But we might want to delete the "service message" that Telegram sends when WebApp data is received.
    try:
        await update.effective_message.delete()
    except telegram.error.TelegramError:
        logger.exception("Failed to delete webapp bid confirmation message")

# --- 主程式 ---
async def main():
    global auction_engine
    # 創建並連接數據庫
    store = await create_store(DATABASE_URL)
    await store.connect()

    # 初始化 AuctionEngine（逐漸接管 current_auction 全局 dict）
    auction_engine = AuctionEngine(store, ITEM_DURATION)

    # 啟動 Web Server (為了 Zeabur 保持活躍)
    await run_web_server()

    # 設置 Bot
    application = Application.builder().token(TOKEN).build()

    # 註冊處理器（遷移到 core/handlers.py）
    _reg_fns = build_registration_handlers(store, auction_engine, ADMIN_IDS)
    _start_reg, _get_name, _get_phone, _get_email, _get_pickup, _cancel_reg = _reg_fns
    reg_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", _start_reg),
            CallbackQueryHandler(_start_reg, pattern="^edit_profile$")
        ],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, _get_name)],
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, _get_phone)],
            EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, _get_email)],
            PICKUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, _get_pickup)],
            BIDDING_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_private_bid_text)],
        },
        fallbacks=[CommandHandler("cancel", _cancel_reg)],
    )
    
    # Admin auction creation handlers（遷移到 core/admin.py）
    _admin_fns = build_admin_handlers(store, auction_engine, ADMIN_IDS)
    (_new_auction_start, _get_auction_photo, _get_auction_title, _get_auction_price, _get_bin_price, _cancel_admin) = _admin_fns
    auction_handler = ConversationHandler(
        entry_points=[
            CommandHandler("new_auction", _new_auction_start),
            CallbackQueryHandler(_new_auction_start, pattern="^admin_add_single$"),
        ],
        states={
            WAITING_PHOTO: [MessageHandler(filters.PHOTO, _get_auction_photo)],
            WAITING_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, _get_auction_title)],
            WAITING_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, _get_auction_price)],
            WAITING_BIN_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, _get_bin_price)],
        },
        fallbacks=[CommandHandler("cancel", _cancel_admin)],
    )

    import_members_handler = ConversationHandler(
        entry_points=[CommandHandler("import_members", import_members_start)],
        states={
            WAITING_MEMBERS_CSV: [MessageHandler(filters.Document.ALL, import_members_receive)],
        },
        fallbacks=[CommandHandler("cancel", cancel_import_members)],
    )

    application.add_handler(reg_handler)
    application.add_handler(auction_handler)
    application.add_handler(import_members_handler)
    application.add_handler(CallbackQueryHandler(start_auction_action, pattern="^start_auction_"))
    application.add_handler(CallbackQueryHandler(queue_auction_action, pattern="^queue_auction_"))
    application.add_handler(CallbackQueryHandler(handle_bin_callback, pattern="^bin_"))
    application.add_handler(CallbackQueryHandler(handle_bid_button, pattern="^bid_"))
    application.add_handler(CallbackQueryHandler(handle_numpad_click, pattern="^numpad_"))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern="^admin_"))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern="^settle_date_"))
    application.add_handler(CallbackQueryHandler(lambda u, c: _settle_daily(store, u.callback_query, u.effective_bot), pattern="^confirm_settle_date$"))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern="^cancel_end_session$"))
    # FIXED: handle_batch_callback was undefined - comment out until implemented
    # application.add_handler(CallbackQueryHandler(handle_batch_callback, pattern="^batch_"))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern="^adm_ord_"))
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
    application.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, lambda u, c: handle_webapp_bid(u, c, store)))

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
    import aiohttp.web  # Import here to avoid circular or top-level issues if not installed

    if not TOKEN:
        logger.error("Error: BOT_TOKEN is not set in environment variables.")
        exit(1)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass  # Intentional: graceful shutdown on Ctrl+C
    except Exception:
        logger.exception("Fatal error during startup, exiting")
        exit(1)
