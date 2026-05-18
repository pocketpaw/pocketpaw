"""Shared fixtures for cloud tests.

Installs a RecordingBus for every test so ``emit()`` calls inside services
don't raise AssertionError (the real bus is only set up in ``init_realtime``
during app startup, which tests don't invoke). Tests that want to assert
on emitted events request the ``recording_bus`` fixture explicitly to read
``bus.events``.

Also exposes:
- ``mongo_db`` — Beanie initialized against a fresh mongomock-motor DB
  for the test. Used by service-level tests that exercise real Beanie
  query paths instead of relying on a Protocol fake.
- ``cloud_app_client`` — a FastAPI app with the enterprise chat routers
  mounted and auth/license dependencies overridden, used by HTTP-layer
  tests so they don't need a real JWT.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

# Every test under ``tests/cloud/`` exercises ``ee.cloud.*``, which pulls
# ``beanie`` (the cloud-extras stack) on import. Skip the whole tree with
# a clear reason when those extras aren't installed, instead of letting
# pytest emit a per-file collection error that's easy to miss in a
# verbose log. CI installs everything via ``uv sync --dev --all-extras``
# so this is a no-op there; locally it just makes the contract explicit.
pytest.importorskip(
    "beanie",
    reason="ee/cloud tests require the cloud extras — install with `uv sync --dev --all-extras`",
)
pytest.importorskip("mongomock_motor", reason="mongomock-motor is required for cloud tests")

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from ee.cloud._core.realtime.events import Event


class RecordingBus:
    """Test EventBus that records published events instead of fanning out.

    Drop-in replacement for the production bus. Tests assert on
    ``bus.events`` to verify emit-time behavior. ``subscribe`` is a no-op
    so the bus satisfies the same Protocol shape as :class:`InProcessBus`.
    """

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def publish(self, event: Event) -> None:
        self.events.append(event)

    def subscribe(self, event_type: str, handler) -> None:  # noqa: ARG002
        # Tests can install their own subscribers via the real InProcessBus
        # in a dedicated fixture; the recording bus stays inert by design.
        return


@pytest.fixture(autouse=True)
def recording_bus():
    """Install a RecordingBus for every test.

    Tests that don't care about events ignore the fixture; tests that
    do request it explicitly to inspect ``bus.events``.
    """
    from ee.cloud._core.realtime import bus as bus_mod

    rec = RecordingBus()
    prev = bus_mod._bus  # type: ignore[attr-defined]
    bus_mod._bus = rec  # type: ignore[attr-defined]
    yield rec
    bus_mod._bus = prev  # type: ignore[attr-defined]


@pytest_asyncio.fixture
async def mongo_db() -> Any:
    """Initialize Beanie against an isolated in-memory mongomock-motor DB.

    Each test gets a uniquely-named database. Beanie >=1.26 calls
    ``database.list_collection_names(authorizedCollections=True, nameOnly=True)``;
    mongomock-motor doesn't accept those kwargs, so we wrap the method
    to drop unknown kwargs.
    """
    from beanie import init_beanie
    from mongomock_motor import AsyncMongoMockClient

    from ee.cloud.memory.documents import MemoryFactDoc
    from ee.cloud.models import ALL_DOCUMENTS

    db_name = f"test_{uuid.uuid4().hex[:8]}"
    client = AsyncMongoMockClient()
    db = client[db_name]

    original = db.list_collection_names

    async def _safe_list_collection_names(*_args, **_kwargs):
        return await original()

    db.list_collection_names = _safe_list_collection_names  # type: ignore[method-assign]

    await init_beanie(database=db, document_models=[*ALL_DOCUMENTS, MemoryFactDoc])
    yield db


def _fixed_user() -> str:
    return "u1"


def _fixed_workspace() -> str:
    return "w1"


def _no_op_license() -> None:
    return None


@pytest_asyncio.fixture
async def cloud_app_client() -> AsyncClient:
    from ee.cloud._core.http import add_error_handler
    from ee.cloud.chat.agent_router import router as agent_router
    from ee.cloud.license import require_license
    from ee.cloud.shared.deps import current_user_id, current_workspace_id

    app = FastAPI()
    add_error_handler(app)
    app.include_router(agent_router)
    app.dependency_overrides[current_user_id] = _fixed_user
    app.dependency_overrides[current_workspace_id] = _fixed_workspace
    app.dependency_overrides[require_license] = _no_op_license

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


# ---------------------------------------------------------------------------
# Audit fixtures — ee.cloud.audit entity (B1).
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def audit_store_tmp(tmp_path):
    """Fresh AuditStore backed by a tmp SQLite file.

    Tests that exercise the cloud audit entity inject this via
    ``audit_service.agent_list_audit(ctx, body, store=...)`` so the
    home-directory singleton (``get_audit_store``) is never touched.
    """
    from pocketpaw.audit.store import AuditStore

    store = AuditStore(db_path=tmp_path / "audit.db")
    yield store


@pytest_asyncio.fixture
async def make_audit_entry(audit_store_tmp):
    """Factory that inserts an audit row scoped to a workspace.

    The store's ``log_entry`` does not accept ``workspace_id`` directly;
    workspace tenancy travels on ``context.workspace_id`` (the same JSON
    column ``search_entries`` rolls up over). Tests stay terse:

        await make_audit_entry("w1", action="x", description="...")
    """

    async def _make(
        workspace_id: str,
        *,
        actor: str = "system",
        action: str = "test.action",
        category: str = "decision",
        description: str = "test entry",
        pocket_id: str | None = None,
        context: dict | None = None,
        metadata: dict | None = None,
        status: str = "completed",
    ) -> str:
        merged_context = dict(context or {})
        merged_context.setdefault("workspace_id", workspace_id)
        return await audit_store_tmp.log_entry(
            actor=actor,
            action=action,
            category=category,
            description=description,
            pocket_id=pocket_id,
            context=merged_context,
            metadata=metadata,
            status=status,
        )

    return _make


# ---------------------------------------------------------------------------
# Plan session fixtures — ee.cloud.planner / mission_control plan-sessions
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def make_plan_session(mongo_db):  # noqa: ARG001 — fixture forces Beanie init
    """Factory that inserts a ``PlanSession`` Beanie doc + a linked Project.

    The drafts list endpoint resolves session ``name`` from the linked
    Project, so the factory inserts both — callers that only care about
    the session can ignore the returned project id.

    Each call returns ``(plan_session_id, project_id)`` so tests can
    correlate the inserted doc with its display name.
    """

    from ee.cloud.models.planner import PlanSession as _PlanSessionDoc
    from ee.cloud.models.project import Project as _ProjectDoc

    async def _make(
        workspace_id: str,
        *,
        name: str = "Q2 Marketing Plan",
        status: str = "ready",
        task_ids: list[str] | None = None,
        project_id: str | None = None,
    ) -> tuple[str, str]:
        # Insert the Project first so the listing endpoint can resolve
        # the display name.
        proj = _ProjectDoc(
            workspace=workspace_id,
            name=name,
            description="",
            color="",
            lead_id=None,
            status="active",
            created_by="u1",
        )
        await proj.insert()
        resolved_project_id = project_id or str(proj.id)

        doc = _PlanSessionDoc(
            workspace=workspace_id,
            project_id=resolved_project_id,
            status=status,
            prd_file_id=None,
            plan_file_id=None,
            goal_file_id=None,
            task_ids=list(task_ids or []),
            agent_gaps=[],
            dependency_warnings=[],
        )
        await doc.insert()
        return str(doc.id), resolved_project_id

    return _make
