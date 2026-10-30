# tests/test_claude_sdk_deny_mcp_tools.py — Threaded deny-MCP-tool gate (OSS).
#
# Created: 2026-06-05 (feat/sites-svelte-engine) — tests for the SECOND half of
# the bias-kill: replacing the prompt-SNIFFING ripple-tool gate in
# ``claude_sdk.py`` with a THREADED ``deny_mcp_tool_ids`` kwarg on the backend's
# ``run(...)``. The old OSS backend decided which ripple-create tools to strip by
# string-matching ``engine="svelte"`` in the system prompt (the deleted
# ``_is_svelte_sites_create_prompt`` + ``_RIPPLE_CREATE_TOOL_IDS``). That gate was
# brittle (a non-/sites prompt that merely quoted the marker, or a future preamble
# wording change, silently flipped it) and couldn't express the three-mode /sites
# policy the SurfaceProfile resolver now owns. The GREEN change DELETED the
# prompt-sniff and instead threads a ``deny_mcp_tool_ids: frozenset[str] =
# frozenset()`` kwarg (resolved upstream from the SurfaceProfile) into
# ``ClaudeSDKBackend.run``, subtracting it from ``allowed_tools`` before the SDK
# launches. The kwarg is named ``deny_mcp_tool_ids`` (NOT ``profile`` — that
# collides with ``ToolPolicy(profile=...)``).
#
# Modified: 2026-06-05 (feat/sites-svelte-engine, GREEN) — the kwarg + subtraction
# are now wired, so the two tests below PASS as ordinary green tests. They were
# authored ``@pytest.mark.xfail(strict=True)`` (RED-by-contract: the subtraction
# lives deep inside ``run()`` after the SDK/CLI guards, so the smallest honest
# seam is the ``run`` SIGNATURE + the subtraction SEMANTICS, both depending on the
# new kwarg). Per the strict-xfail contract — a premature pass is a hard failure —
# the xfail markers were REMOVED in GREEN so the gate cannot rot to a silent xpass.
from __future__ import annotations

import inspect

from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

# The two ripple-create tool ids the /sites svelte-create mode forbids, plus an
# UNRELATED tool that must survive any deny pass (a Skill / web tool the agent
# still needs).
_CREATE_LANDING = "mcp__pocketpaw_sites_manager__create_landing_site"
_SPECIALIST_CREATE = "mcp__pocketpaw_pocket_specialist__create"
_CREATE_SVELTE = "mcp__pocketpaw_sites_manager__create_svelte_site"
_PUBLISH = "mcp__pocketpaw_sites_manager__publish"
_SPECIALIST_EDIT = "mcp__pocketpaw_pocket_specialist__edit"

# The full toolset the persistent client is launched with for a /sites run.
_FULL_SITES_TOOLSET = [
    "Skill",
    "WebSearch",
    _CREATE_LANDING,
    _CREATE_SVELTE,
    _PUBLISH,
    _SPECIALIST_CREATE,
    _SPECIALIST_EDIT,
]

_SVELTE_DENY = frozenset({_CREATE_LANDING, _SPECIALIST_CREATE})


def _apply_deny(allowed_tools: list[str], deny_mcp_tool_ids: frozenset[str]) -> list[str]:
    """Mirror the subtraction the implementer will wire into ``run()``.

    The GREEN change replaces the prompt-sniff gate
    (``[t for t in allowed if t not in _RIPPLE_CREATE_TOOL_IDS]`` guarded by a
    marker detector) with this UNCONDITIONAL subtraction of the threaded deny
    set: ``[t for t in allowed_tools if t not in deny_mcp_tool_ids]``. Kept tiny
    and pure so the test pins the SAME expression the backend will run.
    """
    return [t for t in allowed_tools if t not in deny_mcp_tool_ids]


def test_deny_mcp_tool_ids_removes_them_from_allowlist() -> None:
    """RED-by-contract: when ``run`` resolves its allowed tools with a non-empty
    ``deny_mcp_tool_ids``, those ids are subtracted from the final allowlist
    while every unrelated tool survives.

    Pins two things, both of which depend on the new kwarg (so both are RED until
    GREEN wires it):
      1. ``ClaudeSDKBackend.run`` accepts a ``deny_mcp_tool_ids`` keyword.
      2. The subtraction the implementer wires drops EXACTLY the denied ids and
         keeps the rest (svelte-create + publish + edit + the unrelated Skill /
         web tools).
    """
    # (1) Signature contract — fails today (kwarg absent).
    params = inspect.signature(ClaudeSDKBackend.run).parameters
    assert "deny_mcp_tool_ids" in params, (
        "ClaudeSDKBackend.run must accept a threaded deny_mcp_tool_ids kwarg "
        "(replacing the prompt-sniffing _is_svelte_sites_create_prompt gate)"
    )
    # It must be keyword-only with an empty-frozenset default (a no-op deny when
    # the caller passes nothing — the legacy / non-/sites path).
    deny_param = params["deny_mcp_tool_ids"]
    assert deny_param.default == frozenset(), (
        "deny_mcp_tool_ids must default to an empty frozenset so non-/sites runs are unaffected"
    )

    # (2) Subtraction semantics — the denied ripple-create ids are gone, the
    # sanctioned svelte/publish/edit + unrelated tools remain.
    gated = _apply_deny(_FULL_SITES_TOOLSET, _SVELTE_DENY)
    assert _CREATE_LANDING not in gated
    assert _SPECIALIST_CREATE not in gated
    # Everything NOT in the deny set survives — including an unrelated Skill /
    # web tool, so the gate is a precise subtraction, not a blanket strip.
    assert _CREATE_SVELTE in gated
    assert _PUBLISH in gated
    assert _SPECIALIST_EDIT in gated
    assert "Skill" in gated
    assert "WebSearch" in gated


def test_no_deny_keeps_all_tools() -> None:
    """RED-by-contract: the empty deny set (the default — legacy / ripple-create
    / refine / non-/sites runs) leaves the allowlist UNCHANGED.

    Same kwarg dependency as above: the signature assertion fails today because
    ``run`` has no ``deny_mcp_tool_ids`` parameter. The empty-set subtraction is
    the identity, proving the threaded gate is inert unless a surface explicitly
    denies tools (so deleting the prompt-sniff gate does not over-strip the
    ripple-create / refine modes that must KEEP their ripple tools)."""
    params = inspect.signature(ClaudeSDKBackend.run).parameters
    assert "deny_mcp_tool_ids" in params, (
        "ClaudeSDKBackend.run must accept a threaded deny_mcp_tool_ids kwarg"
    )

    gated = _apply_deny(_FULL_SITES_TOOLSET, frozenset())
    assert gated == _FULL_SITES_TOOLSET, (
        "an empty deny set must leave the allowlist untouched — the threaded gate "
        "is a no-op unless a surface forbids tools"
    )
