"""Shared fixtures for cloud tests.

Installs a RecordingBus for every test so ``emit()`` calls inside services
don't raise AssertionError (the real bus is only set up in ``init_realtime``
during app startup, which tests don't invoke). Tests that want to assert
on emitted events request the ``recording_bus`` fixture explicitly to read
``bus.events``.

Also installs, session-wide and autouse, ``local_store_home``: it redirects
``pocketpaw.stores._DATA_DIR`` at a tmp directory so a test that reaches an
unpatched local-store factory writes there instead of into the developer's real
``~/.pocketpaw`` (see the fixture for what made this visible).

Also exposes:
- ``mongo_db`` — Beanie initialized against a fresh mongomock-motor DB
  for the test. Used by service-level tests that exercise real Beanie
  query paths instead of relying on a Protocol fake.
- ``cloud_app_client`` — a FastAPI app with the enterprise chat routers
  mounted and auth/license dependencies overridden, used by HTTP-layer
  tests so they don't need a real JWT.
- ``override_workspace_role`` — pins a caller ROLE + workspace on a bare test
  app so ``require_action(...)`` guards run their real role check. Added
  2026-08-16 when the paw-bar admin routes moved off ``require_scope("admin")``
  onto the role gate: those router-level tests mount the router with no auth
  stack, so ``current_active_user`` has nothing to resolve and every admin
  route answers 401 until the dep is overridden.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

# Tier 2 ``worker`` module evaluates ``POCKETPAW_REDIS_URL`` at import
# (arq bypasses the descriptor protocol via __dict__ access, so eager eval
# is the only option). Set a stub here so ``from pocketpaw_ee.cloud.chat.runs
# import worker`` succeeds during test collection. Tests that need to assert
# the unset-env behaviour use ``monkeypatch.delenv`` against the helper
# function ``worker._redis_settings``, not the class attribute.
os.environ.setdefault("POCKETPAW_REDIS_URL", "redis://test:6379/0")

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
from pocketpaw_ee.cloud._core.realtime.events import Event


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


@pytest.fixture(autouse=True, scope="session")
def local_store_home(tmp_path_factory):
    """Point the local store factory at a session tmp dir, never ``~/.pocketpaw``.

    Added 2026-08-01 (AL-2). ``pocketpaw.stores`` resolves every workspace-keyed
    SQLite file under ``_DATA_DIR``, which defaults to the DEVELOPER'S HOME. A
    cloud test that reaches an unpatched factory therefore writes real files into
    the machine's live PocketPaw data directory — which the AL-2 agent-ledger
    emitters made visible immediately: one run of ``-k paw_bar`` left 25
    ``~/.pocketpaw/workspaces/<mongomock ObjectId>/agent_ledger.db`` files behind.
    Nothing was corrupted (the ids are per-run and random), but a suite that
    writes into the machine it runs on is one deploy-shaped accident away from
    mattering, and on a box serving a live demo that accident is expensive.

    SESSION-scoped and NOT paired with a cache reset, deliberately: one directory
    for the whole run keeps any handle the bounded LRU already cached valid, so
    this changes WHERE files land and nothing else about how tests behave.
    """
    from pocketpaw import stores

    original = stores._DATA_DIR
    stores._DATA_DIR = tmp_path_factory.mktemp("pocketpaw-home")
    yield stores._DATA_DIR
    stores._DATA_DIR = original


@pytest.fixture(autouse=True)
def inert_delegate_bridge():
    """Keep the Code Mode delegate bridge off the network in unit tests.

    ``get_delegate_bridge()`` auto-enables whenever ``POCKETPAW_REDIS_URL`` is
    set — and this conftest sets it to the stub ``redis://test:6379/0`` purely so
    the arq worker module imports (see the note at the top of this file).
    Without this fixture that stub hands every test a LIVE Redis bridge pointed
    at a host that does not resolve, so each delegate pays a connect timeout
    before pushing its frame and any test that reads the pushed frame races it.

    Tests covering the cross-process path inject their own bridge explicitly,
    which is the honest way to test a two-process rendezvous anyway.
    """
    from pocketpaw_ee.cloud.codeagent import bridge as bridge_mod

    bridge_mod.set_delegate_bridge(bridge_mod.NullDelegateBridge())
    yield
    bridge_mod._reset_for_tests()


@pytest.fixture(autouse=True)
def _site_pages_are_serving(monkeypatch):
    """Never let a cloud test's site capture make a REAL request to the internet.

    A site screenshot polls the site's own url before rendering it (a deploy is live
    at Cloudflare before it is live at the edge, and a picture of the 404 in between
    lands on the card permanently). Publish-driven tests live in this tree too, and
    the poll's retry schedule runs to ~90s — so one test that happens to await after
    a publish would stall CI on a DNS lookup for a hostname that does not exist,
    which is a miserable thing to diagnose.

    Nothing in this tree reaches the probe today (the capture is a detached task and
    these tests end before it runs), so this is insurance, not a fix. It defaults the
    probe to ready and zeroes both delay schedules; the gate's own tests live in
    tests/ee/sites/test_capture_readiness.py and patch these same attributes.
    """
    try:
        from pocketpaw_ee.sites import screenshot as screenshot_mod
    except Exception:  # noqa: BLE001 — OSS-only install: nothing to stub
        yield
        return

    async def _serving(_url: str, **_kw) -> bool:
        return True

    monkeypatch.setattr(screenshot_mod, "_url_is_serving", _serving)
    monkeypatch.setattr(screenshot_mod, "_READY_DELAYS", ())
    monkeypatch.setattr(screenshot_mod, "_READY_DELAYS_MANUAL", ())
    yield


@pytest.fixture(autouse=True)
def recording_bus():
    """Install a RecordingBus for every test.

    Tests that don't care about events ignore the fixture; tests that
    do request it explicitly to inspect ``bus.events``.
    """
    from pocketpaw_ee.cloud._core.realtime import bus as bus_mod

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
    from pocketpaw_ee.cloud.memory.documents import MemoryFactDoc
    from pocketpaw_ee.cloud.models import ALL_DOCUMENTS

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


def fake_workspace_user(role: str = "admin", workspace_id: str = "w1", user_id: str = "u1") -> Any:
    """User stand-in shaped like ``ee.cloud.models.user.User`` — only the fields
    the RBAC chain reads (``id``, ``active_workspace``, ``workspaces``).

    ``role`` is the caller's WorkspaceRole in ``workspace_id`` (member | admin |
    owner), which is what ``check_workspace_action`` compares against the ACTIONS
    matrix. Nothing here is faked past the identity: the guard itself runs for real.
    """
    from types import SimpleNamespace

    return SimpleNamespace(
        id=user_id,
        active_workspace=workspace_id,
        workspaces=[SimpleNamespace(workspace=workspace_id, role=role)],
    )


def override_workspace_role(
    app: FastAPI,
    role: str = "admin",
    workspace_id: str = "w1",
    user_id: str = "u1",
) -> None:
    """Pin a caller ROLE + active workspace on a bare test app.

    Router-level tests mount a single router on a plain ``FastAPI()`` with no auth
    stack, so ``current_active_user`` (fastapi-users) finds no session and any
    ``require_action`` guard 401s before its role check ever runs. Overriding both
    deps here lets the guard execute for real against the given role — pass
    ``role="member"`` to assert a 403, ``role="admin"``/``"owner"`` to assert
    success.

    Also installs ``add_error_handler``: a denied guard raises the cloud-native
    ``Forbidden`` (a CloudError), which without the handler escapes as an
    unhandled exception instead of the 403 envelope production returns. The call
    is idempotent, so an app that already registered it is unaffected.
    """
    from pocketpaw_ee.cloud._core.deps import current_workspace_id
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user

    add_error_handler(app)
    user = fake_workspace_user(role=role, workspace_id=workspace_id, user_id=user_id)
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[current_workspace_id] = lambda: workspace_id


@pytest_asyncio.fixture
async def cloud_app_client() -> AsyncClient:
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.chat.agent_router import router as agent_router
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.cloud.shared.deps import current_user_id, current_workspace_id

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

    from pocketpaw_ee.cloud.models.planner import PlanSession as _PlanSessionDoc
    from pocketpaw_ee.cloud.models.project import Project as _ProjectDoc

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
