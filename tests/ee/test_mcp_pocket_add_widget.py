# tests/ee/test_mcp_pocket_add_widget.py — the home agent's add_widget MCP tool.
# Created: 2026-05-22 — TDD coverage for the writable widget-mutation tool on
# the in-process ``pocketpaw_pocket`` MCP server (reachable by the
# claude_agent_sdk backend). The tool lets the home-pocket agent pin a real
# widget (chart, table, list, …) onto the home grid:
#   1. The tool id is published on ``POCKET_TOOL_IDS`` so the claude_sdk
#      allowlist auto-includes it (no callable tool without an allowlist
#      entry).
#   2. The tool accepts a chart widget with a populated ``data`` series and
#      round-trips the rippleSpec ``spec`` through ``add_widget_for_agent``.
#   3. A malformed widget spec (invented props) is rejected by manifest
#      validation — the tool returns an error so the agent can retry.
#   4. A ``type="native"`` widget skips manifest validation (no rippleSpec).

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

# Fake manifest declaring ``chart`` with the canonical ``variant``/``data``
# props. A chart with ``series``/``xAxis`` (invented) trips an unknown-prop
# warning — the same drift the pocket specialist guards against.
_FAKE_MANIFEST = {
    "schema": "ripple.manifest/v1",
    "widgets": [
        {"type": "chart", "props": {"variant": {}, "data": {}}},
        {"type": "stat", "props": {"label": {}, "value": {}}},
    ],
}


def _chart_spec() -> dict:
    return {
        "type": "chart",
        "props": {
            "variant": "bar",
            "data": [
                {"label": "Mon", "value": 1200},
                {"label": "Tue", "value": 1850},
                {"label": "Wed", "value": 1400},
                {"label": "Thu", "value": 2100},
                {"label": "Fri", "value": 2600},
                {"label": "Sat", "value": 900},
                {"label": "Sun", "value": 700},
            ],
        },
    }


def test_add_widget_tool_id_is_published_for_the_allowlist() -> None:
    """The claude_sdk backend only calls an MCP tool whose id is on the
    allowlist, and that allowlist is built from each provider's
    ``tool_ids()``. The new add_widget tool id must be on
    ``POCKET_TOOL_IDS`` so it is reachable."""
    from pocketpaw_ee.agent.mcp_servers.pockets import (
        ADD_WIDGET_TOOL_ID,
        POCKET_TOOL_IDS,
        SERVER_NAME,
    )

    assert ADD_WIDGET_TOOL_ID == f"mcp__{SERVER_NAME}__add_widget"
    assert ADD_WIDGET_TOOL_ID in POCKET_TOOL_IDS


def test_pocket_mcp_provider_advertises_add_widget_tool_id() -> None:
    """The entry-point provider's ``tool_ids()`` feeds the claude_sdk
    allowlist loop — the new id must come through it."""
    from pocketpaw_ee.agent.mcp_servers.pockets import ADD_WIDGET_TOOL_ID
    from pocketpaw_ee.extensions import CloudPocketMcpProvider

    assert ADD_WIDGET_TOOL_ID in CloudPocketMcpProvider().tool_ids()


@pytest.mark.asyncio
async def test_add_widget_handler_accepts_chart_with_data_series() -> None:
    """A chart widget with a real 7-point data series round-trips: the
    handler validates the spec, calls ``add_widget_for_agent``, and reports
    success."""
    from pocketpaw_ee.agent.mcp_servers import pockets as pockets_mcp

    captured: dict = {}

    async def _fake_add(pocket_id: str, widget: dict) -> dict:
        captured["pocket_id"] = pocket_id
        captured["widget"] = widget
        return {"ok": True, "pocket": {"_id": pocket_id, "widgets": [widget]}}

    with (
        patch.object(
            pockets_mcp,
            "_get_manifest_for_validation",
            new=AsyncMock(return_value=_FAKE_MANIFEST),
        ),
        patch(
            "pocketpaw_ee.cloud.pockets.agent_context.add_widget_for_agent",
            new=AsyncMock(side_effect=_fake_add),
        ),
    ):
        out = await pockets_mcp._add_widget_handler(
            {
                "pocket_id": "home-1",
                "widget": {
                    "name": "7-day sales",
                    "type": "chart",
                    "icon": "trending-up",
                    "spec": _chart_spec(),
                },
            }
        )

    assert not out.get("is_error"), out
    assert captured["pocket_id"] == "home-1"
    # The rippleSpec subtree is forwarded as the widget's ``spec``.
    assert captured["widget"]["spec"]["type"] == "chart"
    assert len(captured["widget"]["spec"]["props"]["data"]) == 7
    body = json.loads(out["content"][0]["text"])
    assert body["_id"] == "home-1"


@pytest.mark.asyncio
async def test_add_widget_handler_rejects_malformed_spec() -> None:
    """A chart spec with invented props (``series``/``xAxis``) fails manifest
    validation. The handler must return an error WITHOUT persisting, so the
    agent can re-emit a corrected spec."""
    from pocketpaw_ee.agent.mcp_servers import pockets as pockets_mcp

    add_mock = AsyncMock(return_value={"ok": True, "pocket": {}})
    with (
        patch.object(
            pockets_mcp,
            "_get_manifest_for_validation",
            new=AsyncMock(return_value=_FAKE_MANIFEST),
        ),
        patch(
            "pocketpaw_ee.cloud.pockets.agent_context.add_widget_for_agent",
            new=add_mock,
        ),
    ):
        out = await pockets_mcp._add_widget_handler(
            {
                "pocket_id": "home-1",
                "widget": {
                    "name": "bad chart",
                    "type": "chart",
                    "spec": {
                        "type": "chart",
                        "props": {"series": [], "xAxis": "day"},
                    },
                },
            }
        )

    assert out.get("is_error") is True
    # Nothing was persisted — validation gates the write.
    add_mock.assert_not_awaited()
    # The error names the offending props so the agent can fix them.
    text = out["content"][0]["text"]
    assert "series" in text or "xAxis" in text


@pytest.mark.asyncio
async def test_add_widget_handler_skips_validation_for_native_widget() -> None:
    """A ``type="native"`` widget has no rippleSpec — manifest validation is
    skipped and the widget persists straight through."""
    from pocketpaw_ee.agent.mcp_servers import pockets as pockets_mcp

    async def _fake_add(pocket_id: str, widget: dict) -> dict:
        return {"ok": True, "pocket": {"_id": pocket_id, "widgets": [widget]}}

    # No manifest mock — a native widget must not need it.
    with patch(
        "pocketpaw_ee.cloud.pockets.agent_context.add_widget_for_agent",
        new=AsyncMock(side_effect=_fake_add),
    ):
        out = await pockets_mcp._add_widget_handler(
            {
                "pocket_id": "home-1",
                "widget": {"name": "Mission · Tray", "type": "native", "icon": "inbox"},
            }
        )

    assert not out.get("is_error"), out


@pytest.mark.asyncio
async def test_add_widget_handler_requires_pocket_id() -> None:
    """The tool is pocket-scoped — without a ``pocket_id`` it errors instead
    of writing to an unknown surface."""
    from pocketpaw_ee.agent.mcp_servers import pockets as pockets_mcp

    out = await pockets_mcp._add_widget_handler({"widget": {"name": "x", "type": "native"}})
    assert out.get("is_error") is True
