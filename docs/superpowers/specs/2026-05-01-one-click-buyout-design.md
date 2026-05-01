## 目標

將群組拍賣訊息底部嘅「⚡️ 一口價」改成一按即買斷（有二次確認），唔需要跳去私訊再輸入金額。

## 現況問題

- 目前「⚡️ 一口價」同「✍️ 點擊私訊出價」都係 deep link 去同一個 `start=bid`，用戶體驗上冇分別。
- 真正買斷判斷只會喺私訊出價時（price >= bin_price）先觸發，唔符合「一按即買斷」。

## 範圍

- 只改「群組拍賣訊息底部」嘅一口價按鈕行為。
- 私訊暗標出價流程保留（仍然用 deep link 私訊）。
- 保持暗標：成交後群組只顯示「已成交 + 價錢 + 得標者名頭 4 個字元」。

## 互動設計

### 1) 群組訊息底部按鈕

- 平時：
  - `⚡️ 一口價 $X`（CallbackQuery）
  - `✍️ 點擊私訊出價`（URL deep link，保留現狀）
- 用戶按 `⚡️ 一口價 $X` 後：
  - 將同一條訊息嘅 keyboard 替換成：
    - `✅ 確認買斷 $X`（CallbackQuery）
    - `❌ 取消`（CallbackQuery）

### 2) 確認與提示

- Confirm 成功：立即結標，群組訊息 caption 更新顯示成交。
- Cancel：恢復原本按鈕（顯示一口價＋私訊出價）。
- 未註冊用戶按買斷：彈出 alert 提示先去私訊機器人 `/start` 註冊。
- 若拍賣已結束/正在結標：彈出 alert 提示「已結束」。

## 成交顯示格式

- 成交後群組 caption 只顯示：
  - `✅ 已成交`
  - `⚡️ $<bin_price>`
  - `得標：<winner_prefix>`
- `winner_prefix` = `winner_first_name` 取頭 4 個字元（不足 4 用實際長度）。

## 技術設計

### Callback 定義

- `bin_confirm`：第一下按，顯示二次確認 UI
- `bin_execute`：確認買斷（最終成交）
- `bin_cancel`：取消並恢復原按鈕

（如需區分不同訊息/拍賣，可加入 message_id 作 token，但現階段用 current_auction 全局狀態判斷）

### 核心邏輯

- `bin_execute` 需要：
  - 檢查 `current_auction["active"] == True`
  - 檢查 `current_auction["bin_price"] > 0`
  - 檢查用戶已註冊（`store.is_registered(user.id)`）
  - 用 `auction_lock` 包住，防止同時兩個人買斷
  - 將出價視作 `price = bin_price`，觸發立即結標（沿用既有 `process_blind_bid` / `end_auction` 流程）
  - 更新群組訊息 caption：成交顯示格式（見上）
  - 取消 timer task（避免之後 edit_message_caption 覆蓋成交訊息）

### 錯誤處理

- 所有 callback handler 都要先 `await query.answer()`（或 show_alert=True）避免 Telegram 端「無反應」。
- edit_message_reply_markup / edit_message_caption 失敗要記錄 log，但唔應 crash 主流程。

## 驗證方式

- 手動測試：
  - 群組按「⚡️ 一口價」→ 出現 Confirm/Cancel
  - Confirm → 立即結標；caption 變成「✅ 已成交 | ⚡️ $X | 得標：XXXX」
  - Cancel → 按鈕恢復
  - 兩個 account 同時 Confirm：只得 1 個成功，另一個收到「已結束」
  - 未註冊 account Confirm：alert 提示先註冊

