"""Integration tests for the granular ``rippleSpec.ui`` mutation ops.

These exercise the agent-facing ``*_for_agent`` wrappers in
``ee/cloud/pockets/agent_context.py``, which delegate to the service
layer's ``agent_add_node`` / ``agent_replace_node`` / ``agent_set_node_prop`` /
``agent_move_node`` / ``agent_remove_node``. The Beanie doc fetch is
mocked; emit + SSE push are captured so we can assert what flowed out.

The canonical regression — "rename one cell in a 100-row table" — lives
near the bottom: it locks down that we DON'T fall back to
``agent_update`` for surgical edits and that the SSE payload only
carries the touched subtree.
"""

from __future__ import annotations

import copy
from contextlib import ExitStack
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pocketpaw_ee.cloud.pockets import agent_context
from pocketpaw_ee.cloud.pockets import service as pocket_service

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeDoc:
    """Minimum surface to stand in for a ``_PocketDoc`` in these tests.

    The mutations operate on ``self.rippleSpec`` in place, just like a
    real Beanie doc. ``save()`` is an async no-op (we capture calls
    separately via ``saves``).
    """

    def __init__(self, pocket_id: str, ripple_spec: dict[str, Any]):
        self.id = pocket_id
        self.workspace = "w1"
        self.name = "Test Pocket"
        self.description = ""
        self.type = "custom"
        self.icon = ""
        self.color = ""
        self.owner = "u1"
        self.visibility = "workspace"
        self.team: list[str] = []
        self.agents: list[str] = []
        self.widgets: list[Any] = []
        self.rippleSpec = ripple_spec
        self.share_link_token = None
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
            "widgets": list(self.widgets),
            "rippleSpec": self.rippleSpec,
            "share_link_token": self.share_link_token,
            "share_link_access": self.share_link_access,
            "shared_with": list(self.shared_with),
            "tool_specs": list(self.tool_specs),
        }


@pytest.fixture
def fake_doc():
    """Default fixture: a small 3-row table pocket."""
    spec = {
        "version": "1.0",
        "ui": {
            "id": "n_root0000",
            "type": "flex",
            "props": {"direction": "column"},
            "children": [
                {"id": "n_header00", "type": "heading", "props": {"text": "Hi"}},
                {
                    "id": "n_table000",
                    "type": "table",
                    "props": {},
                    "children": [
                        {"id": "n_row00001", "type": "row", "props": {"label": "alice"}},
                        {"id": "n_row00002", "type": "row", "props": {"label": "bob"}},
                        {"id": "n_row00003", "type": "row", "props": {"label": "carol"}},
                    ],
                },
            ],
        },
    }
    return _FakeDoc("507f1f77bcf86cd799439011", spec)


