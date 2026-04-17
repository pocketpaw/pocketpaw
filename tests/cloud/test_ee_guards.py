# Tests for ee/guards RBAC + ABAC module.
# Created: 2026-04-10

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from pocketpaw.ee.guards.abac import (
    ACTION_ROLES,
    PLAN_FEATURES,
    ROLE_TOOL_LIMITS,
    evaluate_policy,
)
from pocketpaw.ee.guards.policy import PolicyContext, PolicyResult
from pocketpaw.ee.guards.rbac import (
    Forbidden,
    PocketAccess,
    WorkspaceRole,
    check_pocket_access,
    check_workspace_role,
)

# ---------------------------------------------------------------------------
# WorkspaceRole
# ---------------------------------------------------------------------------


class TestWorkspaceRole:
    """Tests for WorkspaceRole enum and helpers."""

    def test_member_level_is_1(self):
        assert WorkspaceRole.MEMBER.level == 1

    def test_admin_level_is_2(self):
        assert WorkspaceRole.ADMIN.level == 2

    def test_owner_level_is_3(self):
        assert WorkspaceRole.OWNER.level == 3

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("member", WorkspaceRole.MEMBER),
            ("admin", WorkspaceRole.ADMIN),
            ("owner", WorkspaceRole.OWNER),
            ("ADMIN", WorkspaceRole.ADMIN),
            ("Owner", WorkspaceRole.OWNER),
        ],
    )
    def test_from_str_valid(self, value: str, expected: WorkspaceRole):
        assert WorkspaceRole.from_str(value) == expected

    def test_from_str_invalid_raises_valueerror(self):
        with pytest.raises(ValueError, match="Unknown workspace role"):
            WorkspaceRole.from_str("superadmin")

    @pytest.mark.parametrize(
        "role,str_val",
        [
            (WorkspaceRole.MEMBER, "member"),
            (WorkspaceRole.ADMIN, "admin"),
            (WorkspaceRole.OWNER, "owner"),
        ],
    )
    def test_str_value_matches_strenum(self, role: WorkspaceRole, str_val: str):
        assert str(role) == str_val
        assert role == str_val


# ---------------------------------------------------------------------------
# PocketAccess
# ---------------------------------------------------------------------------


class TestPocketAccess:
    """Tests for PocketAccess enum and helpers."""

    def test_view_level_is_1(self):
        assert PocketAccess.VIEW.level == 1

    def test_comment_level_is_2(self):
        assert PocketAccess.COMMENT.level == 2

    def test_edit_level_is_3(self):
        assert PocketAccess.EDIT.level == 3

    def test_owner_level_is_4(self):
        assert PocketAccess.OWNER.level == 4

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("view", PocketAccess.VIEW),
            ("comment", PocketAccess.COMMENT),
            ("edit", PocketAccess.EDIT),
            ("owner", PocketAccess.OWNER),
        ],
    )
    def test_from_str_valid(self, value: str, expected: PocketAccess):
        assert PocketAccess.from_str(value) == expected

    def test_from_str_invalid_raises_valueerror(self):
        with pytest.raises(ValueError, match="Unknown pocket access"):
            PocketAccess.from_str("write")


# ---------------------------------------------------------------------------
# check_workspace_role
# ---------------------------------------------------------------------------


class TestCheckWorkspaceRole:
    """Tests for the check_workspace_role guard function."""

    def test_owner_passes_admin_check(self):
        check_workspace_role(WorkspaceRole.OWNER, minimum=WorkspaceRole.ADMIN)

    def test_admin_passes_admin_check(self):
        check_workspace_role(WorkspaceRole.ADMIN, minimum=WorkspaceRole.ADMIN)

    def test_member_fails_admin_check(self):
        with pytest.raises(Forbidden) as exc_info:
            check_workspace_role(WorkspaceRole.MEMBER, minimum=WorkspaceRole.ADMIN)
        assert exc_info.value.code == "workspace.insufficient_role"

    def test_owner_passes_owner_check(self):
        check_workspace_role(WorkspaceRole.OWNER, minimum=WorkspaceRole.OWNER)

    def test_admin_fails_owner_check(self):
        with pytest.raises(Forbidden) as exc_info:
            check_workspace_role(WorkspaceRole.ADMIN, minimum=WorkspaceRole.OWNER)
        assert exc_info.value.code == "workspace.insufficient_role"

    def test_accepts_raw_string(self):
        # "admin" string should resolve and pass an admin minimum check
        check_workspace_role("admin", minimum=WorkspaceRole.ADMIN)

    def test_invalid_role_string_raises_valueerror(self):
        with pytest.raises(ValueError):
            check_workspace_role("superuser", minimum=WorkspaceRole.MEMBER)


