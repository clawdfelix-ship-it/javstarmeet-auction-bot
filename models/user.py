"""User dataclass."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class User:
    id: int
    name: str
    phone: str
    email: str = ""
    pickup: str = ""
    created_at: datetime | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "User":
        created = data.get("created_at")
        if isinstance(created, str):
            try:
                created = datetime.fromisoformat(created)
            except Exception:
                created = None
        return cls(
            id=data["user_id"],
            name=data.get("name", ""),
            phone=data.get("phone", ""),
            email=data.get("email", ""),
            pickup=data.get("pickup", ""),
            created_at=created,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.id,
            "name": self.name,
            "phone": self.phone,
            "email": self.email,
            "pickup": self.pickup,
        }
