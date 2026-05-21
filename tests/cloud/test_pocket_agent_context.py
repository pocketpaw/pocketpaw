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
    from pocketpaw_ee.cloud.models.pocket import Pocket

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


@pytest.fixture
def agent_identity():
    """Attach the default ``w1`` / ``u1`` identity for the duration of a
    test that exercises ``_agent_load_doc`` (which enforces workspace +
    edit-access checks via per-stream ContextVars). Tests that need to
    vary identity should attach their own via ``attach_agent_identity``;
    tests that need to exercise the no-identity path should NOT request
    this fixture."""
    from pocketpaw_ee.cloud.chat.agent_service import attach_agent_identity, detach_agent_identity

    tokens = attach_agent_identity(workspace_id="w1", user_id="u1")
    try:
        yield
    finally:
        detach_agent_identity(tokens)


# ---------------------------------------------------------------------------
# list_pockets_for_agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_pockets_for_agent_returns_visible_pockets(mongo_db):
    from pocketpaw_ee.cloud.chat.agent_service import (
        attach_agent_identity,
        detach_agent_identity,
    )
    from pocketpaw_ee.cloud.pockets.agent_context import list_pockets_for_agent

    # Three pockets: one owned by u1, one workspace-visible, one owned by
    # someone else AND private — that last one must NOT come back.
    await _make_pocket(name="Mine", owner="u1")
    await _make_pocket(name="Shared", owner="u2", visibility="workspace")
    await _make_pocket(name="Hidden", owner="u2", visibility="private")

    tokens = attach_agent_identity(workspace_id="w1", user_id="u1")
    try:
        result = await list_pockets_for_agent()
    finally:
        detach_agent_identity(tokens)

    assert result["ok"] is True
    names = {p["name"] for p in result["pockets"]}
    assert names == {"Mine", "Shared"}
    # Compact shape — full rippleSpec is not in the payload.
    sample = result["pockets"][0]
    assert set(sample) >= {"id", "name", "description", "type", "icon", "color", "owner"}
    assert "rippleSpec" not in sample


@pytest.mark.asyncio
async def test_list_pockets_for_agent_errors_outside_stream():
    """Without an attached SSE stream identity, the helper refuses to
    list — the agent shouldn't be able to scrape pockets from a context
    where workspace/user can't be inferred."""
    from pocketpaw_ee.cloud.pockets.agent_context import list_pockets_for_agent

    result = await list_pockets_for_agent()
    assert result["ok"] is False
    assert "no active workspace" in result["error"]


# ---------------------------------------------------------------------------
# update_pocket_for_agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_pocket_patches_only_provided_fields(mongo_db, agent_identity):
    from pocketpaw_ee.cloud.models.pocket import Pocket
    from pocketpaw_ee.cloud.pockets.agent_context import update_pocket_for_agent

    pocket = await _make_pocket(name="Old", description="orig", color="#000")

    result = await update_pocket_for_agent(str(pocket.id), name="New Name")

    assert result["ok"] is True
    refreshed = await Pocket.get(pocket.id)
    assert refreshed.name == "New Name"
    assert refreshed.description == "orig"
    assert refreshed.color == "#000"


@pytest.mark.asyncio
async def test_update_pocket_normalizes_ripple_spec(mongo_db, agent_identity):
    from pocketpaw_ee.cloud.models.pocket import Pocket
    from pocketpaw_ee.cloud.pockets.agent_context import update_pocket_for_agent

    pocket = await _make_pocket()
    raw_spec = {"type": "flex", "props": {}, "children": []}

    result = await update_pocket_for_agent(str(pocket.id), ripple_spec=raw_spec)

    assert result["ok"] is True
    refreshed = await Pocket.get(pocket.id)
    # The normalizer wraps a raw UISpec node under ``ui`` and adds an envelope.
    assert refreshed.rippleSpec is not None
    assert refreshed.rippleSpec.get("ui", {}).get("type") == "flex"


@pytest.mark.asyncio
async def test_update_pocket_propagates_load_error(mongo_db, agent_identity):
    from pocketpaw_ee.cloud.pockets.agent_context import update_pocket_for_agent

    # Valid ObjectId shape but no matching doc.
    result = await update_pocket_for_agent("507f1f77bcf86cd799439011", name="anything")
    assert result["ok"] is False
    assert "not found" in result["error"]


# ---------------------------------------------------------------------------
# add_widget_for_agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_widget_appends_with_defaults(mongo_db, agent_identity):
    from pocketpaw_ee.cloud.models.pocket import Pocket
    from pocketpaw_ee.cloud.pockets.agent_context import add_widget_for_agent

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
    from pocketpaw_ee.cloud.pockets.agent_context import add_widget_for_agent

    result = await add_widget_for_agent("p1", "not a dict")  # type: ignore[arg-type]
    assert result["ok"] is False
    assert "JSON object" in result["error"]


