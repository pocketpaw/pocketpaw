"""Unit tests for the agent-facing pocket helpers.

These back the in-process MCP tools (``sdk_mcp_pocket.py``) that the cloud
SSE chat agent uses to read/write the pocket it lives inside. Tests use
the ``mongo_db`` fixture (mongomock-motor) so we exercise the real
service layer end-to-end instead of mocking Beanie.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest


async def _make_pocket(**fields):
    """Insert a fresh Pocket and return it. Defaults match what the
    agent helpers exercise (workspace, owner, basic shape)."""
    from ee.cloud.models.pocket import Pocket

    base = dict(
        workspace="w1",
        name="Test Pocket",
        description="",
        type="custom",
        icon="",
        color="",
        owner="u1",
        visibility="workspace",
    )
    base.update(fields)
    doc = Pocket(**base)
    await doc.insert()
    return doc


# ---------------------------------------------------------------------------
# update_pocket_for_agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_pocket_patches_only_provided_fields(mongo_db):
    from ee.cloud.models.pocket import Pocket
    from ee.cloud.pockets.agent_context import update_pocket_for_agent

    pocket = await _make_pocket(name="Old", description="orig", color="#000")

    result = await update_pocket_for_agent(str(pocket.id), name="New Name")

    assert result["ok"] is True
    refreshed = await Pocket.get(pocket.id)
    assert refreshed.name == "New Name"
    assert refreshed.description == "orig"
    assert refreshed.color == "#000"


@pytest.mark.asyncio
async def test_update_pocket_normalizes_ripple_spec(mongo_db):
    from ee.cloud.models.pocket import Pocket
    from ee.cloud.pockets.agent_context import update_pocket_for_agent

    pocket = await _make_pocket()
    raw_spec = {"type": "flex", "props": {}, "children": []}

    result = await update_pocket_for_agent(str(pocket.id), ripple_spec=raw_spec)

    assert result["ok"] is True
    refreshed = await Pocket.get(pocket.id)
    # The normalizer wraps a raw UISpec node under ``ui`` and adds an envelope.
    assert refreshed.rippleSpec is not None
    assert refreshed.rippleSpec.get("ui", {}).get("type") == "flex"


@pytest.mark.asyncio
async def test_update_pocket_propagates_load_error(mongo_db):
    from ee.cloud.pockets.agent_context import update_pocket_for_agent

    # Valid ObjectId shape but no matching doc.
    result = await update_pocket_for_agent("507f1f77bcf86cd799439011", name="anything")
    assert result["ok"] is False
    assert "not found" in result["error"]


# ---------------------------------------------------------------------------
# add_widget_for_agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_widget_appends_with_defaults(mongo_db):
    from ee.cloud.models.pocket import Pocket
    from ee.cloud.pockets.agent_context import add_widget_for_agent

    pocket = await _make_pocket()
    spec = {"name": "Sales", "type": "metric", "data": {"value": 10}}

    result = await add_widget_for_agent(str(pocket.id), spec)

    assert result["ok"] is True
    refreshed = await Pocket.get(pocket.id)
    assert len(refreshed.widgets) == 1
    assert refreshed.widgets[0].name == "Sales"
    assert refreshed.widgets[0].type == "metric"
    assert refreshed.widgets[0].data == {"value": 10}


@pytest.mark.asyncio
async def test_add_widget_rejects_non_dict_widget(mongo_db):
    from ee.cloud.pockets.agent_context import add_widget_for_agent

    result = await add_widget_for_agent("p1", "not a dict")  # type: ignore[arg-type]
    assert result["ok"] is False
    assert "JSON object" in result["error"]


# ---------------------------------------------------------------------------
# update_widget_for_agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_widget_patches_listed_fields(mongo_db):
    from ee.cloud.models.pocket import Pocket, Widget
    from ee.cloud.pockets.agent_context import update_widget_for_agent

    widget = Widget(name="Sales", type="metric", data={"value": 10})
    pocket = await _make_pocket(widgets=[widget])

    result = await update_widget_for_agent(
        str(pocket.id), widget.id, {"name": "Revenue", "data": {"value": 20}}
    )

    assert result["ok"] is True
    refreshed = await Pocket.get(pocket.id)
    assert refreshed.widgets[0].name == "Revenue"
    assert refreshed.widgets[0].data == {"value": 20}
    # Type wasn't in the patch payload, so it stays.
    assert refreshed.widgets[0].type == "metric"


@pytest.mark.asyncio
async def test_update_widget_returns_error_when_widget_missing(mongo_db):
    from ee.cloud.pockets.agent_context import update_widget_for_agent

    pocket = await _make_pocket()

    result = await update_widget_for_agent(str(pocket.id), "missing", {"name": "x"})
    assert result["ok"] is False
    assert "missing" in result["error"]


# ---------------------------------------------------------------------------
# remove_widget_for_agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_widget_drops_matching_id(mongo_db):
    from ee.cloud.models.pocket import Pocket, Widget
    from ee.cloud.pockets.agent_context import remove_widget_for_agent

    keep = Widget(name="Keep", type="text")
    drop = Widget(name="Drop", type="text")
    pocket = await _make_pocket(widgets=[keep, drop])

    result = await remove_widget_for_agent(str(pocket.id), drop.id)

    assert result["ok"] is True
    refreshed = await Pocket.get(pocket.id)
    assert len(refreshed.widgets) == 1
    assert refreshed.widgets[0].id == keep.id


@pytest.mark.asyncio
async def test_remove_widget_errors_when_id_unknown(mongo_db):
    from ee.cloud.pockets.agent_context import remove_widget_for_agent

    pocket = await _make_pocket()

    result = await remove_widget_for_agent(str(pocket.id), "nonexistent")
    assert result["ok"] is False
    assert "nonexistent" in result["error"]


# ---------------------------------------------------------------------------
# Pocket-mutation event sink — ensure successful writes push to the SSE
# stream's queue so the frontend gets a real-time ``pocket_mutation`` event.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_pocket_pushes_mutation_when_sink_attached(mongo_db):
    from ee.cloud.chat.agent_service import (
        attach_sse_event_sink,
        detach_sse_event_sink,
    )
    from ee.cloud.pockets.agent_context import update_pocket_for_agent

    pocket = await _make_pocket(name="Old")
    queue: asyncio.Queue = asyncio.Queue()
    token = attach_sse_event_sink(queue)
    try:
        result = await update_pocket_for_agent(str(pocket.id), name="Brand New")
    finally:
        detach_sse_event_sink(token)

    assert result["ok"] is True
    assert queue.qsize() == 1
    name, payload = queue.get_nowait()
    assert name == "pocket_mutation"
    assert payload["action"] == "replace"
    assert payload["pocket_id"] == str(pocket.id)
    assert payload["pocket"]["name"] == "Brand New"


@pytest.mark.asyncio
async def test_push_is_noop_without_sink(mongo_db):
    """Calling the mutation helpers outside an SSE stream must not raise."""
    from ee.cloud.pockets.agent_context import update_pocket_for_agent

    pocket = await _make_pocket(name="Old")

    # No sink attached — should still return ok without exceptions.
    result = await update_pocket_for_agent(str(pocket.id), name="Whatever")
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_generate_session_title_writes_placeholder_then_haiku():
    """Two-stage cloud titler: instant truncated-message placeholder
    followed by a Haiku-generated title that overwrites it."""
    import asyncio

    from ee.cloud.chat.agent_router import _generate_session_title
    from ee.cloud.chat.agent_service import (
        ScopeContext,
        ScopeKind,
        attach_sse_event_sink,
        detach_sse_event_sink,
    )

    ctx = ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        session_id="websocket_abc",
    )
    user_message = "draft a Q1 plan for the marketing team and include OKRs"

    queue: asyncio.Queue = asyncio.Queue()
    token = attach_sse_event_sink(queue)

    write_calls: list[tuple[str, str]] = []

    async def _fake_write(session_id: str, title: str) -> bool:
        write_calls.append((session_id, title))
        return True

    try:
        with (
            patch(
                "ee.cloud.chat.agent_router._set_session_title_in_mongo",
                side_effect=_fake_write,
            ),
            patch(
                "pocketpaw.memory.titler.generate_title",
                AsyncMock(return_value="Q1 Marketing Plan"),
            ),
        ):
            await _generate_session_title(ctx, user_message)
    finally:
        detach_sse_event_sink(token)

    # Two SSE pushes: instant placeholder from the user's message,
    # then the Haiku-improved title.
    assert queue.qsize() == 2
    first_name, first_payload = queue.get_nowait()
    second_name, second_payload = queue.get_nowait()
    assert first_name == "session_titled"
    assert first_payload["session_id"] == "websocket_abc"
    assert first_payload["title"].startswith("draft a Q1 plan")
    assert second_name == "session_titled"
    assert second_payload == {
        "session_id": "websocket_abc",
        "title": "Q1 Marketing Plan",
    }
    # Two Mongo writes: placeholder, then the Haiku title.
    assert [t for _, t in write_calls] == [first_payload["title"], "Q1 Marketing Plan"]


@pytest.mark.asyncio
async def test_generate_session_title_uses_message_when_haiku_fails():
    """If Haiku raises or returns empty, the placeholder remains as the
    persisted title — that's the fallback the user asked for."""
    import asyncio

    from ee.cloud.chat.agent_router import _generate_session_title
    from ee.cloud.chat.agent_service import (
        ScopeContext,
        ScopeKind,
        attach_sse_event_sink,
        detach_sse_event_sink,
    )

    ctx = ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        session_id="websocket_abc",
    )

    queue: asyncio.Queue = asyncio.Queue()
    token = attach_sse_event_sink(queue)
    write_calls: list[tuple[str, str]] = []

    async def _fake_write(session_id: str, title: str) -> bool:
        write_calls.append((session_id, title))
        return True

    try:
        with (
            patch(
                "ee.cloud.chat.agent_router._set_session_title_in_mongo",
                side_effect=_fake_write,
            ),
            patch(
                "pocketpaw.memory.titler.generate_title",
                AsyncMock(side_effect=RuntimeError("haiku unavailable")),
            ),
        ):
            await _generate_session_title(ctx, "review the new analytics dashboard")
    finally:
        detach_sse_event_sink(token)

    # Only the placeholder push — Haiku failed, no overwrite.
    assert queue.qsize() == 1
    name, payload = queue.get_nowait()
    assert name == "session_titled"
    assert payload["title"] == "review the new analytics dashboard"
    # Single Mongo write for the placeholder.
    assert write_calls == [("websocket_abc", "review the new analytics dashboard")]


