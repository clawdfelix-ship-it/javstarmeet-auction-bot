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
    # For 'set', we can show "Input: $next_val_str" in toast for instant feedback
    if action == "set":
        # Show toast feedback immediately
        await query.answer(f"已輸入: ${next_val_str}")
    else:
        await query.answer()

    if action == "set":
        # Update message with new value
        # We only edit if value is different (though logic usually implies it is, unless 0->0 or max len)
        # But we need to compare with *message content* to be sure, or just try edit anyway.
        # Since we encoded NEXT value, we just use next_val_str directly.
        
        try:
            await query.message.edit_text(
                f"🔢 <b>{html.escape(user.first_name)} 請輸入出價金額：</b>\n\n💰 目前輸入：<b>${next_val_str}</b>",
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
        await process_bid(user, price, query, context.bot)
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
    price = current_auction["current_price"]
    # 永遠隱藏出價者 (暗標)
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
        f"👇 點擊下方按鈕出價！"
    )

def generate_bid_keyboard(current_price):
    # 完全暗標拍賣：所有出價必須透過私訊，所以淨係保留私訊出價入口
    buttons = []
    
    # Add BIN button if set (still clickable, opens private bid)
    bin_price = current_auction.get("bin_price", 0)
    if bin_price > 0:
        bot_username = current_auction.get("bot_username")
        if bot_username:
            url = f"https://t.me/{bot_username}?start=bid"
            buttons.append([InlineKeyboardButton(f"⚡️ 一口價 ${bin_price}", url=url)])
    
    # 永遠保留私訊出價入口
    bot_username = current_auction.get("bot_username")
    if bot_username:
        url = f"https://t.me/{bot_username}?start=bid"
        buttons.append([InlineKeyboardButton("✍️ 點擊私訊出價", url=url)])
    
    return InlineKeyboardMarkup(buttons)

async def auction_timer_loop(bot):
    last_update_time = 0
    event = current_auction["update_event"]
    
    # Ensure event is created if missing
    if event is None:
         event = asyncio.Event()
         current_auction["update_event"] = event
    
    while True:
        try:
            now = datetime.now().timestamp()
            remaining = current_auction["end_time"] - now
            
            if remaining <= 0:
                # 拍賣結束 → 更新最終價格，公開最高出價
                current_auction["current_price"] = current_auction["pending_price"]
                current_auction["highest_bidder"] = current_auction["pending_bidder"]
                current_auction["highest_bidder_name"] = current_auction["pending_bidder_name"]
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

    if price <= current_auction["current_price"]:
        await update.message.reply_text(
            f"❌ 出價必須高於當前價格 (${current_auction['current_price']})。\n請重新輸入："
        )
        return BIDDING_PRICE
        
    # Process the bid
    # In blind auction with delayed price reveal, we just save to pending and don't update public display
    await process_blind_bid(user, price)
    await update.message.reply_text(f"✅ 成功出價：${price}！\n如有更高出價，您將收到通知。")
    return ConversationHandler.END

async def handle_custom_bid_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    # 檢查是否在拍賣中
    if not current_auction["active"]:
         await query.answer("❌ 拍賣已結束", show_alert=True)
         return

    # 引導用戶私聊出價
    bot_username = context.bot.username
    if not bot_username:
        me = await context.bot.get_me()
        bot_username = me.username
        
    url = f"https://t.me/{bot_username}?start=bid"
    
    keyboard = [[InlineKeyboardButton("📩 點擊私聊出價", url=url)]]
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="⚠️ 由於群組禁言，請點擊下方按鈕私訊機器人進行出價：",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# --- 處理出價 (盲標模式：價格隱藏，倒數完先公開) ---
async def process_blind_bid(user, price):
    # 盲標：價格唔會立即公開，淨係儲存起，等到倒數完先更新public display
    if price <= current_auction["current_price"]:
        # 呢個情況唔可能發生，因為 client side 已經check咗
        # 但係保留 double check
        return

    # Check Buy It Now
    bin_price = current_auction["bin_price"]
    if bin_price > 0 and price >= bin_price:
        # End auction immediately
        current_auction["end_time"] = datetime.now().timestamp() # Expire immediately
        if "timer_task" in current_auction and current_auction["timer_task"]:
             current_auction["timer_task"].cancel()
             current_auction["timer_task"] = None

    # 延遲更新：只更新pending price，唔更新public display
    current_auction["pending_price"] = price
    current_auction["pending_bidder"] = user.id
    current_auction["pending_bidder_name"] = user.first_name
    
    # 防狙擊延長：出價响最後 X 秒內 → 自動延長
    now = datetime.now().timestamp()
    remaining = current_auction["end_time"] - now
    # 如果剩餘時間少於 5 秒 → 延長 5 秒
    if remaining < 5:
        current_auction["end_time"] += 5
        # 通知 update event 提早更新
        if current_auction.get("update_event"):
            current_auction["update_event"].set()

    # Notify previous bidder (Async task to avoid blocking)
    previous_bidder_id = current_auction["highest_bidder"]
    if previous_bidder_id and previous_bidder_id != user.id:
        asyncio.create_task(notify_outbid(bot, previous_bidder_id, current_auction["title"], price))