def _patches(doc: _FakeDoc):
    """Patch the seams every granular op touches: doc fetch, realtime
    emit, payload builder (sidesteps domain mapping), the SSE push, and
    the per-stream identity ContextVars ``_agent_load_doc`` reads to
    enforce workspace + edit-access checks.

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
        )
    )
    stack.enter_context(patch("pocketpaw_ee.cloud.pockets.service.emit", new=AsyncMock()))
    stack.enter_context(
        patch(
            "pocketpaw_ee.cloud.pockets.service._pocket_event_payload",
            new=AsyncMock(return_value={"pocket_id": doc.id}),
        )
    )
    # The MCP wrapper imports lazily from ee.cloud.chat.agent_service.
    # Patch where it's looked up.
    stack.enter_context(
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.push_pocket_mutation",
            new=MagicMock(side_effect=_capture),
        )
    )
    # normalize_ripple_spec is fine to leave as-is, but it pulls in
    # the manifest module on first import; patching to identity keeps
    # the test hermetic and fast.
    stack.enter_context(
        patch(
            "pocketpaw_ee.cloud.pockets.service.normalize_ripple_spec",
            new=lambda s: s,
        )
    )
    # _agent_load_doc reads workspace + user from ContextVars and checks
    # access. Default to the matching owner so the existing tests cover
    # the happy path; cross-workspace + access-denied behavior gets its
    # own dedicated tests at the bottom of this file.
    stack.enter_context(
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id",
            new=MagicMock(return_value=doc.workspace),
        )
    )
    stack.enter_context(
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_user_id",
            new=MagicMock(return_value=doc.owner),
        )
    )
    return stack, push_calls


# ---------------------------------------------------------------------------
# add_node
# ---------------------------------------------------------------------------


async def test_add_node_appends_under_parent(fake_doc):
    ctx, push_calls = _patches(fake_doc)
    with ctx:
        result = await agent_context.add_node_for_agent(
            fake_doc.id,
            parent_id="n_table000",
            spec={"type": "row", "props": {"label": "dave"}},
        )

    assert result["ok"] is True
    table = fake_doc.rippleSpec["ui"]["children"][1]
    assert len(table["children"]) == 4
    assert table["children"][3]["props"]["label"] == "dave"
    # New node got an id assigned.
    new_id = result["node_id"]
    assert new_id and new_id.startswith("n_")
    # Exactly one SSE push, with subtree-only payload.
    assert len(push_calls) == 1
    push = push_calls[0]
    assert push["action"] == "node_added"
    assert push["parent_id"] == "n_table000"
    assert push["subtree"]["props"]["label"] == "dave"
    # The push must NOT carry the whole pocket — only the changed subtree.
    assert "pocket" not in push
    assert fake_doc.saves == 1


async def test_add_node_after_id_inserts_in_position(fake_doc):
    ctx, _ = _patches(fake_doc)
    with ctx:
        await agent_context.add_node_for_agent(
            fake_doc.id,
            parent_id="n_table000",
            spec={"type": "row", "props": {"label": "between"}},
            after_id="n_row00001",
        )
    labels = [r["props"]["label"] for r in fake_doc.rippleSpec["ui"]["children"][1]["children"]]
    assert labels == ["alice", "between", "bob", "carol"]


async def test_add_node_unknown_parent_errors(fake_doc):
    ctx, push_calls = _patches(fake_doc)
    with ctx:
        result = await agent_context.add_node_for_agent(
            fake_doc.id,
            parent_id="n_ghost000",
            spec={"type": "row"},
        )
    assert result["ok"] is False
    assert "n_ghost000" in result["error"]
    assert push_calls == []
    # No mutation, no save.
    assert fake_doc.saves == 0


# ---------------------------------------------------------------------------
# replace_node
# ---------------------------------------------------------------------------


async def test_replace_node_swaps_subtree_preserves_id(fake_doc):
    ctx, push_calls = _patches(fake_doc)
    with ctx:
        result = await agent_context.replace_node_for_agent(
            fake_doc.id,
            node_id="n_header00",
            spec={"type": "stat", "props": {"value": "42"}},
        )
    assert result["ok"] is True
    header = fake_doc.rippleSpec["ui"]["children"][0]
    assert header["type"] == "stat"
    assert header["id"] == "n_header00"  # preserved
    assert push_calls[0]["action"] == "node_replaced"


async def test_replace_node_unknown_id_errors(fake_doc):
    ctx, _ = _patches(fake_doc)
    with ctx:
        result = await agent_context.replace_node_for_agent(
            fake_doc.id,
            node_id="n_ghost000",
            spec={"type": "stat"},
        )
    assert result["ok"] is False


# ---------------------------------------------------------------------------
# set_node_prop
# ---------------------------------------------------------------------------


async def test_set_node_prop_writes_into_props(fake_doc):
    ctx, push_calls = _patches(fake_doc)
    with ctx:
        result = await agent_context.set_node_prop_for_agent(
            fake_doc.id,
            node_id="n_row00002",
            prop="label",
            value="robert",
        )
    assert result["ok"] is True
    assert result["old_value"] == "bob"
    row = fake_doc.rippleSpec["ui"]["children"][1]["children"][1]
    assert row["props"]["label"] == "robert"
    push = push_calls[0]
    assert push["action"] == "node_prop_set"
    assert push["node_id"] == "n_row00002"
    assert push["prop"] == "label"
    assert push["value"] == "robert"
    # Subtree carries the single touched row, NOT the full table.
    assert push["subtree"]["id"] == "n_row00002"
    assert "children" not in push["subtree"]


async def test_set_node_prop_handles_top_level_key(fake_doc):
    ctx, _ = _patches(fake_doc)
    with ctx:
        result = await agent_context.set_node_prop_for_agent(
            fake_doc.id,
            node_id="n_row00001",
            prop="show",
            value="{state.visible}",
        )
    assert result["ok"] is True
    row = fake_doc.rippleSpec["ui"]["children"][1]["children"][0]
    assert row["show"] == "{state.visible}"
    # Top-level keys must NOT bleed into props.
    assert "show" not in row["props"]


# ---------------------------------------------------------------------------
# move_node
# ---------------------------------------------------------------------------


async def test_move_node_to_new_parent(fake_doc):
    ctx, push_calls = _patches(fake_doc)
    with ctx:
        result = await agent_context.move_node_for_agent(
            fake_doc.id,
            node_id="n_header00",
            new_parent_id="n_table000",
        )
    assert result["ok"] is True
    table = fake_doc.rippleSpec["ui"]["children"][0]  # was n_header00; now n_table000
    assert table["id"] == "n_table000"
    assert table["children"][-1]["id"] == "n_header00"
    assert push_calls[0]["action"] == "node_moved"


async def test_move_node_into_descendant_rejected(fake_doc):
    ctx, _ = _patches(fake_doc)
    with ctx:
        result = await agent_context.move_node_for_agent(
            fake_doc.id,
            node_id="n_table000",
            new_parent_id="n_row00001",
        )
    assert result["ok"] is False
    assert "descendant" in result["error"]


# ---------------------------------------------------------------------------
# remove_node
# ---------------------------------------------------------------------------


async def test_remove_node_drops_subtree(fake_doc):
    ctx, push_calls = _patches(fake_doc)
    with ctx:
        result = await agent_context.remove_node_for_agent(fake_doc.id, node_id="n_row00002")
    assert result["ok"] is True
    rows = fake_doc.rippleSpec["ui"]["children"][1]["children"]
    assert [r["id"] for r in rows] == ["n_row00001", "n_row00003"]
    push = push_calls[0]
    assert push["action"] == "node_removed"
    assert push["node_id"] == "n_row00002"
    assert push["parent_id"] == "n_table000"


async def test_remove_root_errors(fake_doc):
    ctx, push_calls = _patches(fake_doc)
    with ctx:
        result = await agent_context.remove_node_for_agent(fake_doc.id, node_id="n_root0000")
    assert result["ok"] is False
    assert push_calls == []


# ---------------------------------------------------------------------------
# ID auto-assignment on first read
# ---------------------------------------------------------------------------


async def test_legacy_pocket_gets_ids_on_first_op():
    """A pocket persisted before stable-IDs landed (some nodes lack
    ids) gets them assigned on its next mutation. No big-bang migration
    needed.

    The first granular call assigns ids and persists. We then re-target
    using the now-known root id.
    """
    doc = _FakeDoc(
        "507f1f77bcf86cd799439099",
        {
            "version": "1.0",
            "ui": {
                "type": "flex",
                "children": [
                    {"type": "text", "props": {"value": "hello"}},
                    {"type": "text", "props": {"value": "world"}},
                ],
            },
        },
    )
    ctx, _ = _patches(doc)
    with ctx:
        # First call targets a parent we don't yet know — the op fails
        # (parent_id="UNKNOWN" not found) but ids get assigned during
        # the load-and-ensure-ids phase before the failure.
        result_fail = await agent_context.add_node_for_agent(
            doc.id, parent_id="UNKNOWN", spec={"type": "text"}
        )
        assert result_fail["ok"] is False

        root_id = doc.rippleSpec["ui"]["id"]
        assert root_id and root_id.startswith("n_")
        assert all(k["id"].startswith("n_") for k in doc.rippleSpec["ui"]["children"])

        # Now we can target. Second call succeeds.
        result_ok = await agent_context.add_node_for_agent(
            doc.id, parent_id=root_id, spec={"type": "text"}
        )
        assert result_ok["ok"] is True


# ---------------------------------------------------------------------------
# Canonical regression: rename one row in a 100-row table.
# ---------------------------------------------------------------------------


async def test_rename_one_row_in_100_row_table_emits_one_subtree_frame():
    """The whole point of PR 1: a surgical edit emits ONE small SSE
    frame, never falls back to ``agent_update(ripple_spec=<full>)``, and
    leaves untouched rows verbatim."""
    rows = [
        {"id": f"n_{i:08d}", "type": "row", "props": {"label": f"user_{i}"}} for i in range(100)
    ]
    spec = {
        "version": "1.0",
        "ui": {
            "id": "n_root0000",
            "type": "table",
            "props": {},
            "children": rows,
        },
    }
    doc = _FakeDoc("507f1f77bcf86cd799439088", spec)
    snapshot = copy.deepcopy(spec)

    # Trip-wire: if the granular op silently falls back to agent_update
    # the test fails loudly.
    update_sentinel = AsyncMock(side_effect=AssertionError("agent_update must NOT be called"))

    ctx, push_calls = _patches(doc)
    with (
        ctx,
        patch.object(pocket_service, "agent_update", new=update_sentinel),
    ):
        result = await agent_context.set_node_prop_for_agent(
            doc.id,
            node_id="n_00000042",
            prop="label",
            value="renamed",
        )

    assert result["ok"] is True
    assert update_sentinel.call_count == 0

    # Exactly one SSE frame.
    assert len(push_calls) == 1
    push = push_calls[0]
    assert push["action"] == "node_prop_set"
    assert push["node_id"] == "n_00000042"

    # Subtree carries ONLY the touched row — not the full 100-row table.
    subtree = push["subtree"]
    assert subtree["id"] == "n_00000042"
    assert subtree["props"]["label"] == "renamed"
    # No siblings, no other rows in the frame.
    assert "children" not in subtree

    # All other rows are untouched, byte-for-byte.
    for i, row in enumerate(doc.rippleSpec["ui"]["children"]):
        if i == 42:
            continue
        assert row == snapshot["ui"]["children"][i], f"row {i} drifted"


# ---------------------------------------------------------------------------
# Auth gate on _agent_load_doc — the entry point for every granular
# mutation. The cross-tenant + access-denied paths must look identical
# to a genuinely missing pocket so an agent in workspace A can't
# enumerate the existence of pockets in workspace B by ObjectId guess.
# ---------------------------------------------------------------------------


async def test_cross_workspace_mutation_rejected_as_not_found(fake_doc):
    """An agent whose ContextVars are bound to a different workspace
    must NOT be able to touch this pocket — and the failure must not
    leak existence (same `<id> not found` shape as a genuinely missing
    pocket)."""
    ctx, push_calls = _patches(fake_doc)
    with ctx:
        # Override the workspace ContextVar mock to a foreign workspace.
        with patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id",
            new=MagicMock(return_value="w-other"),
        ):
            result = await agent_context.set_node_prop_for_agent(
                fake_doc.id, node_id="n_header00", prop="text", value="hijacked"
            )
    assert result["ok"] is False
    assert "not found" in result["error"]
    assert push_calls == []
    assert fake_doc.saves == 0
    # Importantly: spec is unchanged.
    assert fake_doc.rippleSpec["ui"]["children"][0]["props"]["text"] == "Hi"


async def test_non_owner_private_pocket_rejected(fake_doc):
    """A user who isn't the owner, isn't in ``shared_with``, and the
    pocket isn't workspace-visible — same `not found` mask."""
    fake_doc.visibility = "private"
    fake_doc.shared_with = []
    ctx, push_calls = _patches(fake_doc)
    with ctx:
        with patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_user_id",
            new=MagicMock(return_value="u-stranger"),
        ):
            result = await agent_context.set_node_prop_for_agent(
                fake_doc.id, node_id="n_header00", prop="text", value="hijacked"
            )
    assert result["ok"] is False
    assert "not found" in result["error"]
    assert push_calls == []


