# tests/cloud/pockets/test_tool_executor.py — feat/invoke-tool-v1.
# Created: 2026-06-15 — unit coverage for the UNLOCKED `tool_executor.run_tool`.
# Updated: 2026-06-15 (v2) — the WRITE path now PROPOSES via Instinct instead of
#   refusing. The old "write → code=blocked, execute never called" test is
#   replaced by the v2 security rule: a WRITE-trust grant calls
#   `propose_external_action` (right kwargs), does NOT call
#   `connectors.service.execute` inline, and returns code="instinct_pending"
#   with a proposed action_id. (risk R1 — the human still gates the write.)
#
# These tests exercise `run_tool` directly (no FastAPI / Mongo) and spy on the
# connector service + the external-action propose helper so they pin the
# load-bearing security contract:
#
#   * THE v2 security rule — a WRITE-trust connector grant PROPOSES (Instinct)
#     and NEVER calls `connectors.service.execute` inline. (risk R1.)
#   * a READ-trust grant fires `execute` and returns its data.
#   * a tool name not on the allowlist → code="not_allowed" (fail-closed).
#   * Gate 1 (bound), Gate 2 (trust lookup), malformed-grant, and CloudError
#     mapping are all asserted.
#
# `run_tool` lazy-imports `connectors.service` inside `_run_connector_tool` and
# `propose_external_action` inside `_propose_connector_write`, so the patch
# targets are the canonical source modules the imports resolve to
# (`pocketpaw_ee.cloud.connectors.service.*`,
# `pocketpaw_ee.cloud.external_actions.propose.propose_external_action`),
# matching the MCP server's test layout.

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.connectors.domain import ConnectorActionInfo  # noqa: E402
from pocketpaw_ee.cloud.connectors.dto import ExecuteActionResponse  # noqa: E402
from pocketpaw_ee.cloud.pockets import tool_executor  # noqa: E402

WS = "ws-alpha"
POCKET = "pocket-1"
USER = "user-alice"


def _read_trust(action: str = "list_issues") -> ConnectorActionInfo:
    return ConnectorActionInfo(
        name=action,
        description="List issues",
        trust_level="auto",
        execution_mode="cloud",
        is_read=True,
    )


def _write_trust(action: str = "create_issue") -> ConnectorActionInfo:
    return ConnectorActionInfo(
        name=action,
        description="Create an issue",
        trust_level="confirm",
        execution_mode="cloud",
        is_read=False,
    )


# ---------------------------------------------------------------------------
# THE v2 security rule — a WRITE grant PROPOSES via Instinct, NEVER fires inline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_grant_proposes_via_instinct_and_never_executes_inline() -> None:
    """RISK #1 (v2) — an unapproved write must be impossible to fire inline.

    A pocket allow-lists a WRITE connector action and the button fires it. The
    executor must:
      * call `propose_external_action` with the right kwargs (the connector
        name, the action, the resolved params, the clicking user, the pocket
        scope) — filing a PENDING Instinct Action;
      * call `connectors.service.execute` ZERO times (the human gates the write,
        so it must NOT run inline);
      * return code="instinct_pending" with the proposed action_id.

    This is the load-bearing v2 invariant: because execute() is trust-agnostic,
    a WRITE that reached it inline would run WITHOUT approval. Gate 3 routes it
    to the propose-and-suspend path instead — the write only fires later, when a
    human approves and the instinct router re-enters execute (covered by the
    approve→execute integration test below + test_external_action_gate.py).
    """
    tool = "connector:github:create_issue"
    proposed_id = "act-pending-123"
    with (
        patch(
            "pocketpaw_ee.cloud.connectors.service.is_connector_bound_to_pocket",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "pocketpaw_ee.cloud.connectors.service.get_action_trust",
            new=AsyncMock(return_value=_write_trust()),
        ),
        patch(
            "pocketpaw_ee.cloud.connectors.service.execute",
            new=AsyncMock(),
        ) as mock_execute,
        patch(
            "pocketpaw_ee.cloud.external_actions.propose.propose_external_action",
            new=AsyncMock(return_value=proposed_id),
        ) as mock_propose,
    ):
        result = await tool_executor.run_tool(
            workspace_id=WS,
            pocket_id=POCKET,
            user_id=USER,
            tool=tool,
            args={"title": "needs a human to approve"},
            allowed_tools=[tool],
        )

    # THE assertion #1: the write was PROPOSED, not fired.
    mock_propose.assert_awaited_once()
    kw = mock_propose.await_args.kwargs
    assert kw["workspace_id"] == WS
    assert kw["connector_name"] == "github"
    assert kw["action"] == "create_issue"
    assert kw["params"] == {"title": "needs a human to approve"}
    assert kw["requested_by"] == USER
    assert kw["scope"] == "pocket"
    assert kw["pocket_id"] == POCKET

    # THE assertion #2: execute was NEVER awaited inline — the human gates it.
    mock_execute.assert_not_awaited()

    # THE assertion #3: the pending wire shape carries the proposed action_id.
    assert result["ok"] is True
    assert result["code"] == "instinct_pending"
    assert result["status"] == 202
    assert result["tool"] == tool
    assert result["proposed_action_id"] == proposed_id
    assert result["response"]["action_id"] == proposed_id
    assert result["response"]["status"] == "pending_approval"


