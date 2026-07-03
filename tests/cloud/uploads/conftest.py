# conftest.py — shared fixtures for the cloud/uploads test package.
# 2026-07-03 (FL-6): the ``beanie_upload_db`` fixture now resets the Beanie
#   binding on ``FileUpload`` / ``FileFolder`` after each test. Before FL-6 no
#   test read Beanie state from inside the listener, so a leaked binding (the
#   torn-down mongomock db still attached to the Document classes) was
#   harmless. FL-6's listener loads the FileUpload row to honour hide_from_ai,
#   so a leaked binding let one test's seeded rows bleed into an unrelated
#   listener test (e.g. test_listener_vector), flipping the hide gate. Clearing
#   the per-class Beanie settings on teardown restores isolation.
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

    await init_beanie(database=db, document_models=[FileUpload, FileFolder])
    try:
        yield db
    finally:
        # Reset the Beanie binding so this test's mongomock db (about to be
        # torn down) doesn't stay attached to the Document classes and leak
        # seeded rows into a later test that reads Beanie state (FL-6 listener
        # loads the FileUpload row). Beanie stores the collection on a private
        # settings object per Document; drop it so the next init rebinds clean.
        for model in (FileUpload, FileFolder):
            for attr in ("_document_settings", "_settings"):
                if hasattr(model, attr):
                    try:
                        setattr(model, attr, None)
                    except Exception:  # pragma: no cover — defensive
                        pass


@pytest.fixture()
async def store(beanie_upload_db):
    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

    return MongoFileStore()