# ---------------------------------------------------------------------------
# check_pocket_access
# ---------------------------------------------------------------------------


class TestCheckPocketAccess:
    """Tests for the check_pocket_access guard function."""

    def test_edit_passes_comment_check(self):
        check_pocket_access(PocketAccess.EDIT, minimum=PocketAccess.COMMENT)

    def test_view_fails_edit_check(self):
        with pytest.raises(Forbidden) as exc_info:
            check_pocket_access(PocketAccess.VIEW, minimum=PocketAccess.EDIT)
        assert exc_info.value.code == "pocket.insufficient_access"

    def test_owner_passes_all(self):
        for level in (PocketAccess.VIEW, PocketAccess.COMMENT, PocketAccess.EDIT):
            check_pocket_access(PocketAccess.OWNER, minimum=level)

    def test_accepts_raw_string(self):
        # Raw "edit" string should resolve and pass an edit minimum check
        check_pocket_access("edit", minimum=PocketAccess.EDIT)


# ---------------------------------------------------------------------------
# PolicyContext
# ---------------------------------------------------------------------------


class TestPolicyContext:
    """Tests for the PolicyContext dataclass."""

    def test_frozen_cannot_modify_after_creation(self):
        ctx = PolicyContext(
            user_id="u1",
            workspace_id="ws1",
            role=WorkspaceRole.MEMBER,
            action="pocket.create",
        )
        with pytest.raises((AttributeError, TypeError)):
            ctx.user_id = "u2"  # type: ignore[misc]

    def test_defaults_plan_is_team(self):
        ctx = PolicyContext(
            user_id="u1",
            workspace_id="ws1",
            role=WorkspaceRole.MEMBER,
            action="pocket.create",
        )
        assert ctx.plan == "team"

    def test_defaults_optional_fields_are_none(self):
        ctx = PolicyContext(
            user_id="u1",
            workspace_id="ws1",
            role=WorkspaceRole.MEMBER,
            action="pocket.create",
        )
        assert ctx.resource_id is None
        assert ctx.resource_type is None
        assert ctx.pocket_access is None
        assert ctx.agent_id is None
        assert ctx.agent_creator_role is None


# ---------------------------------------------------------------------------
# evaluate_policy — plan gates
# ---------------------------------------------------------------------------


