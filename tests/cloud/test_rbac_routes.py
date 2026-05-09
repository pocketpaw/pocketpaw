# tests/cloud/test_rbac_routes.py — HTTP-level RBAC integration tests.
# Created: 2026-05-07
#
# These tests fire real HTTP requests at the guarded cloud routers using
# FastAPI's TestClient. The goal is to catch wiring bugs — routes that
# were added without the required `dependencies=[Depends(...)]` guard.
#
# What is NOT mocked:
#   - check_workspace_action / resolve_workspace_role — these ARE the wiring
#     we are testing. They run end-to-end against the fake User injected below.
#
# What IS mocked:
#   - current_active_user — we inject FakeUser objects to control identity
#     and workspace membership without a live MongoDB/JWT stack.
#   - require_license — license validation requires env secrets; we bypass
#     it so guard tests are not coupled to the license subsystem.
#   - Service layer (Beanie calls) — for happy-path 200 tests only. The
#     service runs after the guard passes; mocking it lets us assert on the
#     HTTP status without a real database.
#
# App setup note:
#   mount_cloud() initialises the realtime bus and registers many lifecycle
#   handlers. To keep tests fast and isolated we build minimal FastAPI apps
#   that include only the router under test plus the CloudError handler.
#   These thin apps replicate the route registration without the startup
#   plumbing, which also avoids flaky tests from singleton bus state.

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from ee.cloud._core.http import add_error_handler
from ee.cloud.auth import current_active_user
from ee.cloud.license import require_license

# ---------------------------------------------------------------------------
# Fake user builders
# ---------------------------------------------------------------------------


class _FakeMembership:
    """Mimics ee.cloud.models.user.WorkspaceMembership."""

    def __init__(self, workspace: str, role: str = "member") -> None:
        self.workspace = workspace
        self.role = role


class _FakeUser:
    """Duck-typed stand-in for ee.cloud.models.user.User.

    The guards only read:
      user.id          — for audit logging
      user.workspaces  — list of _FakeMembership, to resolve role
      user.active_workspace — used by current_workspace_id dep
    """

    def __init__(
        self,
        user_id: str,
        active_workspace: str | None,
        workspaces: list[_FakeMembership] | None = None,
    ) -> None:
        self.id = user_id
        self.active_workspace = active_workspace
        self.workspaces = workspaces or []


def _member_of(workspace_id: str) -> _FakeUser:
    """A user who is a MEMBER of the given workspace."""
    return _FakeUser(
        user_id="user-member-1",
        active_workspace=workspace_id,
        workspaces=[_FakeMembership(workspace=workspace_id, role="member")],
    )


def _admin_of(workspace_id: str) -> _FakeUser:
    """A user who is an ADMIN of the given workspace."""
    return _FakeUser(
        user_id="user-admin-1",
        active_workspace=workspace_id,
        workspaces=[_FakeMembership(workspace=workspace_id, role="admin")],
    )


def _non_member() -> _FakeUser:
    """A user with no membership in any workspace."""
    return _FakeUser(
        user_id="user-stranger-1",
        active_workspace=None,
        workspaces=[],
    )


def _unauthenticated():
    """Dependency override that simulates a missing/invalid JWT token.

    This mirrors what fastapi-users does when no valid bearer token is
    present: it raises HTTP 401 Unauthorized before the route handler runs.
    """

    async def _raise():
        raise HTTPException(status_code=401, detail="Not authenticated")

    return _raise


# ---------------------------------------------------------------------------
# App factories
# ---------------------------------------------------------------------------


def _make_agents_app(user: _FakeUser | None = None) -> FastAPI:
    """Thin app with only the agents router mounted."""
    from ee.cloud.agents.router import router as agents_router

    app = FastAPI()
    add_error_handler(app)
    app.include_router(agents_router, prefix="/api/v1")
    app.dependency_overrides[require_license] = lambda: None
    if user is None:
        app.dependency_overrides[current_active_user] = _unauthenticated()
    else:
        app.dependency_overrides[current_active_user] = lambda: user
    return app