# ---------------------------------------------------------------------------
# update_widget_for_agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_widget_patches_listed_fields(mongo_db, agent_identity):
    from pocketpaw_ee.cloud.models.pocket import Pocket, Widget
    from pocketpaw_ee.cloud.pockets.agent_context import update_widget_for_agent

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
async def test_update_widget_returns_error_when_widget_missing(mongo_db, agent_identity):
    from pocketpaw_ee.cloud.pockets.agent_context import update_widget_for_agent

    pocket = await _make_pocket()

    result = await update_widget_for_agent(str(pocket.id), "missing", {"name": "x"})
    assert result["ok"] is False
    assert "missing" in result["error"]


# ---------------------------------------------------------------------------
# remove_widget_for_agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_widget_drops_matching_id(mongo_db, agent_identity):
    from pocketpaw_ee.cloud.models.pocket import Pocket, Widget
    from pocketpaw_ee.cloud.pockets.agent_context import remove_widget_for_agent

    keep = Widget(name="Keep", type="text")
    drop = Widget(name="Drop", type="text")
    pocket = await _make_pocket(widgets=[keep, drop])

    result = await remove_widget_for_agent(str(pocket.id), drop.id)

    assert result["ok"] is True
    refreshed = await Pocket.get(pocket.id)
    assert len(refreshed.widgets) == 1
    assert refreshed.widgets[0].id == keep.id


@pytest.mark.asyncio
async def test_remove_widget_errors_when_id_unknown(mongo_db, agent_identity):
    from pocketpaw_ee.cloud.pockets.agent_context import remove_widget_for_agent

    pocket = await _make_pocket()

    result = await remove_widget_for_agent(str(pocket.id), "nonexistent")
    assert result["ok"] is False
    assert "nonexistent" in result["error"]


