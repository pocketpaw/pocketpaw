# tests/cloud/test_home_pocket_update_widget.py — Home agent's refresh path.
# Created: 2026-05-24 — coverage for the new ``update_widget`` MCP tool that
# lets the home agent overwrite an existing tile's data instead of
# delete-then-re-add. Mirrors the patterns in
# ``tests/cloud/test_home_pocket.py`` (tool-id / handler validation) and
# ``tests/cloud/test_pocket_granular_ops.py`` (``_FakeDoc`` round-trip).
#
# What's covered:
#   1. ``UPDATE_WIDGET_TOOL_ID`` is published with the expected name and
#      sits in ``POCKET_TOOL_IDS`` (allowlist regression).
#   2. ``_update_widget_handler`` rejects missing ``pocket_id`` / ``widget_id``
#      / ``fields`` args before reaching out to the store.
#   3. A widget spec carrying invented props is rejected by the same
#      manifest validator ``add_widget`` uses — nothing is persisted.
#   4. The happy-path round trip: handler updates a chart widget's
#      ``spec.props.data`` in place, the saved doc carries the new
#      series, and an SSE push fires.

from __future__ import annotations

from contextlib import ExitStack
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pocketpaw_ee.agent.mcp_servers.pockets import (
    ADD_WIDGET_TOOL_ID,
    POCKET_TOOL_IDS,
    SERVER_NAME,
    UPDATE_WIDGET_TOOL_ID,
    _update_widget_handler,
)
from pocketpaw_ee.cloud.models.pocket import Widget as _WidgetDoc

# ---------------------------------------------------------------------------
# tool id + allowlist registration
# ---------------------------------------------------------------------------


def test_update_widget_tool_id_is_published() -> None:
    """``UPDATE_WIDGET_TOOL_ID`` mirrors the SDK's ``mcp__<server>__<tool>``
    naming so a backend allowlist entry will match the tool the SDK
    surfaces at call time. It must also sit in the ``POCKET_TOOL_IDS``
    tuple so the EE extension that publishes the home-agent allowlist
    picks it up automatically."""
    assert UPDATE_WIDGET_TOOL_ID == f"mcp__{SERVER_NAME}__update_widget"
    assert UPDATE_WIDGET_TOOL_ID == "mcp__pocketpaw_pocket__update_widget"
    assert UPDATE_WIDGET_TOOL_ID in POCKET_TOOL_IDS
    # Sibling ``add_widget`` id is still there — the new tool joins the
    # allowlist, it does not replace the existing entries.
    assert ADD_WIDGET_TOOL_ID in POCKET_TOOL_IDS


# ---------------------------------------------------------------------------
# arg validation — handler short-circuits before touching the store
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_widget_handler_rejects_missing_args() -> None:
    """Each required arg is checked independently. The handler returns an
    MCP error payload and never reaches the agent_context helper — we
    assert that by patching the lazy import target with a sentinel that
    raises on call.
    """
    sentinel = AsyncMock(
        side_effect=AssertionError("update_widget_for_agent must NOT be called"),
    )
    with patch(
        "pocketpaw_ee.cloud.pockets.agent_context.update_widget_for_agent",
        new=sentinel,
    ):
        # No args at all.
        result = await _update_widget_handler({})
        assert result["is_error"] is True
        assert "pocket_id" in result["content"][0]["text"]

        # pocket_id only.
        result = await _update_widget_handler({"pocket_id": "p1"})
        assert result["is_error"] is True
        assert "widget_id" in result["content"][0]["text"]

        # pocket_id + widget_id, no fields.
        result = await _update_widget_handler(
            {"pocket_id": "p1", "widget_id": "w1"},
        )
        assert result["is_error"] is True
        assert "fields" in result["content"][0]["text"]

        # fields wrong type — must be a dict.
        result = await _update_widget_handler(
            {"pocket_id": "p1", "widget_id": "w1", "fields": "not-a-dict"},
        )
        assert result["is_error"] is True

    assert sentinel.call_count == 0


