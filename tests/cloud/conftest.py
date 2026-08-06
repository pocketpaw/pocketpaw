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

Also installs, autouse, ``_isolated_person_store`` (T-2, 2026-08-05): lazily
redirects the people service's default Fabric journal store at a per-test tmp
journal so workspace create / invite accept / Person refresh paths never write
into the developer's real ``~/.soul/journal.db``.

Also installs, autouse, ``_isolated_bus_subscriptions`` (2026-08-06): snapshots
and restores every process-global subscriber registry ``mount_cloud()`` writes
into, so a test that mounts the app cannot leave handlers behind for the tests
that run after it. See the fixture for the order-dependence it fixes.

Also exposes:
- ``clean_bus_slate`` — opt-in companion to the above. Clears the subscriber
  registries so a mount-pin measures ONLY what ``mount_cloud()`` registers.
- ``mongo_db`` — Beanie initialized against a fresh mongomock-motor DB
  for the test. Used by service-level tests that exercise real Beanie
  query paths instead of relying on a Protocol fake.
- ``cloud_app_client`` — a FastAPI app with the enterprise chat routers
  mounted and auth/license dependencies overridden, used by HTTP-layer
  tests so they don't need a real JWT.
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
def _isolated_person_store(tmp_path, monkeypatch):
    """Keep the people service's default Fabric store off the real org journal.

    Added 2026-08-05 (T-2, feat/coupling-person-freshness). ``workspace.create``
    now materializes an owner Person (and ``accept_invite`` has materialized
    members since pp#1366) through ``people_service._default_store``, which
    opens the REAL org journal at ``~/.soul/journal.db`` — so any cloud test
    that creates a workspace would write Person rows into the developer's live
    soul data. Same hazard class ``local_store_home`` guards against.

    LAZY on purpose: the tmp journal is only opened if a test actually reaches
    an unpatched ``_default_store()`` call, so the fixture costs nothing for
    the vast majority of tests. Tests that want to READ the store install
    their own (e.g. ``person_store`` in the workspace/people test files),
    which simply re-patches over this one.
    """
    from pocketpaw_ee.cloud.people import service as people_service

    state: dict = {}

    def _tmp_store():
        if "store" not in state:
            from soul_protocol.engine.journal import open_journal

            from pocketpaw.fabric.journal_store import FabricJournalStore

            journal = open_journal(tmp_path / "person_journal.db")
            store = FabricJournalStore(journal)
            store.bootstrap()
            state["journal"] = journal
            state["store"] = store
        return state["store"]

    monkeypatch.setattr(people_service, "_default_store", _tmp_store)
    yield
    if "journal" in state:
        state["journal"].close()


@pytest.fixture(autouse=True)
def recording_bus():
    """Install a RecordingBus for every test.

    Tests that don't care about events ignore the fixture; tests that
    do request it explicitly to inspect ``bus.events``.

    Owns BOTH module globals in ``_core.realtime.bus``. ``_bus`` is what makes
    the realtime (typed-event) side of ``mount_cloud`` already hermetic: the
    mount calls ``init_realtime()``, which swaps in a real ``InProcessBus``,
    and everything that subscribes to it afterwards (Task→Calendar, the People
    listeners, the outcomes ledger) rides on that object — which this fixture
    drops on teardown. ``_resolver`` is restored for the same reason; it was
    previously left pointing at the last-mounted app's resolver.
    """
    from pocketpaw_ee.cloud._core.realtime import bus as bus_mod

    rec = RecordingBus()
    prev = bus_mod._bus  # type: ignore[attr-defined]
    prev_resolver = bus_mod._resolver  # type: ignore[attr-defined]
    bus_mod._bus = rec  # type: ignore[attr-defined]
    yield rec
    bus_mod._bus = prev  # type: ignore[attr-defined]
    bus_mod._resolver = prev_resolver  # type: ignore[attr-defined]


def _snapshot_bus_subscriptions() -> dict:
    """Capture every process-global subscriber registry ``mount_cloud`` writes.

    Deliberately NOT including ``_core.realtime.bus._bus`` — ``recording_bus``
    above already replaces-and-restores that whole object, so the typed-event
    subscriptions die with it. Everything captured here outlives the test
    unless something puts it back.
    """
    from pocketpaw_ee.cloud.shared.events import event_bus

    import pocketpaw.lifecycle as lifecycle
    from pocketpaw.bus import queue as oss_bus_mod

    oss_bus = oss_bus_mod._bus  # may be None — captured as-is, see _restore
    return {
        "event_handlers": {topic: list(hs) for topic, hs in event_bus._handlers.items()},
        "oss_bus": oss_bus,
        "oss_system": list(oss_bus._system_subscribers) if oss_bus else None,
        "oss_outbound": (
            {ch: list(hs) for ch, hs in oss_bus._outbound_subscribers.items()} if oss_bus else None
        ),
        "lifecycle_registry": dict(lifecycle._registry),
    }


