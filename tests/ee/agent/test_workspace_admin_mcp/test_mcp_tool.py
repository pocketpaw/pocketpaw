# tests/ee/agent/test_workspace_admin_mcp/test_mcp_tool.py — the agent-facing MCP
# surface for workspace administration (feat/workspace-admin-tools, WA-1/2/4/5).
#
# Created: 2026-07-03 (feat/workspace-admin-tools, WA-1).
# Updated: 2026-07-03 (WA-5) — added propose+deny coverage for the seven ADMIN
#   WRITE tools (member_remove, invite_create, invite_revoke, connector_enable,
#   connector_disable, connector_config, workspace_update). Each pins: (a) an ADMIN
#   identity → a PENDING (executed=False) envelope, the STRICT proposed args are
#   right, and the underlying service is SPIED and asserted NOT called inline; (b)
#   a non-ADMIN identity → a deny envelope, propose_admin_action NOT called; plus
#   an unknown-arg-key-is-dropped test (an agent's stray key never reaches the
#   proposal). The propose→approve→executor fires-once / reject-no-fire / adapter-
#   drops-extras coverage lives in tests/cloud/test_admin_action_gate.py.
# Updated: 2026-07-03 (WA-4) — added coverage for the five READ-only tools:
#   workspace_settings_read, invites_list, connectors_list, billing_usage_read,
#   audit_read. Each has a PASS case (correct role → the service is called and its
#   data is returned) and a DENY case (insufficient role → a deny envelope with
#   ok=False/denied=True/code, and the service is SPIED and asserted NOT called).
#   READ tools EXECUTE directly on a gate pass (no Instinct proposal — that's
#   writes only). The contract test now asserts all seven tool ids.
#
# What this pins — the WA-1/WA-2 tools, driven through the REAL handlers:
#   * tool-id / server-name contract (SERVER_NAME, *_TOOL_ID, ADMIN_TOOL_IDS)
#     and the provider exposing the server + tool ids (extensions wiring).
#   * members_list (READ): a MEMBER identity → returns the roster (read allowed);
#     the workspace service is mocked. An identity that fails the RBAC gate →
#     a structured deny envelope (ok=False, denied=True, code), NOT a raised
#     exception; the service is never reached.
#   * member_update_role (WRITE): an ADMIN identity → a NON-executing envelope
#     (executed=False) and — the load-bearing assertion — the
#     ``update_member_role`` service call is spied and asserted NOT called inline
#     (an admin write is human-gated, never fired from the tool). A MEMBER →
#     deny envelope, no service call.
#   * outside-a-stream: no identity ContextVars → an explicit error envelope, no
#     service call.
#
# ``pocketpaw_ee`` is import-skipped on an OSS-only install. The handlers read
# identity through ee.cloud.chat.agent_service ContextVars (set in-test via
# attach_agent_identity). RBAC + the services are patched so nothing touches
# Mongo — this pins the tool's gate + envelope behaviour, not the DB.

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

pytest.importorskip("pocketpaw_ee")

import pocketpaw_ee.agent.mcp_servers.workspace_admin as wa_mcp  # noqa: E402
from pocketpaw_ee.cloud.chat.agent_service import (  # noqa: E402
    attach_agent_identity,
    detach_agent_identity,
)
from pocketpaw_ee.cloud.workspace.domain import WorkspaceMember  # noqa: E402
from pocketpaw_ee.extensions import CloudWorkspaceAdminMcpProvider  # noqa: E402
from pocketpaw_ee.guards.rbac import Forbidden, WorkspaceRole  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


class _identity:
    """Context manager that sets the workspace/user/pocket ContextVars the
    handler reads, then resets them."""

    def __init__(self, *, workspace="w1", user="u1", pocket=None):
        self._ws, self._user, self._pocket = workspace, user, pocket
        self._tokens = None

    def __enter__(self):
        self._tokens = attach_agent_identity(
            workspace_id=self._ws, user_id=self._user, pocket_id=self._pocket
        )
        return self

    def __exit__(self, *exc):
        detach_agent_identity(self._tokens)
        return False


def _body(res: dict) -> dict:
    """Parse the JSON body out of a success MCP response."""
    assert res.get("is_error") is not True, res
    return json.loads(res["content"][0]["text"])


def _member(user_id="u1", role="member") -> WorkspaceMember:
    return WorkspaceMember(
        user_id=user_id,
        email=f"{user_id}@example.com",
        name=user_id.title(),
        avatar="",
        role=role,
        joined_at=datetime.now(UTC),
    )


@pytest.fixture
def _patch_user(monkeypatch):
    """Stub the User doc load so the handler has a user for the RBAC check
    without touching Mongo. The object is opaque — the RBAC gate is itself
    patched per-test, so the loaded user is only a non-None sentinel."""
    monkeypatch.setattr(wa_mcp, "_load_user", _fake_load_user)


async def _fake_load_user(user_id: str):  # noqa: ANN001
    return object()  # non-None sentinel; RBAC is patched separately


# ---------------------------------------------------------------------------
# Contract / wiring
# ---------------------------------------------------------------------------


