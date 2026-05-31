# test_pocket_mutation_family.py — RFC-06 structure/data split (Increment 3).
# Created: 2026-05-22 — verifies that every granular ``agent_*`` edit op
#   emits a DISCRIMINATED ``pocket_mutation`` SSE frame: a node op carries
#   ``family == "updateComponents"`` (structure), a state op carries
#   ``family == "updateDataModel"`` (data). It also pins the additive
#   guarantee — the coarse ``PocketUpdated`` realtime event the service
#   layer emits on the same write is untouched, so older clients keep
#   getting their refetch signal.
"""Does each granular op emit the correctly-discriminated mutation frame?"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest


async def _make_pocket() -> Any:
    """A pocket with a heading node (for node ops) and a tasks array in
    state (for state ops)."""
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
    doc = Pocket(workspace="w1", name="Family", owner="u1", visibility="workspace", rippleSpec=spec)
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


# ---------------------------------------------------------------------------
# Node ops -> family "updateComponents"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_node_prop_set_emits_update_components_frame(mongo_db, agent_identity):
    """A ``set_node_prop`` op mutates ``rippleSpec.ui`` — its
    ``pocket_mutation`` frame must carry ``family == "updateComponents"``."""
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
    mutations = [d for n, d in _frames(sink) if n == "pocket_mutation"]
    assert len(mutations) == 1, "exactly one pocket_mutation frame per op"
    frame = mutations[0]
    assert frame["family"] == "updateComponents"
    assert frame["op"] == "node_prop_set"
    # The legacy un-discriminated keys still ride along (additive).
    assert frame["action"] == "node_prop_set"
    assert frame["node_id"] == "n_head0000"


@pytest.mark.asyncio
async def test_add_node_emits_update_components_frame(mongo_db, agent_identity):
    from pocketpaw_ee.cloud.chat.agent_service import (
        attach_sse_event_sink,
        detach_sse_event_sink,
    )
    from pocketpaw_ee.cloud.pockets import agent_context

    doc = await _make_pocket()
    sink: asyncio.Queue = asyncio.Queue()
    token = attach_sse_event_sink(sink)
    try:
        result = await agent_context.add_node_for_agent(
            str(doc.id),
            parent_id="n_root0000",
            spec={"type": "stat", "props": {"value": "42"}},
        )
    finally:
        detach_sse_event_sink(token)

    assert result["ok"] is True
    mutations = [d for n, d in _frames(sink) if n == "pocket_mutation"]
    assert len(mutations) == 1
    assert mutations[0]["family"] == "updateComponents"
    assert mutations[0]["op"] == "node_added"


# ---------------------------------------------------------------------------
# State ops -> family "updateDataModel"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_state_emits_update_data_model_frame(mongo_db, agent_identity):
    """A ``set_state`` op mutates ``rippleSpec.state`` — its
    ``pocket_mutation`` frame must carry ``family == "updateDataModel"``."""
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
    mutations = [d for n, d in _frames(sink) if n == "pocket_mutation"]
    assert len(mutations) == 1
    frame = mutations[0]
    assert frame["family"] == "updateDataModel"
    assert frame["op"] == "state_set"
    assert frame["action"] == "state_set"
    assert frame["path"] == "filter"


@pytest.mark.asyncio
async def test_append_state_emits_update_data_model_frame(mongo_db, agent_identity):
    from pocketpaw_ee.cloud.chat.agent_service import (
        attach_sse_event_sink,
        detach_sse_event_sink,
    )
    from pocketpaw_ee.cloud.pockets import agent_context

    doc = await _make_pocket()
    sink: asyncio.Queue = asyncio.Queue()
    token = attach_sse_event_sink(sink)
    try:
        result = await agent_context.append_state_for_agent(
            str(doc.id), "tasks", {"id": 2, "status": "todo"}
        )
    finally:
        detach_sse_event_sink(token)

    assert result["ok"] is True
    mutations = [d for n, d in _frames(sink) if n == "pocket_mutation"]
    assert len(mutations) == 1
    assert mutations[0]["family"] == "updateDataModel"
    assert mutations[0]["op"] == "state_appended"


# ---------------------------------------------------------------------------
# Additive guarantee — the coarse PocketUpdated event still fires.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_granular_op_still_emits_pocket_updated(mongo_db, agent_identity):
    """The discriminated frame is ADDITIVE — the service layer still
    emits the coarse ``PocketUpdated`` realtime event on the same write,
    so a client that ignores ``family`` keeps its refetch signal."""
    from unittest.mock import AsyncMock, patch

    from pocketpaw_ee.cloud.pockets import agent_context
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    doc = await _make_pocket()

    emitted: list[Any] = []

    async def _capture(event: Any) -> None:
        emitted.append(event)

    with patch.object(pockets_service, "emit", new=AsyncMock(side_effect=_capture)):
        result = await agent_context.set_state_for_agent(str(doc.id), "filter", "x")
    assert result["ok"] is True

    types = [getattr(e, "type", None) for e in emitted]
    assert "pocket.updated" in types, (
        f"granular op must still emit the coarse PocketUpdated event; saw {types}"
    )


# ---------------------------------------------------------------------------
# The PocketMutationFrame model itself.
# ---------------------------------------------------------------------------


def test_pocket_mutation_frame_to_wire_is_flat():
    """``PocketMutationFrame.to_wire`` flattens ``payload`` to the top
    level and adds the legacy ``action`` alias — so un-discriminated
    consumers keep working byte-for-byte. The plain-English ``kind``
    discriminant rides next to the A2UI ``family`` (RFC 06 position #2)."""
    from pocketpaw_ee.cloud.chat.agent_schemas import PocketMutationFrame, family_for_op

    frame = PocketMutationFrame(
        kind="data",
        family="updateDataModel",
        op="state_set",
        pocket_id="p1",
        payload={"path": "filter", "value": "done"},
    )
    wire = frame.to_wire()
    assert wire == {
        "action": "state_set",
        "op": "state_set",
        "kind": "data",
        "family": "updateDataModel",
        "pocket_id": "p1",
        "path": "filter",
        "value": "done",
    }
    assert family_for_op("node_moved") == "updateComponents"
    assert family_for_op("state_patched") == "updateDataModel"
    assert family_for_op("not_an_op") is None
