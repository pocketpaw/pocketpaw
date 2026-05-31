# test_audit_bridge.py — regression coverage for #1202.
# Created: 2026-05-24 (#1202) — Before this PR, the EE cloud audit
#   writers (``pockets.action_executor._audit_action_run`` and friends)
#   appended rows to the JSONL ``AuditLogger`` sink while the
#   ``GET /api/v1/audit`` reader queried the SQLite ``AuditStore`` sink,
#   so every list/filter call returned 0 entries even though the writes
#   had fired. ``ee.cloud.audit.listeners.register_audit_bridge`` now
#   installs an ``AuditLogger.on_log`` callback that mirrors each event
#   into the SQLite store. These tests prove:
#     * an ``_audit_action_run`` write surfaces through both
#       ``GET /audit`` (no filter) and ``GET /audit?pocket_id={id}``,
#     * a different workspace cannot see the row,
#     * the bridge is idempotent (calling ``register_audit_bridge``
#       twice doesn't double-mirror).
from __future__ import annotations

import pytest

# OSS-only safety per CLAUDE.md: this file imports pocketpaw_ee — skip
# the whole module on an OSS-only install where the wheel is absent.
pytest.importorskip("pocketpaw_ee")

from types import SimpleNamespace  # noqa: E402

import pytest_asyncio  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from pocketpaw_ee.cloud._core.http import add_error_handler  # noqa: E402
from pocketpaw_ee.cloud.audit import listeners as audit_listeners  # noqa: E402
from pocketpaw_ee.cloud.audit import service as audit_service  # noqa: E402
from pocketpaw_ee.cloud.audit.router import router as audit_router  # noqa: E402
from pocketpaw_ee.cloud.auth import current_active_user  # noqa: E402
from pocketpaw_ee.cloud.license import require_license  # noqa: E402

from pocketpaw.audit import store as audit_store_module  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_user(user_id: str = "u1", workspace_id: str | None = "w1") -> SimpleNamespace:
    """Lightweight stand-in for ``ee.cloud.models.user.User``.

    Mirrors the helper in ``test_audit_router.py`` — only the attributes
    the auth chain reads are filled.
    """
    return SimpleNamespace(
        id=user_id,
        active_workspace=workspace_id,
        workspaces=(
            [SimpleNamespace(workspace=workspace_id, role="admin")] if workspace_id else []
        ),
    )


def _install_service_seam(audit_store) -> None:
    """Rebind ``agent_list_audit`` so the router picks up the tmp store.

    The cloud service reads from the ``get_audit_store()`` singleton by
    default; under test we want to point at a tmp file so concurrent
    suite runs don't see each other's writes. The wrapper is restored by
    ``_restore_service``.
    """
    real = audit_service.agent_list_audit

    async def _bound(ctx, body=None, *, store=None):
        return await real(ctx, body, store=store or audit_store)

    audit_service.agent_list_audit = _bound  # type: ignore[assignment]
    audit_service._orig_agent_list_audit = real  # type: ignore[attr-defined]


def _restore_service() -> None:
    real = getattr(audit_service, "_orig_agent_list_audit", None)
    if real is not None:
        audit_service.agent_list_audit = real  # type: ignore[assignment]
        delattr(audit_service, "_orig_agent_list_audit")


def _build_app(audit_store, *, workspace_id: str = "w1", user_id: str = "u1") -> FastAPI:
    """Build a FastAPI app wired to the cloud audit router + tmp store."""
    app = FastAPI()
    add_error_handler(app)
    app.include_router(audit_router)
    app.dependency_overrides[require_license] = lambda: None

    user = _fake_user(user_id=user_id, workspace_id=workspace_id)

    async def _fake_user_dep():
        return user

    app.dependency_overrides[current_active_user] = _fake_user_dep

    # RBAC denial path is exercised in test_audit_router.py — here we
    # just need a permissive guard so the handler actually runs.
    from pocketpaw_ee.cloud._core import deps as core_deps

    core_deps.check_workspace_action = lambda *a, **k: None  # type: ignore[assignment]

    _install_service_seam(audit_store)
    return app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def bridged_store(audit_store_tmp, monkeypatch):
    """Tmp ``AuditStore`` wired in as the global singleton, with the
    bridge installed.

    The bridge listener (``_mirror_to_store``) calls ``get_audit_store()``
    at callback time, so patching the module-level singleton is enough
    to capture every mirrored write into the tmp DB. We pop only OUR
    bridge callback on teardown (the way ``test_pocket_catalog_gate``
    handles its own ``on_log`` listener) so other suites that register
    their own callbacks are not disturbed.
    """
    from pocketpaw.security.audit import get_audit_logger

    monkeypatch.setattr(audit_store_module, "_audit_store", audit_store_tmp)

    # Force a clean install so we know our specific callback is in the
    # list. Calls to ``register_audit_bridge`` from a previous test (or
    # from ``mount_cloud`` already executed in-process) left the flag set
    # and our callback installed, but we want a deterministic state for
    # the per-test ``list.remove`` teardown below.
    audit_listeners._BRIDGE_REGISTERED = False
    audit_listeners.register_audit_bridge()
    yield audit_store_tmp

    # Remove only our bridge callback — leave everything else alone.
    logger = get_audit_logger()
    try:
        logger._callbacks.remove(audit_listeners._mirror_to_store)
    except ValueError:
        pass
    audit_listeners._BRIDGE_REGISTERED = False


@pytest_asyncio.fixture
async def w1_client(bridged_store) -> AsyncClient:
    app = _build_app(bridged_store, workspace_id="w1")
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            yield client
    finally:
        _restore_service()


