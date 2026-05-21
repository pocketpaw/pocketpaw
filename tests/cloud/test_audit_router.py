# test_audit_router.py — HTTP-layer tests for ee/cloud/audit/router.py.
# Created: 2026-05-17 (Q1=B1) — Smokes the new /api/v1/audit surface:
#   tenancy isolation, query-param leak (?workspace_id=), FTS forwarding,
#   category/limit/pocket filters, response envelope parity with the
#   legacy /api/v1/runtime/audit endpoint, and the auth/permission seams.

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.audit import service as audit_service
from pocketpaw_ee.cloud.audit.router import router as audit_router
from pocketpaw_ee.cloud.auth import current_active_user
from pocketpaw_ee.cloud.license import require_license


def _fake_user(user_id: str = "u1", workspace_id: str | None = "w1") -> SimpleNamespace:
    """Lightweight User stand-in shaped like ``ee.cloud.models.user.User``.

    Only the attributes the audit-router auth chain reads are filled in
    (``id``, ``active_workspace``, ``workspaces``). RBAC is bypassed via
    a separate monkeypatch on ``check_workspace_action``.
    """
    return SimpleNamespace(
        id=user_id,
        active_workspace=workspace_id,
        workspaces=[SimpleNamespace(workspace=workspace_id, role="admin")] if workspace_id else [],
    )


def _install_service_seam(audit_store) -> None:
    """Rebind ``agent_list_audit`` so the router-injected service picks up
    the tmp store. The original is restored by ``_restore_service``."""
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


def _build_app(
    audit_store,
    workspace_id: str | None = "w1",
    user_id: str = "u1",
    *,
    skip_auth_override: bool = False,
    permission_denier: bool = False,
    monkeypatch=None,
) -> FastAPI:
    """Build a FastAPI app wired to the audit router.

    Auth: ``current_active_user`` is overridden to a ``SimpleNamespace``
    user. RBAC's ``check_workspace_action`` is patched on the platform
    guards module so the action-guard factory's deny path can be flipped
    per-test (matches the pattern in ``test_knowledge_router.py``).
    """
    app = FastAPI()
    add_error_handler(app)
    app.include_router(audit_router)
    app.dependency_overrides[require_license] = lambda: None

    if not skip_auth_override:
        user = _fake_user(user_id=user_id, workspace_id=workspace_id)

        async def _fake_user_dep():
            return user

        app.dependency_overrides[current_active_user] = _fake_user_dep

        if monkeypatch is not None:
            # ``check_workspace_action`` is imported at module load into
            # ``ee.cloud._core.deps`` (see ``from pocketpaw.ee.guards.deps
            # import check_workspace_action`` at the top of that file).
            # Patching the source module is too late — the consumer binding
            # already points at the original function. Patch the consumer
            # module's symbol directly so the guard's call site sees the
            # stub.
            from pocketpaw_ee.cloud._core import deps as core_deps
            from pocketpaw_ee.guards.rbac import Forbidden as GuardForbidden

            if permission_denier:

                def _deny(*_a, **_k):
                    raise GuardForbidden(
                        code="audit.permission_denied",
                        detail="no audit.read",
                    )

                monkeypatch.setattr(core_deps, "check_workspace_action", _deny)
            else:
                monkeypatch.setattr(core_deps, "check_workspace_action", lambda *a, **k: None)

    # Always inject the tmp store regardless of auth wiring.
    _install_service_seam(audit_store)
    return app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def w1_client(audit_store_tmp, monkeypatch) -> AsyncClient:
    app = _build_app(audit_store_tmp, workspace_id="w1", monkeypatch=monkeypatch)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        try:
            yield client
        finally:
            _restore_service()


@pytest_asyncio.fixture
async def w2_client(audit_store_tmp, monkeypatch) -> AsyncClient:
    app = _build_app(audit_store_tmp, workspace_id="w2", monkeypatch=monkeypatch)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        try:
            yield client
        finally:
            _restore_service()


# ---------------------------------------------------------------------------
# Empty + tenancy
# ---------------------------------------------------------------------------


async def test_returns_empty_for_fresh_workspace(w1_client: AsyncClient) -> None:
    r = await w1_client.get("/audit")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"entries": [], "total": 0}


async def test_returns_workspace_entries_only(w1_client: AsyncClient, make_audit_entry) -> None:
    await make_audit_entry("w1", description="w1-row")
    await make_audit_entry("w2", description="w2-row")
    r = await w1_client.get("/audit")
    assert r.status_code == 200
    descriptions = {e["description"] for e in r.json()["entries"]}
    assert descriptions == {"w1-row"}


async def test_w2_cannot_see_w1_rows(w2_client: AsyncClient, make_audit_entry) -> None:
    await make_audit_entry("w1", description="w1-secret")
    r = await w2_client.get("/audit")
    assert r.status_code == 200
    assert r.json()["entries"] == []


# ---------------------------------------------------------------------------
# Query-param leak guard
# ---------------------------------------------------------------------------


