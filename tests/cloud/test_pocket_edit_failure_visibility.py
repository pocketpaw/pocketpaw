# test_pocket_edit_failure_visibility.py — Regression for the
#   "chat agent claims success, canvas stays stale" bug.
# Created: 2026-05-23 (fix/pocket-edit-not-visible)
#
# The captain hit this on pocket 69e99a910f928e4735de9efc: the chat
# agent fabricated a parent_id (``n_1nchlml3``) and passed it to
# ``pocket_specialist__edit`` as the target of an ``add_node`` op.
# The service correctly rejected with "no node with id 'n_1nchlml3'"
# and the agent-mode adapter correctly returned
# ``ok=False, action="failed", warnings=[<reason>]``. But the MCP
# tool serialized that body and returned it WITHOUT setting
# ``is_error: True`` — so at the Claude tool-use layer it looked
# like a successful tool call, and the chat agent then frequently
# replied "1 op applied" while the canvas stayed stale.
#
# The fix flips ``is_error`` on the MCP response whenever
# ``out.ok`` is False. This test wires up a real (mongomock-motor)
# pocket, drives the granular ops path end-to-end, and asserts the
# is_error flag is set. Fails on origin/dev, passes after the fix.

from __future__ import annotations

import json
from typing import Any

import pytest

pytest.importorskip("pocketpaw_ee")


@pytest.fixture
def agent_identity():
    """Bind workspace/user identity for ContextVar lookups inside the
    granular agent_* helpers. Same pattern as
    ``test_pocket_mutation_family.py::agent_identity``."""
    from pocketpaw_ee.cloud.chat.agent_service import (
        attach_agent_identity,
        detach_agent_identity,
    )

    tokens = attach_agent_identity(workspace_id="w1", user_id="u1")
    try:
        yield
    finally:
        detach_agent_identity(tokens)


async def _make_pocket() -> Any:
    """A minimal pocket with a known root id and a chart child — same
    shape as the captain's "Quick Test" pocket. The chat agent in the
    bug repro fabricated ``n_1nchlml3`` as the root id even though the
    real root was different; we reproduce that exact mismatch below."""
    from pocketpaw_ee.cloud.models.pocket import Pocket

    spec = {
        "version": "1.0",
        "ui": {
            "id": "n_root0000",
            "type": "flex",
            "props": {"direction": "column"},
            "children": [
                {
                    "id": "n_chart000",
                    "type": "chart",
                    "props": {"type": "bar"},
                }
            ],
        },
    }
    doc = Pocket(
        workspace="w1",
        name="Quick Test",
        owner="u1",
        visibility="workspace",
        rippleSpec=spec,
    )
    await doc.insert()
    return doc


@pytest.mark.asyncio
async def test_edit_handler_marks_is_error_when_every_op_is_rejected(mongo_db, agent_identity):
    """The captain's bug, end-to-end.

    Send an ``add_node`` op whose ``parent_id`` does not exist on the
    pocket. Expect:

    * The MCP response has ``is_error: True`` so the chat agent's
      tool-use framework treats it as a failure (and the agent cannot
      fabricate a confident "applied" reply past it).
    * The serialized body still carries ``ok=False`` + the rejection
      reason in ``warnings`` so the chat agent can relay the cause.
    * Nothing persists — the pocket spec is unchanged.
    """
    from pocketpaw_ee.agent.pocket_specialist.mcp_tool import _edit_handler

    doc = await _make_pocket()
    pocket_id = str(doc.id)

    payload = await _edit_handler(
        {
            "pocket_id": pocket_id,
            "intent": (
                "Append a new line chart as the last child of the root "
                "flex (n_1nchlml3), below the existing bar chart."
            ),
            "ops": [
                {
                    "op": "add_node",
                    "args": {
                        # The actual root is ``n_root0000``; this id is
                        # the chat agent's hallucination — matches the
                        # captain's repro.
                        "parent_id": "n_1nchlml3",
                        "spec": {
                            "type": "chart",
                            "props": {"type": "line", "height": 180},
                        },
                    },
                }
            ],
        }
    )

    # The headline assertion — the bug.
    assert payload.get("is_error") is True, (
        "An add_node with a stale parent_id must surface as is_error so "
        "the chat agent cannot fabricate success while the canvas stays "
        "stale (pocket-edit-not-visible regression)."
    )

    # The body still carries the diagnostic detail.
    body = json.loads(payload["content"][0]["text"])
    assert body["ok"] is False
    assert body["action"] == "failed"
    assert body["ops"] == []
    assert body["warnings"], "the rejection reason must ride along"
    assert any("n_1nchlml3" in w for w in body["warnings"])

    # And nothing got persisted — the pocket spec is untouched.
    from pocketpaw_ee.cloud.models.pocket import Pocket

    refreshed = await Pocket.get(doc.id)
    assert refreshed is not None
    ui = refreshed.rippleSpec["ui"]
    # Still one child (the original chart) — no line chart appended.
    assert len(ui["children"]) == 1
    assert ui["children"][0]["id"] == "n_chart000"


@pytest.mark.asyncio
async def test_edit_handler_does_not_flag_is_error_on_real_apply(mongo_db, agent_identity):
    """An ``add_node`` against the REAL parent_id persists the new
    child and returns ``ok=True``. The MCP response must NOT carry
    ``is_error`` — that would make every successful apply look like a
    tool error and trigger spurious retries by the framework."""
    from pocketpaw_ee.agent.pocket_specialist.mcp_tool import _edit_handler

    doc = await _make_pocket()
    pocket_id = str(doc.id)

    payload = await _edit_handler(
        {
            "pocket_id": pocket_id,
            "intent": "Append a line chart at the end of the root flex",
            "ops": [
                {
                    "op": "add_node",
                    "args": {
                        "parent_id": "n_root0000",
                        "spec": {
                            "type": "chart",
                            "props": {"type": "line", "height": 180},
                        },
                    },
                }
            ],
        }
    )

    assert "is_error" not in payload, "A successful apply must not be flagged as a tool error"
    body = json.loads(payload["content"][0]["text"])
    assert body["ok"] is True
    assert len(body["ops"]) == 1
    assert body["ops"][0]["op"] == "add_node"

    # Persisted — the new child landed.
    from pocketpaw_ee.cloud.models.pocket import Pocket

    refreshed = await Pocket.get(doc.id)
    assert refreshed is not None
    ui = refreshed.rippleSpec["ui"]
    assert len(ui["children"]) == 2
    # New chart was appended after the existing bar chart.
    assert ui["children"][-1]["type"] == "chart"
    assert ui["children"][-1]["props"]["type"] == "line"