async def test_shared_with_can_edit(fake_doc):
    """``shared_with`` entries get edit access — mirrors the REST
    path's ``_check_domain_edit_access``."""
    fake_doc.visibility = "private"
    fake_doc.shared_with = ["u-collaborator"]
    ctx, push_calls = _patches(fake_doc)
    with ctx:
        with patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_user_id",
            new=MagicMock(return_value="u-collaborator"),
        ):
            result = await agent_context.set_node_prop_for_agent(
                fake_doc.id, node_id="n_header00", prop="text", value="updated"
            )
    assert result["ok"] is True
    assert len(push_calls) == 1


async def test_no_active_stream_rejected(fake_doc):
    """When ContextVars aren't set (e.g. called outside a chat stream),
    every agent op fails with a clear message."""
    ctx, push_calls = _patches(fake_doc)
    with ctx:
        with (
            patch(
                "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id",
                new=MagicMock(return_value=None),
            ),
            patch(
                "pocketpaw_ee.cloud.chat.agent_service.current_user_id",
                new=MagicMock(return_value=None),
            ),
        ):
            result = await agent_context.set_node_prop_for_agent(
                fake_doc.id, node_id="n_header00", prop="text", value="x"
            )
    assert result["ok"] is False
    assert "no active workspace/user" in result["error"]
    assert push_calls == []


