"""Batch auction queue management + admin panel UI."""
import asyncio
import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

ITEM_DURATION = 25
PAUSE_BETWEEN_ITEMS = 3

BATCH_PANEL_MESSAGE_ID = None
BATCH_PANEL_CHAT_ID = None


def get_batch_state(current_auction: dict) -> str:
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


def build_batch_admin_keyboard(state: str) -> InlineKeyboardMarkup:
    """Build inline keyboard based on current batch state."""
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


def build_batch_admin_text(state: str, current_auction: dict) -> str:
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