def _restore_bus_subscriptions(snap: dict) -> None:
    from pocketpaw_ee.cloud.shared.events import event_bus

    import pocketpaw.lifecycle as lifecycle
    from pocketpaw.bus import queue as oss_bus_mod

    event_bus._handlers.clear()
    event_bus._handlers.update(snap["event_handlers"])

    # Restore the singleton REFERENCE first: a test may have swapped or reset
    # it (``lifecycle.reset_all()`` nulls it), in which case writing the
    # subscriber lists onto the captured object is only correct once that
    # object is the singleton again.
    oss_bus_mod._bus = snap["oss_bus"]
    if snap["oss_bus"] is not None:
        snap["oss_bus"]._system_subscribers[:] = snap["oss_system"]
        snap["oss_bus"]._outbound_subscribers.clear()
        snap["oss_bus"]._outbound_subscribers.update(snap["oss_outbound"])

    lifecycle._registry.clear()
    lifecycle._registry.update(snap["lifecycle_registry"])


@pytest.fixture(autouse=True)
def _isolated_bus_subscriptions():
    """Keep ``mount_cloud()``'s subscriber registrations inside one test.

    Added 2026-08-06 after the coupling sprint merged. ``mount_cloud()``
    registers roughly a dozen bridges onto PROCESS-GLOBAL singletons, and the
    mount-pin tests that assert those registrations each call it for real. The
    pins that predated this fixture snapshot-and-restored only their OWN topic,
    so every pin left the other bridges subscribed for the rest of the session
    — ``tests/cloud/test_integration.py`` alone mounted 15 times and left 15
    copies of every ``shared.events`` handler behind.

    That is invisible until a later test counts side effects. It cost four
    failures in ``test_instinct_approvals_governance.py``, which passed alone
    and failed after ``test_integration.py``: three counted notifications and
    saw doubles, and ``test_create_does_not_notify_without_the_bridge_
    registered`` — the control that asserts the UNregistered behaviour — got a
    notification from a bridge a previous FILE had subscribed.

    AUTOUSE, matching ``local_store_home`` / ``_isolated_person_store`` above:
    the hazard is created by production code mutating a global, so opting in
    would mean every one of the ~20 ``mount_cloud`` call sites under
    ``tests/cloud/`` remembering to — and the next one that forgets
    re-introduces exactly this bug. What a mount-pin DOES opt into is
    ``clean_bus_slate``, which is about measurement, not hermeticity.

    Three registries, all of which ``mount_cloud`` writes and none of which
    reset themselves:

    * ``shared.events.event_bus._handlers`` — the string-topic bus. Carries
      the lead→notification, lead→growth, instinct-approval→notification and
      meeting bridges, plus the pre-existing ``shared.event_handlers`` and
      ``agent_bridge`` subscribers. This is the one that caused the failures.
    * The OSS ``MessageBus`` (``pocketpaw.bus``) — where the alert→notification
      bridge lands via ``subscribe_system``. Its ``register_*`` is
      unsubscribe-first so it does not accumulate, but a test may still clear
      or replace it (see ``clean_bus_slate``).
    * ``pocketpaw.lifecycle._registry`` — not a bus, but the same class of
      leak and reachable from the same tests: ``reset_all()`` CLEARS the whole
      registry, so one call silently disarms ``reset_all()`` for every test
      that runs later.
    """
    snap = _snapshot_bus_subscriptions()
    yield
    _restore_bus_subscriptions(snap)


@pytest.fixture
def clean_bus_slate():
    """Blank subscriber registries, so a mount-pin measures only the mount.

    The companion to ``_isolated_bus_subscriptions``: that one guarantees a
    test cannot leak OUT, this one guarantees nothing leaked IN. A pin asserting
    "``mount_cloud`` subscribed handler X" is only a pin if X is absent
    beforehand — otherwise deleting the production ``register_*`` call leaves a
    handler some earlier test subscribed, and the assertion passes on it.

    Restoration is deliberately NOT this fixture's job; the autouse fixture
    already puts everything back.
    """
    from pocketpaw_ee.cloud.shared.events import event_bus

    from pocketpaw.bus import get_message_bus

    event_bus._handlers.clear()
    get_message_bus()._system_subscribers.clear()
    return None


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
