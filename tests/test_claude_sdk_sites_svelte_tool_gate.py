# tests/test_claude_sdk_sites_svelte_tool_gate.py — Paw Sites Svelte-track tool gate.
#
# Created: 2026-06-05 (feat/sites-svelte-engine) — Regression tests for the
# deterministic ripple-tool exclusion on the Paw Sites Svelte track. Prose-only
# routing in the /sites create preamble ("PREFER create_svelte_site, do NOT call
# create_landing_site") failed: the agent ignored the preamble and called a
# ripple create tool (mcp__pocketpaw_sites_manager__create_landing_site /
# mcp__pocketpaw_pocket_specialist__create), producing a rippleSpec landing page
# (the ripple_spec.unknown_widget_type warnings) instead of a hand-written Svelte
# site. The fix gates allowed_tools in claude_sdk.py: when the system prompt
# carries the `<surface kind="sites" ... engine="svelte" />` marker the two ripple
# create tools are stripped from the allowlist, so the agent is physically unable
# to choose them. These tests pin:
#   1. The marker detector matches the REAL svelte create preamble and rejects the
#      ripple-create preamble, the refine preamble, and non-sites prompts.
#   2. The exclusion drops create_landing_site + pocket_specialist__create while
#      keeping create_svelte_site + publish.
#   3. Refine keeps pocket_specialist__edit; ripple-engine create and non-sites
#      prompts keep their ripple tools (no svelte marker → no gating).
from __future__ import annotations

import pytest

from pocketpaw.agents.claude_sdk import (
    _RIPPLE_CREATE_TOOL_IDS,
    _is_svelte_sites_create_prompt,
)

# Tool ids the gate must drop on the svelte track, and the ones it must keep.
_CREATE_LANDING = "mcp__pocketpaw_sites_manager__create_landing_site"
_SPECIALIST_CREATE = "mcp__pocketpaw_pocket_specialist__create"
_CREATE_SVELTE = "mcp__pocketpaw_sites_manager__create_svelte_site"
_PUBLISH = "mcp__pocketpaw_sites_manager__publish"
_SPECIALIST_EDIT = "mcp__pocketpaw_pocket_specialist__edit"


def _apply_gate(allowed_tools: list[str], prompt: str) -> list[str]:
    """Mirror the gate in ``ClaudeSDKBackend.run`` over a tool list + prompt.

    Kept tiny and pure so the test exercises the SAME exclusion expression the
    backend runs (``[t for t in allowed if t not in _RIPPLE_CREATE_TOOL_IDS]``
    guarded by the marker detector) without standing up a Claude subprocess.
    """
    if _is_svelte_sites_create_prompt(prompt):
        return [t for t in allowed_tools if t not in _RIPPLE_CREATE_TOOL_IDS]
    return list(allowed_tools)


# Full toolset the persistent client is launched with for a /sites chat run
# (the set captured in the forced-svelte run log).
_FULL_SITES_TOOLSET = [
    "Agent",
    "Bash",
    "Skill",
    _CREATE_LANDING,
    _CREATE_SVELTE,
    _PUBLISH,
    _SPECIALIST_CREATE,
    _SPECIALIST_EDIT,
]


# ── 1. Marker detector against the REAL preambles ──


def _svelte_preamble() -> str:
    from pocketpaw_ee.cloud.surface.domain import SurfaceMeta
    from pocketpaw_ee.cloud.surface.handlers import sites as sites_handler

    return sites_handler._svelte_create_preamble(SurfaceMeta(route_path="/sites"))


def _ripple_body() -> str:
    # The ripple create branch (engine != "svelte") is currently shadowed by the
    # live ``if True:`` test override in ``_create_preamble``, so reconstruct its
    # identifying marker shape directly — the test must not depend on the override
    # being reverted. This mirrors the real tag the ripple branch emits:
    # kind="sites", NO engine attribute (so the svelte gate must NOT match it).
    return (
        '<surface kind="sites" route="/sites" />\n'
        "<sites-orientation>build and publish a marketing site…</sites-orientation>\n"
        "<sites-procedure>PREFER the pocketpaw-create-paw-site skill; fall back to "
        "mcp__pocketpaw_pocket_specialist__create then "
        "mcp__pocketpaw_sites_manager__publish.</sites-procedure>"
    )


