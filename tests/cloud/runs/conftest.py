"""Fixtures local to the chat-runs test suite."""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def runs_app_client(mongo_db) -> AsyncClient:  # noqa: ARG001 — forces Beanie init
    """FastAPI app with the runs router mounted, deps overridden to u1/w1."""
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.chat.runs.router import router as runs_router
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.cloud.shared.deps import current_user_id, current_workspace_id

    app = FastAPI()
    add_error_handler(app)
    app.include_router(runs_router)

    app.dependency_overrides[current_user_id] = lambda: "u1"
    app.dependency_overrides[current_workspace_id] = lambda: "w1"
    app.dependency_overrides[require_license] = lambda: None

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


@pytest_asyncio.fixture
async def seed_run(mongo_db):  # noqa: ARG001 — forces Beanie init
    """Insert a ChatRunDoc r1 owned by workspace w1 / user u1 / scope session:s1."""
    from pocketpaw_ee.cloud.chat.runs import service as run_service
    from pocketpaw_ee.cloud.chat.runs.domain import RunSpec

    spec = RunSpec(
        run_id="r1",
        workspace_id="w1",
        context_type="session",
        scope_id="s1",
        session_key="session:s1",
        group=None,
        user_id="u1",
        agent_id="a1",
        client_message_id="c1",
        user_message_id="m1",
        content="hi",
        history=[],
        intent=None,
    )
    return await run_service.create_run(spec)


@pytest.fixture(autouse=True)
def _fresh_worker_bootstrap():
    """Reset the worker's lane counter around every test in this directory.

    ``worker._startup`` became refcounted on 2026-09-04 (backend-perf C1): two arq
    lanes now share one process, so the bootstrap runs for the FIRST lane and the
    second is a no-op. That makes it PROCESS state, and a test session is one
    process — so the second test to call ``_startup`` would otherwise be silently
    skipped, and its assertions about what boot registers would fail for a reason
    that has nothing to do with what it is testing.

    Autouse rather than per-test because the trap is invisible: nothing about
    calling ``_startup`` suggests a previous test could have consumed it.
    """
    from pocketpaw_ee.cloud.chat.runs import worker as _worker

    _worker._reset_bootstrap_for_tests()
    yield
    _worker._reset_bootstrap_for_tests()
