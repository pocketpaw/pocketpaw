"""Fixtures for MongoMemoryStore tests.

Uses ``mongomock-motor`` so the suite runs in CI without a real MongoDB
service. Each test gets an isolated in-memory database via a uniquely-named
mock client.
"""

from __future__ import annotations

import uuid

import pytest


@pytest.fixture()
async def beanie_memory_db():
    """Initialize Beanie against an in-memory mongomock-motor database.

    Beanie >=1.26 calls ``database.list_collection_names(authorizedCollections=True,
    nameOnly=True)``; mongomock-motor's stub doesn't accept those kwargs.
    We wrap the method to drop unknown kwargs so the suite runs in CI
    without a real MongoDB service.
    """
    from beanie import init_beanie
    from mongomock_motor import AsyncMongoMockClient
    from pocketpaw_ee.cloud.memory.documents import MemoryFactDoc
    from pocketpaw_ee.cloud.models import ALL_DOCUMENTS

    db_name = f"test_memory_{uuid.uuid4().hex[:8]}"
    client = AsyncMongoMockClient()
    db = client[db_name]

    original = db.list_collection_names

    async def _safe_list_collection_names(*_args, **_kwargs):
        # mongomock-motor doesn't honour authorizedCollections / nameOnly;
        # the no-arg call returns the same list we need for Beanie init.
        return await original()

    db.list_collection_names = _safe_list_collection_names  # type: ignore[method-assign]

    await init_beanie(database=db, document_models=[*ALL_DOCUMENTS, MemoryFactDoc])
    yield db


@pytest.fixture()
async def store(beanie_memory_db):
    """A fresh MongoMemoryStore bound to the per-test database."""
    from pocketpaw_ee.cloud.memory.mongo_store import MongoMemoryStore

    return MongoMemoryStore()
