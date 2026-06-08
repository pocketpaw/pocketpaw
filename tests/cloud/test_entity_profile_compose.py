# tests/cloud/test_entity_profile_compose.py
# Created: 2026-06-06 (feat/entity-pocket-profile-field, entity-rooms chunk ①)
# — RED-first tests for ``compose_entity_profile``, the PURE (no-I/O) helper
# that folds an entity pocket's ``PocketSurfaceProfile`` override (the
# JSON-shaped dict on ``Pocket.surface_profile``) OVER a base ``SurfaceProfile``
# resolved from the surface kind. The compose precedence (the entity-rooms
# payoff): ripple_mode entity-wins-if-set else base; deny_mcp_tool_ids UNION;
# allowed_sdk_tools UNION; skill_names UNION; system_message_override
# entity-wins-if-set. A ``None`` / empty override is identity (zero regression).

from __future__ import annotations

from pocketpaw_ee.cloud.surface import SurfaceProfile
from pocketpaw_ee.cloud.surface.service import compose_entity_profile


def test_compose_none_override_is_identity():
    """No entity override (``None``) returns the base unchanged — the
    no-pocket / legacy path. This is the zero-regression guarantee."""
    base = SurfaceProfile(ripple_mode="on", deny_mcp_tool_ids=frozenset({"x"}))
    assert compose_entity_profile(base, None) is base


def test_compose_empty_override_is_identity_equivalent():
    """An override dict with no opinions (all defaults) composes to a profile
    equal to the base — it adds nothing."""
    base = SurfaceProfile(ripple_mode="on", deny_mcp_tool_ids=frozenset({"x"}))
    out = compose_entity_profile(base, {})
    assert out.ripple_mode == "on"
    assert out.deny_mcp_tool_ids == frozenset({"x"})


def test_compose_ripple_entity_wins_when_set():
    """The entity's ``ripple_mode`` overrides the base when present."""
    base = SurfaceProfile(ripple_mode="on")
    out = compose_entity_profile(base, {"ripple_mode": "off"})
    assert out.ripple_mode == "off"


def test_compose_ripple_falls_back_to_base_when_none():
    """``ripple_mode=None`` means 'no opinion' → keep the base ripple."""
    base = SurfaceProfile(ripple_mode="off")
    out = compose_entity_profile(base, {"ripple_mode": None})
    assert out.ripple_mode == "off"


def test_compose_deny_is_union():
    """deny is the UNION: the entity's denies ADD to the base's (hard cap
    grows, never shrinks)."""
    base = SurfaceProfile(ripple_mode="on", deny_mcp_tool_ids=frozenset({"base_tool"}))
    out = compose_entity_profile(base, {"deny_mcp_tool_ids": ["entity_tool"]})
    assert out.deny_mcp_tool_ids == frozenset({"base_tool", "entity_tool"})


def test_compose_skill_names_union():
    """skill_names is a UNION — the entity adds its skills to the base set."""
    base = SurfaceProfile(ripple_mode="on", skill_names=frozenset({"base_skill"}))
    out = compose_entity_profile(base, {"skill_names": ["github", "base_skill"]})
    assert out.skill_names == frozenset({"base_skill", "github"})


def test_compose_allowed_sdk_tools_union():
    """allowed_sdk_tools is a UNION across base and entity. ``None`` on a side
    means 'no allowlist contribution' (that side adds nothing)."""
    base = SurfaceProfile(ripple_mode="on", allowed_sdk_tools=frozenset({"Read"}))
    out = compose_entity_profile(base, {"allowed_sdk_tools": ["WebFetch"]})
    assert out.allowed_sdk_tools == frozenset({"Read", "WebFetch"})


def test_compose_allowed_sdk_tools_none_both_sides_stays_none():
    """When neither base nor entity set an allowlist, the result stays ``None``
    (no surface restriction) — not an empty frozenset (which would deny all)."""
    base = SurfaceProfile(ripple_mode="on", allowed_sdk_tools=None)
    out = compose_entity_profile(base, {"allowed_sdk_tools": None})
    assert out.allowed_sdk_tools is None


def test_compose_allowed_sdk_tools_entity_only():
    """Entity sets an allowlist, base does not → the entity's set wins as the
    allowlist (base contributes nothing)."""
    base = SurfaceProfile(ripple_mode="on", allowed_sdk_tools=None)
    out = compose_entity_profile(base, {"allowed_sdk_tools": ["WebFetch"]})
    assert out.allowed_sdk_tools == frozenset({"WebFetch"})


def test_compose_system_message_override_entity_wins():
    """The entity's ``system_message_override`` wins when set, else base's."""
    base = SurfaceProfile(ripple_mode="on", system_message_override="base msg")
    out = compose_entity_profile(base, {"system_message_override": "entity msg"})
    assert out.system_message_override == "entity msg"

    out2 = compose_entity_profile(base, {"system_message_override": None})
    assert out2.system_message_override == "base msg"


def test_compose_full_entity_over_sites_base():
    """End-to-end: a populated entity override folded over a non-trivial base
    (e.g. a /sites svelte-create base with its own deny set). Ripple entity
    wins; the deny sets UNION."""
    base = SurfaceProfile(
        ripple_mode="off",
        deny_mcp_tool_ids=frozenset({"base_deny"}),
        skill_names=frozenset({"create-svelte-site"}),
    )
    out = compose_entity_profile(
        base,
        {
            "ripple_mode": "on",
            "deny_mcp_tool_ids": ["refund"],
            "skill_names": ["github"],
            "allowed_sdk_tools": ["WebFetch"],
            "system_message_override": "be terse",
        },
    )
    assert out.ripple_mode == "on"  # entity wins
    assert out.deny_mcp_tool_ids == frozenset({"base_deny", "refund"})  # union
    assert out.skill_names == frozenset({"create-svelte-site", "github"})  # union
    assert out.allowed_sdk_tools == frozenset({"WebFetch"})
    assert out.system_message_override == "be terse"