def test_build_context_block_pocket_mode_does_not_raise_on_format_braces():
    """Regression: literal ``{type, props, children}`` braces in the cloud
    preamble were being treated as ``str.format`` placeholders, blowing
    up every pocket-mode chat with ``KeyError: 'type, props, children'``.
    Doubled braces (``{{...}}``) keep them literal."""
    from ee.cloud.chat.agent_service import (
        ScopeContext,
        ScopeKind,
        build_context_block,
    )

    ctx = ScopeContext(
        kind=ScopeKind.POCKET,
        scope_id="p-mongo-id",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        pocket_id="p-mongo-id",
    )
    block = build_context_block(ctx)
    # The pocket-id substitution worked and the literal UISpec example
    # text survived intact.
    assert "p-mongo-id" in block
    assert "{type, props, children}" in block


def test_build_context_block_pocket_create_intent_skips_mcp_preamble():
    """When ``ctx.intent == 'pocket_create'``, the pockets-builder owns
    the turn — ``build_context_block`` MUST NOT inject the MCP cloud
    preamble or the OSS creation prompt, otherwise the agent reaches for
    tools the builder already replaced.  Only the bare scope/participants
    block is emitted."""
    from ee.cloud.chat.agent_service import (
        ScopeContext,
        ScopeKind,
        build_context_block,
    )

    ctx = ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        intent="pocket_create",
    )
    block = build_context_block(ctx)
    assert "<cloud-pocket-tools>" not in block
    assert "<scope>session s1</scope>" in block


