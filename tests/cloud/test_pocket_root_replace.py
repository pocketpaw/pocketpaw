# tests/cloud/test_pocket_root_replace.py
# Created: 2026-05-21 — reproduction + regression coverage for the
# agent-mode root-replace bug: calling ``replace_node`` on the ROOT node
# of a pocket reported ``ok=true, action="applied"`` but the swap did
# not persist.
#
# The reproduction uses the REAL Beanie doc (mongomock via the
# ``mongo_db`` fixture) so it exercises genuine ``doc.save()`` and a real
# re-fetch from the database — the in-memory fake doc used by
# ``test_pocket_granular_ops.py`` shares one Python object, so a "swap
# lost on persist" gap could never surface there.
#
# Two fixes are covered here:
#   1. ``spec_ops.replace_node`` now handles the root by mutating it in
#      place (``root.clear()/update()``) instead of raising — the
#      partially-applied fix this task kept.
#   2. ``adapters._apply_ops`` no longer reports ``ok=True,
#      action="applied"`` when ZERO ops actually applied. Before the
#      fix, the root ``replace_node`` op was rejected by the service,
#      the rejection went into ``warnings``, and the run still claimed
#      ``applied`` — the silent failure the captain observed. A run
#      whose only op was rejected now returns ``ok=False,
#      action="failed"``.
"""Regression tests for replacing the ROOT node of a pocket's UI tree."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pocketpaw_ee.cloud.pockets import service as pocket_service

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_bare_dashboard_pocket() -> Any:
    """Insert a real Pocket whose UI root is a single bare
    ``project-dashboard`` — the exact shape the bug report names: a
    single-widget pocket that needs its root wrapped in a ``flex`` so a
    sibling section can be added beside it.
    """
    from pocketpaw_ee.cloud.models.pocket import Pocket

    spec = {
        "version": "1.0",
        "lifecycle": {"type": "persistent", "id": "pocket-abc123"},
        "title": "Delivery",
        "name": "Delivery",
        "color": "#0A84FF",
        "metadata": {"category": "custom", "color": "#0A84FF"},
        "ui": {
            "id": "n_root0000",
            "type": "project-dashboard",
            "props": {"title": "Delivery"},
        },
    }
    doc = Pocket(
        workspace="w1",
        name="Delivery",
        description="",
        type="custom",
        icon="",
        color="",
        owner="u1",
        visibility="workspace",
        rippleSpec=spec,
    )
    await doc.insert()
    return doc


def _flex_wrapping(root_node: dict[str, Any]) -> dict[str, Any]:
    """Build a ``flex`` replacement whose first child is the original
    root — the canonical "wrap a bare root so a sibling fits" shape."""
    return {
        "type": "flex",
        "props": {"direction": "column", "gap": 16},
        "children": [
            copy.deepcopy(root_node),
            {"type": "section", "props": {"title": "New section"}},
        ],
    }


@pytest.fixture
def agent_identity():
    """Attach the ``w1`` / ``u1`` per-stream identity that
    ``_agent_load_doc`` reads for its workspace + edit-access checks."""
    from pocketpaw_ee.cloud.chat.agent_service import (
        attach_agent_identity,
        detach_agent_identity,
    )

    tokens = attach_agent_identity(workspace_id="w1", user_id="u1")
    try:
        yield
    finally:
        detach_agent_identity(tokens)


# ---------------------------------------------------------------------------
# Service-level reproduction — agent_replace_node on the ROOT, then a real
# re-fetch from the database.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_root_replace_persists_via_service(mongo_db, agent_identity):
    """REPRO: replacing the root via ``agent_replace_node`` must persist
    to the database.

    Before the fix this failed — the re-fetched pocket still showed the
    original bare ``project-dashboard`` root, not the ``flex`` wrapper.
    """
    doc = await _make_bare_dashboard_pocket()
    pocket_id = str(doc.id)
    original_root = copy.deepcopy(doc.rippleSpec["ui"])
    assert original_root["type"] == "project-dashboard"

    result, err = await pocket_service.agent_replace_node(
        pocket_id,
        node_id=original_root["id"],
        spec=_flex_wrapping(original_root),
    )

    assert err is None, f"agent_replace_node reported an error: {err}"
    assert result is not None

    # Re-fetch from the database — a genuinely fresh load, not the same
    # Python object. This is what the canvas / chat re-fetch sees.
    view, view_err = await pocket_service.agent_view(pocket_id)
    assert view_err is None
    assert view is not None

    ui = view["rippleSpec"]["ui"]
    assert ui["type"] == "flex", (
        f"root swap did not persist — re-fetched UI root is still {ui.get('type')!r}"
    )
    assert isinstance(ui.get("children"), list) and len(ui["children"]) == 2
    assert ui["children"][0]["type"] == "project-dashboard"
    assert ui["children"][1]["type"] == "section"


@pytest.mark.asyncio
async def test_nonroot_replace_still_persists(mongo_db, agent_identity):
    """REGRESSION GUARD: a non-root replace must keep persisting."""
    from pocketpaw_ee.cloud.models.pocket import Pocket

    spec = {
        "version": "1.0",
        "ui": {
            "id": "n_root0000",
            "type": "flex",
            "props": {"direction": "column"},
            "children": [
                {"id": "n_header00", "type": "heading", "props": {"text": "Hi"}},
            ],
        },
    }
    doc = Pocket(
        workspace="w1",
        name="Two",
        owner="u1",
        visibility="workspace",
        rippleSpec=spec,
    )
    await doc.insert()
    pocket_id = str(doc.id)

    result, err = await pocket_service.agent_replace_node(
        pocket_id,
        node_id="n_header00",
        spec={"type": "stat", "props": {"value": "42"}},
    )
    assert err is None
    assert result is not None

    view, view_err = await pocket_service.agent_view(pocket_id)
    assert view_err is None
    child = view["rippleSpec"]["ui"]["children"][0]
    assert child["type"] == "stat"
    assert child["id"] == "n_header00"  # id preserved


# ---------------------------------------------------------------------------
# Agent-mode reproduction — the full EditAgentModeAdapter op path.
#
# This is the path the captain's deployment runs: pocket_specialist_mode=
# agent on the Claude Code backend. The chat agent hands back a
# ``replace_node`` op; ``_apply_ops`` dispatches it through the same
# granular edit tools the subagent flow uses.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_root_replace_via_agent_mode_adapter(mongo_db, agent_identity):
    """REPRO (agent-mode): an agent-mode ``replace_node`` op targeting the
    root must persist the swap to the database."""
    from pocketpaw_ee.agent.pocket_specialist.adapters import EditAgentModeAdapter
    from pocketpaw_ee.agent.pocket_specialist.runtime import PocketSpecialistEditInput

    from pocketpaw.config import Settings

    doc = await _make_bare_dashboard_pocket()
    pocket_id = str(doc.id)
    original_root = copy.deepcopy(doc.rippleSpec["ui"])

    edit_input = PocketSpecialistEditInput(
        pocket_id=pocket_id,
        intent="Wrap the dashboard in a flex so I can add a section beside it.",
        ops=[
            {
                "op": "replace_node",
                "args": {
                    "node_id": original_root["id"],
                    "spec": _flex_wrapping(original_root),
                },
            }
        ],
    )

    adapter = EditAgentModeAdapter()
    out = await adapter.edit(
        edit_input,
        workspace_id="w1",
        user_id="u1",
        settings=Settings(),
    )

    assert out.ok is True, f"agent-mode edit reported failure: {out.error}"
    assert not out.warnings, f"agent-mode edit produced warnings: {out.warnings}"

    view, view_err = await pocket_service.agent_view(pocket_id)
    assert view_err is None
    ui = view["rippleSpec"]["ui"]
    assert ui["type"] == "flex", (
        f"agent-mode root swap did not persist — re-fetched root is still {ui.get('type')!r}"
    )
    assert len(ui["children"]) == 2


@pytest.mark.asyncio
async def test_agent_mode_reports_failure_when_no_op_applied(mongo_db, agent_identity):
    """ACCOUNTING GUARD: when every supplied op is rejected and ZERO ops
    apply, the agent-mode edit must report ``ok=False, action="failed"``
    — not the silent ``ok=True, action="applied"`` that hid the
    root-replace rejection.

    A ``replace_node`` op targeting a node id that does not exist is
    rejected by the service (``{ok: false}``, not a raised exception) —
    the exact rejection class that previously slipped through as a
    misleading ``applied``.
    """
    from pocketpaw_ee.agent.pocket_specialist.adapters import EditAgentModeAdapter
    from pocketpaw_ee.agent.pocket_specialist.runtime import PocketSpecialistEditInput

    from pocketpaw.config import Settings

    doc = await _make_bare_dashboard_pocket()
    pocket_id = str(doc.id)

    edit_input = PocketSpecialistEditInput(
        pocket_id=pocket_id,
        intent="Replace a node that isn't there.",
        ops=[
            {
                "op": "replace_node",
                "args": {"node_id": "n_ghost000", "spec": {"type": "flex"}},
            }
        ],
    )

    adapter = EditAgentModeAdapter()
    out = await adapter.edit(
        edit_input,
        workspace_id="w1",
        user_id="u1",
        settings=Settings(),
    )

    assert out.ok is False, "rejected-only run must report ok=False"
    assert out.action == "failed", (
        f"rejected-only run must report action='failed', got {out.action!r}"
    )
    assert out.error, "a failed run must carry an error explaining why"
    assert out.warnings, "the per-op rejection reason must surface in warnings"
    assert out.ops == [], "no ops were applied"