def test_server_name_and_tool_ids_contract():
    assert wa_mcp.SERVER_NAME == "pocketpaw_workspace_admin"
    assert wa_mcp.MEMBERS_LIST_TOOL_ID == "mcp__pocketpaw_workspace_admin__members_list"
    assert wa_mcp.MEMBER_UPDATE_ROLE_TOOL_ID == "mcp__pocketpaw_workspace_admin__member_update_role"
    assert (
        wa_mcp.WORKSPACE_SETTINGS_READ_TOOL_ID
        == "mcp__pocketpaw_workspace_admin__workspace_settings_read"
    )
    assert wa_mcp.INVITES_LIST_TOOL_ID == "mcp__pocketpaw_workspace_admin__invites_list"
    assert wa_mcp.CONNECTORS_LIST_TOOL_ID == "mcp__pocketpaw_workspace_admin__connectors_list"
    assert wa_mcp.BILLING_USAGE_READ_TOOL_ID == "mcp__pocketpaw_workspace_admin__billing_usage_read"
    assert wa_mcp.AUDIT_READ_TOOL_ID == "mcp__pocketpaw_workspace_admin__audit_read"
    # WA-5 write tool ids.
    assert wa_mcp.MEMBER_REMOVE_TOOL_ID == "mcp__pocketpaw_workspace_admin__member_remove"
    assert wa_mcp.INVITE_CREATE_TOOL_ID == "mcp__pocketpaw_workspace_admin__invite_create"
    assert wa_mcp.INVITE_REVOKE_TOOL_ID == "mcp__pocketpaw_workspace_admin__invite_revoke"
    assert wa_mcp.CONNECTOR_ENABLE_TOOL_ID == "mcp__pocketpaw_workspace_admin__connector_enable"
    assert wa_mcp.CONNECTOR_DISABLE_TOOL_ID == "mcp__pocketpaw_workspace_admin__connector_disable"
    assert wa_mcp.CONNECTOR_CONFIG_TOOL_ID == "mcp__pocketpaw_workspace_admin__connector_config"
    assert wa_mcp.WORKSPACE_UPDATE_TOOL_ID == "mcp__pocketpaw_workspace_admin__workspace_update"
    assert set(wa_mcp.ADMIN_TOOL_IDS) == {
        wa_mcp.MEMBERS_LIST_TOOL_ID,
        wa_mcp.MEMBER_UPDATE_ROLE_TOOL_ID,
        wa_mcp.WORKSPACE_SETTINGS_READ_TOOL_ID,
        wa_mcp.INVITES_LIST_TOOL_ID,
        wa_mcp.CONNECTORS_LIST_TOOL_ID,
        wa_mcp.BILLING_USAGE_READ_TOOL_ID,
        wa_mcp.AUDIT_READ_TOOL_ID,
        wa_mcp.MEMBER_REMOVE_TOOL_ID,
        wa_mcp.INVITE_CREATE_TOOL_ID,
        wa_mcp.INVITE_REVOKE_TOOL_ID,
        wa_mcp.CONNECTOR_ENABLE_TOOL_ID,
        wa_mcp.CONNECTOR_DISABLE_TOOL_ID,
        wa_mcp.CONNECTOR_CONFIG_TOOL_ID,
        wa_mcp.WORKSPACE_UPDATE_TOOL_ID,
    }


def test_provider_exposes_server_and_tool_ids():
    provider = CloudWorkspaceAdminMcpProvider()
    assert set(provider.tool_ids()) == set(wa_mcp.ADMIN_TOOL_IDS)
    built = provider.build_server()
    # None only when the claude_agent_sdk isn't installed; the ee test env has it.
    if built is not None:
        name, _server = built
        assert name == wa_mcp.SERVER_NAME


# ---------------------------------------------------------------------------
# members_list — READ
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_members_list_member_allowed_returns_roster(monkeypatch, _patch_user):
    """A MEMBER may read the roster — gate passes, service returns members."""
    monkeypatch.setattr(
        "pocketpaw_ee.guards.deps.check_workspace_action", lambda *a, **k: WorkspaceRole.MEMBER
    )

    called = {}

    async def _list_members(ctx, workspace_id):  # noqa: ANN001
        called["ctx_user"] = ctx.user_id
        called["ws"] = workspace_id
        return [_member("u1", "member"), _member("u2", "admin")]

    import pocketpaw_ee.cloud.workspace.service as ws_service

    monkeypatch.setattr(ws_service, "list_members", _list_members)

    with _identity(workspace="w1", user="u1"):
        res = await wa_mcp._members_list_handler({})

    body = _body(res)
    assert body["ok"] is True
    assert body["workspace_id"] == "w1"
    assert body["count"] == 2
    assert {m["role"] for m in body["members"]} == {"member", "admin"}
    assert called == {"ctx_user": "u1", "ws": "w1"}


@pytest.mark.asyncio
async def test_members_list_denied_returns_envelope_not_raised(monkeypatch, _patch_user):
    """A gate failure is CAUGHT and returned as a deny envelope, not raised —
    and the workspace service is never reached."""

    def _deny(*a, **k):  # noqa: ANN001, ANN002
        raise Forbidden(code="workspace.not_member", detail="nope")

    monkeypatch.setattr("pocketpaw_ee.guards.deps.check_workspace_action", _deny)

    service_called = {"hit": False}

    async def _list_members(ctx, workspace_id):  # noqa: ANN001
        service_called["hit"] = True
        return []

    import pocketpaw_ee.cloud.workspace.service as ws_service

    monkeypatch.setattr(ws_service, "list_members", _list_members)

    with _identity(workspace="w1", user="outsider"):
        res = await wa_mcp._members_list_handler({})

    body = _body(res)  # a SUCCESS-shaped envelope carrying the denial
    assert body["ok"] is False
    assert body["denied"] is True
    assert body["code"] == "workspace.not_member"
    assert service_called["hit"] is False  # gate blocked before the read