@pytest.mark.asyncio
async def test_write_grant_propose_failure_maps_to_clean_wire_error() -> None:
    """If `propose_external_action` raises (store down, etc.) the executor maps
    it to a structured wire error (code="propose_failed") and STILL never fires
    execute — a propose failure must not fall through to an inline write."""
    tool = "connector:github:create_issue"
    with (
        patch(
            "pocketpaw_ee.cloud.connectors.service.is_connector_bound_to_pocket",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "pocketpaw_ee.cloud.connectors.service.get_action_trust",
            new=AsyncMock(return_value=_write_trust()),
        ),
        patch(
            "pocketpaw_ee.cloud.connectors.service.execute",
            new=AsyncMock(),
        ) as mock_execute,
        patch(
            "pocketpaw_ee.cloud.external_actions.propose.propose_external_action",
            new=AsyncMock(side_effect=RuntimeError("instinct store unavailable")),
        ),
    ):
        result = await tool_executor.run_tool(
            workspace_id=WS,
            pocket_id=POCKET,
            user_id=USER,
            tool=tool,
            args={"title": "x"},
            allowed_tools=[tool],
        )

    assert result["ok"] is False
    assert result["code"] == "propose_failed"
    mock_execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# A READ grant fires execute() and returns data
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_grant_fires_execute_and_returns_data() -> None:
    """An auto-trust (read) connector grant reaches `connectors.service.execute`
    and returns its data, mapped to the RunToolResponse wire shape."""
    tool = "connector:github:list_issues"
    exec_result = ExecuteActionResponse(
        success=True,
        data=[{"number": 1, "title": "first issue"}],
        execution_mode="cloud",
    )
    with (
        patch(
            "pocketpaw_ee.cloud.connectors.service.is_connector_bound_to_pocket",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "pocketpaw_ee.cloud.connectors.service.get_action_trust",
            new=AsyncMock(return_value=_read_trust()),
        ),
        patch(
            "pocketpaw_ee.cloud.connectors.service.execute",
            new=AsyncMock(return_value=exec_result),
        ) as mock_execute,
    ):
        result = await tool_executor.run_tool(
            workspace_id=WS,
            pocket_id=POCKET,
            user_id=USER,
            tool=tool,
            args={"owner": "acme", "repo": "api"},
            allowed_tools=[tool],
        )

    assert result["ok"] is True
    assert result["tool"] == tool
    assert result["status"] == 200
    assert result["response"][0]["title"] == "first issue"
    assert result["error"] is None
    # The execute call carried the resolved action + pocket scope — the SAME
    # ExecuteActionRequest the chat MCP `connector_execute` builds.
    mock_execute.assert_awaited_once()
    call = mock_execute.await_args
    assert call.args[0] == WS
    assert call.args[1] == "github"
    req = call.args[2]
    assert req.action == "list_issues"
    assert req.params == {"owner": "acme", "repo": "api"}
    assert req.scope == "pocket"
    assert req.pocket_id == POCKET
    assert call.kwargs["user_id"] == USER


