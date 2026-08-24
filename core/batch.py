"""Batch auction queue management."""
import asyncio
import html
import logging
import os
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Global state for batch admin panel (kept at module level for simplicity)
BATCH_PANEL_MESSAGE_ID = None
BATCH_PANEL_CHAT_ID = None


def get_batch_state(engine) -> str:
    """Determine current batch state for panel display."""
    if engine.state.batch_abort:
        return "aborting"
    if engine.state.batch_mode:
        if engine.state.batch_paused:
            return "paused"
        return "running"
    if engine.state.scheduled_start:
        return "scheduled"
    queue = engine.state.batch_queue
    if queue:
        return "idle"
    return "empty"


def build_batch_admin_keyboard(state: str) -> "InlineKeyboardMarkup":
    """Build inline keyboard based on current batch state."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
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


def build_batch_admin_text(state: str, engine) -> str:
    """Build admin panel text based on current batch state."""
    from config import ITEM_DURATION, PAUSE_BETWEEN_ITEMS
    queue_len = len(engine.state.batch_queue)
    sched_time = engine.state.scheduled_start or "未設定"
    target_type = engine.state.batch_target_group or "prod"
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
        idx = engine.state.batch_current_index + 1
        title = html.escape(engine.state.title or "?")
        return (
            "📋 <b>批次拍賣控制台</b>\n\n"
            f"📦 進度：Item {idx}/{queue_len}\n"
            f"📌 當前：{title}\n"
            f"🕐 排程：{sched_time}\n"
            f"📢 目標：{target_desc}\n\n"
            f"▶️ <b>拍賣進行中...</b>"
        )

    if state == "paused":
        idx = engine.state.batch_current_index + 1
        title = html.escape(engine.state.title or "?")
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


async def show_batch_admin_panel(bot, engine, chat_id=None, update_existing=True):
    """Send or edit the admin batch control panel message."""
    global BATCH_PANEL_MESSAGE_ID, BATCH_PANEL_CHAT_ID

    state = get_batch_state(engine)
    text = build_batch_admin_text(state, engine)
    keyboard = build_batch_admin_keyboard(state)

    admin_id = engine.store and getattr(engine.store, '_admin_id', None)
    from config import ADMIN_IDS
    if not admin_id and ADMIN_IDS:
        admin_id = ADMIN_IDS[0]

    target_chat_id = chat_id or admin_id
    if not target_chat_id:
        return

    try:
        if update_existing and BATCH_PANEL_MESSAGE_ID and BATCH_PANEL_CHAT_ID:
            try:
                await bot.edit_message_text(
                    chat_id=BATCH_PANEL_CHAT_ID,
                    message_id=BATCH_PANEL_MESSAGE_ID,
                    text=text,
                    reply_markup=keyboard,
                    parse_mode="HTML"
                )
                return
            except Exception:
                BATCH_PANEL_MESSAGE_ID = None
                BATCH_PANEL_CHAT_ID = None

        msg = await bot.send_message(
            chat_id=target_chat_id,
            text=text,
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        BATCH_PANEL_MESSAGE_ID = msg.message_id
        BATCH_PANEL_CHAT_ID = target_chat_id

    except Exception as e:
        logger.error(f"Failed to show batch admin panel: {e}")


async def notify_batch_progress(bot, engine) -> None:
    """Notify admin of batch progress and update the admin panel."""
    await show_batch_admin_panel(bot, engine)

    from config import ADMIN_IDS
    admin_id = ADMIN_IDS[0] if ADMIN_IDS else None
    queue_len = len(engine.state.batch_queue)
    current_idx = engine.state.batch_current_index + 1
    title = engine.state.title or "?"

    if admin_id:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=f"📦 <b>批次拍賣進度</b>\n\n"
                     f"項目：{current_idx}/{queue_len}\n"
                     f"當前：{html.escape(title)}\n"
                     f"模式：{'運行中' if not engine.state.batch_paused else '⏸ 已暫停'}",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.warning(f"Failed to notify admin of batch progress: {e}")


async def notify_batch_complete(bot, engine) -> None:
    """Notify when batch auction is complete."""
    global BATCH_PANEL_MESSAGE_ID, BATCH_PANEL_CHAT_ID

    total_items = len(engine.state.batch_queue)
    from config import ADMIN_IDS
    admin_id = ADMIN_IDS[0] if ADMIN_IDS else None

    engine.state.batch_mode = False
    engine.state.batch_queue = []
    engine.state.batch_current_index = 0
    engine.state.batch_paused = False
    engine.state.batch_abort = False
    engine.state.scheduled_start = None
    BATCH_PANEL_MESSAGE_ID = None
    BATCH_PANEL_CHAT_ID = None

    if admin_id:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=f"✅ <b>批次拍賣完成！</b>\n\n共完成 {total_items} 件拍賣品",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.warning(f"Failed to notify admin of batch complete: {e}")


async def notify_batch_aborted(bot, engine) -> None:
    """Notify when batch auction is aborted."""
    global BATCH_PANEL_MESSAGE_ID, BATCH_PANEL_CHAT_ID

    from config import ADMIN_IDS
    admin_id = ADMIN_IDS[0] if ADMIN_IDS else None

    engine.state.batch_mode = False
    engine.state.batch_queue = []
    engine.state.batch_current_index = 0
    engine.state.batch_paused = False
    engine.state.batch_abort = False
    engine.state.scheduled_start = None
    BATCH_PANEL_MESSAGE_ID = None
    BATCH_PANEL_CHAT_ID = None

    if admin_id:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text="🛑 <b>批次拍賣已終止</b>\n\n隊列已清空。",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.warning(f"Failed to notify admin of batch abort: {e}")


async def download_image_to_file_id(bot, url: str) -> str | None:
    """Download an image from URL and send it to bot's own chat to get a file_id."""
    import tempfile
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            image_data = response.read()

        content_type = response.headers.get('Content-Type', 'image/jpeg')
        ext = '.jpg'
        if 'png' in content_type:
            ext = '.png'
        elif 'gif' in content_type:
            ext = '.gif'
        elif 'webp' in content_type:
            ext = '.webp'

        from config import ADMIN_IDS
        admin_id = ADMIN_IDS[0] if ADMIN_IDS else None
        if not admin_id:
            logger.error("No admin ID configured for photo download")
            return None

        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(image_data)
            tmp_path = tmp.name

        with open(tmp_path, 'rb') as f:
            msg = await bot.send_photo(chat_id=admin_id, photo=f)

        os.unlink(tmp_path)
        return msg.photo[-1].file_id

    except Exception as e:
        logger.error(f"Failed to download image from {url}: {e}")
        return None


