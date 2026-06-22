# tests/cloud/surface/test_surface_registry.py — SR-2 registry guarantees.
#
# Created: 2026-06-22 (feat/surface-registry-backend-profiles, SR-2) — guards the
# two SR-2 additions to the declarative SURFACES registry:
#   1. ``_assert_registry_complete`` — the startup consistency check that the
#      registry is a clean 1:1 with ``SurfaceKind`` (no orphan rows, no dupes,
#      no missing kinds). Tests inject a mutated row list and assert it RAISES.
#   2. The SITES meta-fork now lives on the SITES row's ``profile_resolver``.
#      We re-pin the three-mode branching through the public ``resolve_profile``
#      to prove the registry sources the EXACT pre-SR-2 behavior: svelte-create
#      drops ripple + denies the two ripple-create tools; ripple-create and
#      refine keep ripple and deny nothing (refine wins over engine).

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.surface import SurfaceKind, SurfaceMeta, SurfaceProfile, resolve_profile
from pocketpaw_ee.cloud.surface.surface_registry import (
    SURFACES,
    SurfaceSpec,
    _assert_registry_complete,
    _route_for,
    generic,
)

_SITES_SVELTE_CREATE_DENY = frozenset(
    {
        "mcp__pocketpaw_sites_manager__create_landing_site",
        "mcp__pocketpaw_pocket_specialist__create",
    }
)


# ---------------------------------------------------------------------------
# Completeness assertion (the SR design's "keep the enum + assert" answer).
# ---------------------------------------------------------------------------


def test_real_registry_passes_completeness_check():
    """The shipped SURFACES list is a clean 1:1 with SurfaceKind — the assertion
    that runs at import must accept it without raising."""
    _assert_registry_complete()  # default arg = the real SURFACES
    _assert_registry_complete(SURFACES)


def test_completeness_check_fails_on_missing_kind():
    """Drop a kind's row → the assertion fires (a SurfaceKind with no spec)."""
    incomplete = [s for s in SURFACES if s.kind is not SurfaceKind.GENERIC]
    with pytest.raises(AssertionError, match="missing rows"):
        _assert_registry_complete(incomplete)


def test_completeness_check_fails_on_duplicate_kind():
    """Add a second row for an existing kind → the assertion fires (dupe)."""
    dupe_row = SurfaceSpec(SurfaceKind.HOME, _route_for(SurfaceKind.HOME), generic.build_preamble)
    with pytest.raises(AssertionError, match="duplicate rows"):
        _assert_registry_complete([*SURFACES, dupe_row])


def test_completeness_check_fails_on_orphan_kind():
    """A row whose ``kind`` is not a real SurfaceKind → the assertion fires."""

    class _Bogus:
        value = "bogus"

    orphan = SurfaceSpec(_Bogus(), "/bogus", generic.build_preamble)  # type: ignore[arg-type]
    with pytest.raises(AssertionError, match="non-SurfaceKind"):
        _assert_registry_complete([*SURFACES, orphan])


# ---------------------------------------------------------------------------
# SITES meta-fork spot-check — sourced from the SITES row's profile_resolver.
# ---------------------------------------------------------------------------


def test_sites_svelte_create_drops_ripple_and_denies():
    """svelte-create (engine="svelte", no pocket_id) → ripple OFF + deny set +
    create-svelte-site skill. The ONLY /sites mode that loses ripple."""
    profile = resolve_profile(SurfaceKind.SITES, SurfaceMeta(engine="svelte"))
    assert isinstance(profile, SurfaceProfile)
    assert profile.ripple_mode == "off"
    assert profile.deny_mcp_tool_ids == _SITES_SVELTE_CREATE_DENY
    assert "create-svelte-site" in profile.skill_names


def test_sites_ripple_create_keeps_ripple():
    """ripple-create (engine None/"ripple", no pocket_id) → ripple ON, no deny."""
    for meta in (SurfaceMeta(), SurfaceMeta(engine="ripple")):
        profile = resolve_profile(SurfaceKind.SITES, meta)
        assert profile.ripple_mode == "on", f"{meta!r} must keep ripple"
        assert profile.deny_mcp_tool_ids == frozenset(), f"{meta!r} must deny nothing"


def test_sites_refine_keeps_ripple_and_wins_over_engine():
    """refine (pocket_id set) → ripple ON, no deny — and a pocket_id wins over
    engine="svelte" (a published svelte site re-opened for refine still edits a
    ripple spec)."""
    for meta in (
        SurfaceMeta(pocket_id="pkt_1"),
        SurfaceMeta(pocket_id="pkt_1", engine="svelte"),
    ):
        profile = resolve_profile(SurfaceKind.SITES, meta)
        assert profile.ripple_mode == "on", f"refine {meta!r} must keep ripple"
        assert profile.deny_mcp_tool_ids == frozenset(), f"refine {meta!r} must deny nothing"
