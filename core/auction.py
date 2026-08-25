"""Auction state machine and bid logic."""
import asyncio
import html
import json
import logging
import os
from datetime import datetime
from typing import TYPE_CHECKING, Optional, Tuple

from models.auction import AuctionState, AuctionItem

if TYPE_CHECKING:
    from store.base import Store

logger = logging.getLogger(__name__)

# Countdown seconds that trigger a UI update
UPDATE_POINTS = [60, 45, 30, 25, 20, 15, 10, 5, 4, 3, 2, 1]

# --- Auction State Persistence ---

AUCTION_STATE_FILE = "auction_state.json"


def save_auction_state(state: AuctionState) -> None:
    """Persist auction state to JSON for crash recovery."""
    try:
        if state.title and state.chat_id:
            data = state.to_dict()
            # Convert datetime to isoformat string for JSON serialization
            data["start_time"] = (
                state.start_time.isoformat() if state.start_time else None
            )
            with open(AUCTION_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
            logger.info(f"Auction state saved: {state.title}")
        elif os.path.exists(AUCTION_STATE_FILE):
            try:
                os.remove(AUCTION_STATE_FILE)
            except Exception:
                logger.exception("Failed to remove stale auction state file")
    except Exception as e:
        logger.error(f"Failed to save auction state: {e}")


def load_auction_state(state: AuctionState) -> bool:
    """Load auction state from JSON. Returns True if restored."""
    if not os.path.exists(AUCTION_STATE_FILE):
        return False
    try:
        with open(AUCTION_STATE_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if loaded.get("active") and loaded.get("title"):
            # Restore fields one by one, preserving runtime objects
            state.active = loaded.get("active", False)
            state.title = loaded.get("title", "")
            state.photo_id = loaded.get("photo_id")
            state.base_price = loaded.get("base_price", 0)
            state.current_price = loaded.get("current_price", 0)
            state.bin_price = loaded.get("bin_price", 0)
            state.pending_price = loaded.get("pending_price", 0)
            state.pending_bidder = loaded.get("pending_bidder")
            state.pending_bidder_name = loaded.get("pending_bidder_name", "無")
            state.bidders = loaded.get("bidders", [])
            state.highest_bidder = loaded.get("highest_bidder")
            state.highest_bidder_name = loaded.get("highest_bidder_name", "無")
            state.message_id = loaded.get("message_id")
            state.chat_id = loaded.get("chat_id")
            state.session_id = loaded.get("session_id")
            state.session_seq = loaded.get("session_seq", 0)
            state.bot_username = loaded.get("bot_username")
            state._ending = False
            state.bin_confirm_user_id = loaded.get("bin_confirm_user_id")
            state.bin_confirm_expires_at = loaded.get("bin_confirm_expires_at", 0)
            state.batch_mode = loaded.get("batch_mode", False)
            state.batch_queue = loaded.get("batch_queue", [])
            state.batch_current_index = loaded.get("batch_current_index", 0)
            state.batch_paused = loaded.get("batch_paused", False)
            state.batch_abort = loaded.get("batch_abort", False)
            state.batch_target_group = loaded.get("batch_target_group")
            state.scheduled_start = loaded.get("scheduled_start")
            # Keep update_event cleared and timer_task = None (runtime objects)
            state.update_event.clear()

            # Recalculate end_time
            if isinstance(state.end_time, (int, float)):
                remaining = state.end_time - datetime.now().timestamp()
                if remaining < 5:
                    state.active = False
                    logger.info(
                        f"Loaded auction '{loaded.get('title')}' has expired; skipping resume."
                    )
                    save_auction_state(state)
                    return False
            logger.info(
                f"Auction state restored: {loaded.get('title')}, "
                f"remaining {remaining:.0f}s"
            )
            return True
    except Exception:
        logger.exception("Failed to load auction state")
    return False


# --- Auction Engine ---


class AuctionEngine:
    """Core auction state machine - all bid logic, timer, and state transitions."""

    def __init__(self, store: "Store", item_duration: int = 25):
        self.store = store
        self.state = AuctionState()
        self.state.update_event = asyncio.Event()
        self._lock = asyncio.Lock()
        self._item_duration = item_duration

    # --- Auction Lifecycle ---

    async def start_auction(
        self,
        title: str,
        photo_id: str,
        base_price: int,
        bin_price: int,
        target_chat_id: int,
        is_charity: bool = False,
    ) -> None:
        """Initialize and start a new auction."""
        async with self._lock:
            session_id, session_seq = await self.store.get_next_session()

            self.state.active = True
            self.state.title = title
            self.state.photo_id = photo_id
            self.state.base_price = base_price
            self.state.current_price = base_price
            self.state.bin_price = bin_price
            self.state.pending_price = base_price
            self.state.pending_bidder = None
            self.state.pending_bidder_name = "無"
            self.state.bidders = []
            self.state.highest_bidder = None
            self.state.highest_bidder_name = "無"
            self.state.start_time = datetime.now()
            self.state.end_time = datetime.now().timestamp() + self._item_duration
            self.state.is_charity = is_charity
            self.state.session_id = session_id
            self.state.session_seq = session_seq
            self.state.chat_id = target_chat_id
            self.state.bin_confirm_user_id = None
            self.state.bin_confirm_expires_at = 0
            self.state._ending = False
            self.state.update_event.clear()

    async def process_bid(
        self, user_id: int, price: int, user_name: str, bot=None
    ) -> dict:
        """
        Process a blind bid. Returns a dict with action info.
        Possible actions: 'accepted', 'outbid', 'buyout', 'error'.
        """
        async with self._lock:
            if self.state._ending:
                self.state.end_time = datetime.now().timestamp() + 2
                logger.info(f"Late bid accepted; auction extended by 2s (user {user_id})")

            if any(b["id"] == user_id for b in self.state.bidders):
                return {"action": "error", "message": "你已經出過價了"}

            self.state.pending_price = price
            self.state.pending_bidder = user_id
            self.state.pending_bidder_name = user_name
            self.state.bidders.append({
                "id": user_id,
                "name": user_name,
                "price": price,
                "time": datetime.now().timestamp(),
            })

            # Wake up timer_loop so it can refresh the message immediately
            # (showing the new bidder count / countdown).
            if self.state.update_event:
                self.state.update_event.set()

            if self.state.bin_price > 0 and price >= self.state.bin_price:
                self.state.end_time = datetime.now().timestamp()
                if self.state.timer_task:
                    self.state.timer_task.cancel()
                return {"action": "buyout"}

            return {"action": "accepted"}

    async def confirm_buyout(
        self, user_id: int, user_name: str, price: int
    ) -> None:
        """Execute a buyout at bin_price."""
        async with self._lock:
            self.state.bin_confirm_user_id = None
            self.state.bin_confirm_expires_at = 0

            updated = False
            for b in self.state.bidders:
                if b.get("id") == user_id:
                    b["name"] = user_name
                    b["price"] = price
                    b["time"] = datetime.now().timestamp()
                    updated = True
                    break
            if not updated:
                self.state.bidders.append({
                    "id": user_id,
                    "name": user_name,
                    "price": price,
                    "time": datetime.now().timestamp(),
                })

            self.state.active = False
            self.state.current_price = price
            self.state.highest_bidder = user_id
            self.state.highest_bidder_name = user_name
            self.state._ending = True

            if self.state.timer_task:
                try:
                    self.state.timer_task.cancel()
                except Exception:
                    logger.exception("Failed to cancel timer task")

            save_auction_state(self.state)

    async def end_auction(self, bot) -> dict:
        """End the auction. Returns dict with winner info."""
        self.state._ending = True

        sorted_bidders = sorted(
            self.state.bidders, key=lambda x: (-x["price"], x.get("time", 0))
        )

        # Charity auction: winner is the second-highest bidder, free of charge
        if self.state.is_charity:
            if len(sorted_bidders) >= 2:
                winner = sorted_bidders[1]  # Second highest bidder
                winner_id = winner["id"]
                winner_name = winner["name"]
                price = 0  # Free
            else:
                winner_id = None
                winner_name = "無"
                price = 0
        else:
            # Normal auction: highest bidder wins
            if sorted_bidders:
                winner = sorted_bidders[0]
                winner_id = winner["id"]
                winner_name = winner["name"]
                price = winner["price"]
            else:
                winner_id = None
                winner_name = "無"
                price = 0

        self.state.active = False
        self.state.current_price = price
        self.state.highest_bidder = winner_id
        self.state.highest_bidder_name = winner_name

        save_auction_state(self.state)

        self.state._ending = False

        return {
            "winner_id": winner_id,
            "winner_name": winner_name,
            "price": price,
            "sorted_bidders": sorted_bidders,
        }

    # --- Timer ---

    async def timer_loop(self, bot) -> None:
        """Countdown loop - updates auction message at countdown points."""
        last_updated_point = None
        last_bidder_count = 0

        while True:
            if not self.state.active:
                break

            now = datetime.now().timestamp()
            remaining = self.state.end_time - now

            if remaining <= 0:
                try:
                    await self.end_auction(bot)
                except Exception:
                    logger.exception("Failed to end auction in timer loop")
                break

            # current_point = the largest UPDATE_POINTS threshold that
            # remaining has just reached or passed. Iterate ascending so we
            # pick the first (i.e., largest-from-below) point that is ≤ remaining.
            # e.g. remaining=20 → 20 (just hit 20); remaining=19 → 15 (last hit);
            # remaining=0.5 → 1 (last hit). This is "what threshold did we last cross?"
            current_point = None
            for point in reversed(UPDATE_POINTS):  # ascending: [1,2,3,4,5,10,15,20,25,30,45,60]
                if remaining >= point:
                    current_point = point
                    break

            current_bidder_count = len(self.state.bidders)
            new_bid = current_bidder_count != last_bidder_count

            should_update = (
                current_point is not None and last_updated_point != current_point
            ) or new_bid

            if should_update:
                try:
                    from core.text import generate_auction_text, generate_bid_keyboard
                    await bot.edit_message_caption(
                        chat_id=self.state.chat_id,
                        message_id=self.state.message_id,
                        caption=generate_auction_text(self.state, remaining),
                        reply_markup=generate_bid_keyboard(self.state),
                        parse_mode="HTML",
                    )
                    last_updated_point = current_point
                    last_bidder_count = current_bidder_count
                except Exception as e:
                    if "message is not modified" not in str(e):
                        logger.warning(f"Update message failed: {e}")
                    last_updated_point = current_point
                    last_bidder_count = current_bidder_count

            if current_point is not None:
                next_point = None
                for point in reversed(UPDATE_POINTS):
                    if point < current_point:
                        next_point = point
                        break
                if next_point is not None:
                    wait_time = min(remaining - next_point, 1.0)
                else:
                    wait_time = 0.1
            else:
                wait_time = 1.0

            wait_time = max(0.1, min(wait_time, 1.0))

            try:
                await asyncio.wait_for(
                    self.state.update_event.wait(), timeout=wait_time
                )
                self.state.update_event.clear()
            except asyncio.TimeoutError:
                pass

    # --- Properties / Helpers ---

    @property
    def is_active(self) -> bool:
        return self.state.active

    @property
    def batch_mode(self) -> bool:
        return self.state.batch_mode

    @property
    def batch_abort(self) -> bool:
        return self.state.batch_abort

    @property
    def batch_paused(self) -> bool:
        return self.state.batch_paused

    def get_current_price(self) -> int:
        return self.state.current_price

    def get_bin_price(self) -> int:
        return self.state.bin_price

    def get_title(self) -> str:
        return self.state.title

    def get_chat_id(self) -> Optional[int]:
        return self.state.chat_id

    def get_message_id(self) -> Optional[int]:
        return self.state.message_id

    def get_bot_username(self) -> Optional[str]:
        return self.state.bot_username

    def set_bot_username(self, username: str) -> None:
        self.state.bot_username = username

    def set_message_id(self, message_id: int) -> None:
        self.state.message_id = message_id

    def set_timer_task(self, task) -> None:
        self.state.timer_task = task

    def cancel_timer(self) -> None:
        if self.state.timer_task:
            try:
                self.state.timer_task.cancel()
            except Exception:
                logger.exception("Failed to cancel timer task")
            self.state.timer_task = None

    def set_bin_confirm(self, user_id: int, expires_at: float) -> None:
        self.state.bin_confirm_user_id = user_id
        self.state.bin_confirm_expires_at = expires_at

    def clear_bin_confirm(self) -> None:
        self.state.bin_confirm_user_id = None
        self.state.bin_confirm_expires_at = 0

    def get_bin_confirm(self) -> Tuple[Optional[int], float]:
        return self.state.bin_confirm_user_id, self.state.bin_confirm_expires_at

    def set_chat_id(self, chat_id: int) -> None:
        self.state.chat_id = chat_id

    def get_item_duration(self) -> int:
        return self._item_duration

    def get_session(self) -> Tuple[Optional[str], int]:
        return self.state.session_id, self.state.session_seq

    def get_pending_bidder_name(self) -> str:
        return self.state.pending_bidder_name

    def get_pending_price(self) -> int:
        return self.state.pending_price

    def get_bidders(self) -> list:
        return self.state.bidders