async def test_rejects_workspace_id_query_param(w1_client: AsyncClient, make_audit_entry) -> None:
    await make_audit_entry("w2", description="w2-row")
    r = await w1_client.get("/audit", params={"workspace_id": "w2"})
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "audit.workspace_id_forbidden"


# ---------------------------------------------------------------------------
# Filters forwarded to the store
# ---------------------------------------------------------------------------


async def test_q_param_forwards_to_search(w1_client: AsyncClient, make_audit_entry) -> None:
    await make_audit_entry("w1", description="apple pie")
    await make_audit_entry("w1", description="banana split")
    r = await w1_client.get("/audit", params={"q": "apple"})
    assert r.status_code == 200
    descriptions = {e["description"] for e in r.json()["entries"]}
    assert descriptions == {"apple pie"}


async def test_category_filter_forwards(w1_client: AsyncClient, make_audit_entry) -> None:
    await make_audit_entry("w1", category="security", description="s-row")
    await make_audit_entry("w1", category="decision", description="d-row")
    r = await w1_client.get("/audit", params={"category": "security"})
    assert r.status_code == 200
    cats = [e["category"] for e in r.json()["entries"]]
    assert cats == ["security"]


async def test_limit_forwards(w1_client: AsyncClient, make_audit_entry) -> None:
    for i in range(5):
        await make_audit_entry("w1", description=f"row-{i}")
    r = await w1_client.get("/audit", params={"limit": 2})
    assert r.status_code == 200
    body = r.json()
    assert len(body["entries"]) == 2
    assert body["total"] == len(body["entries"])


async def test_pocket_id_filter_forwards(w1_client: AsyncClient, make_audit_entry) -> None:
    await make_audit_entry("w1", pocket_id="p1", description="for-p1")
    await make_audit_entry("w1", pocket_id="p2", description="for-p2")
    r = await w1_client.get("/audit", params={"pocket_id": "p1"})
    assert r.status_code == 200
    pockets = {e["pocket_id"] for e in r.json()["entries"]}
    assert pockets == {"p1"}


# ---------------------------------------------------------------------------
# Envelope parity with legacy runtime audit response
# ---------------------------------------------------------------------------


def test_envelope_field_parity_with_runtime() -> None:
    """The cloud Audit envelope must be a structural superset of the
    legacy runtime envelope so the desktop client's ``mapAuditEntry``
    keeps working when the audit screen swaps over to the new
    endpoint."""
    from pocketpaw_ee.cloud.audit.dto import AuditEntryDTO, AuditListResponse

    from pocketpaw.audit.models import AuditEntry
    from pocketpaw.audit.runtime_router import RuntimeAuditResponse

    cloud = AuditListResponse(entries=[], total=0).model_dump()
    runtime = RuntimeAuditResponse(entries=[], total=0).model_dump()
    assert set(cloud.keys()) == set(runtime.keys())

    cloud_entry = AuditEntryDTO(
        id="1",
        timestamp="2026-05-17T00:00:00+00:00",
        actor="system",
        action="x",
        category="decision",
        description="d",
    ).model_dump()
    runtime_entry = AuditEntry(
        actor="system", action="x", category="decision", description="d"
    ).model_dump()
    assert set(cloud_entry.keys()) == set(runtime_entry.keys())


# ---------------------------------------------------------------------------
# Auth / permissions seams
# ---------------------------------------------------------------------------


async def test_missing_auth_returns_401(audit_store_tmp) -> None:
    """Without any ``current_active_user`` override the fastapi-users auth
    chain runs against a request with no Bearer/cookie — short-circuits
    to 401 before the handler runs."""
    app = _build_app(audit_store_tmp, workspace_id="w1", skip_auth_override=True)
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.get("/audit")
            assert r.status_code == 401
    finally:
        _restore_service()


async def test_missing_audit_read_permission_returns_403(audit_store_tmp, monkeypatch) -> None:
    app = _build_app(
        audit_store_tmp,
        workspace_id="w1",
        permission_denier=True,
        monkeypatch=monkeypatch,
    )
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.get("/audit")
            assert r.status_code == 403, r.text
            assert r.json()["error"]["code"] == "audit.permission_denied"
    finally:
        _restore_service()


async def test_ctx_without_workspace_returns_empty(audit_store_tmp, monkeypatch) -> None:
    """A user with no active workspace must not 500 and must not leak
    any rows. The workspace-scope dep guard fires first with a 400 for
    ``no active workspace``; the service-level invariant lives in the
    matching test in ``test_audit_service.py``.
    """
    # No active workspace at all — current_workspace_id (used by the
    # action guard) raises HTTPException(400) before the handler runs.
    # The service-level empty-response invariant is owned by
    # test_audit_service.py::test_ctx_without_workspace_returns_empty.
    app = _build_app(audit_store_tmp, workspace_id=None, monkeypatch=monkeypatch)
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.get("/audit")
            assert r.status_code == 400
    finally:
        _restore_service()


# Silence ruff unused-import nudge — these are used in fixture composition.
_unused: tuple[Any, ...] = ()