async def run_batch_auction_loop(bot, engine, store, admin_id) -> None:
    """Main loop for batch auction - runs after each item ends."""
    from config import PAUSE_BETWEEN_ITEMS

    await asyncio.sleep(PAUSE_BETWEEN_ITEMS)

    if engine.state.batch_abort:
        await notify_batch_aborted(bot, engine)
        return

    if engine.state.batch_paused:
        while engine.state.batch_paused and not engine.state.batch_abort:
            await asyncio.sleep(1)
        if engine.state.batch_abort:
            await notify_batch_aborted(bot, engine)
            return

    engine.state.batch_current_index += 1

    if engine.state.batch_current_index > len(engine.state.batch_queue):
        await notify_batch_complete(bot, engine)
        return

    item = engine.state.batch_queue[engine.state.batch_current_index - 1]
    await start_single_batch_item(bot, engine, store, item, admin_id)


async def start_single_batch_item(bot, engine, store, item, admin_id) -> None:
    """Start a single auction item from the batch queue."""
    from config import ITEM_DURATION
    from core.text import generate_auction_text, generate_bid_keyboard

    if engine.state.batch_abort:
        return

    title = item.get("title", "未知商品")
    price = int(item.get("price", 0))
    bin_price = int(item.get("bin_price", 0))
    photo_id = item.get("photo_id")
    target_chat_id = item.get("target_chat_id")

    if not photo_id or not target_chat_id:
        logger.error(f"Batch item missing photo_id or target_chat_id: {title}")
        engine.state.batch_current_index += 1
        asyncio.create_task(run_batch_auction_loop(bot, engine, store, admin_id))
        return

    session_id, session_seq = await store.get_next_session()
    target_chat_id = int(target_chat_id)

    # Reset auction state
    engine.state.active = True
    engine.state.title = title
    engine.state.base_price = price
    engine.state.current_price = price
    engine.state.pending_price = price
    engine.state.pending_bidder = None
    engine.state.pending_bidder_name = "無"
    engine.state.bidders = []
    engine.state.bin_price = bin_price
    engine.state.bin_confirm_user_id = None
    engine.state.bin_confirm_expires_at = 0
    engine.state.photo_id = photo_id
    engine.state.highest_bidder = None
    engine.state.highest_bidder_name = "無"
    engine.state.start_time = datetime.now()
    engine.state.end_time = datetime.now().timestamp() + ITEM_DURATION
    engine.state.session_id = session_id
    engine.state.session_seq = session_seq
    engine.state.chat_id = target_chat_id
    engine.state._ending = False
    engine.state.update_event.clear()

    try:
        me = await bot.get_me()
        engine.state.bot_username = me.username
    except Exception as e:
        logger.error(f"Failed to get bot username: {e}")

    text = generate_auction_text(engine.state, ITEM_DURATION)
    keyboard = generate_bid_keyboard(engine.state)

    try:
        msg = await bot.send_photo(
            chat_id=target_chat_id,
            photo=photo_id,
            caption=text,
            reply_markup=keyboard,
            parse_mode="HTML"
        )

        if engine.state.timer_task:
            engine.state.timer_task.cancel()

        engine.state.message_id = msg.message_id
        engine.state.timer_task = asyncio.create_task(engine.timer_loop(bot))

        await notify_batch_progress(bot, engine)

    except Exception as e:
        logger.error(f"Failed to start batch item '{title}': {e}")
        engine.state.batch_current_index += 1
        asyncio.create_task(run_batch_auction_loop(bot, engine, store, admin_id))