@pytest.mark.asyncio
async def test_read_grant_failure_maps_to_502() -> None:
    """A read that runs but the connector reports failure → ok:false, status 502,
    with the connector's error surfaced (no exception bubbles)."""
    tool = "connector:github:list_issues"
    exec_result = ExecuteActionResponse(
        success=False,
        data=None,
        error="upstream 500",
        execution_mode="cloud",
    )
    with (
        patch(
            "pocketpaw_ee.cloud.connectors.service.is_connector_bound_to_pocket",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "pocketpaw_ee.cloud.connectors.service.get_action_trust",
            new=AsyncMock(return_value=_read_trust()),
        ),
        patch(
            "pocketpaw_ee.cloud.connectors.service.execute",
            new=AsyncMock(return_value=exec_result),
        ),
    ):
        result = await tool_executor.run_tool(
            workspace_id=WS,
            pocket_id=POCKET,
            user_id=USER,
            tool=tool,
            args={},
            allowed_tools=[tool],
        )

    assert result["ok"] is False
    assert result["status"] == 502
    assert result["error"] == "upstream 500"


@pytest.mark.asyncio
async def test_read_grant_cloud_error_maps_to_wire() -> None:
    """A CloudError from execute (e.g. 503 local-agent-unavailable) is caught
    and mapped to the wire shape — never raised into the route."""
    tool = "connector:github:list_issues"
    from pocketpaw_ee.cloud._core.errors import CloudError

    with (
        patch(
            "pocketpaw_ee.cloud.connectors.service.is_connector_bound_to_pocket",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "pocketpaw_ee.cloud.connectors.service.get_action_trust",
            new=AsyncMock(return_value=_read_trust()),
        ),
        patch(
            "pocketpaw_ee.cloud.connectors.service.execute",
            new=AsyncMock(
                side_effect=CloudError(503, "connector.local_agent_unavailable", "open your app")
            ),
        ),
    ):
        result = await tool_executor.run_tool(
            workspace_id=WS,
            pocket_id=POCKET,
            user_id=USER,
            tool=tool,
            args={},
            allowed_tools=[tool],
        )

    assert result["ok"] is False
    assert result["code"] == "connector.local_agent_unavailable"
    assert result["status"] == 503
    assert result["error"] == "open your app"