class TestEvaluatePolicy:
    """Tests for the full ABAC evaluate_policy function."""

    # --- Plan feature gates ---

    def test_plan_gate_allows_team_feature_pockets(self):
        ctx = PolicyContext(
            user_id="u1",
            workspace_id="ws1",
            role=WorkspaceRole.ADMIN,
            action="pocket.create",
            plan="team",
        )
        result = evaluate_policy(ctx)
        assert result.allowed is True

    def test_plan_gate_blocks_missing_feature_automations(self):
        # "automation.*" prefix maps to the "automations" feature,
        # which requires business/enterprise plan.
        ctx = PolicyContext(
            user_id="u1",
            workspace_id="ws1",
            role=WorkspaceRole.ADMIN,
            action="automation.create",
            plan="team",
        )
        result = evaluate_policy(ctx)
        assert result.allowed is False
        assert result.code == "plan.feature_denied"

    def test_enterprise_plan_allows_all_features(self):
        enterprise_actions = ["automation.create", "audit.read", "pocket.create"]
        for action in enterprise_actions:
            ctx = PolicyContext(
                user_id="u1",
                workspace_id="ws1",
                role=WorkspaceRole.OWNER,
                action=action,
                plan="enterprise",
            )
            result = evaluate_policy(ctx)
            # plan gate should not block — role gate might still apply
            assert result.code != "plan.feature_denied", (
                f"Enterprise plan should not gate {action!r}"
            )

    # --- Role minimum for action ---

    def test_role_sufficient_for_action(self):
        # member doing pocket.create — ACTION_ROLES maps this to MEMBER
        ctx = PolicyContext(
            user_id="u1",
            workspace_id="ws1",
            role=WorkspaceRole.MEMBER,
            action="pocket.create",
            plan="team",
        )
        result = evaluate_policy(ctx)
        assert result.allowed is True

    def test_role_insufficient_for_action(self):
        # member trying workspace.delete — requires OWNER
        ctx = PolicyContext(
            user_id="u1",
            workspace_id="ws1",
            role=WorkspaceRole.MEMBER,
            action="workspace.delete",
            plan="enterprise",
        )
        result = evaluate_policy(ctx)
        assert result.allowed is False
        assert result.code == "workspace.insufficient_role"

    # --- Agent ceiling ---

    def test_agent_ceiling_blocks_escalation(self):
        # agent was created by a MEMBER, but context role is ADMIN → denied
        ctx = PolicyContext(
            user_id="u1",
            workspace_id="ws1",
            role=WorkspaceRole.ADMIN,
            action="settings.write",
            plan="enterprise",
            agent_id="agent-42",
            agent_creator_role=WorkspaceRole.MEMBER,
        )
        result = evaluate_policy(ctx)
        assert result.allowed is False
        assert result.code == "agent.ceiling_exceeded"

    def test_agent_ceiling_allows_within_bounds(self):
        # agent was created by ADMIN, context role is also ADMIN → allowed
        ctx = PolicyContext(
            user_id="u1",
            workspace_id="ws1",
            role=WorkspaceRole.ADMIN,
            action="pocket.create",
            plan="team",
            agent_id="agent-99",
            agent_creator_role=WorkspaceRole.ADMIN,
        )
        result = evaluate_policy(ctx)
        assert result.allowed is True

    # --- Unknown actions ---

    def test_unknown_action_defaults_to_member_allowed(self):
        # Action not in ACTION_ROLES — no role minimum, so any role passes
        ctx = PolicyContext(
            user_id="u1",
            workspace_id="ws1",
            role=WorkspaceRole.MEMBER,
            action="custom.unknown_action",
            plan="team",
        )
        result = evaluate_policy(ctx)
        assert result.allowed is True

    # --- Tool whitelist ---

    def test_tool_whitelist_blocks_member_shell(self):
        # Members have a restricted tool set — "shell" is not in it
        ctx = PolicyContext(
            user_id="u1",
            workspace_id="ws1",
            role=WorkspaceRole.MEMBER,
            action="tool.shell",
            plan="team",
        )
        result = evaluate_policy(ctx)
        assert result.allowed is False
        assert result.code == "agent.tool_not_allowed"

    def test_tool_whitelist_allows_member_search(self):
        # "web_search" is explicitly in MEMBER's allowed tool set
        ctx = PolicyContext(
            user_id="u1",
            workspace_id="ws1",
            role=WorkspaceRole.MEMBER,
            action="tool.web_search",
            plan="team",
        )
        result = evaluate_policy(ctx)
        assert result.allowed is True

    def test_tool_whitelist_allows_admin_anything(self):
        # ADMIN has None limit — all tools allowed
        ctx = PolicyContext(
            user_id="u1",
            workspace_id="ws1",
            role=WorkspaceRole.ADMIN,
            action="tool.shell",
            plan="team",
        )
        result = evaluate_policy(ctx)
        assert result.allowed is True


# ---------------------------------------------------------------------------
# PolicyResult
# ---------------------------------------------------------------------------


class TestPolicyResult:
    """Tests for PolicyResult dataclass defaults and immutability."""

    def test_defaults_code_and_detail_empty(self):
        result = PolicyResult(allowed=True)
        assert result.code == ""
        assert result.detail == ""

    def test_frozen_cannot_modify(self):
        result = PolicyResult(allowed=False, code="role_insufficient")
        with pytest.raises((AttributeError, TypeError)):
            result.allowed = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Static table sanity checks (regression guards)
# ---------------------------------------------------------------------------


class TestPlanFeatureTable:
    """Validate the PLAN_FEATURES table contract."""

    def test_team_has_core_four(self):
        assert {"pockets", "sessions", "agents", "memory"} <= PLAN_FEATURES["team"]

    def test_business_superset_of_team(self):
        assert PLAN_FEATURES["team"] <= PLAN_FEATURES["business"]

    def test_enterprise_superset_of_business(self):
        assert PLAN_FEATURES["business"] <= PLAN_FEATURES["enterprise"]

    def test_enterprise_has_audit_and_sso(self):
        assert "audit" in PLAN_FEATURES["enterprise"]
        assert "sso" in PLAN_FEATURES["enterprise"]


