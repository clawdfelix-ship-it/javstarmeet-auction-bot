"""Store factory - creates either PostgreSQL or JSON store based on config."""
import os
import logging

logger = logging.getLogger(__name__)


async def create_store(database_url: str = None, data_path: str = None):
    """Create and return the appropriate store instance.

    Args:
        database_url: PostgreSQL connection string. If set, uses asyncpg.
        data_path: Path for JSON fallback file. Defaults to "data.json".

    Returns:
        A Store subclass instance (PostgresStore or JsonStore).
    """
    from store.postgres import PostgresStore
    from store.json_store import JsonStore

    if database_url:
        logger.info("Using PostgreSQL store")
        store = PostgresStore(database_url)
        await store.connect()
        return store
    else:
        path = data_path or os.getenv("DATA_PATH", "data.json")
        logger.info(f"Using JSON store: {path}")
        return JsonStore(path)
