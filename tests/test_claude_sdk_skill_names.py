# tests/test_claude_sdk_skill_names.py
# Created: 2026-06-07 (feat/entity-pocket-profile-field, entity-rooms A2) —
# pins the OSS Claude SDK backend's acceptance of the threaded per-entity
# ``skill_names`` kwarg (the keystone that makes SurfaceProfile.skill_names
# LIVE).
# Modified: 2026-06-13 (fix/claude-sdk-warm-client-skills) — the warm-client
# BYPASS invariant is RETIRED. The persistent-client cache key now folds in a
# digest of the plugin IDENTITY (sorted skill names + bundled flag) via
# ``_client_cache_key(..., plugin_digest=...)``, so a skill run can REUSE the
# warm subprocess instead of re-spawning a fresh stateless query every turn
# (the ~6s/turn latency bug). This test was the one that codified the old gap;
# it now asserts the inverse — equal skill sets → equal key, different →
# different. The end-to-end reuse/no-leak/lifecycle behavior lives in
# tests/test_claude_sdk_skill_warm_reuse.py.

from __future__ import annotations

import inspect

from pocketpaw.agents.claude_sdk import ClaudeSDKBackend


def test_run_accepts_skill_names_kwarg() -> None:
    """``ClaudeSDKBackend.run`` accepts ``skill_names`` defaulting to empty."""
    params = inspect.signature(ClaudeSDKBackend.run).parameters
    assert "skill_names" in params, "ClaudeSDKBackend.run must accept skill_names"
    assert params["skill_names"].default == frozenset(), (
        "skill_names must default to an empty frozenset so non-entity runs are unaffected"
    )


def test_cache_key_folds_in_plugin_digest_so_warm_client_can_be_reused() -> None:
    """The persistent-client cache key now folds in the plugin-identity digest,
    so the warm client CAN tell a skill run apart from a non-skill run — equal
    skill sets produce an equal key (warm reuse) and different sets produce a
    different key (rebuild). This is the inverse of the old behavior that forced
    a bypass. CRITICAL: the digest is keyed on skill IDENTITY, never the
    materialized ``plugins=`` PATH (a fresh ``mkdtemp`` per run), so reuse is not
    defeated by path churn."""
    digest_a = ClaudeSDKBackend._plugin_digest(frozenset({"skillA"}), bundled=False)
    digest_a_again = ClaudeSDKBackend._plugin_digest(frozenset({"skillA"}), bundled=False)
    digest_b = ClaudeSDKBackend._plugin_digest(frozenset({"skillB"}), bundled=False)

    from types import SimpleNamespace

    base = SimpleNamespace(
        system_prompt="IDENTITY",
        model="claude",
        allowed_tools=["Read"],
    )

    key_a = ClaudeSDKBackend._client_cache_key(base, session_key="s1", plugin_digest=digest_a)
    key_a2 = ClaudeSDKBackend._client_cache_key(
        base, session_key="s1", plugin_digest=digest_a_again
    )
    key_b = ClaudeSDKBackend._client_cache_key(base, session_key="s1", plugin_digest=digest_b)
    key_none = ClaudeSDKBackend._client_cache_key(base, session_key="s1")

    assert key_a == key_a2, "same skill set → same key, so the warm client is reused"
    assert key_a != key_b, "different skill set → different key, so the client rebuilds"
    assert key_a != key_none, "a skill run must not collide with a non-skill run"


# --- P1: the surface allowlist is authoritative over the bundled plugin ---
# Added 2026-07-25 (feat/bundled-skills-per-surface). The bundled skills ship
# as a local plugin loaded via SDK ``plugins=``, which is INDEPENDENT of
# ``skill_names`` — so a surface could not withhold ``pocketpaw-create-pocket``
# by naming a narrower set (see surface_registry.py's _CODE_SKILL_DENY note).
# Gating the wholesale load on an EMPTY skill_names makes the per-surface
# allowlist real: name skills and you get exactly those, name none and you get
# the full bundled set (unchanged for general chat).


def test_bundled_plugin_withheld_when_surface_names_skills() -> None:
    """A surface that names skills does NOT also get the whole bundled set."""
    assert not ClaudeSDKBackend._should_load_bundled_plugin(
        enabled=True, skill_names=frozenset({"code-react"})
    ), "naming a skill subset must suppress the wholesale bundled plugin"


def test_bundled_plugin_loads_when_no_skill_names() -> None:
    """Back-compat: no named skills → the full bundled set still loads."""
    assert ClaudeSDKBackend._should_load_bundled_plugin(enabled=True, skill_names=frozenset()), (
        "general chat must keep the full bundled set"
    )


def test_bundled_plugin_respects_the_settings_toggle() -> None:
    """``sdk_load_bundled_skills=False`` still wins over everything."""
    assert not ClaudeSDKBackend._should_load_bundled_plugin(enabled=False, skill_names=frozenset())
