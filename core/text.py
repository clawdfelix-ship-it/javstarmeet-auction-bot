"""Auction text generation helpers (shared between handlers and engine)."""
import html

from models.auction import AuctionState


def generate_auction_text(state: AuctionState, remaining_seconds: float) -> str:
    """Build the auction message caption."""
    title = html.escape(state.title)
    price = state.base_price if state.active else state.current_price
    bin_price = state.bin_price
    bidder = "㊙️ (匿名暗標)"
    seq = state.session_seq or "?"

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


def build_bin_confirm_keyboard(bin_price: int, user_id: int) -> "InlineKeyboardMarkup":
    """Build BIN confirmation keyboard for a specific user."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    keyboard = [
        [InlineKeyboardButton(
            f"✅ 確認買斷 ${bin_price}", callback_data=f"bin_execute_{user_id}"
        )],
        [InlineKeyboardButton("❌ 取消", callback_data=f"bin_cancel_{user_id}")],
    ]
    return InlineKeyboardMarkup(keyboard)


def generate_bid_keyboard(state: AuctionState) -> "InlineKeyboardMarkup":
    """Build the current bid keyboard based on auction state."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    bin_price = int(state.bin_price or 0)
    confirm_uid, confirm_expires_at = state.bin_confirm_user_id, float(state.bin_confirm_expires_at or 0)
    now = datetime.now().timestamp()

    if confirm_uid and confirm_expires_at and now < confirm_expires_at and bin_price > 0:
        return build_bin_confirm_keyboard(bin_price, int(confirm_uid))

    if confirm_uid and confirm_expires_at and now >= confirm_expires_at:
        state.bin_confirm_user_id = None
        state.bin_confirm_expires_at = 0

    buttons = []

    if bin_price > 0:
        buttons.append([InlineKeyboardButton(
            f"⚡️ 一口價 ${bin_price}", callback_data="bin_confirm"
        )])

    bot_username = state.bot_username
    if bot_username:
        url = f"https://t.me/{bot_username}?start=bid"
        buttons.append([InlineKeyboardButton("✍️ 點擊私訊出價", url=url)])

    return InlineKeyboardMarkup(buttons)


def generate_final_text(state: AuctionState, sorted_bidders: list) -> str:
    """Build the auction-end message caption."""
    title = html.escape(state.title)
    price = state.current_price

    if sorted_bidders:
        bidders_lines = "\n".join(
            f"  {i+1}. {html.escape(b['name'])} — <b>${b['price']}</b>"
            for i, b in enumerate(sorted_bidders)
        )
        bidders_text = f"\n📋 <b>投標記錄：</b>\n{bidders_lines}\n"
    else:
        bidders_text = "\n📋 沒有投標者"

    return (
        f"🛑 <b>拍賣結束！</b> 🛑\n\n"
        f"📦 {title}\n"
        f"💰 最終成交價：<b>${price}</b>\n"
        f"🏆 得標者：{html.escape(state.highest_bidder_name or '無')}\n"
        f"{bidders_text}\n"
        f"系統將自動發送結算連結給得標者。"
    )


def generate_buyout_text(state: AuctionState, winner_name: str, price: int) -> str:
    """Build the buyout-end message caption."""
    winner_prefix = truncate_name_prefix(winner_name, 4)
    return (
        f"✅ <b>已成交</b>\n"
        f"⚡️ <b>${price}</b>\n"
        f"🏆 得標：{html.escape(winner_prefix)}"
    )


def truncate_name_prefix(name: str, length: int = 4) -> str:
    if not name:
        return ""
    return name[:length]


from datetime import datetime



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
