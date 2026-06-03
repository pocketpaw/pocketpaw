# tests/cloud/pockets/test_pattern_field.py
# Created: 2026-06-03 (feat/sites-landing-brain, Task P1) — pins that the
# create-pocket layout ``pattern`` persists end-to-end as a first-class
# Pocket field. A site authored by the marketing brain stamps
# ``type="site"`` + ``pattern="landing"`` so the sites generator can tell
# a landing page apart from a dashboard. The field is optional: legacy
# pockets (no pattern) read back ``None`` with NO Mongo migration.
#
# create() returns the legacy wire dict (camelCase / snake mix via
# ``pocket_to_wire_dict``), not a domain object — ``pattern`` is a single
# word so the wire key stays ``"pattern"``. Assertions are against that
# wire dict, the same way ``test_gallery_site_exclusion.py`` reads
# ``wire["_id"]``.
#
# Uses the shared ``mongo_db`` fixture (tests/cloud/conftest.py): Beanie
# over an in-memory Mongo with ALL_DOCUMENTS registered + an autouse
# RecordingBus so the create() emit() succeeds.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.cloud.pockets.dto import CreatePocketRequest

pytestmark = pytest.mark.usefixtures("mongo_db")

_WS = "ws_pattern"
_USER = "user_pattern"


@pytest.mark.asyncio
async def test_create_persists_pattern() -> None:
    """A site pocket stamped with pattern="landing" reads it back."""
    wire = await pockets_service.create(
        _WS,
        _USER,
        CreatePocketRequest(name="Dentist site", type="site", pattern="landing"),
    )
    assert wire["type"] == "site"
    assert wire["pattern"] == "landing"


@pytest.mark.asyncio
async def test_pattern_defaults_none_backcompat() -> None:
    """A pocket created without a pattern reads back None — no migration."""
    wire = await pockets_service.create(_WS, _USER, CreatePocketRequest(name="x"))
    assert wire["pattern"] is None


@pytest.mark.asyncio
async def test_pattern_survives_get_roundtrip() -> None:
    """The pattern persists in Mongo: fetch a fresh wire dict by id and the
    pattern is still there (proves it's stored on the doc, not just echoed
    back from the create body)."""
    created = await pockets_service.create(
        _WS,
        _USER,
        CreatePocketRequest(name="Bakery landing", type="site", pattern="landing"),
    )
    fetched = await pockets_service.get(created["_id"], _USER)
    assert fetched["pattern"] == "landing"


@pytest.mark.asyncio
async def test_create_from_ripple_spec_stamps_pattern() -> None:
    """The inline auto-create path accepts an optional pattern and stamps it."""
    spec = {
        "version": "1.0",
        "state": {},
        "ui": {"id": "n_root0001", "type": "flex", "props": {}, "children": []},
    }
    pocket_id = await pockets_service.create_from_ripple_spec(
        _WS, _USER, spec, description="landing", pattern="landing"
    )
    assert pocket_id is not None
    fetched = await pockets_service.get(pocket_id, _USER)
    assert fetched["pattern"] == "landing"
