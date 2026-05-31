"""Fixtures local to the chat-runs test suite."""

from __future__ import annotations

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
