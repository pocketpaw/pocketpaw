# tests/cloud/test_pocket_edit_action_gate.py
# 2026-05-23 — Post-apply action-wiring gate on the agent-mode edit path.
#
# Why this file exists. PR #1196 added ``validate_action_wiring_strict``
# inside ``_gate_catalog`` so an agent-create that authors a fictitious
# action verb (``action: "fetch"``) gets bounced with a corrective hint
# and ``is_error: true`` (the chat agent's retry signal). But the
# edit path runs through ``EditAgentModeAdapter._apply_ops``, which
# commits granular ops via ``update_pocket`` — and that path uses
# ``_gate_catalog(strict=False)`` so violations only LOG, never raise.
#
# Captain verified this gap live: after #1196 merged, asking the
# specialist to "fix the broken Refresh button" produced a new spec
# where the verb was renamed from ``fetch`` to ``backend_fetch`` —
# same loophole, fresh disguise — and the run reported success because
# no strict gate ran at the end of the batch.
#
# This file covers the follow-up: after _apply_ops finishes, validate
# the assembled spec strictly and surface ``ok=False`` + the
# corrective hint so the chat agent's ``is_error`` path (#1190) fires
# a retry instead of accepting the broken assembly.

from __future__ import annotations

from typing import Any

import pytest