async def handle_bid_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    
    if query.data == "bid_custom":
        # Check if active
        if not current_auction["active"]:
             await query.answer("❌ 拍賣已結束", show_alert=True)
             return
             
        # Check if user has registered
        if not await store.is_registered(user.id):
            bot_username = context.bot.username
            if not bot_username:
                me = await context.bot.get_me()
                bot_username = me.username
            url = f"https://t.me/{bot_username}?start=register"
            await query.answer("⚠️ 請先點此註冊！", url=url)
            return

        # Always use Numpad (In-Chat Keyboard) for custom bid
        # Send Numpad
        try:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"🔢 <b>{html.escape(user.first_name)} 請輸入出價金額：</b>\n\n💰 目前輸入：<b>$0</b>",
                reply_markup=generate_numpad_keyboard("0", user.id),
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
             # Fallback to ForceReply if Numpad fails (e.g. permission issue?)
             try:
                prompt_msg = await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=f"@{user.username} {CUSTOM_BID_PROMPT}",
                    reply_markup=ForceReply(selective=True)
                )
             except:
                bot_username = context.bot.username
                if not bot_username:
                    me = await context.bot.get_me()
                    bot_username = me.username
                url = f"https://t.me/{bot_username}?start=bid"
                await query.answer("⚠️ 無法打開數字鍵盤，請點此私訊出價", url=url)
        return

    if not current_auction["active"]:
        await query.answer("❌ 拍賣已結束", show_alert=True)
        return

    if not await store.is_registered(user.id):
        bot_username = context.bot.username
        if not bot_username:
            me = await context.bot.get_me()
            bot_username = me.username
        url = f"https://t.me/{bot_username}?start=register"
        await query.answer("⚠️ 請先點此註冊！", url=url)
        return

    if await store.is_blacklisted(user.id):
        await query.answer("⛔ 您已被禁止參與拍賣", show_alert=True)
        return

    data = query.data 
    
    # Handle BIN
    if data.startswith("bid_bin_"):
        bin_price = int(data.split("_")[2])
        # Validate bin price again just in case
        if current_auction["bin_price"] > 0 and bin_price == current_auction["bin_price"]:
             await process_blind_bid(user, bin_price)
        else:
             await query.answer("❌ 一口價無效或已變更", show_alert=True)
        return

    # Handle increments - format: bid_inc_{new_price}
    # Clicking increment opens numpad in group chat (still blind bidding, price not revealed to others until end)
    if data.startswith("bid_inc_"):
        target_price = int(data.split("_")[2])
        
        # Because this is blind bidding, clicking increment just opens the numpad with that price pre-filled
        # The actual bid is only confirmed when user clicks enter, so no price is revealed yet
        try:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"🔢 <b>{html.escape(user.first_name)} 請確認出價：</b>\n\n💰 出價金額：<b>${target_price}</b>",
                reply_markup=generate_numpad_keyboard(str(target_price), user.id),
                parse_mode=parseMode.HTML
            )
        except Exception as e:
            logger.error(f"Failed to open numpad: {e}")
            # Fallback to private URL
            bot_username = context.bot.username
            if not bot_username:
                me = await context.bot.get_me()
                bot_username = me.username
            url = f"https://t.me/{bot_username}?start=bid"
            await query.answer("⚠️ 無法打開數字鍵盤，請點此私訊出價", url=url)
        return

