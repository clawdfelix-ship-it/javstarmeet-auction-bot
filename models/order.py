"""Order dataclass."""
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class Order:
    order_id: str
    user_id: int
    item: str
    price: int
    status: str  # pending, won, paid, shipped, cancelled
    created_at: datetime | None = None
    session_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Order":
        created = data.get("created_at") or data.get("time")
        if isinstance(created, str):
            try:
                created = datetime.fromisoformat(created)
            except Exception:
                created = None
        return cls(
            order_id=data["order_id"],
            user_id=data["user_id"],
            item=data["item"],
            price=data["price"],
            status=data.get("status", "pending"),
            created_at=created,
            session_id=data.get("session_id"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "user_id": self.user_id,
            "item": self.item,
            "price": self.price,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "session_id": self.session_id,
        }