@pytest.mark.asyncio
async def test_members_list_outside_stream_errors():
    """No identity ContextVars → an explicit error envelope, no service call."""
    res = await wa_mcp._members_list_handler({})
    assert res.get("is_error") is True
    assert "no active workspace" in res["content"][0]["text"]


# ---------------------------------------------------------------------------
# member_update_role — WRITE (Instinct-gated; never inline)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_update_role_admin_files_proposal_not_inline(monkeypatch, _patch_user):
    """WA-2 — an ADMIN passes the gate; the tool files a live Instinct proposal
    (``propose_admin_action`` is spied) and returns a PENDING (executed=False)
    envelope carrying the action id. The ``update_member_role`` service call is
    spied and asserted NOT called inline (an admin write is human-gated)."""
    monkeypatch.setattr(
        "pocketpaw_ee.guards.deps.check_workspace_action", lambda *a, **k: WorkspaceRole.ADMIN
    )

    # Spy the propose helper (the tool imports it function-locally).
    propose_spy = {}

    async def _propose_admin_action(**kwargs):  # noqa: ANN003
        propose_spy.update(kwargs)
        return "action-abc123"

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.admin_proposals.propose.propose_admin_action",
        _propose_admin_action,
    )

    # Spy the mutation sink — it must NOT be called inline.
    mutate_spy = {"called": False}

    async def _update_member_role(*a, **k):  # noqa: ANN002, ANN003
        mutate_spy["called"] = True

    import pocketpaw_ee.cloud.workspace.service as ws_service

    monkeypatch.setattr(ws_service, "update_member_role", _update_member_role)

    with _identity(workspace="w1", user="admin1"):
        res = await wa_mcp._member_update_role_handler({"user_id": "u2", "role": "admin"})

    body = _body(res)
    assert body["ok"] is True
    assert body["executed"] is False  # the load-bearing rule
    assert body["status"] == "pending_approval"
    assert body["action_id"] == "action-abc123"
    assert body["proposed_change"] == {
        "user_id": "u2",
        "role": "admin",
        "workspace_id": "w1",
    }
    # The proposal was filed with the right shape — RBAC action key, args, and
    # the PROPOSER identity (used for the execute-time RBAC re-check).
    assert propose_spy["workspace_id"] == "w1"
    assert propose_spy["action"] == "workspace.member.role_change"
    assert propose_spy["args"] == {"target_user_id": "u2", "role": "admin"}
    assert propose_spy["proposer_user_id"] == "admin1"
    # THE assertion: the admin write did NOT fire inline.
    assert mutate_spy["called"] is False


@pytest.mark.asyncio
async def test_member_update_role_member_denied_no_service_call(monkeypatch, _patch_user):
    """A MEMBER is denied — deny envelope, no proposal, no service call."""

    def _deny(*a, **k):  # noqa: ANN001, ANN002
        raise Forbidden(code="workspace.insufficient_role", detail="need admin")

    monkeypatch.setattr("pocketpaw_ee.guards.deps.check_workspace_action", _deny)

    spy = {"called": False}

    async def _update_member_role(*a, **k):  # noqa: ANN002, ANN003
        spy["called"] = True

    import pocketpaw_ee.cloud.workspace.service as ws_service

    monkeypatch.setattr(ws_service, "update_member_role", _update_member_role)

    with _identity(workspace="w1", user="member1"):
        res = await wa_mcp._member_update_role_handler({"user_id": "u2", "role": "admin"})

    body = _body(res)
    assert body["ok"] is False
    assert body["denied"] is True
    assert body["code"] == "workspace.insufficient_role"
    assert spy["called"] is False


@pytest.mark.asyncio
async def test_member_update_role_rejects_bad_role(monkeypatch, _patch_user):
    """An out-of-set role is refused before any gate/mutation."""
    monkeypatch.setattr(
        "pocketpaw_ee.guards.deps.check_workspace_action", lambda *a, **k: WorkspaceRole.ADMIN
    )
    with _identity(workspace="w1", user="admin1"):
        res = await wa_mcp._member_update_role_handler({"user_id": "u2", "role": "superuser"})
    assert res.get("is_error") is True
    assert "role is required" in res["content"][0]["text"]


@pytest.mark.asyncio
async def test_member_update_role_outside_stream_errors():
    """No identity ContextVars → an explicit error envelope."""
    res = await wa_mcp._member_update_role_handler({"user_id": "u2", "role": "admin"})
    assert res.get("is_error") is True
    assert "no active workspace" in res["content"][0]["text"]


# ---------------------------------------------------------------------------
# WA-4 READ tools — shared helpers
# ---------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402


def _allow(role=WorkspaceRole.MEMBER):
    """Patch factory: check_workspace_action passes, returning ``role``."""
    return lambda *a, **k: role


