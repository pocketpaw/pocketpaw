# conftest.py — shared fixtures for the EE uploads test package.
# 2026-07-03 (FL-12b): ``beanie_upload_db`` now also registers ``ShareLink`` so
# the public share-link store/route tests get a real (mongomock) collection.
from __future__ import annotations

import uuid
from pathlib import Path

import pytest


@pytest.fixture()
def tmp_upload_root(tmp_path: Path) -> Path:
    """Isolated storage root for each test."""
    root = tmp_path / "uploads"
    root.mkdir()
    return root


@pytest.fixture()
async def beanie_upload_db():
    from beanie import init_beanie
    from mongomock_motor import AsyncMongoMockClient

    db_name = f"test_uploads_{uuid.uuid4().hex[:8]}"
    client = AsyncMongoMockClient()
    db = client[db_name]

    original = db.list_collection_names

    async def _safe(*_a, **_kw):
        return await original()

    db.list_collection_names = _safe  # type: ignore[method-assign]

    # Import after db creation to avoid circular imports
    from pocketpaw_ee.cloud.uploads.models import FileFolder, FileUpload
    from pocketpaw_ee.cloud.uploads.share_models import ShareLink

    await init_beanie(database=db, document_models=[FileUpload, FileFolder, ShareLink])
    yield db


@pytest.fixture()
async def store(beanie_upload_db):
    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

    return MongoFileStore()
