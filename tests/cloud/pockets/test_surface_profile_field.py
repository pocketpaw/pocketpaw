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
#
# Updated: 2026-06-06 (feat/entity-pocket-profile-field) — added the UPDATE
# path: ``UpdatePocketRequest`` carries the optional ``surface_profile`` and
# ``pockets_service.update`` honours three-way partial semantics — present +
# non-null SETs/REPLACEs, explicit ``null`` CLEARS, absent leaves the existing
# value unchanged (no clobber on unrelated edits). Tests pin all four cases
# end-to-end through Mongo (set, change, leave-unchanged, clear).
#
# Updated: 2026-06-08 (M3 v2 — create-time surface_profile derivation) — added
# the CREATE-TIME-DERIVATION round-trips: create() calls the conservative
# ``derive_create_time_profile`` helper when no explicit profile is supplied. The
# helper table is empty today, so an un-profiled create still persists None
# (zero regression); an explicit caller profile is still respected; and a
# monkeypatched rule proves a derived profile DOES persist through create().

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.models.pocket import Pocket, PocketSurfaceProfile
from pocketpaw_ee.cloud.pockets.dto import (
    CreatePocketRequest,
    PocketResponse,
    UpdatePocketRequest,
)
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


def test_update_pocket_request_surface_profile_three_way():
    """The update DTO distinguishes absent / explicit-null / populated.

    The three-way partial semantics ride on ``model_fields_set`` (the
    ``exclude_unset`` convention): ``None`` is BOTH the default and the
    "clear" signal, so absence is detected via whether the field was set.
    """
    # Absent — not in the partial update; leave-unchanged.
    absent = UpdatePocketRequest(name="x")
    assert "surface_profile" not in absent.model_fields_set
    assert absent.surface_profile is None

    # Explicit null — clear the override.
    cleared = UpdatePocketRequest(surfaceProfile=None)
    assert "surface_profile" in cleared.model_fields_set
    assert cleared.surface_profile is None

    # Populated — set/replace.
    populated = UpdatePocketRequest(
        surfaceProfile={"ripple_mode": "off", "skill_names": ["github"]}
    )
    assert "surface_profile" in populated.model_fields_set
    assert populated.surface_profile is not None
    assert populated.surface_profile.ripple_mode == "off"


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


# ---------------------------------------------------------------------------
# UPDATE path — set / change / leave-unchanged / clear (the auto-authoring
# foundation). Mirrors the create round-trip; verifies persistence by
# re-fetching by id, not echoing the update response.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_sets_surface_profile() -> None:
    """Updating an un-profiled pocket SETS the surface_profile (persisted)."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    created = await pockets_service.create(_WS, _USER, CreatePocketRequest(name="x"))
    assert created["surfaceProfile"] is None

    wire = await pockets_service.update(
        created["_id"],
        _USER,
        UpdatePocketRequest(surfaceProfile={"ripple_mode": "off", "skill_names": ["github"]}),
    )
    assert wire["surfaceProfile"]["ripple_mode"] == "off"
    assert wire["surfaceProfile"]["skill_names"] == ["github"]

    fetched = await pockets_service.get(created["_id"], _USER)
    assert fetched["surfaceProfile"]["ripple_mode"] == "off"
    assert fetched["surfaceProfile"]["skill_names"] == ["github"]


@pytest.mark.asyncio
async def test_update_changes_surface_profile() -> None:
    """Updating a profiled pocket REPLACES the surface_profile wholesale."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    created = await pockets_service.create(
        _WS,
        _USER,
        CreatePocketRequest(
            name="x", surface_profile={"ripple_mode": "off", "skill_names": ["github"]}
        ),
    )

    await pockets_service.update(
        created["_id"],
        _USER,
        UpdatePocketRequest(
            surfaceProfile={"ripple_mode": "trim", "deny_mcp_tool_ids": ["refund"]}
        ),
    )

    fetched = await pockets_service.get(created["_id"], _USER)
    assert fetched["surfaceProfile"]["ripple_mode"] == "trim"
    assert fetched["surfaceProfile"]["deny_mcp_tool_ids"] == ["refund"]
    # Replaced wholesale — the old skill_names are gone.
    assert fetched["surfaceProfile"]["skill_names"] == []