# ---------------------------------------------------------------------------
# manifest validation — invented props are rejected before the save
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_widget_handler_validates_spec() -> None:
    """A ``fields={"spec": ...}`` carrying a prop the renderer does not
    know about is rejected with the same wording ``add_widget`` uses.
    Validation runs before the agent_context helper is reached, so the
    store is never touched on rejection.
    """
    # Minimal chart manifest that only declares ``variant`` + ``data`` as
    # allowed props. ``madeUpProp`` is not in the manifest so the validator
    # surfaces it as an unknown prop the agent must remove. Real manifest
    # shape: ``widgets`` is a list of ``{type, props}`` entries where
    # ``props`` is the dict of allowed prop names → schema.
    fake_manifest: dict[str, Any] = {
        "widgets": [
            {
                "type": "chart",
                "props": {"variant": {}, "data": {}},
            },
        ],
    }
    sentinel = AsyncMock(
        side_effect=AssertionError("update_widget_for_agent must NOT be called"),
    )
    with (
        patch(
            "pocketpaw_ee.agent.mcp_servers.pockets._get_manifest_for_validation",
            new=AsyncMock(return_value=fake_manifest),
        ),
        patch(
            "pocketpaw_ee.cloud.pockets.agent_context.update_widget_for_agent",
            new=sentinel,
        ),
    ):
        result = await _update_widget_handler(
            {
                "pocket_id": "p1",
                "widget_id": "w1",
                "fields": {
                    "spec": {
                        "type": "chart",
                        "props": {"madeUpProp": "bad"},
                    },
                },
            },
        )

    assert result["is_error"] is True
    body = result["content"][0]["text"]
    # The validator's error names the offending prop so the agent can
    # re-emit a fixed spec.
    assert "madeUpProp" in body
    # And the store helper never ran — invalid specs are caught before
    # any write reaches the doc.
    assert sentinel.call_count == 0


# ---------------------------------------------------------------------------
# round-trip — handler overwrites the widget spec end-to-end
# ---------------------------------------------------------------------------


class _FakeHomePocketDoc:
    """Stand-in for a ``_PocketDoc`` whose ``widgets`` array holds a single
    chart tile. Mirrors the surface ``agent_update_widget`` reads: an
    ``id``/``workspace``/``owner`` plus a ``widgets`` list of ``_WidgetDoc``
    sub-models. ``save()`` is an async no-op that bumps a counter.
    """

    def __init__(self, pocket_id: str, widget: _WidgetDoc) -> None:
        self.id = pocket_id
        self.workspace = "w-home"
        self.name = "Home"
        self.description = ""
        self.type = "home"
        self.icon = ""
        self.color = ""
        self.owner = "u-home"
        self.visibility = "private"
        self.team: list[str] = []
        self.agents: list[str] = []
        self.widgets: list[_WidgetDoc] = [widget]
        self.rippleSpec: dict[str, Any] | None = None
        self.share_link_token: str | None = None
        self.share_link_access = "view"
        self.shared_with: list[str] = []
        self.tool_specs: list[Any] = []
        self.saves = 0

    async def save(self) -> None:
        self.saves += 1

    def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
        return {
            "_id": self.id,
            "workspace": self.workspace,
            "name": self.name,
            "description": self.description,
            "type": self.type,
            "icon": self.icon,
            "color": self.color,
            "owner": self.owner,
            "visibility": self.visibility,
            "team": list(self.team),
            "agents": list(self.agents),
            "widgets": [w.model_dump(by_alias=True) for w in self.widgets],
            "rippleSpec": self.rippleSpec,
            "share_link_token": self.share_link_token,
            "share_link_access": self.share_link_access,
            "shared_with": list(self.shared_with),
            "tool_specs": list(self.tool_specs),
        }