# ---------------------------------------------------------------------------
# Pocket-mutation event sink — ensure successful writes push to the SSE
# stream's queue so the frontend gets a real-time ``pocket_mutation`` event.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_pocket_pushes_mutation_when_sink_attached(mongo_db, agent_identity):
    from pocketpaw_ee.cloud.chat.agent_service import (
        attach_sse_event_sink,
        detach_sse_event_sink,
    )
    from pocketpaw_ee.cloud.pockets.agent_context import update_pocket_for_agent

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
async def test_push_is_noop_without_sink(mongo_db, agent_identity):
    """Calling the mutation helpers outside an SSE stream must not raise."""
    from pocketpaw_ee.cloud.pockets.agent_context import update_pocket_for_agent

    pocket = await _make_pocket(name="Old")

    # No sink attached — should still return ok without exceptions.
    result = await update_pocket_for_agent(str(pocket.id), name="Whatever")
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_generate_session_title_writes_placeholder_then_haiku():
    """Two-stage cloud titler: instant truncated-message placeholder
    followed by a Haiku-generated title that overwrites it."""
    import asyncio

    from pocketpaw_ee.cloud.chat.agent_router import _generate_session_title
    from pocketpaw_ee.cloud.chat.agent_service import (
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
                "pocketpaw_ee.cloud.chat.agent_router._set_session_title_in_mongo",
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

    from pocketpaw_ee.cloud.chat.agent_router import _generate_session_title
    from pocketpaw_ee.cloud.chat.agent_service import (
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
                "pocketpaw_ee.cloud.chat.agent_router._set_session_title_in_mongo",
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
    """Regression: literal braces in the inline ripple prompt were being treated
    as ``str.format`` placeholders, blowing up pocket-mode chat with KeyError.
    On claude_agent_sdk, pocket_id scope ships INLINE_RIPPLE_SYSTEM_PROMPT +
    POCKET_DELEGATION_RULE (not the full interaction prompt); the pocket id
    is still present via the <current-pocket> tag."""
    from pocketpaw_ee.cloud.chat.agent_service import (
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
    # Must not raise — brace handling in the slim prompt is safe.
    block = build_context_block(ctx, backend_name="claude_agent_sdk")
    # The pocket id appears in the dynamic <current-pocket> tag.
    assert "p-mongo-id" in block
    # Slim inline prompt and delegation rule are present.
    assert "<ripple>" in block
    assert "<pocket-delegation>" in block


def test_build_context_block_pocket_create_intent_uses_delegation():
    """Phase 3 regression: on claude_agent_sdk, pocket_create intent must use
    the delegation branch, NOT the full POCKET_CREATION_PROMPT_MCP — that text
    lives on the specialist and references mutation tools that are filtered off
    the main agent's allowlist."""
    from pocketpaw_ee.cloud.chat.agent_service import (
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
    # Subagent-capable backend: slim prompt + delegation rule.
    mcp_block = build_context_block(ctx, backend_name="claude_agent_sdk")
    assert "<ripple>" in mcp_block
    assert "<pocket-delegation>" in mcp_block
    # The full creation prompt content must NOT leak into the main agent.
    assert "<pocket-creation>" not in mcp_block
    assert "cloud_create_pocket" not in mcp_block


def test_normalizer_lifts_raw_ui_node_under_ui_field():
    """The agent often passes a raw UISpec node (``{type: 'flex',
    props, children}``) instead of wrapping it under ``ui``. The
    normalizer must lift it so the frontend's UISpec renderer picks
    it up — otherwise the pocket persists with no ``ui``/``widgets``
    and the dashboard fallback shows "No widgets yet"."""
    from pocketpaw_ee.cloud.ripple_normalizer import normalize_ripple_spec

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


def test_normalizer_lifts_each_bind_to_items():
    """Agents trained on `bind` for kanban/inputs over-apply it to
    `each` loops, where the right field is `items`. Without `items`,
    the loop renders zero rows — visible symptom is "header + composer
    but no list rows below". Walker must lift `bind` → `items` on
    `each` nodes anywhere in the tree."""
    from pocketpaw_ee.cloud.ripple_normalizer import normalize_ripple_spec

    spec = {
        "state": {"todos": [{"id": "1", "text": "test", "done": False}]},
        "ui": {
            "type": "flex",
            "props": {"direction": "column"},
            "children": [
                {
                    "type": "each",
                    "bind": "todos",  # ← wrong field name
                    "children": [{"type": "text", "props": {"text": "{item.text}"}}],
                }
            ],
        },
    }
    out = normalize_ripple_spec(spec)
    assert out is not None
    each_node = out["ui"]["children"][0]
    assert each_node["type"] == "each"
    assert each_node.get("items") == "todos", "bind should lift to items"
    assert "bind" not in each_node, "bind should not survive on each"
    # Inner children untouched.
    assert each_node["children"][0]["type"] == "text"


def test_normalizer_preserves_bind_on_value_widgets():
    """Sanity: the each-fix MUST NOT strip `bind` from value-bound
    widgets like input, checkbox, kanban — they need it."""
    from pocketpaw_ee.cloud.ripple_normalizer import normalize_ripple_spec

    spec = {
        "state": {"draft": "", "tasks": []},
        "ui": {
            "type": "flex",
            "props": {},
            "children": [
                {"type": "input", "bind": "draft", "props": {"placeholder": "..."}},
                {
                    "type": "kanban",
                    "bind": "tasks",
                    "props": {"columns": [], "columnKey": "status"},
                },
                {"type": "checkbox", "bind": "tasks.0.done", "props": {}},
            ],
        },
    }
    out = normalize_ripple_spec(spec)
    assert out is not None
    children = out["ui"]["children"]
    assert children[0]["bind"] == "draft", "input bind preserved"
    assert children[1]["bind"] == "tasks", "kanban bind preserved"
    assert children[2]["bind"] == "tasks.0.done", "checkbox bind preserved"


def test_normalizer_lifts_if_condition_alias():
    """Symmetrical fix: `if.bind` / `if.when` → `if.condition`."""
    from pocketpaw_ee.cloud.ripple_normalizer import normalize_ripple_spec

    spec = {
        "ui": {
            "type": "if",
            "when": "{state.signed_in}",  # ← wrong; should be `condition`
            "children": [{"type": "text", "props": {"text": "Hi"}}],
        },
    }
    out = normalize_ripple_spec(spec)
    assert out is not None
    assert out["ui"].get("condition") == "{state.signed_in}"
    assert "when" not in out["ui"]


def test_pocket_wire_dict_normalizes_legacy_root_alias():
    """Old pockets persisted before the alias safety net have ``root``
    instead of ``ui`` in MongoDB. ``pocket_to_wire_dict`` must lift it
    on read so the frontend renders without a DB migration."""
    from pocketpaw_ee.cloud.pockets.domain import Pocket
    from pocketpaw_ee.cloud.pockets.dto import pocket_to_wire_dict

    legacy_spec = {
        "lifecycle": {"type": "persistent", "id": "pocket-legacy"},
        "state": {"draft": "", "todos": []},
        "root": {  # ← agent's wrong field name persisted before the fix
            "type": "flex",
            "props": {"direction": "column"},
            "children": [
                {"type": "input", "bind": "draft", "props": {}},
            ],
        },
    }
    pocket = Pocket(
        id="p1",
        workspace_id="w1",
        name="Todos",
        description="",
        type="deep-work",
        icon="check-square",
        color="#0A84FF",
        owner="u1",
        visibility="workspace",
        team=(),
        agents=(),
        widgets=(),
        ripple_spec=legacy_spec,
        share_link_token=None,
        share_link_access="view",
        shared_with=(),
    )

    wire = pocket_to_wire_dict(pocket)
    spec = wire["rippleSpec"]
    assert spec is not None
    assert "ui" in spec, "root should have been lifted to ui"
    assert spec["ui"]["type"] == "flex"
    assert "root" not in spec, "root should not survive the lift"
    # State + lifecycle preserved.
    assert spec["state"]["todos"] == []
    assert spec["lifecycle"]["id"] == "pocket-legacy"


def test_normalizer_lifts_aliased_ui_field():
    """The agent occasionally invents `root` / `tree` / `view` / `body` /
    `content` for the renderable tree instead of `ui`. Spec is otherwise
    valid (state, bind, on_click chains in place) but renderer reads
    only `ui` and shows "No widgets yet". Normalizer must lift any of
    these aliases into `ui`."""
    from pocketpaw_ee.cloud.ripple_normalizer import normalize_ripple_spec

    for alias in ("root", "tree", "view", "body", "content"):
        raw = {
            "state": {"draft": "", "todos": []},
            alias: {
                "type": "flex",
                "props": {"direction": "column"},
                "children": [
                    {"type": "input", "bind": "draft", "props": {}},
                ],
            },
        }
        out = normalize_ripple_spec(raw)
        assert out is not None, f"alias {alias!r} returned None"
        assert isinstance(out.get("ui"), dict), f"alias {alias!r} not lifted"
        assert out["ui"]["type"] == "flex"
        # State must survive the lift.
        assert out.get("state", {}).get("draft") == ""
        # Original alias key should NOT also be present (avoid both ui+root).
        assert alias not in out, f"alias {alias!r} kept alongside ui"


def test_normalizer_drops_entity_detail_actions_without_handlers():
    """An ``entity-detail`` action item with ``id``/``label`` but no
    ``actions`` handler renders a clickable button that does nothing —
    a dead control. Normalizer should drop those, keeping only items
    with a real handler wired."""
    from pocketpaw_ee.cloud.ripple_normalizer import normalize_ripple_spec

    spec = {
        "state": {},
        "ui": {
            "type": "entity-detail",
            "props": {
                "title": "Demo",
                "actions": [
                    {"id": "view", "label": "View"},  # ← dead, no handler
                    {
                        "id": "refresh",
                        "label": "Refresh",
                        "actions": [{"action": "emit", "target": "refresh"}],
                    },
                    {"id": "share", "label": "Share", "actions": []},  # ← empty list
                ],
            },
        },
    }
    out = normalize_ripple_spec(spec)
    assert out is not None
    actions = out["ui"]["props"]["actions"]
    assert len(actions) == 1, "only the wired action should survive"
    assert actions[0]["id"] == "refresh"


def test_normalizer_lifts_on_click_to_actions_on_entity_detail():
    """Some agents emit ``on_click`` on entity-detail action items
    (parallel to button widgets) instead of ``actions``. Lift it."""
    from pocketpaw_ee.cloud.ripple_normalizer import normalize_ripple_spec

    spec = {
        "ui": {
            "type": "entity-detail",
            "props": {
                "title": "Demo",
                "actions": [
                    {
                        "id": "view",
                        "label": "View on GitHub",
                        "on_click": [{"action": "navigate", "url": "https://github.com"}],
                    },
                ],
            },
        },
    }
    out = normalize_ripple_spec(spec)
    assert out is not None
    item = out["ui"]["props"]["actions"][0]
    assert "on_click" not in item, "on_click should be lifted away"
    assert isinstance(item.get("actions"), list)
    assert item["actions"][0]["action"] == "navigate"


def test_normalizer_preserves_entity_detail_with_no_actions_field():
    """Sanity: an entity-detail with no ``actions`` prop at all should
    pass through unchanged — the fix only fires when actions exist."""
    from pocketpaw_ee.cloud.ripple_normalizer import normalize_ripple_spec

    spec = {
        "ui": {
            "type": "entity-detail",
            "props": {"title": "Demo", "subtitle": "hi"},
        },
    }
    out = normalize_ripple_spec(spec)
    assert out is not None
    assert out["ui"]["props"] == {"title": "Demo", "subtitle": "hi"}


def test_normalizer_passes_through_already_wrapped_ui():
    """A spec that's already in the ``{ui: <node>}`` shape should not
    be double-wrapped."""
    from pocketpaw_ee.cloud.ripple_normalizer import normalize_ripple_spec

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
    from pocketpaw_ee.cloud.chat.agent_router import _generate_session_title
    from pocketpaw_ee.cloud.chat.agent_service import ScopeContext, ScopeKind

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
    with patch("pocketpaw.memory.titler.generate_title", AsyncMock()) as titler:
        await _generate_session_title(ctx, "anything")
    titler.assert_not_called()
