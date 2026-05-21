"""Integration tests for the granular ``rippleSpec.state`` mutation ops.

Exercises the agent-facing ``*_state_for_agent`` wrappers in
``ee/cloud/pockets/agent_context.py``. Mocks the Beanie doc fetch +
event emit; captures SSE pushes.

Canonical regression — "update one task's status in a 100-task list" —
locks down that a state edit emits ONE small SSE frame and never falls
back to a whole-pocket rewrite.
"""

from __future__ import annotations

import copy
from contextlib import ExitStack
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pocketpaw_ee.cloud.pockets import agent_context
from pocketpaw_ee.cloud.pockets import service as pocket_service


class _FakeDoc:
    """Standalone fake; doesn't reuse the granular_ops fixture so this
    file stays runnable in isolation."""

    def __init__(self, pocket_id: str, ripple_spec: dict[str, Any]):
        self.id = pocket_id
        self.workspace = "w1"
        self.name = "Test"
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
            "rippleSpec": self.rippleSpec,
            "owner": self.owner,
        }


@pytest.fixture
def fake_doc():
    return _FakeDoc(
        "507f1f77bcf86cd799439011",
        {
            "version": "1.0",
            "state": {
                "filter": "all",
                "user": {"name": "alice"},
                "tasks": [
                    {"id": "t1", "label": "buy milk", "status": "todo"},
                    {"id": "t2", "label": "walk dog", "status": "done"},
                ],
            },
            "ui": {"id": "n_root0000", "type": "flex", "children": []},
        },
    )