def _make_workspace_app(user: _FakeUser | None = None) -> FastAPI:
    """Thin app with only the workspace router mounted."""
    from ee.cloud.workspace.router import router as workspace_router

    app = FastAPI()
    add_error_handler(app)
    app.include_router(workspace_router, prefix="/api/v1")
    app.dependency_overrides[require_license] = lambda: None
    if user is None:
        app.dependency_overrides[current_active_user] = _unauthenticated()
    else:
        app.dependency_overrides[current_active_user] = lambda: user
    return app


def _make_kb_app(user: _FakeUser | None = None) -> FastAPI:
    """Thin app with only the KB router mounted."""
    from ee.cloud.kb.router import router as kb_router

    app = FastAPI()
    add_error_handler(app)
    app.include_router(kb_router, prefix="/api/v1")
    app.dependency_overrides[require_license] = lambda: None
    if user is None:
        app.dependency_overrides[current_active_user] = _unauthenticated()
    else:
        app.dependency_overrides[current_active_user] = lambda: user
    return app


# ---------------------------------------------------------------------------
# Section 1: Unauthenticated requests get 401
#
# Guarded routes must reject callers with no auth before any business
# logic runs. We override current_active_user to raise 401, matching the
# real fastapi-users behaviour for missing/invalid tokens.
# ---------------------------------------------------------------------------


class TestUnauthenticated:
    """No auth header → 401 on every guarded route."""

    def test_agents_create_requires_auth(self) -> None:
        """POST /api/v1/agents is guarded — no auth must return 401."""
        client = TestClient(_make_agents_app(user=None), raise_server_exceptions=False)
        resp = client.post("/api/v1/agents", json={"name": "test-agent", "backend": "native"})
        assert resp.status_code == 401

    def test_workspace_update_requires_auth(self) -> None:
        """PATCH /api/v1/workspaces/{id} is guarded — no auth must return 401."""
        client = TestClient(_make_workspace_app(user=None), raise_server_exceptions=False)
        resp = client.patch("/api/v1/workspaces/ws1", json={"name": "New Name"})
        assert resp.status_code == 401

    def test_workspace_view_requires_auth(self) -> None:
        """GET /api/v1/workspaces/{id} is guarded — no auth must return 401."""
        client = TestClient(_make_workspace_app(user=None), raise_server_exceptions=False)
        resp = client.get("/api/v1/workspaces/ws1")
        assert resp.status_code == 401

    def test_kb_search_requires_auth(self) -> None:
        """POST /api/v1/kb/search is guarded — no auth must return 401."""
        client = TestClient(_make_kb_app(user=None), raise_server_exceptions=False)
        resp = client.post("/api/v1/kb/search", json={"query": "test", "limit": 5})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Section 2: MEMBER access
#
# A workspace MEMBER can create agents (agent.create → MEMBER) and search
# the knowledge base (kb.read → MEMBER), but is blocked from workspace
# mutations that require ADMIN (workspace.update → ADMIN).
# ---------------------------------------------------------------------------