# ---------------------------------------------------------------------------
# Fail-closed allowlist gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_grant_returns_not_allowed_and_never_touches_connectors() -> None:
    """A tool name not on the allowlist is rejected BEFORE any connector call —
    the fail-closed gate. execute / trust / bind are never touched."""
    with (
        patch(
            "pocketpaw_ee.cloud.connectors.service.is_connector_bound_to_pocket",
            new=AsyncMock(),
        ) as mock_bound,
        patch(
            "pocketpaw_ee.cloud.connectors.service.get_action_trust",
            new=AsyncMock(),
        ) as mock_trust,
        patch(
            "pocketpaw_ee.cloud.connectors.service.execute",
            new=AsyncMock(),
        ) as mock_execute,
    ):
        result = await tool_executor.run_tool(
            workspace_id=WS,
            pocket_id=POCKET,
            user_id=USER,
            tool="connector:github:list_issues",
            args={},
            allowed_tools=[],  # empty allowlist — fail-closed
        )

    assert result["ok"] is False
    assert result["code"] == "not_allowed"
    mock_bound.assert_not_awaited()
    mock_trust.assert_not_awaited()
    mock_execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_grant_for_other_tool_does_not_authorize_this_one() -> None:
    """A grant for tool A must not authorize tool B — the gate matches the
    invoked name EXACTLY."""
    with patch(
        "pocketpaw_ee.cloud.connectors.service.execute",
        new=AsyncMock(),
    ) as mock_execute:
        result = await tool_executor.run_tool(
            workspace_id=WS,
            pocket_id=POCKET,
            user_id=USER,
            tool="connector:github:create_issue",
            args={},
            allowed_tools=["connector:github:list_issues"],
        )

    assert result["code"] == "not_allowed"
    mock_execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# Gate 1 (bind / tenancy) + Gate 2 (trust lookup) + malformed grant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unbound_connector_is_rejected_before_trust_or_execute() -> None:
    """Gate 1 — a connector not bound to THIS pocket is rejected before the
    trust lookup or execute (the tenant boundary, defense-in-depth)."""
    tool = "connector:github:list_issues"
    with (
        patch(
            "pocketpaw_ee.cloud.connectors.service.is_connector_bound_to_pocket",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "pocketpaw_ee.cloud.connectors.service.get_action_trust",
            new=AsyncMock(),
        ) as mock_trust,
        patch(
            "pocketpaw_ee.cloud.connectors.service.execute",
            new=AsyncMock(),
        ) as mock_execute,
    ):
        result = await tool_executor.run_tool(
            workspace_id=WS,
            pocket_id=POCKET,
            user_id=USER,
            tool=tool,
            args={},
            allowed_tools=[tool],
        )

    assert result["ok"] is False
    assert result["code"] == "not_reachable"
    mock_trust.assert_not_awaited()
    mock_execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_action_is_unknown_tool_without_execute() -> None:
    """Gate 2 — an allowlisted connector grant whose action the connector
    doesn't define → code="unknown_tool", execute never called."""
    tool = "connector:github:no_such_action"
    with (
        patch(
            "pocketpaw_ee.cloud.connectors.service.is_connector_bound_to_pocket",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "pocketpaw_ee.cloud.connectors.service.get_action_trust",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "pocketpaw_ee.cloud.connectors.service.execute",
            new=AsyncMock(),
        ) as mock_execute,
    ):
        result = await tool_executor.run_tool(
            workspace_id=WS,
            pocket_id=POCKET,
            user_id=USER,
            tool=tool,
            args={},
            allowed_tools=[tool],
        )

    assert result["ok"] is False
    assert result["code"] == "unknown_tool"
    mock_execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_malformed_connector_grant_is_bad_grant() -> None:
    """A grant that starts with `connector:` but lacks an action segment is a
    structured bad_grant error — not a crash, never an execute."""
    tool = "connector:github"  # missing :<action>
    with patch(
        "pocketpaw_ee.cloud.connectors.service.execute",
        new=AsyncMock(),
    ) as mock_execute:
        result = await tool_executor.run_tool(
            workspace_id=WS,
            pocket_id=POCKET,
            user_id=USER,
            tool=tool,
            args={},
            allowed_tools=[tool],
        )

    assert result["ok"] is False
    assert result["code"] == "bad_grant"
    mock_execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# Non-connector (built-in/registry) grant — not wired in v1
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_builtin_tool_grant_is_unknown_tool_in_v1() -> None:
    """A non-connector grant (e.g. a built-in `web_fetch`) is allowlisted but
    has no registry implementation in v1 → code="unknown_tool"."""
    result = await tool_executor.run_tool(
        workspace_id=WS,
        pocket_id=POCKET,
        user_id=USER,
        tool="web_fetch",
        args={"url": "https://example.com"},
        allowed_tools=["web_fetch"],
    )

    assert result["ok"] is False
    assert result["code"] == "unknown_tool"
