"""JSON file store implementation (fallback when no PostgreSQL)."""
import json
import logging
import os
from datetime import datetime
from typing import Any

from store.base import Store

logger = logging.getLogger(__name__)


class JsonStore(Store):
    """JSON file-based implementation of the Store interface."""

    def __init__(self, db_file: str = "data.json"):
        self._db_file = db_file
        self._data: dict[str, Any] = {
            "users": {},
            "blacklist": [],
            "auctions": [],
            "orders": [],
            "sessions": [],
            "config": {},
        }
        self._load()

    @property
    def is_pg(self) -> bool:
        return False

    def _load(self) -> None:
        if os.path.exists(self._db_file):
            try:
                with open(self._db_file, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception as e:
                logger.error(f"Failed to load data: {e}")

    def _save(self) -> None:
        try:
            with open(self._db_file, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to save data: {e}")

    # --- User Methods ---
    async def register_user(self, user_id: int, info: dict) -> None:
        self._data["users"][str(user_id)] = info
        self._save()

    async def get_user(self, user_id: int) -> dict | None:
        return self._data["users"].get(str(user_id))

    async def is_registered(self, user_id: int) -> bool:
        return str(user_id) in self._data["users"]

    async def get_all_users(self) -> list[dict]:
        return list(self._data["users"].values())

    # --- Blacklist Methods ---
    async def add_blacklist(self, user_id: int, reason: str = "violation") -> None:
        if user_id not in self._data["blacklist"]:
            self._data["blacklist"].append(user_id)
            self._save()

    async def remove_blacklist(self, user_id: int) -> None:
        if user_id in self._data["blacklist"]:
            self._data["blacklist"].remove(user_id)
            self._save()

    async def is_blacklisted(self, user_id: int) -> bool:
        return user_id in self._data["blacklist"]

    # --- Session Methods ---
    async def get_next_session(self) -> tuple[str, int]:
        today = datetime.now().strftime("%Y-%m-%d")
        if "sessions" not in self._data:
            self._data["sessions"] = []
        count = len([s for s in self._data["sessions"] if s["date"] == today])
        seq = count + 1
        session_id = f"{today.replace('-', '')}-{seq}"
        self._data["sessions"].append({"session_id": session_id, "date": today, "seq_num": seq})
        self._save()
        return session_id, seq

    # --- Order Methods ---
    async def add_order(self, order: dict) -> None:
        self._data["orders"].append(order)
        self._save()

    async def get_all_orders(self) -> list[dict]:
        return self._data["orders"]

    async def update_order_status(self, order_id: str, status: str) -> None:
        for o in self._data["orders"]:
            if o['order_id'] == order_id:
                o['status'] = status
                self._save()
                break

    async def get_user_orders(self, user_id: int) -> list[dict]:
        return [o for o in self._data["orders"] if str(o['user_id']) == str(user_id)]

    async def get_session_orders(self, session_id: str) -> list[dict]:
        return [o for o in self._data["orders"] if o.get('session_id') == session_id]

    # --- Config Methods ---
    async def set_config(self, key: str, value: Any) -> None:
        self._data["config"][key] = value
        self._save()

    async def get_config(self, key: str) -> Any:
        return self._data["config"].get(key)

    async def get_auction_queue(self) -> list[dict]:
        raw = await self.get_config("auction_queue")
        if not raw:
            return []
        try:
            return json.loads(raw)
        except Exception:
            return []

    async def set_auction_queue(self, queue: list[dict]) -> None:
        await self.set_config("auction_queue", json.dumps(queue))
