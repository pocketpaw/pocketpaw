"""Regression tests for pocket rippleSpec node-id stamping (#1172).

Changes: 2026-05-21 (#1172) — new file. Reproduces the bug where a
freshly created pocket has an id-less ``rippleSpec.ui`` tree, so the
chat agent fetching it via ``fetch_pocket_for_agent`` has no
``n_xxxxxxxx`` id to target with granular edit ops. Verifies node ids
are stamped at persist time (in ``normalize_ripple_spec``) and that
existing id-less pockets self-heal on first agent read.
"""

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.pockets import spec_ops


def _all_nodes(node: object) -> list[dict]:
    """Flatten every dict node in a UISpec tree (walks children +
    else_children)."""
    out: list[dict] = []
    if isinstance(node, dict):
        out.append(node)
        for key in ("children", "else_children"):
            kids = node.get(key)
            if isinstance(kids, list):
                for kid in kids:
                    out.extend(_all_nodes(kid))
    return out


def _assert_every_node_has_valid_id(ui: object) -> None:
    nodes = _all_nodes(ui)
    assert nodes, "expected at least one node in the ui tree"
    for n in nodes:
        nid = n.get("id")
        assert spec_ops.is_valid_id(nid), (
            f"node type={n.get('type')!r} has invalid/missing id {nid!r} "
            f"— expected an n_xxxxxxxx id"
        )
    # Ids must be unique across the tree.
    ids = [n["id"] for n in nodes]
    assert len(ids) == len(set(ids)), "node ids must be unique across the tree"


async def _make_pocket(**fields):
    """Insert a fresh Pocket through the normal Beanie path."""
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
    """Attach the default ``w1`` / ``u1`` SSE-stream identity so
    ``agent_view`` / ``_agent_load_doc`` pass their workspace +
    edit-access checks."""
    from pocketpaw_ee.cloud.chat.agent_service import (
        attach_agent_identity,
        detach_agent_identity,
    )

    tokens = attach_agent_identity(workspace_id="w1", user_id="u1")
    try:
        yield
    finally:
        detach_agent_identity(tokens)


# A nested UISpec tree as the drafting LLM produces it — no node ids
# anywhere, exactly the shape #1172 reproduces.
_ID_LESS_SPEC = {
    "version": "1.0",
    "state": {"draft": "", "todos": []},
    "ui": {
        "type": "flex",
        "props": {"direction": "column", "gap": 16},
        "children": [
            {"type": "heading", "props": {"text": "Todos", "level": 1}},
            {"type": "input", "bind": "draft", "props": {"placeholder": "Add..."}},
            {
                "type": "each",
                "items": "todos",
                "children": [
                    {"type": "text", "props": {"text": "{item.text}"}},
                ],
            },
        ],
    },
}


@pytest.mark.asyncio
async def test_fetch_pocket_for_agent_returns_node_ids(mongo_db, agent_identity):
    """A pocket created via the normal create/persist path must, when
    fetched through ``fetch_pocket_for_agent``, return a rippleSpec
    ``ui`` tree where EVERY node carries a valid ``n_xxxxxxxx`` id.

    Without ids the chat agent has nothing to put in ``parent_id`` /
    ``node_id`` and every granular edit op fails with
    ``no node with id X``.
    """
    from pocketpaw_ee.cloud.pockets.agent_context import fetch_pocket_for_agent
    from pocketpaw_ee.cloud.pockets.dto import CreatePocketRequest
    from pocketpaw_ee.cloud.pockets.service import create

    # Create a pocket exactly the way the create flow does — the spec
    # goes through normalize_ripple_spec on the way to MongoDB.
    body = CreatePocketRequest(
        name="Todos",
        description="",
        type="custom",
        icon="check-square",
        color="#0A84FF",
        visibility="workspace",
        ripple_spec=dict(_ID_LESS_SPEC),
    )
    created = await create("w1", "u1", body)
    # The wire dict keys the pocket id under ``_id`` (Mongo-style).
    pocket_id = str(created.get("_id") or created.get("id"))

    result = await fetch_pocket_for_agent(pocket_id)
    assert result["ok"] is True, result.get("error")

    spec = result["pocket"]["rippleSpec"]
    assert spec is not None, "fetched pocket has no rippleSpec"
    ui = spec.get("ui")
    assert isinstance(ui, dict), "fetched rippleSpec has no 'ui' root"

    _assert_every_node_has_valid_id(ui)


@pytest.mark.asyncio
async def test_agent_view_self_heals_legacy_id_less_pocket(mongo_db, agent_identity):
    """Defense-in-depth: a pocket already in MongoDB without node ids
    (persisted before the fix) must self-heal on first agent read so
    legacy pockets become editable without a DB migration."""
    from pocketpaw_ee.cloud.models.pocket import Pocket
    from pocketpaw_ee.cloud.pockets.service import agent_view

    # Insert the id-less spec straight onto the doc, bypassing the
    # normalizer — simulates a pocket persisted before the fix shipped.
    doc = await _make_pocket(rippleSpec=dict(_ID_LESS_SPEC))

    view, err = await agent_view(str(doc.id))
    assert err is None, err
    ui = view["rippleSpec"]["ui"]
    _assert_every_node_has_valid_id(ui)

    # The heal must be persisted, not just applied to the returned view.
    refreshed = await Pocket.get(doc.id)
    _assert_every_node_has_valid_id(refreshed.rippleSpec["ui"])