def _refine_preamble() -> str:
    from pocketpaw_ee.cloud.surface.domain import SurfaceMeta
    from pocketpaw_ee.cloud.surface.handlers import sites as sites_handler

    return sites_handler._refine_preamble(SurfaceMeta(route_path="/sites", pocket_id="pk-1"))


def test_detector_matches_real_svelte_create_preamble() -> None:
    pytest.importorskip("pocketpaw_ee")
    preamble = _svelte_preamble()
    # Sanity: the real preamble carries both markers the detector keys on.
    assert 'kind="sites"' in preamble
    assert 'engine="svelte"' in preamble
    assert _is_svelte_sites_create_prompt(preamble) is True


def test_detector_rejects_ripple_create_preamble() -> None:
    pytest.importorskip("pocketpaw_ee")
    # The ripple create branch emits kind="sites" with NO engine attribute.
    preamble = _ripple_body()
    assert 'engine="svelte"' not in preamble
    assert _is_svelte_sites_create_prompt(preamble) is False


def test_detector_rejects_refine_preamble() -> None:
    pytest.importorskip("pocketpaw_ee")
    preamble = _refine_preamble()
    # Refine is mode="refine" with no svelte engine marker.
    assert 'mode="refine"' in preamble
    assert 'engine="svelte"' not in preamble
    assert _is_svelte_sites_create_prompt(preamble) is False


def test_detector_rejects_non_sites_prompt_even_with_engine_word() -> None:
    # A non-sites prompt that merely mentions engine="svelte" in prose must not
    # trip the gate — the detector requires the sites-surface marker too.
    assert _is_svelte_sites_create_prompt('arbitrary text engine="svelte"') is False
    assert _is_svelte_sites_create_prompt("<pocket-scope>\nbuild a dashboard") is False
    assert _is_svelte_sites_create_prompt(None) is False
    assert _is_svelte_sites_create_prompt("") is False


# ── 2 + 3. Exclusion behavior over the full toolset ──


def test_svelte_create_excludes_ripple_tools_keeps_svelte_and_publish() -> None:
    pytest.importorskip("pocketpaw_ee")
    gated = _apply_gate(_FULL_SITES_TOOLSET, _svelte_preamble())
    # The two ripple create tools are GONE — the agent cannot choose them.
    assert _CREATE_LANDING not in gated
    assert _SPECIALIST_CREATE not in gated
    # The sanctioned svelte create path + publish survive.
    assert _CREATE_SVELTE in gated
    assert _PUBLISH in gated


def test_svelte_create_keeps_specialist_edit_for_safety() -> None:
    # Only the *create* specialist tool is dropped; edit is a different id and is
    # not in the exclusion set (refine uses it; don't break it even if both a
    # svelte create marker and edit tool ever coexist).
    pytest.importorskip("pocketpaw_ee")
    gated = _apply_gate(_FULL_SITES_TOOLSET, _svelte_preamble())
    assert _SPECIALIST_EDIT in gated


def test_refine_prompt_keeps_all_tools() -> None:
    pytest.importorskip("pocketpaw_ee")
    gated = _apply_gate(_FULL_SITES_TOOLSET, _refine_preamble())
    # Refine carries no svelte marker → nothing is gated. The edit path stays.
    assert _SPECIALIST_EDIT in gated
    assert _CREATE_LANDING in gated  # untouched: ripple tools remain available
    assert _SPECIALIST_CREATE in gated


def test_ripple_engine_create_keeps_ripple_tools() -> None:
    # engine != "svelte" create (the default ripple marketing brain) must keep
    # its ripple create tools — the toggle only changes the svelte track.
    gated = _apply_gate(_FULL_SITES_TOOLSET, _ripple_body())
    assert _CREATE_LANDING in gated
    assert _SPECIALIST_CREATE in gated
    assert _CREATE_SVELTE in gated


def test_non_sites_prompt_keeps_ripple_tools() -> None:
    # A pocket/dashboard chat (no sites surface) keeps the full toolset.
    gated = _apply_gate(_FULL_SITES_TOOLSET, "<pocket-scope>\nedit this dashboard")
    assert gated == _FULL_SITES_TOOLSET


def test_exclusion_set_is_exactly_the_two_ripple_create_ids() -> None:
    # Guard the constant so a future edit can't silently widen/narrow the gate.
    assert _RIPPLE_CREATE_TOOL_IDS == frozenset({_CREATE_LANDING, _SPECIALIST_CREATE})
