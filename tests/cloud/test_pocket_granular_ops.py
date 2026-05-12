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

from ee.cloud.pockets import agent_context
from ee.cloud.pockets import service as pocket_service

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
            "ee.cloud.pockets.service._PocketDoc.get",
            new=AsyncMock(return_value=doc),
        )
    )
    stack.enter_context(patch("ee.cloud.pockets.service.emit", new=AsyncMock()))
    stack.enter_context(
        patch(
            "ee.cloud.pockets.service._pocket_event_payload",
            new=AsyncMock(return_value={"pocket_id": doc.id}),
        )
    )
    # The MCP wrapper imports lazily from ee.cloud.chat.agent_service.
    # Patch where it's looked up.
    stack.enter_context(
        patch(
            "ee.cloud.chat.agent_service.push_pocket_mutation",
            new=MagicMock(side_effect=_capture),
        )
    )
    # normalize_ripple_spec is fine to leave as-is, but it pulls in
    # the manifest module on first import; patching to identity keeps
    # the test hermetic and fast.
    stack.enter_context(
        patch(
            "ee.cloud.pockets.service.normalize_ripple_spec",
            new=lambda s: s,
        )
    )
    # _agent_load_doc reads workspace + user from ContextVars and checks
    # access. Default to the matching owner so the existing tests cover
    # the happy path; cross-workspace + access-denied behavior gets its
    # own dedicated tests at the bottom of this file.
    stack.enter_context(
        patch(
            "ee.cloud.chat.agent_service.current_workspace_id",
            new=MagicMock(return_value=doc.workspace),
        )
    )
    stack.enter_context(
        patch(
            "ee.cloud.chat.agent_service.current_user_id",
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
            "ee.cloud.chat.agent_service.current_workspace_id",
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
            "ee.cloud.chat.agent_service.current_user_id",
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
            "ee.cloud.chat.agent_service.current_user_id",
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
                "ee.cloud.chat.agent_service.current_workspace_id",
                new=MagicMock(return_value=None),
            ),
            patch(
                "ee.cloud.chat.agent_service.current_user_id",
                new=MagicMock(return_value=None),
            ),
        ):
            result = await agent_context.set_node_prop_for_agent(
                fake_doc.id, node_id="n_header00", prop="text", value="x"
            )
    assert result["ok"] is False
    assert "no active workspace/user" in result["error"]
    assert push_calls == []
