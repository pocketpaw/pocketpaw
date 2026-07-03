# tests/ee/agent/test_workspace_admin_mcp/test_mcp_tool.py — the agent-facing MCP
# surface for workspace administration (feat/workspace-admin-tools, WA-1).
#
# Created: 2026-07-03 (feat/workspace-admin-tools, WA-1).
#
# What this pins — the two WA-1 MCP tools, driven through the REAL handlers:
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
# attach_agent_identity). RBAC + the workspace service are patched so nothing
# touches Mongo — this pins the tool's gate + envelope behaviour, not the DB.

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
    assert set(wa_mcp.ADMIN_TOOL_IDS) == {
        wa_mcp.MEMBERS_LIST_TOOL_ID,
        wa_mcp.MEMBER_UPDATE_ROLE_TOOL_ID,
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
