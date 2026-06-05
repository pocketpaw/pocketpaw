# tests/cloud/pockets/test_surface_profile_field.py
# Created: 2026-06-05 (feat/entity-pocket-profile-field, entity-rooms chunk ②)
# — pins the optional per-entity ``surface_profile`` override on the Pocket
# end-to-end: the Beanie model, the create-pocket DTO / PocketResponse, and
# the create() wire round-trip. The field MIRRORS the surface-domain
# ``SurfaceProfile`` (ripple_mode / allowed_sdk_tools / deny_mcp_tool_ids /
# skill_names / system_message_override) with JSON-friendly types (lists, not
# frozensets) so chunk ①'s entity-aware resolve_profile can do roughly
# ``SurfaceProfile(**pocket.surface_profile)``.
#
# The field is OPTIONAL: default ``None`` → zero behaviour change for every
# existing pocket. legacy pockets (no surface_profile) read back ``None`` with
# NO Mongo migration. Assertions follow the test_pattern_field.py precedent
# (wire-dict round-trip via the shared ``mongo_db`` fixture) plus pure
# model/DTO unit checks that need no Mongo.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.models.pocket import Pocket, PocketSurfaceProfile
from pocketpaw_ee.cloud.pockets.dto import CreatePocketRequest, PocketResponse
from pocketpaw_ee.cloud.surface.domain import SurfaceProfile

# ---------------------------------------------------------------------------
# Pure model / DTO unit checks (no Mongo)
# ---------------------------------------------------------------------------


def test_pocket_surface_profile_defaults_to_none():
    """An existing-shape pocket carries no surface_profile — zero behaviour change."""
    p = Pocket.model_construct(workspace="w1", name="n", owner="u1")
    assert p.surface_profile is None


def test_pocket_surface_profile_roundtrips_populated():
    """A populated surface_profile round-trips with all sub-fields optional."""
    p = Pocket.model_construct(
        workspace="w1",
        name="n",
        owner="u1",
        surface_profile=PocketSurfaceProfile(
            ripple_mode="off",
            skill_names=["github"],
        ),
    )
    assert p.surface_profile is not None
    assert p.surface_profile.ripple_mode == "off"
    assert p.surface_profile.skill_names == ["github"]
    # Untouched fields stay at their optional defaults.
    assert p.surface_profile.allowed_sdk_tools is None
    assert p.surface_profile.deny_mcp_tool_ids == []
    assert p.surface_profile.system_message_override is None


def test_pocket_surface_profile_accepts_plain_dict():
    """Validation coerces a plain JSON-ish dict (the Mongo / wire shape)."""
    p = Pocket(
        workspace="w1",
        name="n",
        owner="u1",
        surface_profile={"ripple_mode": "off", "skill_names": ["github"]},
    )
    assert p.surface_profile.ripple_mode == "off"
    assert p.surface_profile.skill_names == ["github"]


def test_surface_profile_feeds_surface_domain_surfaceprofile():
    """Chunk ① consumes the field as ``SurfaceProfile(**pocket.surface_profile)``.

    The JSON-friendly lists must hydrate the frozenset-typed descriptor.
    """
    sub = PocketSurfaceProfile(
        ripple_mode="off",
        allowed_sdk_tools=["WebFetch"],
        deny_mcp_tool_ids=["pocket_authoring"],
        skill_names=["github"],
        system_message_override="be terse",
    )
    payload = sub.model_dump(exclude_none=True)
    # Mirror chunk ①'s hydration: lists → frozensets where the descriptor wants them.
    profile = SurfaceProfile(
        ripple_mode=payload["ripple_mode"],
        allowed_sdk_tools=frozenset(payload["allowed_sdk_tools"]),
        deny_mcp_tool_ids=frozenset(payload["deny_mcp_tool_ids"]),
        skill_names=frozenset(payload["skill_names"]),
        system_message_override=payload["system_message_override"],
    )
    assert profile.ripple_mode == "off"
    assert profile.skill_names == frozenset({"github"})
    assert profile.deny_mcp_tool_ids == frozenset({"pocket_authoring"})


def test_create_pocket_request_surface_profile_optional():
    """The create DTO accepts an optional surface_profile, defaulting to None."""
    req = CreatePocketRequest(name="x")
    assert req.surface_profile is None

    req2 = CreatePocketRequest(
        name="x",
        surface_profile={"ripple_mode": "off", "skill_names": ["github"]},
    )
    assert req2.surface_profile is not None
    assert req2.surface_profile.ripple_mode == "off"


def test_pocket_response_serializes_surface_profile():
    """PocketResponse carries the field; default None, populated round-trips."""
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    base = dict(
        id="p1",
        workspace="ws1",
        name="Test Pocket",
        description="desc",
        type="custom",
        icon="",
        color="",
        owner="u1",
        visibility="private",
        team=[],
        agents=[],
        widgets=[],
        shared_with=[],
        created_at=now,
        updated_at=now,
    )
    resp_none = PocketResponse(**base)
    assert resp_none.surface_profile is None

    resp = PocketResponse(
        **base,
        surface_profile={"ripple_mode": "off", "skill_names": ["github"]},
    )
    dumped = resp.model_dump()
    assert dumped["surface_profile"]["ripple_mode"] == "off"
    assert dumped["surface_profile"]["skill_names"] == ["github"]


# ---------------------------------------------------------------------------
# Mongo wire round-trip (mirrors test_pattern_field.py)
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.usefixtures("mongo_db")

_WS = "ws_surface_profile"
_USER = "user_surface_profile"


@pytest.mark.asyncio
async def test_create_persists_surface_profile() -> None:
    """A pocket created with a surface_profile reads it back on the wire."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    wire = await pockets_service.create(
        _WS,
        _USER,
        CreatePocketRequest(
            name="Orders entity",
            surface_profile={"ripple_mode": "off", "skill_names": ["github"]},
        ),
    )
    assert wire["surfaceProfile"]["ripple_mode"] == "off"
    assert wire["surfaceProfile"]["skill_names"] == ["github"]


@pytest.mark.asyncio
async def test_surface_profile_defaults_none_backcompat() -> None:
    """A pocket created without a surface_profile reads back None — no migration."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    wire = await pockets_service.create(_WS, _USER, CreatePocketRequest(name="x"))
    assert wire["surfaceProfile"] is None


@pytest.mark.asyncio
async def test_surface_profile_survives_get_roundtrip() -> None:
    """The surface_profile persists in Mongo (fetched fresh by id, not echoed)."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    created = await pockets_service.create(
        _WS,
        _USER,
        CreatePocketRequest(
            name="Bright Smile entity",
            surface_profile={"ripple_mode": "trim", "deny_mcp_tool_ids": ["refund"]},
        ),
    )
    fetched = await pockets_service.get(created["_id"], _USER)
    assert fetched["surfaceProfile"]["ripple_mode"] == "trim"
    assert fetched["surfaceProfile"]["deny_mcp_tool_ids"] == ["refund"]