def _deny_forbidden(code="workspace.insufficient_role", detail="nope"):
    """Patch factory: check_workspace_action raises Forbidden."""

    def _deny(*a, **k):  # noqa: ANN001, ANN002
        raise Forbidden(code=code, detail=detail)

    return _deny


# ---------------------------------------------------------------------------
# workspace_settings_read — READ (workspace.view / MEMBER)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workspace_settings_read_member_allowed(monkeypatch, _patch_user):
    """A MEMBER may read settings — gate passes, the service is called, and a
    compact view (no owner id / asset refs) comes back."""
    monkeypatch.setattr("pocketpaw_ee.guards.deps.check_workspace_action", _allow())

    called = {}

    async def _get(ctx, workspace_id):  # noqa: ANN001
        called["ctx_user"] = ctx.user_id
        called["ws"] = workspace_id
        return SimpleNamespace(
            name="Acme",
            slug="acme",
            plan="business",
            seats=10,
            member_count=3,
            branding=SimpleNamespace(
                display_name="Acme Inc",
                tab_title="Acme",
                accent_color="#123456",
                show_paw_mark=False,
                logo_asset="secret-asset-id",  # must NOT leak into the view
            ),
        )

    import pocketpaw_ee.cloud.workspace.service as ws_service

    monkeypatch.setattr(ws_service, "get", _get)

    with _identity(workspace="w1", user="u1"):
        res = await wa_mcp._workspace_settings_read_handler({})

    body = _body(res)
    assert body["ok"] is True
    assert body["name"] == "Acme"
    assert body["plan"] == "business"
    assert body["seats"] == 10
    assert body["seats_used"] == 3
    assert body["seats_available"] == 7
    assert body["branding"] == {
        "display_name": "Acme Inc",
        "tab_title": "Acme",
        "accent_color": "#123456",
        "show_paw_mark": False,
    }
    assert "logo_asset" not in body["branding"]  # asset refs are internal
    assert "owner" not in body
    assert called == {"ctx_user": "u1", "ws": "w1"}


@pytest.mark.asyncio
async def test_workspace_settings_read_denied_no_service_call(monkeypatch, _patch_user):
    """A gate failure → deny envelope (not raised); the service is NOT reached."""
    monkeypatch.setattr(
        "pocketpaw_ee.guards.deps.check_workspace_action",
        _deny_forbidden("workspace.not_member"),
    )
    hit = {"called": False}

    async def _get(ctx, workspace_id):  # noqa: ANN001
        hit["called"] = True

    import pocketpaw_ee.cloud.workspace.service as ws_service

    monkeypatch.setattr(ws_service, "get", _get)

    with _identity(workspace="w1", user="outsider"):
        res = await wa_mcp._workspace_settings_read_handler({})

    body = _body(res)
    assert body["ok"] is False
    assert body["denied"] is True
    assert body["code"] == "workspace.not_member"
    assert hit["called"] is False


# ---------------------------------------------------------------------------
# invites_list — READ (invite.create / ADMIN)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invites_list_admin_allowed(monkeypatch, _patch_user):
    """An ADMIN may read pending invites — gate passes, the service returns rows."""
    monkeypatch.setattr(
        "pocketpaw_ee.guards.deps.check_workspace_action", _allow(WorkspaceRole.ADMIN)
    )

    called = {}

    async def _list_invites(workspace_id):  # noqa: ANN001
        called["ws"] = workspace_id
        return [
            SimpleNamespace(
                id="inv1",
                email="a@example.com",
                role="member",
                invited_by="admin1",
                group_id=None,
                expires_at=datetime.now(UTC),
            )
        ]

    import pocketpaw_ee.cloud.workspace.service as ws_service

    monkeypatch.setattr(ws_service, "list_invites", _list_invites)

    with _identity(workspace="w1", user="admin1"):
        res = await wa_mcp._invites_list_handler({})

    body = _body(res)
    assert body["ok"] is True
    assert body["count"] == 1
    assert body["invites"][0]["email"] == "a@example.com"
    assert called == {"ws": "w1"}


@pytest.mark.asyncio
async def test_invites_list_member_denied_no_service_call(monkeypatch, _patch_user):
    """A MEMBER (insufficient role) → deny envelope; the service is NOT reached."""
    monkeypatch.setattr(
        "pocketpaw_ee.guards.deps.check_workspace_action",
        _deny_forbidden("workspace.insufficient_role"),
    )
    hit = {"called": False}

    async def _list_invites(workspace_id):  # noqa: ANN001
        hit["called"] = True
        return []

    import pocketpaw_ee.cloud.workspace.service as ws_service

    monkeypatch.setattr(ws_service, "list_invites", _list_invites)

    with _identity(workspace="w1", user="member1"):
        res = await wa_mcp._invites_list_handler({})

    body = _body(res)
    assert body["ok"] is False
    assert body["denied"] is True
    assert body["code"] == "workspace.insufficient_role"
    assert hit["called"] is False