class TestActionRolesTable:
    """Validate key entries in the ACTION_ROLES table."""

    def test_billing_manage_requires_owner(self):
        assert ACTION_ROLES["billing.manage"] == WorkspaceRole.OWNER

    def test_workspace_delete_requires_owner(self):
        assert ACTION_ROLES["workspace.delete"] == WorkspaceRole.OWNER

    def test_pocket_create_requires_member(self):
        assert ACTION_ROLES["pocket.create"] == WorkspaceRole.MEMBER

    def test_settings_write_requires_admin(self):
        assert ACTION_ROLES["settings.write"] == WorkspaceRole.ADMIN


class TestRoleToolLimitsTable:
    """Validate the tool whitelist table."""

    def test_member_has_web_search(self):
        assert "web_search" in ROLE_TOOL_LIMITS[WorkspaceRole.MEMBER]

    def test_admin_has_no_limit(self):
        assert ROLE_TOOL_LIMITS[WorkspaceRole.ADMIN] is None

    def test_owner_has_no_limit(self):
        assert ROLE_TOOL_LIMITS[WorkspaceRole.OWNER] is None


# ---------------------------------------------------------------------------
# FastAPI dependency tests — require_role, require_plan_feature
#
# These tests use a minimal app that injects workspace context into
# request.state, mirroring how the real middleware would populate it.
# deps.py is built in parallel by the implementation agent — tests are
# written against the contract and will pass once the source lands.
# ---------------------------------------------------------------------------


def _inject_workspace_state(
    request: Request,
    *,
    user_id: str = "u1",
    workspace_id: str = "ws1",
    role: str = "admin",
    plan: str = "team",
) -> None:
    """Populate request.state with the fields deps.py reads.

    Must be called from a test middleware or dependency before the guard
    dependency runs.  Mirrors what AuthMiddleware would set in production.
    """
    request.state.user_context = {"user_id": user_id}
    request.state.workspace_membership = {"workspace_id": workspace_id, "role": role}
    request.state.workspace_plan = plan


def _role_check_app(
    *,
    role: str = "admin",
    plan: str = "team",
    workspace_id: str = "ws1",
    minimum_role: str = "admin",
) -> FastAPI:
    """Minimal FastAPI app wiring require_role under test conditions."""
    from pocketpaw.ee.guards.deps import require_role

    app = FastAPI()

    @app.middleware("http")
    async def inject(request: Request, call_next):
        _inject_workspace_state(request, role=role, plan=plan, workspace_id=workspace_id)
        return await call_next(request)

    @app.get(
        "/role-check",
        dependencies=[Depends(require_role(minimum_role))],
    )
    async def role_endpoint():
        return {"ok": True}

    return app


def _feature_check_app(
    *,
    plan: str = "team",
    feature: str = "automations",
    workspace_id: str = "ws1",
) -> FastAPI:
    """Minimal FastAPI app wiring require_plan_feature under test conditions."""
    from pocketpaw.ee.guards.deps import require_plan_feature

    app = FastAPI()

    @app.middleware("http")
    async def inject(request: Request, call_next):
        _inject_workspace_state(request, plan=plan, workspace_id=workspace_id)
        return await call_next(request)

    @app.get(
        "/feature-check",
        dependencies=[Depends(require_plan_feature(feature))],
    )
    async def feature_endpoint():
        return {"ok": True}

    return app


class TestRequireRoleDep:
    """Tests for the require_role FastAPI dependency."""

    def test_passes_when_role_sufficient(self):
        app = _role_check_app(role="admin", minimum_role="admin")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/role-check",
            headers={"X-Workspace-Id": "ws1"},
        )
        assert resp.status_code == 200

    def test_returns_403_when_role_insufficient(self):
        app = _role_check_app(role="member", minimum_role="admin")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/role-check",
            headers={"X-Workspace-Id": "ws1"},
        )
        assert resp.status_code == 403

    def test_owner_passes_admin_minimum(self):
        app = _role_check_app(role="owner", minimum_role="admin")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/role-check",
            headers={"X-Workspace-Id": "ws1"},
        )
        assert resp.status_code == 200

    def test_returns_401_when_no_user_context(self):
        """When middleware does not populate user_context, dep must return 401."""
        from pocketpaw.ee.guards.deps import require_role

        app = FastAPI()

        @app.get("/no-auth", dependencies=[Depends(require_role("member"))])
        async def no_auth_endpoint():
            return {"ok": True}

        # No middleware injecting state — user_context is missing
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/no-auth", headers={"X-Workspace-Id": "ws1"})
        assert resp.status_code == 401

    def test_returns_403_when_workspace_id_missing(self):
        """Missing X-Workspace-Id header or query param should return 400."""
        from pocketpaw.ee.guards.deps import require_role

        app = FastAPI()

        @app.middleware("http")
        async def inject(request: Request, call_next):
            # Inject auth but no workspace header
            request.state.user_context = {"user_id": "u1"}
            request.state.workspace_membership = {"workspace_id": "ws1", "role": "admin"}
            return await call_next(request)

        @app.get("/missing-ws", dependencies=[Depends(require_role("member"))])
        async def missing_ws_endpoint():
            return {"ok": True}

        client = TestClient(app, raise_server_exceptions=False)
        # No X-Workspace-Id header — should get 400
        resp = client.get("/missing-ws")
        assert resp.status_code == 400