@pytest.mark.asyncio
async def test_update_absent_leaves_surface_profile_unchanged() -> None:
    """An unrelated edit (surface_profile absent) must NOT clobber the override."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    created = await pockets_service.create(
        _WS,
        _USER,
        CreatePocketRequest(
            name="x", surface_profile={"ripple_mode": "off", "skill_names": ["github"]}
        ),
    )

    # Edit only the name — surface_profile is not in the partial update.
    await pockets_service.update(created["_id"], _USER, UpdatePocketRequest(name="renamed"))

    fetched = await pockets_service.get(created["_id"], _USER)
    assert fetched["name"] == "renamed"
    assert fetched["surfaceProfile"]["ripple_mode"] == "off"
    assert fetched["surfaceProfile"]["skill_names"] == ["github"]


@pytest.mark.asyncio
async def test_update_explicit_null_clears_surface_profile() -> None:
    """An explicit null CLEARS the override so a pocket can be un-profiled."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    created = await pockets_service.create(
        _WS,
        _USER,
        CreatePocketRequest(
            name="x", surface_profile={"ripple_mode": "off", "skill_names": ["github"]}
        ),
    )

    await pockets_service.update(created["_id"], _USER, UpdatePocketRequest(surfaceProfile=None))

    fetched = await pockets_service.get(created["_id"], _USER)
    assert fetched["surfaceProfile"] is None


# ---------------------------------------------------------------------------
# M3 v2 — create-time derivation through create(). The helper table is empty
# today, so the live behaviour is: un-profiled create persists None, explicit
# caller profile is respected. A monkeypatched rule proves the WIRING — a
# derived profile reaches the persisted pocket.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_no_profile_no_mapping_persists_none() -> None:
    """No explicit profile + no matching rule (empty table) → persists None.

    This is the live zero-regression case: today's table is empty, so a
    type="site"/pattern="landing" pocket inherits the surface-kind default
    instead of carrying a redundant entity override.
    """
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    wire = await pockets_service.create(
        _WS,
        _USER,
        CreatePocketRequest(name="Landing", type="site", pattern="landing"),
    )
    assert wire["surfaceProfile"] is None
    fetched = await pockets_service.get(wire["_id"], _USER)
    assert fetched["surfaceProfile"] is None


@pytest.mark.asyncio
async def test_create_explicit_profile_never_overridden_by_derivation(monkeypatch) -> None:
    """An explicit caller profile wins even when a derivation rule WOULD match."""
    import pocketpaw_ee.cloud.pockets.create_profile_defaults as cpd
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    # Inject a rule that would fire for type="site" — it must be ignored because
    # the caller supplied an explicit profile.
    monkeypatch.setattr(
        cpd,
        "_CREATE_TIME_RULES",
        [(("site", None), lambda: cpd.PocketSurfaceProfile(skill_names=["derived"]))],
    )

    wire = await pockets_service.create(
        _WS,
        _USER,
        CreatePocketRequest(
            name="Landing",
            type="site",
            pattern="landing",
            surface_profile={"ripple_mode": "off", "skill_names": ["explicit"]},
        ),
    )
    fetched = await pockets_service.get(wire["_id"], _USER)
    assert fetched["surfaceProfile"]["ripple_mode"] == "off"
    assert fetched["surfaceProfile"]["skill_names"] == ["explicit"]


@pytest.mark.asyncio
async def test_create_derives_profile_when_rule_matches(monkeypatch) -> None:
    """A matching create-time rule (no explicit caller profile) → derived profile persists."""
    import pocketpaw_ee.cloud.pockets.create_profile_defaults as cpd
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    monkeypatch.setattr(
        cpd,
        "_CREATE_TIME_RULES",
        [(("site", "landing"), lambda: cpd.PocketSurfaceProfile(skill_names=["derived"]))],
    )

    wire = await pockets_service.create(
        _WS,
        _USER,
        CreatePocketRequest(name="Landing", type="site", pattern="landing"),
    )
    assert wire["surfaceProfile"]["skill_names"] == ["derived"]
    fetched = await pockets_service.get(wire["_id"], _USER)
    assert fetched["surfaceProfile"]["skill_names"] == ["derived"]
