# tests/ee/sites/conftest.py
# Created: 2026-06-18 (feat/sites-dedupe-migration, PERF-2).
#
# PERF-2's ``test_dedupe.py`` has PURE picker tests (``pick_canonical``) that build
# in-memory ``Site`` docs WITHOUT the function-scoped ``beanie_test_db`` DB fixture
# — the picker never touches the DB. But Beanie's ``Document.__init__`` resolves the
# document's collection settings, which raises ``CollectionWasNotInitialized`` until
# ``init_beanie`` has run at least once in the process. The async tests in the same
# file DO take ``beanie_test_db``, but it is function-scoped (its mongomock client
# goes out of scope on teardown), so a pure test that runs in isolation — or after
# that teardown — has no initialised settings and cannot even CONSTRUCT a ``Site``.
#
# This session-scoped autouse fixture runs ``init_beanie`` ONCE against a throwaway
# mongomock database so model CONSTRUCTION works everywhere in ``tests/ee/sites/``.
# It does NOT replace ``beanie_test_db``: the per-test fixture still re-inits against
# its own fresh DB for the async tests' DB isolation (Beanie re-init just repoints
# the settings at the new client). This fixture only guarantees the settings exist
# so a fixtureless pure test can build a doc.
from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture(scope="session", autouse=True)
async def _sites_beanie_settings() -> Any:
    """Initialise Beanie once for the sites test module so in-memory ``Site``
    construction works in the pure (fixtureless) picker tests."""
    from beanie import init_beanie
    from mongomock_motor import AsyncMongoMockClient
    from pocketpaw_ee.cloud.memory.documents import MemoryFactDoc
    from pocketpaw_ee.cloud.models import ALL_DOCUMENTS

    client = AsyncMongoMockClient()
    db = client["test_ee_sites_settings"]

    original = db.list_collection_names

    async def _safe_list_collection_names(*_args: Any, **_kwargs: Any) -> list[str]:
        return await original()

    db.list_collection_names = _safe_list_collection_names  # type: ignore[method-assign]

    await init_beanie(database=db, document_models=[*ALL_DOCUMENTS, MemoryFactDoc])
    yield