# ---------------------------------------------------------------------------
# connectors_list — READ (workspace.view / MEMBER)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connectors_list_member_allowed(monkeypatch, _patch_user):
    """A MEMBER may list connectors — gate passes; the service is called with the
    caller's user_id (so its per-user permission filter runs)."""
    monkeypatch.setattr("pocketpaw_ee.guards.deps.check_workspace_action", _allow())

    called = {}

    async def _list_connectors(workspace_id, *, user_id=None):  # noqa: ANN001
        called["ws"] = workspace_id
        called["user_id"] = user_id
        return [
            SimpleNamespace(
                name="gmail",
                display_name="Gmail",
                type="oauth",
                enabled=True,
                status="connected",
                last_sync_status="ok",
                last_sync_at=datetime.now(UTC),
            )
        ]

    import pocketpaw_ee.cloud.connectors.service as conn_service

    monkeypatch.setattr(conn_service, "list_connectors", _list_connectors)

    with _identity(workspace="w1", user="u1"):
        res = await wa_mcp._connectors_list_handler({})

    body = _body(res)
    assert body["ok"] is True
    assert body["count"] == 1
    assert body["connectors"][0]["name"] == "gmail"
    assert body["connectors"][0]["status"] == "connected"
    # The caller's user_id is threaded so the service applies their permissions.
    assert called == {"ws": "w1", "user_id": "u1"}


@pytest.mark.asyncio
async def test_connectors_list_denied_no_service_call(monkeypatch, _patch_user):
    """A gate failure → deny envelope; the connectors service is NOT reached."""
    monkeypatch.setattr(
        "pocketpaw_ee.guards.deps.check_workspace_action",
        _deny_forbidden("workspace.not_member"),
    )
    hit = {"called": False}

    async def _list_connectors(workspace_id, *, user_id=None):  # noqa: ANN001
        hit["called"] = True
        return []

    import pocketpaw_ee.cloud.connectors.service as conn_service

    monkeypatch.setattr(conn_service, "list_connectors", _list_connectors)

    with _identity(workspace="w1", user="outsider"):
        res = await wa_mcp._connectors_list_handler({})

    body = _body(res)
    assert body["ok"] is False
    assert body["denied"] is True
    assert hit["called"] is False


# ---------------------------------------------------------------------------
# billing_usage_read — READ (workspace.view / MEMBER)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_billing_usage_read_member_allowed(monkeypatch, _patch_user):
    """A MEMBER may read usage — gate passes; start/end are threaded to the
    service as start_date/end_date and the summary comes back."""
    monkeypatch.setattr("pocketpaw_ee.guards.deps.check_workspace_action", _allow())

    called = {}

    async def _get_workspace_usage(workspace_id, *, start_date=None, end_date=None):  # noqa: ANN001
        called["ws"] = workspace_id
        called["start_date"] = start_date
        called["end_date"] = end_date
        return SimpleNamespace(
            start_date="2026-06-01",
            end_date="2026-06-30",
            models=["gpt-4o"],
            total_credits=42,
            buckets=[
                SimpleNamespace(
                    date="2026-06-01",
                    total_credits=42,
                    by_model={"gpt-4o": SimpleNamespace(credits=42, requests=3, tokens=0)},
                )
            ],
        )

    import pocketpaw_ee.cloud.billing.usage as usage_service

    monkeypatch.setattr(usage_service, "get_workspace_usage", _get_workspace_usage)

    with _identity(workspace="w1", user="u1"):
        res = await wa_mcp._billing_usage_read_handler({"start": "2026-06-01", "end": "2026-06-30"})

    body = _body(res)
    assert body["ok"] is True
    assert body["total_credits"] == 42
    assert body["models"] == ["gpt-4o"]
    assert body["buckets"][0]["by_model"]["gpt-4o"]["credits"] == 42
    assert called == {"ws": "w1", "start_date": "2026-06-01", "end_date": "2026-06-30"}


@pytest.mark.asyncio
async def test_billing_usage_read_denied_no_service_call(monkeypatch, _patch_user):
    """A gate failure → deny envelope; the usage service is NOT reached."""
    monkeypatch.setattr(
        "pocketpaw_ee.guards.deps.check_workspace_action",
        _deny_forbidden("workspace.not_member"),
    )
    hit = {"called": False}

    async def _get_workspace_usage(workspace_id, *, start_date=None, end_date=None):  # noqa: ANN001
        hit["called"] = True

    import pocketpaw_ee.cloud.billing.usage as usage_service

    monkeypatch.setattr(usage_service, "get_workspace_usage", _get_workspace_usage)

    with _identity(workspace="w1", user="outsider"):
        res = await wa_mcp._billing_usage_read_handler({})

    body = _body(res)
    assert body["ok"] is False
    assert body["denied"] is True
    assert hit["called"] is False


# ---------------------------------------------------------------------------
# audit_read — READ (audit.read / ADMIN)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_read_admin_allowed(monkeypatch, _patch_user):
    """An ADMIN may read the audit log — gate passes; limit is threaded into the
    AuditQueryRequest and rows come back in the wire shape."""
    monkeypatch.setattr(
        "pocketpaw_ee.guards.deps.check_workspace_action", _allow(WorkspaceRole.ADMIN)
    )

    called = {}

    async def _list_events_response(workspace_id, query):  # noqa: ANN001
        called["ws"] = workspace_id
        called["limit"] = query.limit
        return SimpleNamespace(
            items=[
                SimpleNamespace(
                    id="ev1",
                    actorId="admin1",
                    action="workspace.updated",
                    targetType="workspace",
                    targetId="w1",
                    metadata={"patched": {"name": "New"}},
                    at="2026-07-03T00:00:00Z",
                )
            ],
            nextCursor="cursor-xyz",
        )

    import pocketpaw_ee.cloud.audit.service as audit_service

    monkeypatch.setattr(audit_service, "list_events_response", _list_events_response)

    with _identity(workspace="w1", user="admin1"):
        res = await wa_mcp._audit_read_handler({"limit": 5})

    body = _body(res)
    assert body["ok"] is True
    assert body["count"] == 1
    assert body["items"][0]["action"] == "workspace.updated"
    assert body["items"][0]["actor_id"] == "admin1"
    assert body["next_cursor"] == "cursor-xyz"
    assert called == {"ws": "w1", "limit": 5}