# ---------------------------------------------------------------------------
# set_prop_array_item — surgical edit of one item inside a node prop-array.
# ---------------------------------------------------------------------------


class TestSetPropArrayItem:
    """Surgical edit of one item inside a node's prop-array."""

    @pytest.mark.asyncio
    async def test_updates_matched_item_by_field(self, fake_doc):
        # Seed a chart with a 4-slice donut.
        chart = {
            "id": "n_chart000",
            "type": "chart",
            "props": {
                "type": "donut",
                "data": [
                    {"label": "Online Store", "value": 62},
                    {"label": "POS", "value": 18},
                    {"label": "Social", "value": 12},
                    {"label": "Other", "value": 8},
                ],
            },
        }
        fake_doc.rippleSpec["ui"]["children"].append(chart)

        ctx, _ = _patches(fake_doc)
        with ctx:
            result, err = await pocket_service.agent_set_prop_array_item(
                fake_doc.id,
                node_id="n_chart000",
                prop="data",
                match={"by_field": "label", "equals": "Other"},
                partial={"value": 5},
            )

        assert err is None
        assert result["item_index"] == 3
        assert result["item"] == {"label": "Other", "value": 5}
        assert chart["props"]["data"][3]["value"] == 5
        # Unchanged siblings preserved.
        assert chart["props"]["data"][0]["value"] == 62
        assert fake_doc.saves == 1

    @pytest.mark.asyncio
    async def test_unsupported_prop_array_rejected(self, fake_doc):
        ctx, _ = _patches(fake_doc)
        with ctx:
            result, err = await pocket_service.agent_set_prop_array_item(
                fake_doc.id,
                node_id="n_header00",  # heading, not chart/table/etc.
                prop="text",
                match={"index": 0},
                partial={"text": "Hi"},
            )
        assert result is None
        assert err is not None
        assert "unsupported_prop_array" in err

    @pytest.mark.asyncio
    async def test_missing_node_errors_cleanly(self, fake_doc):
        ctx, _ = _patches(fake_doc)
        with ctx:
            result, err = await pocket_service.agent_set_prop_array_item(
                fake_doc.id,
                node_id="n_does_not_exist",
                prop="data",
                match={"index": 0},
                partial={},
            )
        assert result is None
        assert "no node with id" in err

    @pytest.mark.asyncio
    async def test_item_not_found_returns_error(self, fake_doc):
        chart = {
            "id": "n_chart000",
            "type": "chart",
            "props": {"data": [{"label": "A", "value": 1}]},
        }
        fake_doc.rippleSpec["ui"]["children"].append(chart)
        ctx, _ = _patches(fake_doc)
        with ctx:
            result, err = await pocket_service.agent_set_prop_array_item(
                fake_doc.id,
                node_id="n_chart000",
                prop="data",
                match={"by_field": "label", "equals": "Z"},
                partial={"value": 99},
            )
        assert result is None
        assert "not_found" in err

    @pytest.mark.asyncio
    async def test_ambiguous_match_returns_candidates(self, fake_doc):
        chart = {
            "id": "n_chart000",
            "type": "chart",
            "props": {
                "data": [
                    {"label": "Other", "value": 1},
                    {"label": "Other", "value": 2},
                ]
            },
        }
        fake_doc.rippleSpec["ui"]["children"].append(chart)
        ctx, _ = _patches(fake_doc)
        with ctx:
            result, err = await pocket_service.agent_set_prop_array_item(
                fake_doc.id,
                node_id="n_chart000",
                prop="data",
                match={"by_field": "label", "equals": "Other"},
                partial={"value": 99},
            )
        assert result is None
        assert "ambiguous" in err


# ---------------------------------------------------------------------------
# append_prop_array_item — append (or insert-after-match) into a prop-array.
# ---------------------------------------------------------------------------