async def _insert_pocket_with_root() -> Any:
    """Insert a minimal pocket whose UI root is a ``flex`` ready to
    accept children. The test uses ``add_node`` ops so the service
    controls the new child ids — no need to pre-bake ids in the
    fixture (the existing pocket-granular tests do the same).
    """
    from pocketpaw_ee.cloud.models.pocket import Pocket

    spec = {
        "version": "1.0",
        "lifecycle": {"type": "persistent", "id": "pocket-edit-gate"},
        "title": "Pets Tracker",
        "name": "Pets Tracker",
        "color": "#0A84FF",
        "metadata": {"category": "custom"},
        "ui": {
            "id": "n_rootblob",
            "type": "flex",
            "props": {"direction": "column", "gap": "12px"},
            "children": [],
        },
    }
    doc = Pocket(
        workspace="w1",
        name="Pets Tracker",
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


@pytest.fixture
def agent_identity():
    """Attach the ``w1`` / ``u1`` per-stream identity that
    ``_agent_load_doc`` reads for its workspace + edit-access checks.
    """
    from pocketpaw_ee.cloud.chat.agent_service import (
        attach_agent_identity,
        detach_agent_identity,
    )

    tokens = attach_agent_identity(workspace_id="w1", user_id="u1")
    try:
        yield
    finally:
        detach_agent_identity(tokens)


@pytest.fixture(autouse=True)
def _stub_catalog_so_strict_gate_can_run(monkeypatch):
    """The post-apply gate runs inside ``_gate_catalog`` which needs a
    manifest. Stub the manifest lookup to a list that includes every
    type the test uses, so the catalog walk passes cleanly and any
    failure that surfaces comes from the action-wiring gate (the
    behaviour we're actually testing).
    """
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    async def _stub():
        return ["flex", "heading", "button", "text", "table", "data-grid", "if", "each"]

    monkeypatch.setattr(pockets_service, "_catalog_allowed_types", _stub)


# ---------------------------------------------------------------------------
# The captain's Test-D loophole — invented verb post-rename
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invented_verb_in_edit_returns_ok_false(mongo_db, agent_identity):
    """REGRESSION: an edit that adds a Refresh button whose ``on_click``
    uses a fictitious verb (``action: "backend_fetch"`` — the exact
    rename the captain observed after the prompt-only fix) must
    report ``ok=False`` so the chat agent retries instead of
    accepting the broken assembly. The granular op itself lands in
    Mongo — the post-apply strict gate is what surfaces the failure.
    """
    from pocketpaw_ee.agent.pocket_specialist.adapters import EditAgentModeAdapter
    from pocketpaw_ee.agent.pocket_specialist.runtime import PocketSpecialistEditInput

    from pocketpaw.config import Settings

    doc = await _insert_pocket_with_root()
    pocket_id = str(doc.id)

    edit_input = PocketSpecialistEditInput(
        pocket_id=pocket_id,
        intent="Add a Refresh button that fetches /pet/1 from the backend.",
        ops=[
            {
                "op": "add_node",
                "args": {
                    "parent_id": "n_rootblob",
                    "spec": {
                        "type": "button",
                        "props": {
                            "label": "Refresh",
                            "on_click": {
                                "action": "backend_fetch",
                                "endpoint": "/pet/1",
                                "target": "pet_rows",
                            },
                        },
                    },
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

    assert out.ok is False, (
        f"agent-mode edit must reject an invented verb but reported ok=True; "
        f"error={out.error!r} warnings={out.warnings!r}"
    )
    assert out.error and "backend_fetch" in out.error, (
        f"corrective hint must name the offending verb; got error={out.error!r}"
    )
    # The corrective hint must teach the agent the right shape, not
    # just say "rejected" — that's what unblocks the next retry.
    assert any(verb in (out.error or "") for verb in ("run_source", "api")), (
        f"hint must recommend a real verb; got error={out.error!r}"
    )


# ---------------------------------------------------------------------------
# Negative — a properly-wired edit still passes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proper_run_source_edit_passes(mongo_db, agent_identity):
    """The gate does NOT false-positive on a properly-wired edit. A
    Refresh button added via ``add_node`` whose ``on_click`` points
    at a declared source must succeed."""
    from pocketpaw_ee.agent.pocket_specialist.adapters import EditAgentModeAdapter
    from pocketpaw_ee.agent.pocket_specialist.runtime import PocketSpecialistEditInput

    from pocketpaw.config import Settings

    doc = await _insert_pocket_with_root()
    # Author a source up front so the run_source reference resolves.
    doc.rippleSpec["sources"] = {
        "pets": {
            "method": "GET",
            "path": "/pet/findByStatus?status=available",
            "bind": "state.pets",
            "refresh": ["pocket_open", "manual"],
        }
    }
    await doc.save()
    pocket_id = str(doc.id)

    edit_input = PocketSpecialistEditInput(
        pocket_id=pocket_id,
        intent="Add a Refresh button wired to the pets source.",
        ops=[
            {
                "op": "add_node",
                "args": {
                    "parent_id": "n_rootblob",
                    "spec": {
                        "type": "button",
                        "props": {
                            "label": "Refresh",
                            "on_click": {"action": "run_source", "source": "pets"},
                        },
                    },
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

    assert out.ok is True, (
        f"properly-wired edit reported failure; error={out.error!r} warnings={out.warnings!r}"
    )
    assert not out.error


# ---------------------------------------------------------------------------
# Negative — non-live edits do not invoke the gate at all
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_live_edit_unaffected(mongo_db, agent_identity):
    """A "Save draft" button using ``action: "set"`` is properly
    wired and not live-claiming — the gate must leave it alone.
    """
    from pocketpaw_ee.agent.pocket_specialist.adapters import EditAgentModeAdapter
    from pocketpaw_ee.agent.pocket_specialist.runtime import PocketSpecialistEditInput

    from pocketpaw.config import Settings

    doc = await _insert_pocket_with_root()
    pocket_id = str(doc.id)

    edit_input = PocketSpecialistEditInput(
        pocket_id=pocket_id,
        intent="Add a save-draft button.",
        ops=[
            {
                "op": "add_node",
                "args": {
                    "parent_id": "n_rootblob",
                    "spec": {
                        "type": "button",
                        "props": {
                            "label": "Save draft",
                            "on_click": {
                                "action": "set",
                                "target": "draft.saved",
                                "value": True,
                            },
                        },
                    },
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

    assert out.ok is True, (
        f"non-live edit incorrectly failed; error={out.error!r} warnings={out.warnings!r}"
    )