def _patches(doc: _FakeDoc):
    """Patch the doc fetch + emit/push seams + identity ContextVars.

    ``_agent_load_doc`` reads workspace/user from per-stream ContextVars
    and rejects when they don't match the pocket's tenancy. Default to
    the doc's own workspace + owner so the happy-path tests pass; the
    auth-gate tests at the bottom of this file override these mocks.

    Returns ``(ExitStack, push_calls)`` — use as ``with ctx: ...``.
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
# set_state
# ---------------------------------------------------------------------------


async def test_set_state_top_level(fake_doc):
    ctx, push_calls = _patches(fake_doc)
    with ctx:
        result = await agent_context.set_state_for_agent(fake_doc.id, "filter", "done")
    assert result["ok"] is True
    assert result["old_value"] == "all"
    assert fake_doc.rippleSpec["state"]["filter"] == "done"
    push = push_calls[0]
    assert push["action"] == "state_set"
    assert push["path"] == "filter"
    assert push["value"] == "done"
    assert push["old_value"] == "all"


async def test_set_state_array_index(fake_doc):
    ctx, push_calls = _patches(fake_doc)
    with ctx:
        result = await agent_context.set_state_for_agent(fake_doc.id, "tasks[0].status", "done")
    assert result["ok"] is True
    assert fake_doc.rippleSpec["state"]["tasks"][0]["status"] == "done"
    assert push_calls[0]["path"] == "tasks[0].status"


async def test_set_state_creates_intermediates(fake_doc):
    """Writing a deep path through a missing branch creates dicts on
    the way. Lets the agent set up nested state without a separate
    bootstrap call."""
    ctx, _ = _patches(fake_doc)
    with ctx:
        result = await agent_context.set_state_for_agent(
            fake_doc.id, "ui_prefs.theme.color", "blue"
        )
    assert result["ok"] is True
    assert fake_doc.rippleSpec["state"]["ui_prefs"]["theme"]["color"] == "blue"


async def test_set_state_out_of_range_errors(fake_doc):
    ctx, _ = _patches(fake_doc)
    with ctx:
        result = await agent_context.set_state_for_agent(fake_doc.id, "tasks[99].label", "x")
    assert result["ok"] is False
    assert "out of range" in result["error"]


async def test_set_state_creates_state_when_pocket_has_none():
    """A brand-new pocket without an explicit ``state`` field still
    accepts state writes — the service materialises an empty dict."""
    doc = _FakeDoc(
        "507f1f77bcf86cd799439022",
        {"version": "1.0", "ui": {"id": "n_root0000", "type": "flex"}},
    )
    ctx, _ = _patches(doc)
    with ctx:
        result = await agent_context.set_state_for_agent(doc.id, "count", 1)
    assert result["ok"] is True
    assert doc.rippleSpec["state"] == {"count": 1}


# ---------------------------------------------------------------------------
# append_state
# ---------------------------------------------------------------------------


async def test_append_state_grows_array(fake_doc):
    ctx, push_calls = _patches(fake_doc)
    with ctx:
        result = await agent_context.append_state_for_agent(
            fake_doc.id, "tasks", {"id": "t3", "label": "pay bills", "status": "todo"}
        )
    assert result["ok"] is True
    assert result["new_length"] == 3
    assert fake_doc.rippleSpec["state"]["tasks"][-1]["id"] == "t3"
    push = push_calls[0]
    assert push["action"] == "state_appended"
    assert push["new_length"] == 3


async def test_append_state_creates_missing_list(fake_doc):
    ctx, _ = _patches(fake_doc)
    with ctx:
        result = await agent_context.append_state_for_agent(fake_doc.id, "tags", "important")
    assert result["ok"] is True
    assert fake_doc.rippleSpec["state"]["tags"] == ["important"]


async def test_append_state_to_non_list_errors(fake_doc):
    ctx, _ = _patches(fake_doc)
    with ctx:
        result = await agent_context.append_state_for_agent(fake_doc.id, "filter", "x")
    assert result["ok"] is False


# ---------------------------------------------------------------------------
# remove_state
# ---------------------------------------------------------------------------


async def test_remove_state_pops_array_element(fake_doc):
    ctx, push_calls = _patches(fake_doc)
    with ctx:
        result = await agent_context.remove_state_for_agent(fake_doc.id, "tasks[0]")
    assert result["ok"] is True
    assert result["removed"]["id"] == "t1"
    assert [t["id"] for t in fake_doc.rippleSpec["state"]["tasks"]] == ["t2"]
    assert push_calls[0]["action"] == "state_removed"


async def test_remove_state_deletes_key(fake_doc):
    ctx, _ = _patches(fake_doc)
    with ctx:
        result = await agent_context.remove_state_for_agent(fake_doc.id, "filter")
    assert result["ok"] is True
    assert "filter" not in fake_doc.rippleSpec["state"]


async def test_remove_state_missing_errors(fake_doc):
    ctx, _ = _patches(fake_doc)
    with ctx:
        result = await agent_context.remove_state_for_agent(fake_doc.id, "ghost")
    assert result["ok"] is False


# ---------------------------------------------------------------------------
# patch_state
# ---------------------------------------------------------------------------


async def test_patch_state_merges_top_level(fake_doc):
    ctx, push_calls = _patches(fake_doc)
    with ctx:
        result = await agent_context.patch_state_for_agent(
            fake_doc.id, {"filter": "done", "draft": ""}
        )
    assert result["ok"] is True
    assert result["previous"] == {"filter": "all", "draft": None}
    assert fake_doc.rippleSpec["state"]["filter"] == "done"
    assert fake_doc.rippleSpec["state"]["draft"] == ""
    # Untouched keys preserved.
    assert fake_doc.rippleSpec["state"]["user"]["name"] == "alice"
    assert push_calls[0]["action"] == "state_patched"


# ---------------------------------------------------------------------------
# Canonical regression — one task in 100, one SSE frame
# ---------------------------------------------------------------------------


async def test_update_one_task_in_100_emits_one_state_set_frame():
    """The whole point of state-ops: changing one task's status in a
    100-task list fires ONE small SSE frame, never falls back to
    rewriting the spec, and leaves the other 99 tasks byte-identical."""
    tasks = [{"id": f"t{i:03d}", "label": f"task {i}", "status": "todo"} for i in range(100)]
    spec = {
        "version": "1.0",
        "state": {"filter": "all", "tasks": tasks},
        "ui": {"id": "n_root0000", "type": "table", "bind": "{state.tasks}"},
    }
    doc = _FakeDoc("507f1f77bcf86cd799439088", spec)
    snapshot = copy.deepcopy(spec)

    # Trip-wire: if any state edit falls back to whole-spec rewrite, fail.
    update_sentinel = AsyncMock(side_effect=AssertionError("agent_update must NOT be called"))

    ctx, push_calls = _patches(doc)
    with (
        ctx,
        patch.object(pocket_service, "agent_update", new=update_sentinel),
    ):
        result = await agent_context.set_state_for_agent(doc.id, "tasks[42].status", "done")

    assert result["ok"] is True
    assert update_sentinel.call_count == 0

    # Exactly one SSE frame, with subtree-sized payload.
    assert len(push_calls) == 1
    push = push_calls[0]
    assert push["action"] == "state_set"
    assert push["path"] == "tasks[42].status"
    assert push["value"] == "done"
    assert push["old_value"] == "todo"

    # All 99 other tasks byte-identical.
    for i, task in enumerate(doc.rippleSpec["state"]["tasks"]):
        if i == 42:
            continue
        assert task == snapshot["state"]["tasks"][i], f"task {i} drifted"

    # And the UI tree untouched.
    assert doc.rippleSpec["ui"] == snapshot["ui"]