class TestAppendPropArrayItem:
    @pytest.mark.asyncio
    async def test_appends_to_end_by_default(self, fake_doc):
        table = {
            "id": "n_table111",
            "type": "table",
            "props": {"rows": [{"orderId": "#1"}, {"orderId": "#2"}]},
        }
        fake_doc.rippleSpec["ui"]["children"].append(table)
        ctx, _ = _patches(fake_doc)
        with ctx:
            result, err = await pocket_service.agent_append_prop_array_item(
                fake_doc.id,
                node_id="n_table111",
                prop="rows",
                value={"orderId": "#3"},
            )
        assert err is None
        assert result["item_index"] == 2
        assert table["props"]["rows"][-1] == {"orderId": "#3"}

    @pytest.mark.asyncio
    async def test_inserts_after_matched_item(self, fake_doc):
        table = {
            "id": "n_table111",
            "type": "table",
            "props": {"rows": [{"orderId": "#1"}, {"orderId": "#3"}]},
        }
        fake_doc.rippleSpec["ui"]["children"].append(table)
        ctx, _ = _patches(fake_doc)
        with ctx:
            result, err = await pocket_service.agent_append_prop_array_item(
                fake_doc.id,
                node_id="n_table111",
                prop="rows",
                value={"orderId": "#2"},
                after={"by_field": "orderId", "equals": "#1"},
            )
        assert err is None
        assert result["item_index"] == 1
        assert [r["orderId"] for r in table["props"]["rows"]] == ["#1", "#2", "#3"]

    @pytest.mark.asyncio
    async def test_creates_empty_array_if_missing(self, fake_doc):
        node = {"id": "n_node0001", "type": "checklist-layout", "props": {}}
        fake_doc.rippleSpec["ui"]["children"].append(node)
        ctx, _ = _patches(fake_doc)
        with ctx:
            result, err = await pocket_service.agent_append_prop_array_item(
                fake_doc.id,
                node_id="n_node0001",
                prop="items",
                value={"text": "Hi"},
            )
        assert err is None
        assert node["props"]["items"] == [{"text": "Hi"}]
        assert result["item_index"] == 0

    @pytest.mark.asyncio
    async def test_after_target_not_found_errors(self, fake_doc):
        table = {
            "id": "n_table111",
            "type": "table",
            "props": {"rows": [{"orderId": "#1"}]},
        }
        fake_doc.rippleSpec["ui"]["children"].append(table)
        ctx, _ = _patches(fake_doc)
        with ctx:
            result, err = await pocket_service.agent_append_prop_array_item(
                fake_doc.id,
                node_id="n_table111",
                prop="rows",
                value={"orderId": "#2"},
                after={"by_field": "orderId", "equals": "#999"},
            )
        assert result is None
        assert "not_found" in err


class TestRemovePropArrayItem:
    @pytest.mark.asyncio
    async def test_removes_matched_item(self, fake_doc):
        chart = {
            "id": "n_chart000",
            "type": "chart",
            "props": {
                "data": [
                    {"label": "A", "value": 1},
                    {"label": "Other", "value": 2},
                    {"label": "B", "value": 3},
                ]
            },
        }
        fake_doc.rippleSpec["ui"]["children"].append(chart)
        ctx, _ = _patches(fake_doc)
        with ctx:
            result, err = await pocket_service.agent_remove_prop_array_item(
                fake_doc.id,
                node_id="n_chart000",
                prop="data",
                match={"by_field": "label", "equals": "Other"},
            )
        assert err is None
        assert result["removed_index"] == 1
        assert result["removed_item"] == {"label": "Other", "value": 2}
        assert [d["label"] for d in chart["props"]["data"]] == ["A", "B"]

    @pytest.mark.asyncio
    async def test_not_found_errors(self, fake_doc):
        chart = {
            "id": "n_chart000",
            "type": "chart",
            "props": {"data": [{"label": "A"}]},
        }
        fake_doc.rippleSpec["ui"]["children"].append(chart)
        ctx, _ = _patches(fake_doc)
        with ctx:
            result, err = await pocket_service.agent_remove_prop_array_item(
                fake_doc.id,
                node_id="n_chart000",
                prop="data",
                match={"by_field": "label", "equals": "Z"},
            )
        assert result is None
        assert "not_found" in err


# ---------------------------------------------------------------------------
# *_for_agent wrappers — exercise the agent_context.py layer that drives
# SSE node ops + the uniform {ok, ...} shape consumed by LangChain tools.
# ---------------------------------------------------------------------------


