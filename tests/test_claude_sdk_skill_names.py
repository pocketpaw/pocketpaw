# tests/test_claude_sdk_skill_names.py
# Created: 2026-06-07 (feat/entity-pocket-profile-field, entity-rooms A2) —
# pins the OSS Claude SDK backend's acceptance of the threaded per-entity
# ``skill_names`` kwarg (the keystone that makes SurfaceProfile.skill_names
# LIVE) and the warm-client bypass invariant: a run that materializes a per-run
# skill plugin must NOT reuse a warm client, because the persistent-client cache
# key omits ``plugins=`` and options apply only at first connect.

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


def test_cache_key_omits_plugins_so_warm_client_must_be_bypassed() -> None:
    """The persistent-client cache key is built from session + model + tools +
    a prompt-prefix digest — it does NOT fold in ``plugins=``. This is exactly
    why a skill run (which only changes ``plugins=``) MUST bypass the warm
    client; this test documents the gap the bypass closes."""
    from types import SimpleNamespace

    base = SimpleNamespace(
        system_prompt="IDENTITY",
        model="claude",
        allowed_tools=["Read"],
        plugins=[],
    )
    with_plugin = SimpleNamespace(
        system_prompt="IDENTITY",
        model="claude",
        allowed_tools=["Read"],
        plugins=[{"type": "local", "path": "/tmp/x"}],
    )
    k1 = ClaudeSDKBackend._client_cache_key(base, session_key="s1")
    k2 = ClaudeSDKBackend._client_cache_key(with_plugin, session_key="s1")
    assert k1 == k2, (
        "cache key ignores plugins= — so the warm client cannot distinguish a "
        "skill run; the backend bypasses the warm client when skill_names is set"
    )