async def notify_previous_bidder(bot, previous_bidder_id, title, new_price):
    try:
        notify_text = (
            f"⚠️ <b>您的出價已經被超越！</b>\n\n"
            f"📦 商品：{html.escape(title)}\n"
            f"💰 最新價格：${new_price}\n\n"
            f"請留意下一場拍賣！"
        )
        await bot.send_message(chat_id=previous_bidder_id, text=notify_text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning(f"Failed to notify outbid user {previous_bidder_id}: {e}")

async def start_next_queued_auction(bot):
    queue = await store.get_auction_queue()
    if not queue:
        return

    if current_auction["active"]:
        return

    item = queue.pop(0)
    await store.set_auction_queue(queue)

    await asyncio.sleep(10)

    title = item.get("title", "未知商品")
    price = int(item.get("price", 0))
    bin_price = int(item.get("bin_price", 0))
    photo_id = item.get("photo_id")
    target_chat_id = item.get("target_chat_id")

    if not photo_id or not target_chat_id:
        return

    # Get bot username for deep linking
    try:
        me = await bot.get_me()
        current_auction["bot_username"] = me.username
    except Exception as e:
        logger.error(f"Failed to get bot info: {e}")

    # Initialize auction
    session_id, session_seq = await store.get_next_session()
    current_auction["active"] = True
    current_auction["title"] = title
    current_auction["base_price"] = price
    current_auction["current_price"] = price  # Public price starts as opening price
    current_auction["pending_price"] = price  # Pending price same as current initially
    current_auction["bin_price"] = bin_price
    current_auction["photo_id"] = photo_id
    current_auction["pending_bidder"] = None
    current_auction["pending_bidder_name"] = "無"
    current_auction["highest_bidder"] = None
    current_auction["highest_bidder_name"] = "無"
    current_auction["start_time"] = datetime.now()
    current_auction["end_time"] = datetime.now().timestamp() + 25 
    current_auction["session_id"] = session_id
    current_auction["session_seq"] = session_seq 
    current_auction["chat_id"] = target_chat_id

    # Initialize event in the current loop
    if current_auction["update_event"] is None or current_auction["update_event"]._loop != asyncio.get_running_loop():
        current_auction["update_event"] = asyncio.Event()
    else:
        current_auction["update_event"].clear()

    text = generate_auction_text(current_auction["end_time"] - datetime.now().timestamp())
    keyboard = generate_bid_keyboard(price)
    
    try:
        msg = await bot.send_photo(
            chat_id=target_chat_id,
            photo=photo_id,
            caption=text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML
        )
        current_auction["message_id"] = msg.message_id
        current_auction["timer_task"] = asyncio.create_task(auction_timer_loop(bot))
        
        # Admin feedback
        # if it was queued, we don't need a message because original menu is still open
    except Exception as e:
        logger.error(f"Failed to start queued auction: {e}")
        current_auction["active"] = False

async def end_auction(bot):
    current_auction["active"] = False
    winner_id = current_auction["highest_bidder"]
    price = current_auction["current_price"]
    title = current_auction["title"]
    
    # 盲標模式：拍賣結束先公開最高出價
    winner_name = current_auction["highest_bidder_name"]
    
    final_text = (
        f"🛑 <b>拍賣結束！</b> 🛑\n\n"
        f"📦 {html.escape(title)}\n"
        f"💰 最終成交價：<b>${price}</b>\n"
        f"🏆 得標者：{html.escape(winner_name)}\n\n"
        f"系統將自動發送結算通知俾得標者，請等候。"
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
        logger.error(f"Failed to edit auction caption at end: {e}")
        # Fallback: Send a new message if edit fails
        try:
            await bot.send_message(
                chat_id=current_auction["chat_id"],
                text=final_text,
                parse_mode=ParseMode.HTML
            )
        except Exception as e2:
            logger.error(f"Failed to send fallback end message: {e2}")
    
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
            # 發送通知給得標者
            msg = (
                f"🎉 <b>恭喜您標得 <u>{html.escape(title)}</u>！</b>\n\n"
                f"💰 成交價：${price}\n"
                f"📍 交收：{html.escape(user_info.get('pickup', '未定'))}\n\n"
                f"拍賣完結後，我們會另外發送正式結算單給您，請留意。"
            )
            await bot.send_message(chat_id=winner_id, text=msg, parse_mode=ParseMode.HTML)
            
            # Send email notification if user has email
            user_email = user_info.get('email')
            if user_email:
                # Use asyncio.to_thread to prevent blocking the event loop
                asyncio.create_task(asyncio.to_thread(send_email, user_email, f"得標通知：{title}", msg))

        except Exception as e:
            logger.error(f"Failed to DM winner: {e}")
            await bot.send_message(
                chat_id=current_auction["chat_id"], 
                text=f"⚠️ 無法私聊得標者 (ID: {winner_id})，請主動聯繫。"
            )
    
    await start_next_queued_auction(bot)

async def process_bid(user, price, query=None, bot=None):
    # In blind bidding mode this is called from numpad / private bid
    # We just update the pending bid, don't update public display until timer ends
    if price <= current_auction["current_price"]:
        # already checked before, but double check
        if query:
             await query.answer("❌ 出價必須高於當前價格", show_alert=True)
        return

    # Check BIN price
    bin_price = current_auction["bin_price"]
    if bin_price > 0 and price >= bin_price:
        # End immediately, already valid
        current_auction["pending_price"] = price
        current_auction["pending_bidder"] = user.id
        current_auction["pending_bidder_name"] = user.first_name
        current_auction["end_time"] = datetime.now().timestamp() # End immediately
        if current_auction["timer_task"]:
             current_auction["timer_task"].cancel()
        # end_auction will handle finalization
        return

    # Anti-sniping: if bid within last 5 seconds, extend timer by 5 seconds
    now = datetime.now().timestamp()
    remaining = current_auction["end_time"] - now
    if remaining < 5:
        current_auction["end_time"] += 5
        if current_auction.get("update_event"):
            current_auction["update_event"].set()

    # Save the pending bid
    previous_bidder = current_auction["pending_bidder"]
    current_auction["pending_price"] = price
    current_auction["pending_bidder"] = user.id
    current_auction["pending_bidder_name"] = user.first_name

    # Notify outbid to previous bidder
    if previous_bidder and previous_bidder != user.id:
        asyncio.create_task(notify_previous_bidder(bot, previous_bidder, current_auction["title"], price))

# --- 拍賣規則 & Menu ---
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📜 <b>拍賣規則 & 使用指南</b>\n\n"
        "1️⃣ <b>參與資格</b>：首次使用需完成簡單登記 (稱呼、電話、交收地點)。\n"
        "2️⃣ <b>出價方式</b>：\n"
        "   • 點擊拍賣訊息下方快捷鍵 (+$10, +$20 等)\n"
        "   • 彈出數字鍵盤，確認後出價\n"
        "   • 所有出價都係 <b>匿名暗標</b>，直到拍賣完結先公開最高出價\n"
        "   • 防狙擊機制：最後 5 秒內出價自動延長 5 秒\n"
        "3️⃣ <b>得標結算</b>：\n"
        "   • 拍賣完結後，系統會自動私訊得標者發出結算通知\n"
        "   • 每日完結所有拍賣後，管理員會一次性發出總結算單\n"
        "   • 得標者需要響應通知完成付款\n"
        "4️⃣ <b>匿名保證</b>：\n"
        "   • 拍賣進行期間，所有人都睇唔到其他人出價，公平競投\n"
        "   • 淨係得管理員同你自己知你出幾多錢\n"
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
        date_str = o.get('created_at', o.get('time'))
        if isinstance(date_str, str):
            try:
                dt = datetime.fromisoformat(date_str)
                date_key = dt.strftime('%Y-%m-%d')
            except:
                date_key = "未知日期"
        else:
            date_key = date_str.strftime('%Y-%m-%d')
        if date_key not in orders_by_date:
            orders_by_date[date_key] = []
        orders_by_date[date_key].append(o)
    
    for date_key, date_orders in sorted(orders_by_date.items(), reverse=True):
        text += f"\n📅 <b>{date_key}</b>\n"
        for idx, o in enumerate(date_orders, 1):
            status_icon = "✅" if o['status'] == 'won' else ("💰" if o['status'] == 'paid' else "🚚")
            text += f"{status_icon} {idx}. {html.escape(o['item'])} | <b>${o['price']}</b>\n"
        
    if len(orders) > 30:
        text += f"\n<i>(僅顯示最近 30 筆記錄，總共 {len(orders)} 筆)</i>"
        
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
        f"• 名稱：{html.escape(info['name'])}\n"
        f"• 電話：{html.escape(info['phone'])}\n"
        f"• Email：{html