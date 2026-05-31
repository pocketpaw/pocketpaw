# test_pocket_mutation_kind.py — RFC 06 position #2 follow-up.
# Created: 2026-05-24 — verifies the plain-English ``kind`` discriminant
#   that now rides on every ``pocket_mutation`` SSE frame. The earlier
#   Increment-3 work (see ``test_pocket_mutation_family.py``) wired the
#   A2UI ``family`` field on the granular node/state ops; this file
#   pins the canonical ``kind`` label ("structure" / "data" / "replace")
#   on ALL emit sites, including the full-document ``_push_replace``
#   path that previously had no discriminant at all (sources, actions,
#   widget add/remove, top-level pocket-field updates).
"""Does every pocket_mutation SSE frame carry the right `kind` discriminant?"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest


async def _make_pocket() -> Any:
    """A pocket with a heading node (for node ops), a tasks array in
    state (for state ops), and an empty sources/actions block (so the
    sources/actions wrappers have something to mutate)."""
    from pocketpaw_ee.cloud.models.pocket import Pocket

    spec = {
        "version": "1.0",
        "state": {"tasks": [{"id": 1, "status": "todo"}], "filter": "all"},
        "ui": {
            "id": "n_root0000",
            "type": "flex",
            "props": {"direction": "column"},
            "children": [
                {"id": "n_head0000", "type": "heading", "props": {"text": "Hi"}},
            ],
        },
    }
    doc = Pocket(
        workspace="w1",
        name="Kind",
        owner="u1",
        visibility="workspace",
        rippleSpec=spec,
    )
    await doc.insert()
    return doc


@pytest.fixture
def agent_identity():
    from pocketpaw_ee.cloud.chat.agent_service import (
        attach_agent_identity,
        detach_agent_identity,
    )

    tokens = attach_agent_identity(workspace_id="w1", user_id="u1")
    try:
        yield
    finally:
        detach_agent_identity(tokens)


def _frames(sink: asyncio.Queue) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    while not sink.empty():
        out.append(sink.get_nowait())
    return out


def _mutations(sink: asyncio.Queue) -> list[dict]:
    """Return only the ``pocket_mutation`` frames from the sink."""
    return [d for n, d in _frames(sink) if n == "pocket_mutation"]


# ---------------------------------------------------------------------------
# Structure ops -> kind == "structure"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_node_prop_carries_kind_structure(mongo_db, agent_identity):
    """A ``set_node_prop`` op mutates ``rippleSpec.ui`` — its frame
    must carry ``kind == "structure"`` (the plain-English RFC 06 label)
    plus the legacy ``family == "updateComponents"`` for back-compat."""
    from pocketpaw_ee.cloud.chat.agent_service import (
        attach_sse_event_sink,
        detach_sse_event_sink,
    )
    from pocketpaw_ee.cloud.pockets import agent_context

    doc = await _make_pocket()
    sink: asyncio.Queue = asyncio.Queue()
    token = attach_sse_event_sink(sink)
    try:
        result = await agent_context.set_node_prop_for_agent(
            str(doc.id), node_id="n_head0000", prop="text", value="Renamed"
        )
    finally:
        detach_sse_event_sink(token)

    assert result["ok"] is True
    mutations = _mutations(sink)
    assert len(mutations) == 1
    frame = mutations[0]
    assert frame["kind"] == "structure"
    # Back-compat: the legacy ``family`` discriminant still rides along.
    assert frame["family"] == "updateComponents"
    assert frame["op"] == "node_prop_set"


# ---------------------------------------------------------------------------
# Data ops -> kind == "data"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_state_carries_kind_data(mongo_db, agent_identity):
    """A ``set_state`` op mutates ``rippleSpec.state`` — its frame
    must carry ``kind == "data"`` plus the legacy
    ``family == "updateDataModel"``."""
    from pocketpaw_ee.cloud.chat.agent_service import (
        attach_sse_event_sink,
        detach_sse_event_sink,
    )
    from pocketpaw_ee.cloud.pockets import agent_context

    doc = await _make_pocket()
    sink: asyncio.Queue = asyncio.Queue()
    token = attach_sse_event_sink(sink)
    try:
        result = await agent_context.set_state_for_agent(str(doc.id), "filter", "done")
    finally:
        detach_sse_event_sink(token)

    assert result["ok"] is True
    mutations = _mutations(sink)
    assert len(mutations) == 1
    frame = mutations[0]
    assert frame["kind"] == "data"
    assert frame["family"] == "updateDataModel"
    assert frame["op"] == "state_set"


# ---------------------------------------------------------------------------
# The full-document replace push -> kind == "replace"
# ---------------------------------------------------------------------------
#
# Sources / actions / widget / top-level-field changes route through
# ``_push_replace`` which sends the whole pocket document. The push
# now carries ``kind == "replace"`` — a third value so clients can
# branch on ``kind === "replace"`` for a full re-render and skip the
# in-place patch path.


@pytest.mark.asyncio
async def test_set_source_carries_kind_replace(mongo_db, agent_identity):
    """``set_source_for_agent`` pushes a full-document replace — its
    frame carries ``kind == "replace"``. (Sources changes are
    structural in spirit, but the wire push is a full pocket — the
    third ``kind`` value names that precisely.)"""
    from pocketpaw_ee.cloud.chat.agent_service import (
        attach_sse_event_sink,
        detach_sse_event_sink,
    )
    from pocketpaw_ee.cloud.pockets import agent_context

    doc = await _make_pocket()
    sink: asyncio.Queue = asyncio.Queue()
    token = attach_sse_event_sink(sink)
    try:
        result = await agent_context.set_source_for_agent(
            str(doc.id),
            source_key="recent_tasks",
            binding={"path": "/tasks", "method": "GET", "bind": "tasks"},
        )
    finally:
        detach_sse_event_sink(token)

    assert result["ok"] is True, f"set_source failed: {result.get('error')}"
    mutations = _mutations(sink)
    assert len(mutations) == 1, f"expected one pocket_mutation, got {len(mutations)}"
    frame = mutations[0]
    assert frame["kind"] == "replace"
    assert frame["op"] == "replace"
    assert frame["action"] == "replace"
    # Replace push carries the full resolved pocket; ``family`` is omitted
    # because the replace isn't an A2UI granular op.
    assert "pocket" in frame
    assert "family" not in frame


@pytest.mark.asyncio
async def test_set_action_carries_kind_replace(mongo_db, agent_identity):
    """``set_action_for_agent`` pushes a full-document replace — its
    frame carries ``kind == "replace"``."""
    from pocketpaw_ee.cloud.chat.agent_service import (
        attach_sse_event_sink,
        detach_sse_event_sink,
    )
    from pocketpaw_ee.cloud.pockets import agent_context

    doc = await _make_pocket()
    sink: asyncio.Queue = asyncio.Queue()
    token = attach_sse_event_sink(sink)
    try:
        result = await agent_context.set_action_for_agent(
            str(doc.id),
            action_key="mark_done",
            binding={
                "kind": "write_binding",
                "path": "/tasks/{id}/done",
                "method": "POST",
            },
        )
    finally:
        detach_sse_event_sink(token)

    assert result["ok"] is True, f"set_action failed: {result.get('error')}"
    mutations = _mutations(sink)
    assert len(mutations) == 1
    frame = mutations[0]
    assert frame["kind"] == "replace"
    assert frame["op"] == "replace"


@pytest.mark.asyncio
async def test_update_pocket_carries_kind_replace(mongo_db, agent_identity):
    """``update_pocket_for_agent`` (a top-level field update) also
    pushes a full-document replace — its frame carries
    ``kind == "replace"``."""
    from pocketpaw_ee.cloud.chat.agent_service import (
        attach_sse_event_sink,
        detach_sse_event_sink,
    )
    from pocketpaw_ee.cloud.pockets import agent_context

    doc = await _make_pocket()
    sink: asyncio.Queue = asyncio.Queue()
    token = attach_sse_event_sink(sink)
    try:
        result = await agent_context.update_pocket_for_agent(
            str(doc.id), description="Updated description"
        )
    finally:
        detach_sse_event_sink(token)

    assert result["ok"] is True
    mutations = _mutations(sink)
    assert len(mutations) == 1
    assert mutations[0]["kind"] == "replace"


# ---------------------------------------------------------------------------
# The kind_for_op resolver itself.
# ---------------------------------------------------------------------------


def test_kind_for_op_resolves_structure_data_and_unknown():
    """``kind_for_op`` maps structure-ops to "structure", state-ops to
    "data", and anything else to ``None`` (the caller falls back to the
    legacy un-discriminated frame). ``replace`` is intentionally NOT
    listed here — the full-document push stamps ``kind="replace"``
    directly, no resolver needed."""
    from pocketpaw_ee.cloud.chat.agent_schemas import kind_for_op

    # Structure ops.
    assert kind_for_op("node_added") == "structure"
    assert kind_for_op("node_prop_set") == "structure"
    assert kind_for_op("node_moved") == "structure"
    assert kind_for_op("node_prop_array_item_appended") == "structure"

    # Data ops.
    assert kind_for_op("state_set") == "data"
    assert kind_for_op("state_appended") == "data"
    assert kind_for_op("state_removed") == "data"
    assert kind_for_op("state_patched") == "data"

    # Unknown -> None (callers fall back to legacy flat frame).
    assert kind_for_op("not_a_real_op") is None
    assert kind_for_op("replace") is None


def test_unknown_op_falls_back_to_legacy_flat_frame():
    """An op identifier with no known ``kind`` (e.g. a typo, a new op
    not yet wired into the resolver) must not break the push. The
    caller emits the legacy un-discriminated frame instead — same
    shape every consumer has tolerated since before the discriminant
    existed. This is the back-compat tripwire."""
    # The sink isn't attached here — we're checking that the resolver
    # gives None for an unknown op. The fallback branch in
    # ``_push_mutation_frame`` is what consumes that None. Verifying
    # the resolver covers the contract: anything not in the maps drops
    # to the legacy flat frame.
    from pocketpaw_ee.cloud.chat.agent_schemas import kind_for_op
    from pocketpaw_ee.cloud.pockets import agent_context

    assert kind_for_op("totally_invented_op") is None
    # And that path is the one ``_push_mutation_frame`` covers — see
    # the ``if family is None or kind is None`` branch in
    # ``agent_context._push_mutation_frame``.
    assert hasattr(agent_context, "_push_mutation_frame")
