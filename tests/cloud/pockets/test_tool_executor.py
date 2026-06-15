# tests/cloud/pockets/test_tool_executor.py — feat/invoke-tool-v1.
# Created: 2026-06-15 — unit coverage for the UNLOCKED `tool_executor.run_tool`.
#
# These tests exercise `run_tool` directly (no FastAPI / Mongo) and spy on the
# connector service so they pin the load-bearing v1 security contract:
#
#   * THE security rule — a WRITE-trust connector grant NEVER calls
#     `connectors.service.execute` and returns code="blocked". (risk R1.)
#   * a READ-trust grant fires `execute` and returns its data.
#   * a tool name not on the allowlist → code="not_allowed" (fail-closed).
#   * Gate 1 (bound), Gate 2 (trust lookup), malformed-grant, and CloudError
#     mapping are all asserted.
#
# `run_tool` lazy-imports `connectors.service` inside `_run_connector_tool`, so
# the patch target is `pocketpaw_ee.cloud.connectors.service.*` (the canonical
# module object the import resolves to), matching the MCP server's test layout.

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
# THE security rule — a WRITE grant must NEVER reach execute()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_grant_never_calls_execute_and_is_blocked() -> None:
    """RISK #1 — an unapproved write must be impossible in v1.

    A pocket allow-lists a WRITE connector action and the button fires it.
    `connectors.service.execute` must be called ZERO times and the response
    must carry code="blocked". This is the load-bearing invariant: because
    execute() is trust-agnostic, a missing Gate 3 would silently run the write.
    """
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
    ):
        result = await tool_executor.run_tool(
            workspace_id=WS,
            pocket_id=POCKET,
            user_id=USER,
            tool=tool,
            args={"title": "should never be created"},
            allowed_tools=[tool],
        )

    # THE assertion: execute was never awaited.
    mock_execute.assert_not_awaited()
    assert result["ok"] is False
    assert result["code"] == "blocked"
    assert result["tool"] == tool
    assert "v2" in result["error"]
    # The success-shaped response carries the blocked marker for the
    # client's on_success handler to branch on.
    assert result["response"]["blocked"] is True
    assert result["response"]["executed"] is False


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
