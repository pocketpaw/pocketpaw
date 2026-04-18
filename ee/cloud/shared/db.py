"""MongoDB connection and Beanie ODM initialization."""

from __future__ import annotations

import logging

from beanie import init_beanie
from pymongo import AsyncMongoClient

logger = logging.getLogger(__name__)

_client: AsyncMongoClient | None = None


async def init_cloud_db(mongo_uri: str = "mongodb://localhost:27017/paw-cloud") -> None:
    """Initialize Beanie ODM with all document models."""
    global _client

    from ee.cloud.models import ALL_DOCUMENTS

    _client = AsyncMongoClient(mongo_uri)
    db_name = mongo_uri.rsplit("/", 1)[-1].split("?")[0] or "paw-cloud"
    db = _client[db_name]

    await init_beanie(database=db, document_models=ALL_DOCUMENTS)
    logger.info("Cloud DB initialized: %s (%d models)", db_name, len(ALL_DOCUMENTS))


async def close_cloud_db() -> None:
    """Close the client."""
    global _client
    if _client:
        _client.close()
        _client = None


def get_client() -> AsyncMongoClient | None:
    """Return the current MongoDB client, or None if not initialized."""
    return _client
