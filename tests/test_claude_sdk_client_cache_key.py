# Added 2026-05-31 (fix/home-backend-summary-per-turn): regression tests for
# the persistent-client cache key. The home agent baked the backend summary
# ({base_url, auth_type, configured}) into its STATIC system prompt; that
# prompt is applied to the SDK subprocess only at connect() time and ignored
# on warm reuse, so configuring a pocket's backend mid-session was frozen
# until a cold restart. The cache key now folds in a digest of the system
# prompt's stable behavioral prefix, so a config flip rebuilds the persistent
# client on the very next turn. Per-turn KB/memory/history (appended after the
# behavioral prefix) are excluded so the warm-client optimization still holds
# for normal turns.
from __future__ import annotations

from types import SimpleNamespace

import pytest

from pocketpaw.agents.claude_sdk import ClaudeSDKBackend


def _opts(system_prompt, *, model="claude-x", tools=("Agent", "WebSearch")):
    return SimpleNamespace(
        model=model,
        allowed_tools=list(tools),
        system_prompt=system_prompt,
    )


# The two volatile section markers ``pool.run`` appends AFTER the behavioral
# instructions. Anything from the first marker onward must NOT influence the
# key, or every per-turn KB/memory change would needlessly rebuild the client.
_KB_HEADER = "## Your Knowledge Base\nUse the following information..."
_MEM_HEADER = "## Relevant Past Memories\nBelow are memories..."


def test_config_flip_changes_key():
    """A home backend flipping configured:false -> configured:true changes the
    behavioral prefix, so the cache key must differ and force a client rebuild."""
    before = "<home-pocket>\nBackend: not configured\n...rest of static prompt..."
    after = (
        "<home-pocket>\nBackend: configured — https://api.acme.test (auth: bearer)\n"
        "...rest of static prompt..."
    )
    key_before = ClaudeSDKBackend._client_cache_key(_opts(before), session_key="s1")
    key_after = ClaudeSDKBackend._client_cache_key(_opts(after), session_key="s1")
    assert key_before != key_after, (
        "config flip must change the cache key so the warm client rebuilds with "
        "the fresh backend summary — otherwise mid-session config stays frozen"
    )


def test_per_turn_kb_change_keeps_key_stable():
    """Only the per-turn knowledge-base block changed between two turns; the
    behavioral prefix is identical, so the key MUST stay the same (warm reuse)."""
    behavior = "<home-pocket>\nBackend: configured — https://api.acme.test (auth: bearer)\n"
    turn1 = behavior + "\n\n" + _KB_HEADER + "\nsnippet about revenue"
    turn2 = behavior + "\n\n" + _KB_HEADER + "\na totally different KB snippet"
    k1 = ClaudeSDKBackend._client_cache_key(_opts(turn1), session_key="s1")
    k2 = ClaudeSDKBackend._client_cache_key(_opts(turn2), session_key="s1")
    assert k1 == k2, "a per-turn KB change must not rebuild the warm client"


def test_per_turn_memory_change_keeps_key_stable():
    behavior = "<home-pocket>\nBackend: configured — https://api.acme.test (auth: bearer)\n"
    turn1 = behavior + "\n\n" + _MEM_HEADER + "\nremembered fact A"
    turn2 = behavior + "\n\n" + _MEM_HEADER + "\nremembered fact B"
    k1 = ClaudeSDKBackend._client_cache_key(_opts(turn1), session_key="s1")
    k2 = ClaudeSDKBackend._client_cache_key(_opts(turn2), session_key="s1")
    assert k1 == k2


def test_history_change_keeps_key_stable():
    """Cold-start runs inject "# Recent Conversation" history into the prompt.
    That is per-turn and must not influence the key."""
    behavior = "<home-pocket>\nBackend: configured — https://api.acme.test (auth: bearer)\n"
    turn1 = behavior + "\n\n# Recent Conversation\n**User**: hi"
    turn2 = behavior + "\n\n# Recent Conversation\n**User**: hi\n**Assistant**: hello"
    k1 = ClaudeSDKBackend._client_cache_key(_opts(turn1), session_key="s1")
    k2 = ClaudeSDKBackend._client_cache_key(_opts(turn2), session_key="s1")
    assert k1 == k2


def test_model_and_tools_still_keyed():
    """The original key inputs (session, model, allowed_tools) still
    participate — a model or tool change must rebuild the client."""
    sp = "<home-pocket>\nBackend: not configured\n"
    base = ClaudeSDKBackend._client_cache_key(_opts(sp), session_key="s1")
    other_model = ClaudeSDKBackend._client_cache_key(_opts(sp, model="claude-y"), session_key="s1")
    other_tools = ClaudeSDKBackend._client_cache_key(_opts(sp, tools=("Agent",)), session_key="s1")
    other_session = ClaudeSDKBackend._client_cache_key(_opts(sp), session_key="s2")
    assert base != other_model
    assert base != other_tools
    assert base != other_session


def test_file_system_prompt_does_not_crash():
    """On Windows the SDK passes ``system_prompt`` as a ``{type, path}`` dict
    instead of a string. The key computation must tolerate that shape."""
    file_prompt = {"type": "file", "path": "/tmp/system_prompt.md"}
    key = ClaudeSDKBackend._client_cache_key(_opts(file_prompt), session_key="s1")
    assert isinstance(key, str) and key


def test_real_home_behavior_instructions_flip_changes_key():
    """End-to-end guard: build the ACTUAL home behavior instructions with a
    configured:false then configured:true backend summary (a mid-session
    config), wrap each as a system prompt the way ``AgentPool.run`` would, and
    assert the persistent-client cache key differs. This is the regression the
    smoke test surfaced: same session, config flipped, agent read stale state
    until a cold restart. If someone moves the backend summary VALUE into the
    per-turn volatile tail this guard still passes only because the behavioral
    instructions themselves carry it — keep it in the static prefix."""
    pytest.importorskip("pocketpaw_ee")

    from pocketpaw_ee.cloud.chat.agent_service import (
        ScopeContext,
        ScopeKind,
        build_behavior_instructions,
    )

    def _ctx(summary):
        return ScopeContext(
            kind=ScopeKind.POCKET,
            scope_id="home-pocket-1",
            workspace_id="w1",
            user_id="u1",
            members=["u1"],
            target_agent_id="a1",
            agent_ids_in_scope=["a1"],
            pocket_id="home-pocket-1",
            pocket_type="home",
            backend_summary=summary,
        )

    before = build_behavior_instructions(
        _ctx({"configured": False}), backend_name="claude_agent_sdk"
    )
    after = build_behavior_instructions(
        _ctx({"configured": True, "base_url": "https://api.acme.test", "auth_type": "bearer"}),
        backend_name="claude_agent_sdk",
    )
    assert before != after, "behavior instructions must reflect the config flip"

    # Wrap as ``AgentPool.run`` does: behavior prefix, then a volatile per-turn
    # KB tail that differs between the two turns (it must NOT mask the flip).
    sp_before = f"persona\n\n{before}\n\n## Your Knowledge Base\nstale turn-1 snippet"
    sp_after = f"persona\n\n{after}\n\n## Your Knowledge Base\ndifferent turn-2 snippet"
    key_before = ClaudeSDKBackend._client_cache_key(_opts(sp_before), session_key="s1")
    key_after = ClaudeSDKBackend._client_cache_key(_opts(sp_after), session_key="s1")
    assert key_before != key_after, (
        "a mid-session backend config change must rebuild the warm client so "
        "the agent reads the CURRENT backend state on the next message"
    )