@pytest.mark.asyncio
async def test_audit_read_member_denied_no_service_call(monkeypatch, _patch_user):
    """A MEMBER (insufficient role) → deny envelope; the audit service is NOT
    reached (fail-closed — audit is ADMIN-only)."""
    monkeypatch.setattr(
        "pocketpaw_ee.guards.deps.check_workspace_action",
        _deny_forbidden("workspace.insufficient_role"),
    )
    hit = {"called": False}

    async def _list_events_response(workspace_id, query):  # noqa: ANN001
        hit["called"] = True

    import pocketpaw_ee.cloud.audit.service as audit_service

    monkeypatch.setattr(audit_service, "list_events_response", _list_events_response)

    with _identity(workspace="w1", user="member1"):
        res = await wa_mcp._audit_read_handler({"limit": 5})

    body = _body(res)
    assert body["ok"] is False
    assert body["denied"] is True
    assert body["code"] == "workspace.insufficient_role"
    assert hit["called"] is False


@pytest.mark.asyncio
async def test_read_tools_outside_stream_error():
    """Every WA-4 READ tool refuses (error envelope) with no identity context."""
    for handler in (
        wa_mcp._workspace_settings_read_handler,
        wa_mcp._invites_list_handler,
        wa_mcp._connectors_list_handler,
        wa_mcp._billing_usage_read_handler,
        wa_mcp._audit_read_handler,
    ):
        res = await handler({})
        assert res.get("is_error") is True
        assert "no active workspace" in res["content"][0]["text"]


# ---------------------------------------------------------------------------
# WA-5 ADMIN WRITE tools — propose (never inline) + deny. Shared helpers.
# ---------------------------------------------------------------------------