@pytest_asyncio.fixture
async def w2_client(bridged_store) -> AsyncClient:
    app = _build_app(bridged_store, workspace_id="w2")
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            yield client
    finally:
        _restore_service()


# ---------------------------------------------------------------------------
# The core regression — writer → reader round trip.
# ---------------------------------------------------------------------------


async def test_action_run_write_visible_via_get_audit(
    w1_client: AsyncClient, bridged_store
) -> None:
    """Firing ``_audit_action_run`` must leave a row that surfaces via
    the no-filter GET /audit response.

    This is the literal acceptance criterion in issue #1202.
    """
    from pocketpaw_ee.cloud.pockets import action_executor

    action_executor._audit_action_run(
        actor="u1",
        workspace_id="w1",
        pocket_id="pocket_alpha",
        action="create_lease",
        status="success",
        base_url="https://api.example.com",
    )

    r = await w1_client.get("/audit", params={"limit": 20})
    assert r.status_code == 200, r.text
    entries = r.json()["entries"]
    assert len(entries) == 1
    row = entries[0]
    assert row["pocket_id"] == "pocket_alpha"
    assert row["action"] == "pocket.actions.run"
    assert row["actor"] == "u1"


async def test_pocket_id_filter_returns_action_run_row(
    w1_client: AsyncClient, bridged_store
) -> None:
    """``GET /audit?pocket_id={id}`` must return the row the write left.

    Filters on a different pocket_id must return zero rows — proves the
    bridge populates the SQL ``pocket_id`` column (the search predicate
    is ``pocket_id = ?``, not a context LIKE match).
    """
    from pocketpaw_ee.cloud.pockets import action_executor

    action_executor._audit_action_run(
        actor="u1",
        workspace_id="w1",
        pocket_id="pocket_alpha",
        action="create_lease",
        status="success",
        base_url="https://api.example.com",
    )
    action_executor._audit_action_run(
        actor="u1",
        workspace_id="w1",
        pocket_id="pocket_beta",
        action="cancel_lease",
        status="success",
        base_url="https://api.example.com",
    )

    r = await w1_client.get("/audit", params={"pocket_id": "pocket_alpha"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert body["entries"][0]["pocket_id"] == "pocket_alpha"

    # The other pocket's row is still queryable.
    r2 = await w1_client.get("/audit", params={"pocket_id": "pocket_beta"})
    assert r2.status_code == 200
    assert {e["pocket_id"] for e in r2.json()["entries"]} == {"pocket_beta"}

    # And a pocket with no writes returns nothing.
    r3 = await w1_client.get("/audit", params={"pocket_id": "ghost"})
    assert r3.status_code == 200
    assert r3.json() == {"entries": [], "total": 0}


async def test_write_for_other_workspace_is_hidden(w1_client: AsyncClient, bridged_store) -> None:
    """A write logged under workspace ``w2`` must not surface for ``w1``.

    The reader's tenancy filter rolls up via ``json_extract(context,
    '$.workspace_id')`` — proves the bridge preserves the writer's
    ``workspace_id`` under ``context.workspace_id``.
    """
    from pocketpaw_ee.cloud.pockets import action_executor

    action_executor._audit_action_run(
        actor="u_other",
        workspace_id="w2",
        pocket_id="pocket_secret",
        action="purge",
        status="success",
        base_url="https://api.example.com",
    )

    r = await w1_client.get("/audit", params={"limit": 20})
    assert r.status_code == 200
    assert r.json() == {"entries": [], "total": 0}


async def test_failure_status_also_audited(w1_client: AsyncClient, bridged_store) -> None:
    """A rejected / errored write must still be visible to the reader.

    ``_audit_action_run`` fires on EVERY attempt — success, rejection,
    timeout. The bridge maps non-standard writer ``status`` values to a
    valid ``AuditEntry.status`` and preserves the original under
    ``metadata.source_status`` so the UI can still surface the nuance.
    """
    from pocketpaw_ee.cloud.pockets import action_executor

    action_executor._audit_action_run(
        actor="u1",
        workspace_id="w1",
        pocket_id="pocket_alpha",
        action="delete_thing",
        status="rejected",  # allowlist miss
        base_url="https://api.example.com",
    )

    r = await w1_client.get("/audit", params={"pocket_id": "pocket_alpha"})
    assert r.status_code == 200
    rows = r.json()["entries"]
    assert len(rows) == 1
    assert rows[0]["status"] == "rejected"
    assert rows[0]["metadata"].get("source_status") in (None, "rejected")


# ---------------------------------------------------------------------------
# Bridge idempotency
# ---------------------------------------------------------------------------


async def test_register_audit_bridge_is_idempotent(bridged_store) -> None:
    """Calling ``register_audit_bridge`` twice must NOT double-mirror.

    Tests that re-run ``mount_cloud`` should land exactly one row per
    write — duplicates would be subtle and noisy.
    """
    from pocketpaw.security.audit import (
        AuditEvent,
        AuditSeverity,
        get_audit_logger,
    )

    # bridged_store fixture already called register_audit_bridge() once.
    # A second call must be a no-op.
    audit_listeners.register_audit_bridge()
    audit_listeners.register_audit_bridge()

    get_audit_logger().log(
        AuditEvent.create(
            severity=AuditSeverity.WARNING,
            actor="u1",
            action="pocket.actions.run",
            target="pocket_alpha",
            status="success",
            category="pocket_backend_config",
            workspace_id="w1",
            pocket_id="pocket_alpha",
            base_url="https://api.example.com",
        )
    )

    entries = await bridged_store.search_entries(workspace_id="w1", pocket_id="pocket_alpha")
    assert len(entries) == 1, f"expected exactly one mirrored row, got {len(entries)}"