class TestPropArrayItemWrappers:
    """The thin wrappers in agent_context.py: push SSE node ops + return
    the uniform {ok, ...} shape consumed by the LangChain edit tools."""

    @pytest.mark.asyncio
    async def test_set_prop_array_item_for_agent_pushes_sse(self, fake_doc):
        chart = {
            "id": "n_chart000",
            "type": "chart",
            "props": {
                "data": [
                    {"label": "Online Store", "value": 62},
                    {"label": "Other", "value": 8},
                ],
            },
        }
        fake_doc.rippleSpec["ui"]["children"].append(chart)

        ctx, push_calls = _patches(fake_doc)
        with ctx:
            result = await agent_context.set_prop_array_item_for_agent(
                fake_doc.id,
                node_id="n_chart000",
                prop="data",
                match={"by_field": "label", "equals": "Other"},
                partial={"value": 5},
            )

        assert result["ok"] is True
        assert result["item_index"] == 1
        assert result["item"] == {"label": "Other", "value": 5}
        assert chart["props"]["data"][1]["value"] == 5
        # Exactly one SSE push with the granular action.
        assert len(push_calls) == 1
        push = push_calls[0]
        assert push["action"] == "node_prop_array_item_set"
        assert push["node_id"] == "n_chart000"
        assert push["prop"] == "data"
        assert push["item_index"] == 1
        assert push["item"] == {"label": "Other", "value": 5}
        # Subtree-only; never the full pocket.
        assert "pocket" not in push

    @pytest.mark.asyncio
    async def test_set_prop_array_item_for_agent_propagates_error(self, fake_doc):
        ctx, push_calls = _patches(fake_doc)
        with ctx:
            result = await agent_context.set_prop_array_item_for_agent(
                fake_doc.id,
                node_id="n_header00",  # unsupported widget for prop-array ops
                prop="text",
                match={"index": 0},
                partial={"text": "x"},
            )
        assert result["ok"] is False
        assert "unsupported_prop_array" in result["error"]
        assert push_calls == []

    @pytest.mark.asyncio
    async def test_append_prop_array_item_for_agent_pushes_sse(self, fake_doc):
        table = {
            "id": "n_table111",
            "type": "table",
            "props": {"rows": [{"orderId": "#1"}, {"orderId": "#3"}]},
        }
        fake_doc.rippleSpec["ui"]["children"].append(table)

        ctx, push_calls = _patches(fake_doc)
        with ctx:
            result = await agent_context.append_prop_array_item_for_agent(
                fake_doc.id,
                node_id="n_table111",
                prop="rows",
                value={"orderId": "#2"},
                after={"by_field": "orderId", "equals": "#1"},
            )

        assert result["ok"] is True
        assert result["item_index"] == 1
        assert [r["orderId"] for r in table["props"]["rows"]] == ["#1", "#2", "#3"]
        assert len(push_calls) == 1
        push = push_calls[0]
        assert push["action"] == "node_prop_array_item_appended"
        assert push["node_id"] == "n_table111"
        assert push["prop"] == "rows"
        assert push["item_index"] == 1
        assert push["item"] == {"orderId": "#2"}

    @pytest.mark.asyncio
    async def test_remove_prop_array_item_for_agent_pushes_sse(self, fake_doc):
        chart = {
            "id": "n_chart000",
            "type": "chart",
            "props": {
                "data": [
                    {"label": "A", "value": 1},
                    {"label": "Other", "value": 2},
                    {"label": "B", "value": 3},
                ],
            },
        }
        fake_doc.rippleSpec["ui"]["children"].append(chart)

        ctx, push_calls = _patches(fake_doc)
        with ctx:
            result = await agent_context.remove_prop_array_item_for_agent(
                fake_doc.id,
                node_id="n_chart000",
                prop="data",
                match={"by_field": "label", "equals": "Other"},
            )

        assert result["ok"] is True
        assert result["removed_index"] == 1
        assert result["removed_item"] == {"label": "Other", "value": 2}
        assert [d["label"] for d in chart["props"]["data"]] == ["A", "B"]
        assert len(push_calls) == 1
        push = push_calls[0]
        assert push["action"] == "node_prop_array_item_removed"
        assert push["node_id"] == "n_chart000"
        assert push["prop"] == "data"
        assert push["removed_index"] == 1
        assert push["removed_item"] == {"label": "Other", "value": 2}


# ---------------------------------------------------------------------------
# End-to-end canary: the 12-row "All Orders" table edit from the design doc.
# This is the regression the whole Tier-2 work was driven by — editing one
# row must leave every other row byte-identical.
# ---------------------------------------------------------------------------


class TestArrayItemCanary:
    """End-to-end: the 12-row 'All Orders' table edit from the design doc."""

    @pytest.mark.asyncio
    async def test_one_row_edit_does_not_rewrite_other_rows(self, fake_doc):
        rows = [
            {"orderId": f"#{1030 + i}", "customer": f"C{i}", "status": "Fulfilled"}
            for i in range(12)
        ]
        snapshot = [dict(r) for r in rows]
        table = {
            "id": "n_orders00",
            "type": "table",
            "props": {"rows": rows},
        }
        fake_doc.rippleSpec["ui"]["children"].append(table)

        ctx, _ = _patches(fake_doc)
        with ctx:
            result = await agent_context.set_prop_array_item_for_agent(
                fake_doc.id,
                node_id="n_orders00",
                prop="rows",
                match={"by_field": "orderId", "equals": "#1039"},
                partial={"status": "Shipped"},
            )
        assert result["ok"] is True
        assert result["item_index"] == 9
        # All other rows are byte-identical.
        for i, original in enumerate(snapshot):
            if i == 9:
                continue
            assert table["props"]["rows"][i] == original, f"row {i} drifted"
        assert table["props"]["rows"][9]["status"] == "Shipped"
        assert table["props"]["rows"][9]["customer"] == "C9"  # untouched field


# ---------------------------------------------------------------------------
# Kanban node-level bind: end-to-end through the specialist rebuild ops.
# Simulates the granular op sequence a "rebuild this as a kanban" intent
# would emit against an existing Todo Dashboard. Verifies the kanban node
# carries ``bind`` at the NODE level (not inside ``props``) after the full
# replace_node → normalize_ripple_spec → save round-trip, and that a
# subsequent "drag-drop" state mutation persists to the same path the
# bind targets — proving the kanban writeback path is structurally wired.
#
# Pinned by the interactive-by-default block in src/pocketpaw/ripple/_pockets.py
# (the WRONG vs RIGHT examples around line 506-512) and the prompt-content
# guard in tests/unit/test_pocket_delegation_rule_gaps.py::TestInteractiveBindRule.
# This file holds the runtime contract; that file holds the teaching surface.
# ---------------------------------------------------------------------------