class TestRequirePlanFeatureDep:
    """Tests for the require_plan_feature FastAPI dependency."""

    def test_passes_when_plan_has_feature(self):
        app = _feature_check_app(plan="business", feature="automations")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/feature-check",
            headers={"X-Workspace-Id": "ws1"},
        )
        assert resp.status_code == 200

    def test_returns_403_when_plan_lacks_feature(self):
        app = _feature_check_app(plan="team", feature="automations")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/feature-check",
            headers={"X-Workspace-Id": "ws1"},
        )
        assert resp.status_code == 403

    def test_enterprise_passes_audit_feature(self):
        app = _feature_check_app(plan="enterprise", feature="audit")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/feature-check",
            headers={"X-Workspace-Id": "ws1"},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# require_policy — full ABAC integration with agent ceiling
# ---------------------------------------------------------------------------


def _policy_check_app(
    *,
    role: str = "admin",
    plan: str = "enterprise",
    workspace_id: str = "ws1",
    action: str = "settings.write",
    agent_id: str | None = None,
    agent_creator_role: str | None = None,
) -> FastAPI:
    """Minimal app wiring require_policy with optional agent context."""
    from pocketpaw.ee.guards.deps import require_policy

    app = FastAPI()

    @app.middleware("http")
    async def inject(request: Request, call_next):
        _inject_workspace_state(request, role=role, plan=plan, workspace_id=workspace_id)
        if agent_id and agent_creator_role:
            request.state.agent_context = {
                "agent_id": agent_id,
                "creator_role": agent_creator_role,
            }
        return await call_next(request)

    @app.get("/policy-check", dependencies=[Depends(require_policy(action))])
    async def policy_endpoint():
        return {"ok": True}

    return app


class TestRequirePolicyDep:
    """Tests for the require_policy FastAPI dependency — full ABAC chain."""

    def test_passes_when_role_and_plan_sufficient(self):
        app = _policy_check_app(role="admin", plan="enterprise", action="settings.write")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/policy-check", headers={"X-Workspace-Id": "ws1"})
        assert resp.status_code == 200

    def test_blocks_plan_feature(self):
        app = _policy_check_app(role="admin", plan="team", action="automation.create")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/policy-check", headers={"X-Workspace-Id": "ws1"})
        assert resp.status_code == 403

    def test_blocks_insufficient_role(self):
        app = _policy_check_app(role="member", plan="enterprise", action="workspace.delete")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/policy-check", headers={"X-Workspace-Id": "ws1"})
        assert resp.status_code == 403

    def test_agent_ceiling_blocks_via_dep(self):
        """Agent created by member, acting as admin — must be denied."""
        app = _policy_check_app(
            role="admin",
            plan="enterprise",
            action="settings.write",
            agent_id="agent-42",
            agent_creator_role="member",
        )
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/policy-check",
            headers={"X-Workspace-Id": "ws1"},
            params={"agent_id": "agent-42"},
        )
        assert resp.status_code == 403

    def test_agent_ceiling_allows_within_bounds(self):
        """Agent created by admin, acting as admin — should pass."""
        app = _policy_check_app(
            role="admin",
            plan="enterprise",
            action="settings.write",
            agent_id="agent-99",
            agent_creator_role="admin",
        )
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/policy-check",
            headers={"X-Workspace-Id": "ws1"},
            params={"agent_id": "agent-99"},
        )
        assert resp.status_code == 200
