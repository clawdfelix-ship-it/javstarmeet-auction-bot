"""Admin command handlers - auction creation flow."""
import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

logger = logging.getLogger(__name__)

# Conversation states (must match main.py values)
WAITING_PHOTO, WAITING_TITLE, WAITING_PRICE, WAITING_BIN_PRICE = range(4, 8)


def build_admin_handlers(store, auction_engine, ADMIN_IDS):
    """
    Return a list of admin handler functions.
    Call this from main.py to wire up the ConversationHandler.
    """

    async def new_auction_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        message = update.effective_message
        if user.id not in ADMIN_IDS:
            if update.callback_query:
                await update.callback_query.answer("⛔ 權限不足", show_alert=True)
            await message.reply_text("⛔ 權限不足")
            return ConversationHandler.END

        if update.callback_query:
            await update.callback_query.answer()
        await message.reply_text("請發送拍賣品的 <b>圖片</b>：", parse_mode=ParseMode.HTML)
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

        await update.message.reply_text(
            "請輸入 <b>一口價 (Buy It Now)</b> 金額 (純數字，輸入 0 代表不設)：",
            parse_mode=ParseMode.HTML
        )
        return WAITING_BIN_PRICE

    async def get_bin_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            bin_price = int(update.message.text)
            context.user_data['auc_bin_price'] = bin_price
        except ValueError:
            await update.message.reply_text("❌ 格式錯誤，請輸入純數字：")
            return WAITING_BIN_PRICE

        photo_id = context.user_data['auc_photo']
        title = context.user_data['auc_title']
        price = context.user_data['auc_price']
        safe_title = html.escape(title)

        bin_text = f"\n⚡️ 一口價：${bin_price}" if bin_price > 0 else ""

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

    async def cancel_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("已取消上架流程。")
        return ConversationHandler.END

    return [
        new_auction_start,
        get_auction_photo,
        get_auction_title,
        get_auction_price,
        get_bin_price,
        cancel_admin,
    ]
