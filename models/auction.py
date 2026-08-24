"""Auction state dataclass."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class AuctionItem:
    """A single auction item in the batch queue."""
    title: str
    price: int
    photo_id: str | None = None
    bin_price: int = 0
    photo_url: str | None = None
    target_chat_id: int | None = None
    target_type: str = "prod"  # "prod" or "test"


@dataclass
class AuctionState:
    """Holds the current auction state (mirrors current_auction global dict)."""
    active: bool = False
    start_time: datetime | None = None
    end_time: float | None = None
    title: str = ""
    photo_id: str | None = None
    base_price: int = 0
    current_price: int = 0
    bin_price: int = 0
    pending_price: int = 0
    pending_bidder: int | None = None
    pending_bidder_name: str = "無"
    bidders: list[dict] = field(default_factory=list)
    highest_bidder: int | None = None
    highest_bidder_name: str = "無"
    message_id: int | None = None
    chat_id: int | None = None
    timer_task: Any = None
    update_event: Any = None
    session_id: str | None = None
    session_seq: int = 0
    bot_username: str | None = None
    _ending: bool = False
    bin_confirm_user_id: int | None = None
    bin_confirm_expires_at: float = 0

    # Batch auction state
    batch_mode: bool = False
    batch_queue: list[AuctionItem | dict] = field(default_factory=list)
    batch_current_index: int = 0
    batch_paused: bool = False
    batch_abort: bool = False
    batch_target_group: str | None = None
    scheduled_start: str | None = None
    batch_timer_task: Any = None

    def to_dict(self) -> dict:
        return {
            "active": self.active,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "title": self.title,
            "photo_id": self.photo_id,
            "base_price": self.base_price,
            "current_price": self.current_price,
            "bin_price": self.bin_price,
            "pending_price": self.pending_price,
            "pending_bidder": self.pending_bidder,
            "pending_bidder_name": self.pending_bidder_name,
            "bidders": self.bidders,
            "highest_bidder": self.highest_bidder,
            "highest_bidder_name": self.highest_bidder_name,
            "message_id": self.message_id,
            "chat_id": self.chat_id,
            "session_id": self.session_id,
            "session_seq": self.session_seq,
            "bot_username": self.bot_username,
            "_ending": self._ending,
            "bin_confirm_user_id": self.bin_confirm_user_id,
            "bin_confirm_expires_at": self.bin_confirm_expires_at,
            "batch_mode": self.batch_mode,
            "batch_queue": self.batch_queue,
            "batch_current_index": self.batch_current_index,
            "batch_paused": self.batch_paused,
            "batch_abort": self.batch_abort,
            "batch_target_group": self.batch_target_group,
            "scheduled_start": self.scheduled_start,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AuctionState":
        state = cls()
        for key, value in data.items():
            if hasattr(state, key):
                setattr(state, key, value)
        return state