class TestMemberAccess:
    """MEMBER role — allowed on MEMBER-gated routes, denied on ADMIN-gated routes."""

    def test_member_can_create_agent(self) -> None:
        """agent.create requires MEMBER — a member should get past the guard.

        The service call is patched so we don't need a live database. The
        important assertion is that the guard does NOT raise 403 — a 200
        proves the request reached the handler.
        """
        # Patch the agents service so the handler completes without MongoDB.
        mock_agent = MagicMock()
        mock_agent.id = "agent-1"
        mock_agent.name = "my-agent"
        mock_agent.slug = "my-agent"
        mock_agent.backend = "native"
        mock_agent.description = ""
        mock_agent.system_prompt = ""
        mock_agent.avatar = ""
        mock_agent.workspace_id = "ws1"
        mock_agent.owner_id = "user-member-1"
        mock_agent.tools = []
        mock_agent.mcp_servers = []
        mock_agent.scopes = []
        mock_agent.created_at = None

        with patch("ee.cloud.agents.service.create", new=AsyncMock(return_value=mock_agent)):
            app = _make_agents_app(user=_member_of("ws1"))
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                "/api/v1/agents",
                # slug is required by CreateAgentRequest
                json={"name": "my-agent", "slug": "my-agent", "backend": "native"},
            )
        assert resp.status_code == 200

    def test_member_denied_workspace_update(self) -> None:
        """workspace.update requires ADMIN — a MEMBER must get 403."""
        app = _make_workspace_app(user=_member_of("ws1"))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.patch("/api/v1/workspaces/ws1", json={"name": "New Name"})
        assert resp.status_code == 403

    def test_member_denied_workspace_update_error_code(self) -> None:
        """The 403 response must carry the RBAC deny code, not a generic message."""
        app = _make_workspace_app(user=_member_of("ws1"))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.patch("/api/v1/workspaces/ws1", json={"name": "New Name"})
        assert resp.status_code == 403
        body = resp.json()
        # Cloud error envelope: {"error": {"code": "...", "message": "..."}}
        assert "error" in body
        assert body["error"]["code"] == "workspace.insufficient_role"

    def test_member_can_search_kb(self) -> None:
        """kb.read requires MEMBER — a member should get past the guard.

        The kb binary call is patched so the test runs without the kb Go
        binary installed.
        """
        with patch("ee.cloud.kb.router._kb", return_value=[]):
            app = _make_kb_app(user=_member_of("ws1"))
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                "/api/v1/kb/search",
                json={"query": "test query", "limit": 5},
            )
        assert resp.status_code == 200

    def test_member_denied_invite_create(self) -> None:
        """invite.create requires ADMIN — a MEMBER must get 403."""
        app = _make_workspace_app(user=_member_of("ws1"))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/v1/workspaces/ws1/invites",
            json={"email": "newuser@example.com", "role": "member"},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Section 3: ADMIN access
#
# A workspace ADMIN can perform mutations gated at ADMIN level.
# workspace.update → ADMIN, invite.create → ADMIN.
# ---------------------------------------------------------------------------


class TestAdminAccess:
    """ADMIN role — allowed on ADMIN-gated workspace mutation routes."""

    def test_admin_can_update_workspace(self) -> None:
        """workspace.update requires ADMIN — an admin gets past the guard.

        The workspace service is patched to return a minimal domain object
        so the handler completes without hitting MongoDB.
        """
        from datetime import UTC, datetime

        from ee.cloud.workspace.domain import Workspace

        fake_ws = Workspace(
            id="ws1",
            name="Updated Name",
            slug="pocketpaw",
            owner="user-admin-1",
            plan="team",
            seats=5,
            created_at=datetime.now(UTC),
        )

        with patch("ee.cloud.workspace.service.update", new=AsyncMock(return_value=fake_ws)):
            app = _make_workspace_app(user=_admin_of("ws1"))
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.patch("/api/v1/workspaces/ws1", json={"name": "Updated Name"})
        assert resp.status_code == 200

    def test_admin_can_create_invite(self) -> None:
        """invite.create requires ADMIN — an admin gets past the guard.

        The workspace service is patched to return a stub invite.
        """
        from datetime import UTC, datetime

        from ee.cloud.workspace.domain import Invite

        fake_invite = Invite(
            id="inv-1",
            workspace_id="ws1",
            email="newuser@example.com",
            role="member",
            invited_by="user-admin-1",
            token="tok-abc",
            group_id=None,
            accepted=False,
            revoked=False,
            expired=False,
            expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        )

        with patch(
            "ee.cloud.workspace.service.create_invite",
            new=AsyncMock(return_value=fake_invite),
        ):
            with patch(
                "ee.cloud.workspace.service.get",
                new=AsyncMock(),
            ):
                app = _make_workspace_app(user=_admin_of("ws1"))
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post(
                    "/api/v1/workspaces/ws1/invites",
                    json={"email": "newuser@example.com", "role": "member"},
                )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Section 4: Non-member access
