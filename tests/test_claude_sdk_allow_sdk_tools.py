# tests/test_claude_sdk_allow_sdk_tools.py — Threaded per-entity ADD allowlist (OSS).
#
# Created: 2026-06-06 (feat/entity-pocket-profile-field, entity-rooms chunk ①) —
# tests for the OSS half of wiring ``SurfaceProfile.allowed_sdk_tools`` (the
# per-entity ADDITIVE SDK-tool allowlist) into ``ClaudeSDKBackend.run``. It
# mirrors the ``deny_mcp_tool_ids`` threading EXACTLY: a plain ``frozenset[str]``
# forwarded by ``AgentPool.run`` only when non-empty, consumed only by the Claude
# SDK backend. The precedence the backend applies is
# ``effective = (agent_tools ∪ allow) − deny`` — allow is UNIONed in BEFORE the
# deny subtraction, so the surface deny stays the HARD cap (an id in BOTH allow
# and deny ends up denied). No ``pocketpaw_ee`` symbol crosses the boundary.

from __future__ import annotations

import inspect

from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

_AGENT_TOOL = "Read"
_ENTITY_ALLOW = "WebFetch"
_DENIED = "mcp__pocketpaw_pocket_specialist__create"


def _apply_precedence(
    agent_tools: list[str],
    allow_sdk_tools: frozenset[str],
    deny_mcp_tool_ids: frozenset[str],
) -> list[str]:
    """Mirror the precedence the backend wires: ``(agent ∪ allow) − deny``.

    Allow is unioned in (dedup, order-preserving) BEFORE the deny subtraction, so
    a tool present in BOTH allow and deny is removed — deny is the hard cap. Kept
    tiny and pure so the test pins the SAME semantics ``run()`` applies.
    """
    merged = list(agent_tools)
    seen = set(merged)
    for t in allow_sdk_tools:
        if t not in seen:
            merged.append(t)
            seen.add(t)
    return [t for t in merged if t not in deny_mcp_tool_ids]


def test_run_accepts_allow_sdk_tools_kwarg() -> None:
    """``ClaudeSDKBackend.run`` accepts a keyword ``allow_sdk_tools`` defaulting
    to an empty frozenset (a no-op for legacy / non-entity runs)."""
    params = inspect.signature(ClaudeSDKBackend.run).parameters
    assert "allow_sdk_tools" in params, (
        "ClaudeSDKBackend.run must accept a threaded allow_sdk_tools kwarg "
        "(the per-entity additive SDK-tool allowlist)"
    )
    assert params["allow_sdk_tools"].default == frozenset(), (
        "allow_sdk_tools must default to an empty frozenset so non-entity runs are unaffected"
    )


def test_allow_unions_into_allowlist() -> None:
    """The entity's allow tools are ADDED to the agent's tools (union)."""
    out = _apply_precedence([_AGENT_TOOL], frozenset({_ENTITY_ALLOW}), frozenset())
    assert _AGENT_TOOL in out
    assert _ENTITY_ALLOW in out


def test_deny_is_hard_cap_over_allow() -> None:
    """An id in BOTH allow and deny ends up DENIED — deny is the hard cap, applied
    AFTER the allow union."""
    out = _apply_precedence(
        [_AGENT_TOOL],
        frozenset({_ENTITY_ALLOW, _DENIED}),
        frozenset({_DENIED}),
    )
    assert _AGENT_TOOL in out
    assert _ENTITY_ALLOW in out
    assert _DENIED not in out, "deny must win over allow (hard cap)"


def test_empty_allow_is_identity() -> None:
    """The empty allow set (the default — legacy / non-entity runs) leaves the
    agent allowlist unchanged."""
    out = _apply_precedence([_AGENT_TOOL], frozenset(), frozenset())
    assert out == [_AGENT_TOOL]
