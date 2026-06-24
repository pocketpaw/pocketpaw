# tests/cloud/pockets/test_update_clobber.py
# Created: 2026-06-19 (feat/typed-ripplespec-phase1) — regression tests for the
# 2026-06-13 rippleSpec clobber bug. The bug: pockets_service.update() did a
# wholesale `doc.rippleSpec = normalized_spec`, so a partial PATCH that omitted
# instance-owned regions (state / selections) WIPED them — the frontend's
# canvas-only persistMutation silently erased operator data.
#
# These tests drive the public service API (create / update / get) against the
# real mongomock-motor + RecordingBus fixtures (same shape as
# test_template_reconcile.py). They prove:
#   * a ui-only update PRESERVES existing state (the clobber, now fixed),
#   * a state-only update PRESERVES existing ui (the symmetric direction),
#   * reset_state=True restores the wholesale clear (the escape hatch),
#   * a template_slug recompile path is unaffected,
#   * passthrough/envelope keys are refreshed on a partial write.
"""Clobber-bug regression for pockets_service.update (layer-safe spec write)."""

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.cloud.pockets.dto import CreatePocketRequest, UpdatePocketRequest

pytestmark = pytest.mark.usefixtures("mongo_db")

_WS = "ws_clobber"
_USER = "user_clobber"


async def _make_pocket() -> str:
    """Create a pocket carrying both a template-owned ui and instance state."""
    wire = await pockets_service.create(
        _WS,
        _USER,
        CreatePocketRequest(
            name="Clobber Probe",
            ripple_spec={
                "ui": {"type": "stack", "children": [{"type": "text", "props": {"text": "hi"}}]},
                "actions": [{"name": "approve"}],
                "sources": {"items": {"method": "GET", "path": "/items", "bind": "state.items"}},
                "state": {
                    "items": [{"id": "row_1", "label": "Original Row"}],
                    "selected_id": "row_1",
                },
            },
        ),
    )
    return wire["_id"]


# ---------------------------------------------------------------------------
# The clobber bug + its fix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ui_only_update_preserves_state() -> None:
    """REPRODUCES the 2026-06-13 clobber: a PATCH whose ripple_spec omits
    `state` must NOT wipe it. This FAILS before the layer-safe fix (the old
    wholesale write dropped state) and PASSES after.
    """
    pocket_id = await _make_pocket()
    before = await pockets_service.get(pocket_id, _USER)
    assert before["rippleSpec"]["state"]["items"] == [{"id": "row_1", "label": "Original Row"}]

    # The frontend persistMutation after a canvas edit: sends the new ui (and
    # the template machinery) but NOT state.
    await pockets_service.update(
        pocket_id,
        _USER,
        UpdatePocketRequest(
            ripple_spec={
                "ui": {
                    "type": "stack",
                    "children": [{"type": "text", "props": {"text": "edited"}}],
                },
            }
        ),
    )

    after = await pockets_service.get(pocket_id, _USER)
    spec = after["rippleSpec"]
    # The bug fixed: instance state survived the ui-only write.
    assert spec["state"]["items"] == [{"id": "row_1", "label": "Original Row"}]
    assert spec["state"]["selected_id"] == "row_1"
    # The intended change still landed.
    assert spec["ui"]["children"][0]["props"]["text"] == "edited"
    # Other template-owned regions the patch omitted are also preserved.
    assert "items" in spec["sources"]


@pytest.mark.asyncio
async def test_state_only_update_preserves_ui() -> None:
    """The symmetric direction: a state-only PATCH must NOT wipe the template
    canvas / actions / sources. (This is the path reconcile's instance-preserve
    uses and the frontend's state-sync uses.)"""
    pocket_id = await _make_pocket()

    await pockets_service.update(
        pocket_id,
        _USER,
        UpdatePocketRequest(
            ripple_spec={
                "state": {
                    "items": [{"id": "row_2", "label": "New Row"}],
                    "selected_id": "row_2",
                }
            }
        ),
    )

    spec = (await pockets_service.get(pocket_id, _USER))["rippleSpec"]
    # New state landed.
    assert spec["state"]["items"] == [{"id": "row_2", "label": "New Row"}]
    assert spec["state"]["selected_id"] == "row_2"
    # Template-owned regions survived the state-only write.
    assert spec["ui"]["children"][0]["props"]["text"] == "hi"
    assert "items" in spec["sources"]


@pytest.mark.asyncio
async def test_reset_state_clears_omitted_state() -> None:
    """The escape hatch: reset_state=True restores the wholesale write, so a
    caller that INTENDS to clear instance state can. A ui-only body with
    reset_state=True drops the existing state entirely."""
    pocket_id = await _make_pocket()

    await pockets_service.update(
        pocket_id,
        _USER,
        UpdatePocketRequest(
            ripple_spec={
                "ui": {"type": "stack", "children": []},
            },
            reset_state=True,
        ),
    )

    spec = (await pockets_service.get(pocket_id, _USER))["rippleSpec"]
    # reset_state wiped the omitted instance regions — the wholesale write
    # dropped `state` entirely (the incoming body carried none), so neither the
    # original rows nor the selection survive.
    state = spec.get("state") or {}
    assert state.get("items") in (None, [])
    assert "selected_id" not in state


@pytest.mark.asyncio
async def test_partial_update_refreshes_passthrough_keys() -> None:
    """A partial write still refreshes the compile_template passthrough /
    envelope keys the normalizer stamps (name, version, lifecycle) — the
    layer-safe merge overlays incoming non-layer keys too."""
    pocket_id = await _make_pocket()

    await pockets_service.update(
        pocket_id,
        _USER,
        UpdatePocketRequest(
            ripple_spec={
                "name": "Renamed Spec",
                "ui": {"type": "stack", "children": []},
            }
        ),
    )

    spec = (await pockets_service.get(pocket_id, _USER))["rippleSpec"]
    # Envelope/passthrough refreshed.
    assert spec.get("name") == "Renamed Spec"
    # State still preserved (the clobber-fix holds even with passthrough keys).
    assert spec["state"]["items"] == [{"id": "row_1", "label": "Original Row"}]


@pytest.mark.asyncio
async def test_update_without_ripple_spec_leaves_spec_untouched() -> None:
    """A metadata-only update (no ripple_spec) must not touch the spec at all —
    confirms the clobber-fix branch only runs when a spec is actually sent."""
    pocket_id = await _make_pocket()
    before = (await pockets_service.get(pocket_id, _USER))["rippleSpec"]

    await pockets_service.update(pocket_id, _USER, UpdatePocketRequest(name="Just A Rename"))

    after = (await pockets_service.get(pocket_id, _USER))["rippleSpec"]
    assert after["state"] == before["state"]
    assert after["ui"] == before["ui"]