@pytest.fixture
def _spy_propose(monkeypatch):
    """Spy ``propose_admin_action`` (imported function-locally by _propose_write)
    and return a fixed action id. Captures the kwargs so tests can assert the
    STRICT proposed args + proposer identity + RBAC action key."""
    captured: dict = {}

    async def _propose(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        return "action-wa5"

    monkeypatch.setattr("pocketpaw_ee.cloud.admin_proposals.propose.propose_admin_action", _propose)
    return captured


def _admin(monkeypatch):
    """check_workspace_action passes as ADMIN."""
    monkeypatch.setattr(
        "pocketpaw_ee.guards.deps.check_workspace_action", lambda *a, **k: WorkspaceRole.ADMIN
    )


def _spy_service(monkeypatch, module_path: str, fn_name: str) -> dict:
    """Spy a service function so a write tool can assert it was NOT called inline.
    Returns a dict whose ``called`` flag flips True if the service is ever hit."""
    import importlib

    mod = importlib.import_module(module_path)
    spy = {"called": False}

    async def _fn(*a, **k):  # noqa: ANN002, ANN003
        spy["called"] = True

    monkeypatch.setattr(mod, fn_name, _fn)
    return spy


# ---- member_remove --------------------------------------------------------


@pytest.mark.asyncio
async def test_member_remove_admin_proposes_not_inline(monkeypatch, _patch_user, _spy_propose):
    """ADMIN → PENDING envelope; propose filed with STRICT args (only the target
    user id); remove_member is spied and asserted NOT called inline."""
    _admin(monkeypatch)
    svc = _spy_service(monkeypatch, "pocketpaw_ee.cloud.workspace.service", "remove_member")

    with _identity(workspace="w1", user="admin1"):
        res = await wa_mcp._member_remove_handler({"user_id": "u2"})

    body = _body(res)
    assert body["ok"] is True
    assert body["executed"] is False
    assert body["status"] == "pending_approval"
    assert body["action_id"] == "action-wa5"
    assert _spy_propose["action"] == "workspace.member.remove"
    assert _spy_propose["args"] == {"target_user_id": "u2"}
    assert _spy_propose["proposer_user_id"] == "admin1"
    assert svc["called"] is False  # the write did NOT fire inline


@pytest.mark.asyncio
async def test_member_remove_member_denied_no_propose(monkeypatch, _patch_user):
    """A MEMBER → deny envelope; propose_admin_action is NOT called."""
    propose_hit = {"called": False}

    async def _propose(**kwargs):  # noqa: ANN003
        propose_hit["called"] = True
        return "x"

    monkeypatch.setattr("pocketpaw_ee.cloud.admin_proposals.propose.propose_admin_action", _propose)
    monkeypatch.setattr(
        "pocketpaw_ee.guards.deps.check_workspace_action",
        _deny_forbidden("workspace.insufficient_role"),
    )

    with _identity(workspace="w1", user="member1"):
        res = await wa_mcp._member_remove_handler({"user_id": "u2"})

    body = _body(res)
    assert body["ok"] is False
    assert body["denied"] is True
    assert body["code"] == "workspace.insufficient_role"
    assert propose_hit["called"] is False


# ---- invite_create --------------------------------------------------------


@pytest.mark.asyncio
async def test_invite_create_admin_proposes_strict_args(monkeypatch, _patch_user, _spy_propose):
    """ADMIN → PENDING; propose carries ONLY email + role; create_invite not inline."""
    _admin(monkeypatch)
    svc = _spy_service(monkeypatch, "pocketpaw_ee.cloud.workspace.service", "create_invite")

    with _identity(workspace="w1", user="admin1"):
        res = await wa_mcp._invite_create_handler({"email": "a@b.com", "role": "admin"})

    body = _body(res)
    assert body["ok"] is True
    assert body["executed"] is False
    assert _spy_propose["action"] == "invite.create"
    assert _spy_propose["args"] == {"email": "a@b.com", "role": "admin"}
    assert svc["called"] is False


@pytest.mark.asyncio
async def test_invite_create_rejects_owner_role(monkeypatch, _patch_user):
    """An invite can't mint an owner — refused before any gate/propose."""
    _admin(monkeypatch)
    with _identity(workspace="w1", user="admin1"):
        res = await wa_mcp._invite_create_handler({"email": "a@b.com", "role": "owner"})
    assert res.get("is_error") is True
    assert "role must be one of" in res["content"][0]["text"]


@pytest.mark.asyncio
async def test_invite_create_member_denied_no_propose(monkeypatch, _patch_user):
    """A MEMBER → deny envelope; no proposal."""
    propose_hit = {"called": False}

    async def _propose(**kwargs):  # noqa: ANN003
        propose_hit["called"] = True

    monkeypatch.setattr("pocketpaw_ee.cloud.admin_proposals.propose.propose_admin_action", _propose)
    monkeypatch.setattr(
        "pocketpaw_ee.guards.deps.check_workspace_action",
        _deny_forbidden("workspace.insufficient_role"),
    )
    with _identity(workspace="w1", user="member1"):
        res = await wa_mcp._invite_create_handler({"email": "a@b.com", "role": "member"})
    body = _body(res)
    assert body["ok"] is False
    assert body["denied"] is True
    assert propose_hit["called"] is False


# ---- invite_revoke --------------------------------------------------------


@pytest.mark.asyncio
async def test_invite_revoke_admin_proposes_correct_action(monkeypatch, _patch_user, _spy_propose):
    """ADMIN → PENDING; the RBAC action is ``invite.revoke`` (the revoke route's
    action, NOT invite.create); revoke_invite not inline."""
    _admin(monkeypatch)
    svc = _spy_service(monkeypatch, "pocketpaw_ee.cloud.workspace.service", "revoke_invite")

    with _identity(workspace="w1", user="admin1"):
        res = await wa_mcp._invite_revoke_handler({"invite_id": "inv1"})

    body = _body(res)
    assert body["ok"] is True
    assert body["executed"] is False
    assert _spy_propose["action"] == "invite.revoke"
    assert _spy_propose["args"] == {"invite_id": "inv1"}
    assert svc["called"] is False


@pytest.mark.asyncio
async def test_invite_revoke_member_denied_no_propose(monkeypatch, _patch_user):
    propose_hit = {"called": False}

    async def _propose(**kwargs):  # noqa: ANN003
        propose_hit["called"] = True

    monkeypatch.setattr("pocketpaw_ee.cloud.admin_proposals.propose.propose_admin_action", _propose)
    monkeypatch.setattr(
        "pocketpaw_ee.guards.deps.check_workspace_action",
        _deny_forbidden("workspace.insufficient_role"),
    )
    with _identity(workspace="w1", user="member1"):
        res = await wa_mcp._invite_revoke_handler({"invite_id": "inv1"})
    body = _body(res)
    assert body["ok"] is False
    assert body["denied"] is True
    assert propose_hit["called"] is False


# ---- connector_enable / disable / config ----------------------------------


@pytest.mark.asyncio
async def test_connector_enable_admin_proposes(monkeypatch, _patch_user, _spy_propose):
    _admin(monkeypatch)
    svc = _spy_service(monkeypatch, "pocketpaw_ee.cloud.connectors.service", "enable_connector")
    with _identity(workspace="w1", user="admin1"):
        res = await wa_mcp._connector_enable_handler({"name": "gmail"})
    body = _body(res)
    assert body["ok"] is True and body["executed"] is False
    assert _spy_propose["action"] == "connector.manage"
    assert _spy_propose["args"] == {"op": "enable", "name": "gmail"}
    assert svc["called"] is False


@pytest.mark.asyncio
async def test_connector_disable_admin_proposes(monkeypatch, _patch_user, _spy_propose):
    _admin(monkeypatch)
    svc = _spy_service(monkeypatch, "pocketpaw_ee.cloud.connectors.service", "disable_connector")
    with _identity(workspace="w1", user="admin1"):
        res = await wa_mcp._connector_disable_handler({"name": "gmail"})
    body = _body(res)
    assert body["ok"] is True and body["executed"] is False
    assert _spy_propose["action"] == "connector.manage"
    assert _spy_propose["args"] == {"op": "disable", "name": "gmail"}
    assert svc["called"] is False


@pytest.mark.asyncio
async def test_connector_config_admin_proposes_opaque_config(
    monkeypatch, _patch_user, _spy_propose
):
    """The structured config dict rides as OPAQUE data in the args — under a
    single ``config`` key, never smuggled as top-level args."""
    _admin(monkeypatch)
    svc = _spy_service(monkeypatch, "pocketpaw_ee.cloud.connectors.service", "update_config")
    with _identity(workspace="w1", user="admin1"):
        res = await wa_mcp._connector_config_handler(
            {"name": "gmail", "config": {"label": "Inbox", "max": 50}}
        )
    body = _body(res)
    assert body["ok"] is True and body["executed"] is False
    assert _spy_propose["action"] == "connector.manage"
    assert _spy_propose["args"] == {
        "op": "config",
        "name": "gmail",
        "config": {"label": "Inbox", "max": 50},
    }
    assert svc["called"] is False


@pytest.mark.asyncio
async def test_connector_config_requires_config_object(monkeypatch, _patch_user):
    """A missing / non-object config is refused before any gate/propose."""
    _admin(monkeypatch)
    with _identity(workspace="w1", user="admin1"):
        res = await wa_mcp._connector_config_handler({"name": "gmail", "config": "not-an-object"})
    assert res.get("is_error") is True
    assert "config is required" in res["content"][0]["text"]


@pytest.mark.asyncio
async def test_connector_enable_member_denied_no_propose(monkeypatch, _patch_user):
    propose_hit = {"called": False}

    async def _propose(**kwargs):  # noqa: ANN003
        propose_hit["called"] = True

    monkeypatch.setattr("pocketpaw_ee.cloud.admin_proposals.propose.propose_admin_action", _propose)
    monkeypatch.setattr(
        "pocketpaw_ee.guards.deps.check_workspace_action",
        _deny_forbidden("workspace.insufficient_role"),
    )
    with _identity(workspace="w1", user="member1"):
        res = await wa_mcp._connector_enable_handler({"name": "gmail"})
    body = _body(res)
    assert body["ok"] is False
    assert body["denied"] is True
    assert propose_hit["called"] is False


# ---- workspace_update -----------------------------------------------------


@pytest.mark.asyncio
async def test_workspace_update_admin_proposes_only_recognized_fields(
    monkeypatch, _patch_user, _spy_propose
):
    """UNKNOWN-KEY-DROPPED — an agent's stray ``seats`` / ``plan`` keys never reach
    the proposal; only name / settings / branding are proposed."""
    _admin(monkeypatch)
    svc = _spy_service(monkeypatch, "pocketpaw_ee.cloud.workspace.service", "update")
    with _identity(workspace="w1", user="admin1"):
        res = await wa_mcp._workspace_update_handler(
            {"name": "New Name", "seats": 999, "plan": "enterprise", "owner": "evil"}
        )
    body = _body(res)
    assert body["ok"] is True and body["executed"] is False
    assert _spy_propose["action"] == "workspace.update"
    # Only the recognized field survived — the stray keys were dropped.
    assert _spy_propose["args"] == {"name": "New Name"}
    assert svc["called"] is False


@pytest.mark.asyncio
async def test_workspace_update_requires_a_field(monkeypatch, _patch_user):
    """No recognized field → refused before any gate/propose."""
    _admin(monkeypatch)
    with _identity(workspace="w1", user="admin1"):
        res = await wa_mcp._workspace_update_handler({"seats": 5})
    assert res.get("is_error") is True
    assert "at least one of name / settings / branding" in res["content"][0]["text"]


@pytest.mark.asyncio
async def test_workspace_update_member_denied_no_propose(monkeypatch, _patch_user):
    propose_hit = {"called": False}

    async def _propose(**kwargs):  # noqa: ANN003
        propose_hit["called"] = True

    monkeypatch.setattr("pocketpaw_ee.cloud.admin_proposals.propose.propose_admin_action", _propose)
    monkeypatch.setattr(
        "pocketpaw_ee.guards.deps.check_workspace_action",
        _deny_forbidden("workspace.insufficient_role"),
    )
    with _identity(workspace="w1", user="member1"):
        res = await wa_mcp._workspace_update_handler({"name": "New Name"})
    body = _body(res)
    assert body["ok"] is False
    assert body["denied"] is True
    assert propose_hit["called"] is False


@pytest.mark.asyncio
async def test_write_tools_outside_stream_error():
    """Every WA-5 WRITE tool refuses (error envelope) with no identity context."""
    for handler, args in (
        (wa_mcp._member_remove_handler, {"user_id": "u2"}),
        (wa_mcp._invite_create_handler, {"email": "a@b.com"}),
        (wa_mcp._invite_revoke_handler, {"invite_id": "inv1"}),
        (wa_mcp._connector_enable_handler, {"name": "gmail"}),
        (wa_mcp._connector_disable_handler, {"name": "gmail"}),
        (wa_mcp._connector_config_handler, {"name": "gmail", "config": {}}),
        (wa_mcp._workspace_update_handler, {"name": "X"}),
    ):
        res = await handler(args)
        assert res.get("is_error") is True
        assert "no active workspace" in res["content"][0]["text"]
