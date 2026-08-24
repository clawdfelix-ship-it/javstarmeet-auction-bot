"""Configuration loader - loads from environment variables."""
import os
from dotenv import load_dotenv

load_dotenv()

# Bot
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
ADMIN_IDS: list[int] = [int(x) for x in os.getenv("ADMIN_IDS", "582328026").split(",")]

# Database
DATABASE_URL: str | None = os.getenv("DATABASE_URL")
DATA_PATH: str = os.getenv("DATA_PATH", "data.json")

# Group IDs
PROD_GROUP_ID: int | None = None  # resolved from store at runtime
TEST_GROUP_ID: int | None = None

# WebApp
WEBAPP_URL: str | None = os.getenv("WEBAPP_URL")

# Batch auction constants
ITEM_DURATION: int = 25   # seconds per auction item
PAUSE_BETWEEN_ITEMS: int = 3  # seconds pause between items in batch mode

# Custom bid prompt message
CUSTOM_BID_PROMPT: str = "請回覆此訊息輸入您的出價金額 (純數字)："


def load_config() -> dict:
    """Return config as a dict for backward compatibility."""
    return {
        "bot_token": BOT_TOKEN,
        "admin_ids": ADMIN_IDS,
        "database_url": DATABASE_URL,
        "data_path": DATA_PATH,
        "prod_group_id": PROD_GROUP_ID,
        "test_group_id": TEST_GROUP_ID,
        "webapp_url": WEBAPP_URL,
        "item_duration": ITEM_DURATION,
        "pause_between_items": PAUSE_BETWEEN_ITEMS,
        "custom_bid_prompt": CUSTOM_BID_PROMPT,
    }
