# tests/ee/sites/conftest.py
# Created: 2026-06-18 (feat/sites-dedupe-migration, PERF-2).
# Merged 2026-06-17 (fix/sites-plan-gate-asymmetry) — UNION of the PERF-2 Beanie
# settings fixture and the plan-gate default-plan fixture (see below).
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
# This session-scoped autouse fixture (``_sites_beanie_settings``) runs
# ``init_beanie`` ONCE against a throwaway mongomock database so model CONSTRUCTION
# works everywhere in ``tests/ee/sites/``. It does NOT replace ``beanie_test_db``:
# the per-test fixture still re-inits against its own fresh DB for the async tests'
# DB isolation (Beanie re-init just repoints the settings at the new client). This
# fixture only guarantees the settings exist so a fixtureless pure test can build a
# doc.
#
# fix/sites-plan-gate-asymmetry — Sites is now plan-gated at the service
# chokepoint: sites.service.publish()/publish_pocket() and the create MCP handlers
# call require_sites_plan(), which reads the workspace plan via
# workspace_service.get_workspace_plan and raises Forbidden('plan.feature_denied')
# (or NotFound when the workspace is missing). The existing mechanics tests in this
# directory call publish/create with SYNTHETIC workspace ids that have no seeded
# Workspace doc, so the gate would now raise NotFound and mask what they actually
# exercise. The ``_default_sites_plan`` autouse fixture patches get_workspace_plan
# to return a plan that INCLUDES Sites ("business") by default, so the happy-path
# mechanics tests pass the gate. The dedicated gate test (test_plan_gate.py)
# overrides the plan per-test with its own patch (an inner patch of the same target
# wins while active), so the team-plan denial cases still assert correctly.

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("pocketpaw_ee")


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


@pytest.fixture(autouse=True)
def _default_sites_plan(request: pytest.FixtureRequest):
    """Default the workspace plan to one that unlocks Sites ('business') for the
    sites mechanics tests, so the new service-level plan gate doesn't reject their
    synthetic workspace ids. test_plan_gate.py patches the same target per-test to
    exercise the denial paths."""
    # The gate test owns the plan itself — don't double-patch under it.
    if request.module.__name__.endswith("test_plan_gate"):
        yield
        return
    from unittest.mock import patch

    with patch(
        "pocketpaw_ee.cloud.workspace.service.get_workspace_plan",
        new=AsyncMock(return_value="business"),
    ):
        yield
