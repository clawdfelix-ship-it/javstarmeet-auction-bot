"""PostgreSQL store implementation using asyncpg."""
import asyncio
import json
import logging
from datetime import datetime
from typing import Any

try:
    import asyncpg
except ImportError:
    asyncpg = None

from store.base import Store

logger = logging.getLogger(__name__)


class PostgresStore(Store):
    """PostgreSQL implementation of the Store interface."""

    def __init__(self, database_url: str):
        self._database_url = database_url
        self._pool: asyncpg.Pool | None = None

    @property
    def is_pg(self) -> bool:
        return True

    async def connect(self) -> None:
        """Establish connection pool and initialize tables."""
        if not asyncpg:
            logger.error("DATABASE_URL present but asyncpg not installed.")
            raise RuntimeError("asyncpg is required for PostgreSQL support")

        retries = 5
        for i in range(retries):
            try:
                logger.info(f"Connecting to DB... (Attempt {i+1}/{retries})")
                self._pool = await asyncpg.create_pool(self._database_url)
                await self._init_tables()
                logger.info("Connected to PostgreSQL (Async)")
                return
            except Exception as e:
                logger.error(f"Failed to connect to DB: {e}")
                if i < retries - 1:
                    await asyncio.sleep(5)
                else:
                    raise

    async def _init_tables(self) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    name TEXT,
                    phone TEXT,
                    email TEXT,
                    pickup TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS blacklist (
                    user_id BIGINT PRIMARY KEY,
                    reason TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    user_id BIGINT,
                    item TEXT,
                    price INTEGER,
                    status TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    session_id TEXT
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS system_config (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    date TEXT,
                    seq_num INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            try:
                await conn.execute("ALTER TABLE orders ADD COLUMN session_id TEXT")
            except Exception:
                logger.exception("Failed to add session_id column to orders table")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    # --- User Methods ---
    async def register_user(self, user_id: int, info: dict) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO users (user_id, name, phone, email, pickup)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (user_id) DO UPDATE
                SET name=EXCLUDED.name, phone=EXCLUDED.phone, email=EXCLUDED.email, pickup=EXCLUDED.pickup
            """, user_id, info['name'], info['phone'], info.get('email', ''), info['pickup'])

    async def get_user(self, user_id: int) -> dict | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
            if row:
                return dict(row)
            return None

    async def is_registered(self, user_id: int) -> bool:
        user = await self.get_user(user_id)
        return user is not None

    async def get_all_users(self) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM users")
            return [dict(row) for row in rows]

    # --- Blacklist Methods ---
    async def add_blacklist(self, user_id: int, reason: str = "violation") -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO blacklist (user_id, reason) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                user_id, reason
            )

    async def remove_blacklist(self, user_id: int) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM blacklist WHERE user_id = $1", user_id)

    async def is_blacklisted(self, user_id: int) -> bool:
        async with self._pool.acquire() as conn:
            val = await conn.fetchval("SELECT 1 FROM blacklist WHERE user_id = $1", user_id)
            return val is not None

    # --- Session Methods ---
    async def get_next_session(self) -> tuple[str, int]:
        today = datetime.now().strftime("%Y-%m-%d")
        async with self._pool.acquire() as conn:
            count = await conn.fetchval("SELECT COUNT(*) FROM sessions WHERE date = $1", today)
            seq = count + 1
            session_id = f"{today.replace('-','')}-{seq}"
            await conn.execute(
                "INSERT INTO sessions (session_id, date, seq_num) VALUES ($1, $2, $3)",
                session_id, today, seq
            )
            return session_id, seq

    # --- Order Methods ---
    async def add_order(self, order: dict) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO orders (order_id, user_id, item, price, status, created_at, session_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
            """, order['order_id'], order['user_id'], order['item'], order['price'],
                order['status'], datetime.fromisoformat(order['time']), order.get('session_id'))

    async def get_all_orders(self) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM orders ORDER BY created_at DESC")
            return [dict(row) for row in rows]

    async def update_order_status(self, order_id: str, status: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("UPDATE orders SET status = $1 WHERE order_id = $2", status, order_id)

    async def get_user_orders(self, user_id: int) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM orders WHERE user_id = $1 ORDER BY created_at DESC", user_id
            )
            return [dict(row) for row in rows]

    async def get_session_orders(self, session_id: str) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM orders WHERE session_id = $1 ORDER BY user_id, created_at", session_id
            )
            return [dict(row) for row in rows]

    # --- Config Methods ---
    async def set_config(self, key: str, value: Any) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO system_config (key, value) VALUES ($1, $2)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """, key, str(value))

    async def get_config(self, key: str) -> Any:
        async with self._pool.acquire() as conn:
            val = await conn.fetchval("SELECT value FROM system_config WHERE key = $1", key)
            if val:
                if val.isdigit():
                    return int(val)
                return val
            return None

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
