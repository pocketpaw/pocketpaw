# test_surface_profile.py — SurfaceProfile descriptor + resolve_profile resolver.
#
# Created: 2026-06-05 (feat/surface-profile-bias-kill) — RED-first tests for the
# new typed SurfaceProfile primitive and its per-kind resolver, the data
# backbone of the "ripple-default bias" fix.
#
# A SurfaceProfile is the policy a surface carries: whether the ripple LAW
# (INLINE_RIPPLE_SYSTEM_PROMPT) applies (``ripple_mode``), which SDK tools are
# allowed, which MCP tool ids are denied, which skills are surfaced, and an
# optional system-message override. ``resolve_profile(kind, meta)`` is a pure
# table lookup keyed on SurfaceKind.
#
# Scope note (PR 1): only ``ripple_mode`` is CONSUMED in PR 1 — the
# build_behavior_instructions gate reads it to omit the ripple block on /sites.
# The ``deny_mcp_tool_ids`` / ``skill_names`` fields on the SITES row are
# DECLARED + tested DATA that PR 2 will wire (tool-deny + skill-surfacing); they
# are asserted here so the descriptor's shape is locked now and PR 2 only has to
# enforce, not re-design. Only the ``sites`` row changes behavior; every other
# kind AND any unmapped kind falls through to ``ripple_mode="on"`` (today's
# behavior) — zero regression by construction.

from __future__ import annotations

from pocketpaw_ee.cloud.surface import SurfaceKind, SurfaceMeta, SurfaceProfile, resolve_profile


def test_sites_profile_turns_ripple_off():
    """The /sites surface hand-authors a Svelte Paw Site — the ripple LAW is
    wrong there, so its profile turns ripple off."""
    profile = resolve_profile(SurfaceKind.SITES, SurfaceMeta())
    assert isinstance(profile, SurfaceProfile)
    assert profile.ripple_mode == "off"


def test_sites_profile_declares_deny_set_and_skill():
    """PR-2 DATA, declared now: the SITES profile denies the ripple-authoring
    MCP tools and surfaces the svelte-site skill. PR 1 does not yet enforce
    these — it only locks the shape so PR 2 wires enforcement, not design."""
    profile = resolve_profile(SurfaceKind.SITES, SurfaceMeta())
    assert profile.deny_mcp_tool_ids == frozenset(
        {
            "mcp__pocketpaw_sites_manager__create_landing_site",
            "mcp__pocketpaw_pocket_specialist__create",
        }
    )
    assert "create-svelte-site" in profile.skill_names


def test_generic_profile_keeps_ripple_on():
    """The catch-all GENERIC surface keeps ripple on — it's an ordinary chat
    surface where the widget LAW is correct."""
    profile = resolve_profile(SurfaceKind.GENERIC, SurfaceMeta())
    assert profile.ripple_mode == "on"


def test_unmapped_kind_defaults_to_ripple_on():
    """Any kind NOT in the table (e.g. CALENDAR) falls through to the safe
    default — ripple on. This is the zero-regression guarantee: every surface
    except /sites behaves exactly as it does today."""
    profile = resolve_profile(SurfaceKind.CALENDAR, SurfaceMeta())
    assert profile.ripple_mode == "on"


def test_default_profile_has_no_denies_or_skills():
    """The default (ripple-on) profile carries empty deny / skill sets and no
    SDK-tool allowlist — it imposes no surface-specific policy."""
    profile = resolve_profile(SurfaceKind.POCKETS_LIST, SurfaceMeta())
    assert profile.ripple_mode == "on"
    assert profile.deny_mcp_tool_ids == frozenset()
    assert profile.skill_names == frozenset()
    assert profile.allowed_sdk_tools is None
    assert profile.system_message_override is None