#
# A user with no membership in a workspace must be rejected before any
# workspace-scoped operation runs. require_membership and require_action
# both resolve membership from the User.workspaces list; a user absent
# from that list should get 403 with code workspace.not_member.
# ---------------------------------------------------------------------------


class TestNonMemberAccess:
    """User with no membership in workspace X → 403 on workspace-scoped routes."""

    def test_non_member_denied_workspace_view(self) -> None:
        """GET /api/v1/workspaces/{id} uses require_membership — non-member
        must get 403."""
        app = _make_workspace_app(user=_non_member())
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/v1/workspaces/ws1")
        assert resp.status_code == 403

    def test_non_member_workspace_view_error_code(self) -> None:
        """The 403 for a non-member must carry workspace.not_member code."""
        app = _make_workspace_app(user=_non_member())
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/v1/workspaces/ws1")
        assert resp.status_code == 403
        body = resp.json()
        assert "error" in body
        assert body["error"]["code"] == "workspace.not_member"

    def test_non_member_denied_workspace_update(self) -> None:
        """PATCH /workspaces/{id} — non-member gets 403 (not_member before
        role check fires)."""
        app = _make_workspace_app(user=_non_member())
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.patch("/api/v1/workspaces/ws1", json={"name": "Hijack"})
        assert resp.status_code == 403

    def test_non_member_denied_workspace_in_different_ws(self) -> None:
        """User is ADMIN of ws2 but requests ws1 — must get 403."""
        user = _FakeUser(
            user_id="user-admin-ws2",
            active_workspace="ws2",
            workspaces=[_FakeMembership(workspace="ws2", role="admin")],
        )
        app = _make_workspace_app(user=user)
        client = TestClient(app, raise_server_exceptions=False)
        # Requesting ws1, but user only has membership in ws2
        resp = client.patch("/api/v1/workspaces/ws1", json={"name": "Hijack"})
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Section 5: Role boundary assertions
#
# Verify the exact threshold between allowed and denied. These tests
# directly probe the ADMIN/MEMBER boundary on the workspace.update action,
# confirming the guard isn't accidentally wired at the wrong level.
# ---------------------------------------------------------------------------


class TestRoleBoundary:
    """Confirm the exact ADMIN/MEMBER boundary on workspace.update."""

    def test_owner_can_update_workspace(self) -> None:
        """workspace.update requires ADMIN — an OWNER (level > ADMIN) must pass."""
        from datetime import UTC, datetime

        from ee.cloud.workspace.domain import Workspace

        fake_ws = Workspace(
            id="ws1",
            name="Owner Update",
            slug="owner-ws",
            owner="user-owner-1",
            plan="team",
            seats=5,
            created_at=datetime.now(UTC),
        )
        owner_user = _FakeUser(
            user_id="user-owner-1",
            active_workspace="ws1",
            workspaces=[_FakeMembership(workspace="ws1", role="owner")],
        )

        with patch("ee.cloud.workspace.service.update", new=AsyncMock(return_value=fake_ws)):
            app = _make_workspace_app(user=owner_user)
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.patch("/api/v1/workspaces/ws1", json={"name": "Owner Update"})
        assert resp.status_code == 200

    def test_member_denied_kb_write(self) -> None:
        """kb.write requires MEMBER — sanity check that a member IS allowed.

        Because kb.write and kb.read share the same minimum (MEMBER), we
        test that a MEMBER can pass the write guard too (no regression into
        accidentally requiring ADMIN for writes).
        """
        with patch("ee.cloud.kb.router._kb", return_value={"ingested": 1}):
            app = _make_kb_app(user=_member_of("ws1"))
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                "/api/v1/kb/ingest/text",
                json={"text": "hello world", "source": "test"},
            )
        # 200 means the MEMBER guard passed; the kb binary result is mocked
        assert resp.status_code == 200