def _patches_with_real_normalizer(doc: _FakeDoc):
    """Variant of ``_patches`` that leaves ``normalize_ripple_spec`` live so
    we can verify the normalizer's preservation of node-level ``bind``
    across the service's mutation-then-normalize cycle.

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
        )
    )
    stack.enter_context(patch("pocketpaw_ee.cloud.pockets.service.emit", new=AsyncMock()))
    stack.enter_context(
        patch(
            "pocketpaw_ee.cloud.pockets.service._pocket_event_payload",
            new=AsyncMock(return_value={"pocket_id": doc.id}),
        )
    )
    stack.enter_context(
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.push_pocket_mutation",
            new=MagicMock(side_effect=_capture),
        )
    )
    # Intentionally NOT patching normalize_ripple_spec — we want the real
    # normalizer running so bind-preservation is verified end-to-end.
    stack.enter_context(
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id",
            new=MagicMock(return_value=doc.workspace),
        )
    )
    stack.enter_context(
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_user_id",
            new=MagicMock(return_value=doc.owner),
        )
    )
    return stack, push_calls


def _todo_dashboard_doc() -> _FakeDoc:
    """A minimal Todo Dashboard: an input bound to draft, an each loop
    rendering one row per task. No kanban yet — that's what the rebuild
    op introduces."""
    spec = {
        "version": "1.0",
        "state": {
            "draft": "",
            "tasks": [
                {"id": "t1", "label": "Wire kanban", "status": "todo"},
                {"id": "t2", "label": "Verify bind", "status": "in_progress"},
                {"id": "t3", "label": "Ship test", "status": "done"},
            ],
        },
        "ui": {
            "id": "n_root0000",
            "type": "flex",
            "props": {"direction": "column"},
            "children": [
                {
                    "id": "n_inputbox",
                    "type": "input",
                    "bind": "draft",
                    "props": {"placeholder": "Add task..."},
                },
                {
                    "id": "n_taskloop",
                    "type": "each",
                    "items": "state.tasks",
                    "item_as": "task",
                    "children": [
                        {
                            "id": "n_taskrow0",
                            "type": "flex",
                            "props": {"direction": "row"},
                            "children": [
                                {
                                    "id": "n_taskcb00",
                                    "type": "checkbox",
                                    "bind": "tasks.{index}.done",
                                    "props": {},
                                },
                                {
                                    "id": "n_tasktext",
                                    "type": "text",
                                    "props": {"text": "{task.label}"},
                                },
                            ],
                        }
                    ],
                },
            ],
        },
    }
    return _FakeDoc("507f1f77bcf86cd799439099", spec)


class TestKanbanNodeBindPreservedThroughRebuild:
    """End-to-end: simulate the granular-op sequence the edit specialist
    emits for a 'rebuild this as a kanban' intent against a Todo Dashboard.
    Lock down the kanban-writeback contract one layer below the prompt.

    The teaching surface (src/pocketpaw/ripple/_pockets.py interactive-by-default
    block, line ~497-518) tells the model to put ``bind`` at node level.
    The unit guards (tests/unit/test_pocket_delegation_rule_gaps.py::
    TestInteractiveBindRule) check the prompt's content. THIS test pins
    that when the specialist FOLLOWS that teaching — emitting
    ``replace_node`` with ``{"type": "kanban", "bind": "state.tasks", ...}`` —
    the spec round-trips through ``agent_replace_node`` and the live
    ``normalize_ripple_spec`` without ``bind`` getting demoted into
    ``props`` or stripped. Then a 'drag-drop' state mutation persists at
    the same path the bind targets — proving the writeback is structurally
    wired."""

    @pytest.mark.asyncio
    async def test_kanban_bind_lives_at_node_level_after_replace_node(self) -> None:
        """The chat agent ships one ``replace_node`` op turning the
        ``each`` loop into a kanban. The kanban subtree it sends carries
        ``bind`` at the NODE level (sibling of ``type`` / ``props``).
        Post-op, the persisted spec must keep ``bind`` at node level —
        not inside ``props`` — so the renderer's writeback resolver can
        find it."""
        doc = _todo_dashboard_doc()

        # The kanban subtree the specialist emits for "rebuild as a
        # kanban". Mirrors the RIGHT example from the canonical prompt:
        #   {"type": "kanban", "bind": "state.tasks",
        #    "props": {"columns": [...], "columnKey": "status", ...}}
        kanban_spec = {
            "type": "kanban",
            "bind": "state.tasks",
            "props": {
                "columns": [
                    {"key": "todo", "label": "To Do"},
                    {"key": "in_progress", "label": "In Progress"},
                    {"key": "done", "label": "Done"},
                ],
                "columnKey": "status",
                "cardLabel": "{task.label}",
            },
        }

        ctx, push_calls = _patches_with_real_normalizer(doc)
        with ctx:
            result = await agent_context.replace_node_for_agent(
                doc.id,
                node_id="n_taskloop",
                spec=kanban_spec,
            )

        assert result["ok"] is True, f"replace_node failed: {result.get('error')!r}"

        # The each loop is gone, replaced by a kanban at the same slot.
        children = doc.rippleSpec["ui"]["children"]
        kanban_node = children[1]  # same position the each loop was in
        assert kanban_node["type"] == "kanban"
        # The node id is preserved (replace_node policy) so the SSE
        # diff machinery can keep its element-anchor stable.
        assert kanban_node["id"] == "n_taskloop"

        # --- THE CORE CONTRACT ---
        # bind lives at the NODE level, not inside props.
        assert kanban_node.get("bind") == "state.tasks", (
            f"kanban bind must be at node level after replace_node + normalize, "
            f"got node={kanban_node!r}"
        )
        assert "bind" not in kanban_node.get("props", {}), (
            f"kanban bind must NOT be nested inside props — that would render "
            f"read-only and silently drop drag-drop writes (the bug the "
            f"interactive-by-default block in _pockets.py guards against). "
            f"props={kanban_node['props']!r}"
        )
        # The columns array (a regular prop) DOES live inside props.
        assert isinstance(kanban_node["props"].get("columns"), list)
        assert kanban_node["props"]["columnKey"] == "status"

        # The other interactive nodes are untouched — input still binds
        # to draft, both at node level.
        input_node = children[0]
        assert input_node["type"] == "input"
        assert input_node.get("bind") == "draft"
        assert "bind" not in input_node.get("props", {})

        # One SSE push fired for the node replacement.
        assert len(push_calls) == 1
        assert push_calls[0]["action"] == "node_replaced"
        # The subtree the push carries also has node-level bind.
        pushed_subtree = push_calls[0]["subtree"]
        assert pushed_subtree.get("bind") == "state.tasks"
        assert "bind" not in pushed_subtree.get("props", {})

        # Save fired exactly once.
        assert doc.saves == 1

    @pytest.mark.asyncio
    async def test_kanban_drag_drop_persists_to_state_tasks(self) -> None:
        """Programmatic drag-drop simulation: after the rebuild, the
        ``on_drop`` handler the kanban renderer wires writes to the
        same state path the node-level ``bind`` targets. We model this
        as a ``set_state`` write to ``tasks[0].status`` and verify the
        kanban's bind path is unchanged afterwards — i.e. the writeback
        path is still resolvable from the bound node.

        This is the spec-level equivalent of "drag card 1 from To Do to
        Done, refresh, card stays in Done": the state mutation lands at
        the path the bind reads from, so the rerender shows the new
        column placement."""
        doc = _todo_dashboard_doc()
        kanban_spec = {
            "type": "kanban",
            "bind": "state.tasks",
            "props": {
                "columns": [
                    {"key": "todo", "label": "To Do"},
                    {"key": "in_progress", "label": "In Progress"},
                    {"key": "done", "label": "Done"},
                ],
                "columnKey": "status",
            },
        }

        ctx, push_calls = _patches_with_real_normalizer(doc)
        with ctx:
            replace_result = await agent_context.replace_node_for_agent(
                doc.id,
                node_id="n_taskloop",
                spec=kanban_spec,
            )
            assert replace_result["ok"] is True

            # Sanity: bind is at node level before the drag-drop.
            kanban_before = doc.rippleSpec["ui"]["children"][1]
            assert kanban_before["bind"] == "state.tasks"
            assert "bind" not in kanban_before["props"]

            # The "drag-drop": user drags card 1 (status=todo) into the
            # "Done" column. The renderer's on_drop handler writes
            # ``state.tasks[0].status = "done"`` — the same path the
            # kanban's node-level bind targets.
            drop_result = await agent_context.set_state_for_agent(
                doc.id,
                "tasks[0].status",
                "done",
            )

        assert drop_result["ok"] is True
        assert drop_result["old_value"] == "todo"

        # State persisted to the bind target.
        tasks = doc.rippleSpec["state"]["tasks"]
        assert tasks[0]["status"] == "done"
        # The two other tasks are byte-identical — drag-drop is a
        # surgical edit, not a wholesale rewrite.
        assert tasks[1]["status"] == "in_progress"
        assert tasks[2]["status"] == "done"

        # Kanban bind is unchanged — same path, same node-level position.
        # If a future normalizer regression demoted bind into props on
        # subsequent writes, the writeback path would be silently
        # severed; this assertion catches that.
        kanban_after = doc.rippleSpec["ui"]["children"][1]
        assert kanban_after["bind"] == "state.tasks", (
            "kanban bind must survive subsequent state writes — without it "
            "the next render reads from the wrong path"
        )
        assert "bind" not in kanban_after["props"], (
            "bind must NOT migrate into props on subsequent writes"
        )

        # Two SSE pushes total: one for the rebuild, one for the drop.
        actions = [p["action"] for p in push_calls]
        assert actions == ["node_replaced", "state_set"]
        # The state_set push targets the same path the bind names.
        drop_push = push_calls[1]
        assert drop_push["path"] == "tasks[0].status"
        assert drop_push["value"] == "done"

        # Two saves total (replace + state mutation).
        assert doc.saves == 2

    @pytest.mark.asyncio
    async def test_normalizer_does_not_demote_kanban_bind_into_props(self) -> None:
        """Direct normalizer guard for the kanban rebuild scenario — even
        if the chat agent later (mistakenly) ships an update that nests
        ``bind`` inside ``props``, the spec we persist must surface the
        bind at node level. This is the symmetric of
        ``test_normalizer_preserves_bind_on_value_widgets`` (line 532),
        zoomed in on the specific path-form the kanban rebuild uses
        (``state.tasks`` not bare ``tasks``)."""
        from pocketpaw_ee.cloud.ripple_normalizer import normalize_ripple_spec

        spec = {
            "state": {"tasks": []},
            "ui": {
                "type": "flex",
                "props": {},
                "children": [
                    {
                        "type": "kanban",
                        "bind": "state.tasks",
                        "props": {
                            "columns": [],
                            "columnKey": "status",
                        },
                    },
                ],
            },
        }
        out = normalize_ripple_spec(spec)
        assert out is not None
        kanban = out["ui"]["children"][0]
        assert kanban["bind"] == "state.tasks"
        # Crucial: the normalizer must not move bind into props.
        assert "bind" not in kanban["props"]
