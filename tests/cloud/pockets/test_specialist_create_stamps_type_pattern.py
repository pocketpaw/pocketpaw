# tests/cloud/pockets/test_specialist_create_stamps_type_pattern.py
# Created: 2026-06-04 (feat/sites-landing-brain) — pins that the
# pocket_specialist CREATE path (agent_create / persist_pocket) stamps
# ``type`` + ``pattern`` onto the persisted pocket.
#
# Background: P1 threaded ``pattern`` through the REST create() path
# (CreatePocketRequest -> service.create), but the marketing-site brain
# (pocketpaw-create-paw-site) does NOT use that path. It calls
# ``mcp__pocketpaw_pocket_specialist__create`` with hints
# ``type="site"`` + ``pattern="landing"``, which lands on the SEPARATE
# ``agent_create`` service method. That method defaulted ``type_="custom"``
# and never accepted ``pattern``, so the site intent was dropped and the
# pocket persisted as type="custom", pattern=None.
#
# These tests lock the wiring: agent_create must accept ``type_`` +
# ``pattern`` and persist both, while preserving today's defaults
# (type="custom", pattern=None) for callers that pass neither.
#
# Uses the shared ``mongo_db`` fixture (tests/cloud/conftest.py): Beanie
# over an in-memory Mongo with ALL_DOCUMENTS registered + an autouse
# RecordingBus so the agent_create emit() succeeds.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.pockets import service as pockets_service

pytestmark = pytest.mark.usefixtures("mongo_db")

_WS = "ws_specialist"
_USER = "user_specialist"

_MIN_SPEC = {
    "version": "1.0",
    "state": {},
    "ui": {"id": "n_root0001", "type": "flex", "props": {}, "children": []},
}


@pytest.mark.asyncio
async def test_agent_create_stamps_type_and_pattern() -> None:
    """The specialist create path persists type="site" + pattern="landing"
    when the marketing brain passes them as hints."""
    view, pocket_id, err = await pockets_service.agent_create(
        workspace_id=_WS,
        owner_id=_USER,
        name="Bright Smile Dental",
        description="Family dentist landing page",
        type_="site",
        pattern="landing",
        ripple_spec=_MIN_SPEC,
    )
    assert err is None
    assert pocket_id is not None
    assert view is not None
    assert view["type"] == "site"
    assert view["pattern"] == "landing"


@pytest.mark.asyncio
async def test_agent_create_type_pattern_survive_get_roundtrip() -> None:
    """type + pattern are stored on the doc, not just echoed back: fetch a
    fresh wire dict by id and both are still present."""
    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id=_WS,
        owner_id=_USER,
        name="Bakery landing",
        type_="site",
        pattern="landing",
        ripple_spec=_MIN_SPEC,
    )
    assert err is None
    assert pocket_id is not None
    fetched = await pockets_service.get(pocket_id, _USER)
    assert fetched["type"] == "site"
    assert fetched["pattern"] == "landing"


@pytest.mark.asyncio
async def test_agent_create_defaults_unchanged_backcompat() -> None:
    """A specialist create with no type/pattern keeps today's defaults:
    type="custom", pattern=None. Proves the change is additive."""
    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id=_WS,
        owner_id=_USER,
        name="Plain pocket",
        ripple_spec=_MIN_SPEC,
    )
    assert err is None
    assert pocket_id is not None
    fetched = await pockets_service.get(pocket_id, _USER)
    assert fetched["type"] == "custom"
    assert fetched["pattern"] is None


@pytest.mark.asyncio
async def test_agent_mode_validate_and_persist_threads_type_pattern() -> None:
    """End-to-end through the agent-mode adapter the marketing brain uses:
    hints {type:"site", pattern:"landing"} + a pre-drafted spec land on the
    persisted pocket. Locks the full wiring (hints model -> adapter ->
    persist tool -> agent_create), not just the deepest method."""
    from pocketpaw_ee.agent.pocket_specialist.adapters import _validate_and_persist
    from pocketpaw_ee.agent.pocket_specialist.runtime import (
        PocketSpecialistCreateInput,
        PocketSpecialistHints,
    )

    from pocketpaw.config import get_settings

    hints = PocketSpecialistHints(
        name="Bright Smile Dental",
        type="site",
        pattern="landing",
    )
    payload = PocketSpecialistCreateInput(
        brief="A family dentist landing page that captures appointment requests",
        hints=hints,
        spec=_MIN_SPEC,
    )

    out = await _validate_and_persist(
        payload,
        workspace_id=_WS,
        user_id=_USER,
        settings=get_settings(),
        started=0.0,
    )

    assert out.ok is True
    assert out.action == "created"
    assert out.pocket is not None
    assert out.pocket["type"] == "site"
    assert out.pocket["pattern"] == "landing"
    # And it's actually on the doc, not just echoed. The agent view dict
    # serializes the id by alias as ``_id``.
    pocket_id = out.pocket.get("_id") or out.pocket.get("id")
    assert pocket_id is not None
    fetched = await pockets_service.get(pocket_id, _USER)
    assert fetched["type"] == "site"
    assert fetched["pattern"] == "landing"