def test_normalizer_lifts_raw_ui_node_under_ui_field():
    """The agent often passes a raw UISpec node (``{type: 'flex',
    props, children}``) instead of wrapping it under ``ui``. The
    normalizer must lift it so the frontend's UISpec renderer picks
    it up — otherwise the pocket persists with no ``ui``/``widgets``
    and the dashboard fallback shows "No widgets yet"."""
    from ee.cloud.ripple_normalizer import normalize_ripple_spec

    raw = {
        "type": "flex",
        "props": {"direction": "column", "gap": 24},
        "children": [
            {"type": "heading", "props": {"text": "Hi", "level": 1}},
            {"type": "text", "props": {"text": "world"}},
        ],
    }
    out = normalize_ripple_spec(raw)
    assert out is not None
    assert isinstance(out.get("ui"), dict)
    assert out["ui"]["type"] == "flex"
    assert len(out["ui"]["children"]) == 2
    # Envelope fields populated.
    assert out.get("version") == "1.0"
    assert out.get("lifecycle", {}).get("id")


def test_normalizer_passes_through_already_wrapped_ui():
    """A spec that's already in the ``{ui: <node>}`` shape should not
    be double-wrapped."""
    from ee.cloud.ripple_normalizer import normalize_ripple_spec

    out = normalize_ripple_spec(
        {
            "ui": {"type": "flex", "props": {}, "children": []},
            "title": "test",
        }
    )
    assert out is not None
    assert out["ui"]["type"] == "flex"
    # Envelope was added but ``ui`` wasn't nested under another ``ui``.
    assert "ui" in out["ui"] or out["ui"].get("type") == "flex"


@pytest.mark.asyncio
async def test_generate_session_title_skips_when_no_session_id():
    from ee.cloud.chat.agent_router import _generate_session_title
    from ee.cloud.chat.agent_service import ScopeContext, ScopeKind

    ctx = ScopeContext(
        kind=ScopeKind.POCKET,
        scope_id="p1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        session_id=None,
    )
    # Should return without calling the titler at all.
    with patch(
        "pocketpaw.memory.titler.generate_title", AsyncMock()
    ) as titler:
        await _generate_session_title(ctx, "anything")
    titler.assert_not_called()
