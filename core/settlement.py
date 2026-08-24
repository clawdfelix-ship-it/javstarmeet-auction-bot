"""Settlement logic - billing winners by date."""
import html
import logging
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from store.base import Store

logger = logging.getLogger(__name__)


async def process_settlement_by_date(
    store: "Store",
    query,
    date_str: str,
    bot,
) -> None:
    """Settle orders for a specific date - send bills to each winner."""
    await query.message.edit_text(f"⏳ 正在統計 {date_str} 的訂單並發送帳單...")

    orders = await store.get_all_orders()

    target_orders = []
    for o in orders:
        created_at = o.get('created_at') or o.get('time')
        if isinstance(created_at, str):
            if created_at.startswith(date_str):
                target_orders.append(o)
        elif isinstance(created_at, datetime):
            if created_at.strftime('%Y-%m-%d') == date_str:
                target_orders.append(o)

    if not target_orders:
        await query.message.edit_text(f"❌ {date_str} 沒有任何訂單。")
        return

    user_orders: dict = {}
    for o in target_orders:
        uid = o['user_id']
        if uid not in user_orders:
            user_orders[uid] = []
        user_orders[uid].append(o)

    success_count = 0
    fail_count = 0

    for uid, u_orders in user_orders.items():
        try:
            user_info = await store.get_user(uid)
            if not user_info:
                continue

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

            await bot.send_message(chat_id=uid, text=bill_text, parse_mode="HTML")
            success_count += 1

        except Exception as e:
            logger.error(f"Failed to send bill to {uid}: {e}")
            fail_count += 1

    await query.message.edit_text(
        f"✅ <b>結算完成！</b>\n\n"
        f"📅 日期：{date_str}\n"
        f"• 總訂單數：{len(target_orders)}\n"
        f"• 中標人數：{len(user_orders)}\n"
        f"• 發送成功：{success_count}\n"
        f"• 發送失敗：{fail_count}",
        parse_mode="HTML"
    )


async def process_daily_settlement(store: "Store", query, bot) -> None:
    """Settle today's orders."""
    await query.message.edit_text("⏳ 正在統計今日訂單並發送帳單...")

    orders = await store.get_all_orders()
    today_str = datetime.now().strftime('%Y-%m-%d')

    today_orders = []
    for o in orders:
        created_at = o.get('created_at') or o.get('time')
        if isinstance(created_at, str):
            if created_at.startswith(today_str):
                today_orders.append(o)
        elif isinstance(created_at, datetime):
            if created_at.strftime('%Y-%m-%d') == today_str:
                today_orders.append(o)

    if not today_orders:
        await query.message.edit_text("❌ 今日沒有任何訂單。")
        return

    user_orders: dict = {}
    for o in today_orders:
        uid = o['user_id']
        if uid not in user_orders:
            user_orders[uid] = []
        user_orders[uid].append(o)

    success_count = 0
    fail_count = 0

    for uid, u_orders in user_orders.items():
        try:
            user_info = await store.get_user(uid)
            if not user_info:
                continue

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

            await bot.send_message(chat_id=uid, text=bill_text, parse_mode="HTML")
            success_count += 1

        except Exception as e:
            logger.error(f"Failed to send bill to {uid}: {e}")
            fail_count += 1

    await query.message.edit_text(
        f"✅ <b>結算完成！</b>\n\n"
        f"• 總訂單數：{len(today_orders)}\n"
        f"• 中標人數：{len(user_orders)}\n"
        f"• 發送成功：{success_count}\n"
        f"• 發送失敗：{fail_count}",
        parse_mode="HTML"
    )