def _patch_store(doc: _FakeHomePocketDoc):
    """Patch the seams ``agent_update_widget`` reaches: doc fetch, emit,
    payload builder, the SSE push, the per-stream identity ContextVars,
    and the manifest fetch (so spec validation is hermetic).

    Returns ``(ExitStack, push_calls)``. Use as ``with ctx: ...``.
    """
    push_calls: list[dict[str, Any]] = []

    def _capture(payload: dict[str, Any]) -> None:
        push_calls.append(payload)

    stack = ExitStack()
    stack.enter_context(
        patch(
            "pocketpaw_ee.cloud.pockets.service._PocketDoc.get",
            new=AsyncMock(return_value=doc),
        ),
    )
    stack.enter_context(
        patch("pocketpaw_ee.cloud.pockets.service.emit", new=AsyncMock()),
    )
    stack.enter_context(
        patch(
            "pocketpaw_ee.cloud.pockets.service._pocket_event_payload",
            new=AsyncMock(return_value={"pocket_id": doc.id}),
        ),
    )
    stack.enter_context(
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.push_pocket_mutation",
            new=MagicMock(side_effect=_capture),
        ),
    )
    # Patch ``_resolved_view_for_frontend`` explicitly — otherwise the test
    # silently relies on ``_FakeHomePocketDoc.rippleSpec`` being None (which
    # short-circuits the helper before its ``ripple_resolver`` import). A
    # future test variant carrying a real rippleSpec would pull in the
    # resolver and either import-error or make a live call. See PR #1205
    # review N1.
    stack.enter_context(
        patch(
            "pocketpaw_ee.cloud.pockets.agent_context._resolved_view_for_frontend",
            new=AsyncMock(side_effect=lambda v: v),
        ),
    )
    stack.enter_context(
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id",
            new=MagicMock(return_value=doc.workspace),
        ),
    )
    stack.enter_context(
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_user_id",
            new=MagicMock(return_value=doc.owner),
        ),
    )
    # Manifest fetch returns None → spec validation no-ops. A separate
    # test pins the rejection path with a real manifest.
    stack.enter_context(
        patch(
            "pocketpaw_ee.agent.mcp_servers.pockets._get_manifest_for_validation",
            new=AsyncMock(return_value=None),
        ),
    )
    return stack, push_calls


@pytest.mark.asyncio
async def test_update_widget_handler_round_trip() -> None:
    """Set up a home pocket with one chart widget carrying a 1-point
    data series. Call the handler with a 1-point-but-different series.
    Assert the widget's ``spec.props.data`` is the new series, the doc
    was saved once, and an SSE push fired so connected frontends
    re-render the tile."""
    original_spec = {
        "type": "chart",
        "props": {
            "variant": "bar",
            "data": [{"label": "M", "value": 1}],
        },
    }
    widget = _WidgetDoc(
        name="7-day sales",
        type="chart",
        icon="trending-up",
        color="#0A84FF",
        spec=original_spec,
    )
    doc = _FakeHomePocketDoc("507f1f77bcf86cd799430022", widget)
    widget_id = widget.id

    new_spec = {
        "type": "chart",
        "props": {
            "variant": "bar",
            "data": [{"label": "Tue", "value": 99}],
        },
    }

    ctx, push_calls = _patch_store(doc)
    with ctx:
        result = await _update_widget_handler(
            {
                "pocket_id": doc.id,
                "widget_id": widget_id,
                "fields": {"spec": new_spec},
            },
        )

    # Handler did not return an error envelope.
    assert "is_error" not in result, f"handler errored: {result!r}"
    # The persisted widget carries the new series, not the original.
    assert doc.widgets[0].spec is not None
    assert doc.widgets[0].spec["props"]["data"] == [{"label": "Tue", "value": 99}]
    # And one save fired — no double-write.
    assert doc.saves == 1
    # Connected frontends got a single SSE replace-payload so they
    # re-render the tile in place.
    assert len(push_calls) == 1


@pytest.mark.asyncio
async def test_update_widget_handler_returns_unknown_widget_error() -> None:
    """A widget id the pocket does not carry surfaces a clear error from
    the service layer — the handler does not crash, it returns the MCP
    error envelope so the agent can correct itself."""
    widget = _WidgetDoc(
        name="7-day sales",
        type="chart",
        icon="trending-up",
        color="#0A84FF",
        spec={"type": "chart", "props": {"data": [{"label": "M", "value": 1}]}},
    )
    doc = _FakeHomePocketDoc("507f1f77bcf86cd799430033", widget)

    ctx, _ = _patch_store(doc)
    with ctx:
        result = await _update_widget_handler(
            {
                "pocket_id": doc.id,
                "widget_id": "not-a-real-widget-id",
                "fields": {"spec": {"type": "chart", "props": {"data": []}}},
            },
        )

    assert result["is_error"] is True
    # No save fired — unknown widget short-circuits before the doc is touched.
    assert doc.saves == 0
