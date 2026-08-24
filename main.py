"""
JAV Auction Bot — Entry Point

Architecture:
    main.py          → Entry point (this file), wires config + store + engine + adapter
    platform/        → Telegram adapter (polling + command routing)
    core/            → Business logic (auction, batch, settlement)
    store/           → Data layer (PostgreSQL / JSON)
    models/          → Dataclasses (User, Auction, Order)
    config.py        → Environment variable loader

For development: all handlers still live in main_ext.py (to be migrated).
"""
import asyncio
import logging
import os

from dotenv import load_dotenv

from config import BOT_TOKEN, ADMIN_IDS, DATABASE_URL
from store import create_store
from platform import TelegramAdapter

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def main():
    """Initialize components and start the bot."""
    load_dotenv()

    logger.info("Starting JAV Auction Bot...")
    logger.info("Connecting to store...")
    store = await create_store(DATABASE_URL)
    logger.info(f"Store ready (pg={store.is_pg})")

    # Import engine after store is ready (avoids circular import)
    from core.auction import AuctionEngine
    engine = AuctionEngine(store)

    # Start Telegram adapter
    adapter = TelegramAdapter(
        token=BOT_TOKEN,
        admin_ids=ADMIN_IDS,
        engine=engine,
        store=store,
    )

    logger.info("Starting Telegram polling...")
    await adapter.start()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down...")
