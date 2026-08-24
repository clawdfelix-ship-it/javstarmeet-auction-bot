"""Abstract store interface."""
from abc import ABC, abstractmethod
from typing import Any


class Store(ABC):
    """Abstract store defining the data access contract."""

    @property
    @abstractmethod
    def is_pg(self) -> bool: ...

    # --- User Methods ---
    @abstractmethod
    async def register_user(self, user_id: int, info: dict) -> None: ...

    @abstractmethod
    async def get_user(self, user_id: int) -> dict | None: ...

    @abstractmethod
    async def is_registered(self, user_id: int) -> bool: ...

    @abstractmethod
    async def get_all_users(self) -> list[dict]: ...

    # --- Blacklist Methods ---
    @abstractmethod
    async def add_blacklist(self, user_id: int, reason: str = "violation") -> None: ...

    @abstractmethod
    async def remove_blacklist(self, user_id: int) -> None: ...

    @abstractmethod
    async def is_blacklisted(self, user_id: int) -> bool: ...

    # --- Session Methods ---
    @abstractmethod
    async def get_next_session(self) -> tuple[str, int]: ...  # session_id, seq_num

    # --- Order Methods ---
    @abstractmethod
    async def add_order(self, order: dict) -> None: ...

    @abstractmethod
    async def get_all_orders(self) -> list[dict]: ...

    @abstractmethod
    async def update_order_status(self, order_id: str, status: str) -> None: ...

    @abstractmethod
    async def get_user_orders(self, user_id: int) -> list[dict]: ...

    @abstractmethod
    async def get_session_orders(self, session_id: str) -> list[dict]: ...

    # --- Config Methods ---
    @abstractmethod
    async def set_config(self, key: str, value: Any) -> None: ...

    @abstractmethod
    async def get_config(self, key: str) -> Any: ...

    @abstractmethod
    async def get_auction_queue(self) -> list[dict]: ...

    @abstractmethod
    async def set_auction_queue(self, queue: list[dict]) -> None: ...
