"""Telegram command handlers - registration flow."""
import asyncio
import html
import logging
import os
import re

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
    WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

logger = logging.getLogger(__name__)

# Conversation states (must match main.py values)
NAME, PHONE, EMAIL, PICKUP = range(4)
BIDDING_PRICE = 8


def build_registration_handlers(store, auction_engine, ADMIN_IDS):
    """
    Return a list of registration handler functions.
    Call this from main.py to wire up the ConversationHandler.
    """

    async def start_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        is_edit = False

        if update.callback_query:
            await update.callback_query.answer()
            if update.callback_query.data == "edit_profile":
                is_edit = True

        if not is_edit and context.args:
            arg = context.args[0]

            if arg == 'bid':
                if not await store.is_registered(user.id):
                    await update.message.reply_text(
                        "⚠️ 請先完成註冊才能出價！\n請輸入您的 <b>稱呼 (Name)</b>：",
                        parse_mode=ParseMode.HTML
                    )
                    return NAME

                user_info = await store.get_user(user.id)
                missing = []
                if not user_info.get('name'): missing.append('稱呼')
                if not user_info.get('phone'): missing.append('電話')
                if not user_info.get('email'): missing.append('Email')
                if not user_info.get('pickup'): missing.append('交收地點')

                if missing:
                    await update.message.reply_text(
                        f"⚠️ 請先補全以下資料才能出價：\n" +
                        "\n".join(f"- {m}" for m in missing) +
                        "\n\n請點擊 /start 更新資料",
                        parse_mode=ParseMode.HTML
                    )
                    return ConversationHandler.END

                if not auction_engine.state.active:
                    await update.message.reply_text("❌ 當前沒有進行中的拍賣。")
                    return ConversationHandler.END

                await update.message.reply_text(
                    f"🔥 <b>正在拍賣：{html.escape(auction_engine.state.title or '?')}</b>\n\n"
                    f"請輸入您的 <b>出價金額</b> (純數字)：",
                    parse_mode=ParseMode.HTML
                )
                return BIDDING_PRICE

            elif arg == 'bid_webapp':
                if not await store.is_registered(user.id):
                    await update.message.reply_text(
                        "⚠️ 請先完成註冊才能出價！\n請輸入您的 <b>稱呼 (Name)</b>：",
                        parse_mode=ParseMode.HTML
                    )
                    return NAME

                user_info = await store.get_user(user.id)
                missing = []
                if not user_info.get('name'): missing.append('稱呼')
                if not user_info.get('phone'): missing.append('電話')
                if not user_info.get('email'): missing.append('Email')
                if not user_info.get('pickup'): missing.append('交收地點')

                if missing:
                    await update.message.reply_text(
                        f"⚠️ 請先補全以下資料才能出價：\n" +
                        "\n".join(f"- {m}" for m in missing) +
                        "\n\n請點擊 /start 更新資料",
                        parse_mode=ParseMode.HTML
                    )
                    return ConversationHandler.END

                if not auction_engine.state.active:
                    await update.message.reply_text("❌ 當前沒有進行中的拍賣。")
                    return ConversationHandler.END

                webapp_url = os.getenv("WEBAPP_URL")
                if not webapp_url:
                    await update.message.reply_text("⚠️ 系統未配置 WebApp，請使用傳統出價方式。")
                    return ConversationHandler.END

                if not webapp_url.startswith("https://"):
                    webapp_url = f"https://{webapp_url}"

                keyboard = [[InlineKeyboardButton("✍️ 開啟出價頁面", web_app=WebAppInfo(url=webapp_url))]]
                await update.message.reply_text(
                    "👇 請點擊下方按鈕開啟出價視窗：",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                return ConversationHandler.END

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

        if not is_edit and await store.is_registered(user.id):
            user_info = await store.get_user(user.id)
            missing = []
            if not user_info.get('name'): missing.append('稱呼')
            if not user_info.get('phone'): missing.append('電話')
            if not user_info.get('email'): missing.append('Email')
            if not user_info.get('pickup'): missing.append('交收地點')
            if missing:
                is_edit = True

        msg_text = "👋 歡迎來到極速拍賣機器人！\n為了確保交易順利，請先完成簡單的登記。\n\n請輸入您的 <b>稱呼 (Name)</b>："
        if is_edit:
            existing_info = await store.get_user(user.id)
            existing_name = (existing_info or {}).get('name', '')
            existing_phone = (existing_info or {}).get('phone', '')
            existing_email = (existing_info or {}).get('email', '')
            existing_pickup = (existing_info or {}).get('pickup', '')

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
        if text.lower() == 'skip' and context.user_data.get('reg_name'):
            pass
        else:
            context.user_data['reg_name'] = text
        await update.message.reply_text("✅ 收到。請輸入您的 <b>電話號碼</b> (例如 91234567)：", parse_mode=ParseMode.HTML)
        return PHONE

    async def get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text.lower() == 'skip' and context.user_data.get('reg_phone'):
            pass
        else:
            context.user_data['reg_phone'] = text
        await update.message.reply_text("✅ 收到。請輸入您的 <b>Email</b> (用於得標通知)：", parse_mode=ParseMode.HTML)
        return EMAIL

    async def get_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text.lower() == 'skip' and context.user_data.get('reg_email'):
            pass
        else:
            email_pattern = r'^[\w.\-]+@[\w.\-]+\.\w+$'
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
        if text.lower() == 'skip' and context.user_data.get('reg_pickup'):
            pass
        elif text in ['旺角店自取']:
            context.user_data['reg_pickup'] = text
        else:
            await update.message.reply_text(
                "⚠️ 請選擇有效的選項 (旺角店自取)，或輸入「skip」保留現有值。"
            )
            return PICKUP

        user = update.effective_user
        info = {
            "name": context.user_data['reg_name'],
            "phone": context.user_data['reg_phone'],
            "email": context.user_data['reg_email'],
            "pickup": context.user_data['reg_pickup']
        }
        await store.register_user(user.id, info)

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

    return [
        start_register,
        get_name,
        get_phone,
        get_email,
        get_pickup,
        cancel_register,
    ]
