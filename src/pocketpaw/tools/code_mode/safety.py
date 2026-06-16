# Code Mode safety gate — the single source of truth for the read-safe
# allowlist shared by the stub generator and the RPC bridge.
# Created: 2026-06-16 (feat/code-mode-ptc) — Programmatic Tool Calling v1.
#
# Why a shared module: stubgen.py uses this to decide which tools become
# stubs; bridge.py RE-VALIDATES every incoming call against the SAME predicate
# (defense in depth — the bridge never trusts the stub list it shipped). One
# definition, two enforcement points, no drift.
#
# Classification (v1, READ-ONLY):
#   1. TRUST CEILING — a tool whose ``trust_level`` is anything above
#      ``standard`` ("medium" / "high" / "critical") is NEVER read-safe. This
#      single rule kills shell, run_python, fabric_query (high),
#      connector_execute (medium), every gmail/drive/calendar write, and the
#      instinct_* tools. No allowlist entry can override the ceiling.
#   2. MUTATION DENY-SET — ``trust_level == "standard"`` is necessary but NOT
#      sufficient: write_file, edit_file, create_pocket, add_widget, etc. carry
#      the default "standard" trust yet mutate state. They are denied by name.
#   3. EXPLICIT ALLOWLIST — only tools on the curated read-only allowlist AND
#      passing the ceiling are exposed. Allowlist-by-construction: an unknown
#      tool is denied until a human adds it here, never auto-included.
#
# The ``instinct_pending`` sentinel: a result that carries it means a gated
# write parked for human approval. ``carries_instinct_pending()`` lets the
# bridge reject any such result so a parked write can never round-trip into a
# code-mode script's data flow.

from __future__ import annotations

from typing import Any

# Trust levels at or below this are eligible (subject to the deny-set and
# allowlist below). Anything stronger is rejected outright by the ceiling.
_READ_SAFE_TRUST = frozenset({"standard"})

# The curated read-only allowlist (tool NAMES, not class names). Every entry
# is a registered builtin that ONLY reads — no file writes, no external sends,
# no state mutation, no human-approval gating. An entry must ALSO pass the
# trust ceiling to be exposed; the two gates are AND-ed.
#
# Deliberately conservative for v1. fabric_query / kb_search are NOT here:
# fabric_query carries trust_level "high" (fails the ceiling) and there is no
# registered standard-trust kb_search tool. Exposing a true read-only Fabric/KB
# surface in code mode is a v2 item that needs an action-level read classifier
# (the connectors ``is_read`` flag) — out of scope for this gate.
READ_SAFE_TOOL_NAMES = frozenset(
    {
        # Filesystem reads (jailed by the tools themselves).
        "read_file",
        "list_dir",
        "directory_tree",
        # Web / knowledge reads (no side effects).
        "web_search",
        "url_extract",
        "wiki",
        "weather",
        "currency",
        "translate",
        "research",
        # Community / media reads.
        "reddit_search",
        "reddit_read",
        "reddit_trending",
        "spotify_search",
        "spotify_now_playing",
        # Local diagnostics (read-only).
        "system_info",
    }
)

# Tools that carry ``standard`` trust but MUTATE state or have side effects —
# denied even though they pass the trust ceiling. Belt-and-braces on top of the
# allowlist: the allowlist already excludes these, but naming them here makes
# the intent explicit and guards against a future allowlist edit slipping one
# through. Keep in sync with the registered standard-trust write/side-effect
# tools (see tests/test_code_mode_safety.py which asserts none leak in).
_MUTATING_DENY_NAMES = frozenset(
    {
        "write_file",
        "edit_file",
        "create_pocket",
        "add_widget",
        "remove_widget",
        "start_flow",
        "deliver_artifact",
        "image_generate",
        "text_to_speech",
        "speech_to_text",
        "ocr",
        "create_skill",
        "remember",
        "forget",
        "new_session",
        "clear_session",
        "delete_session",
        "switch_session",
        "rename_session",
    }
)

# Sentinel emitted (in a result string) by a tool whose action was parked for
# human approval — see tools/builtin/instinct_tools.py. A code-mode script must
# never receive a result carrying it.
INSTINCT_PENDING_SENTINEL = "instinct_pending"


def _trust_level(tool: Any) -> str:
    """Read a tool's ``trust_level`` defensively (default ``standard``)."""
    return str(getattr(tool, "trust_level", "standard") or "standard")


def passes_trust_ceiling(tool: Any) -> bool:
    """True when the tool's trust level is read-safe-eligible.

    The hard ceiling: ``medium`` / ``high`` / ``critical`` are always rejected,
    no allowlist override. Acceptance-criteria law — a high/critical tool is
    never exposed AND never executed via the bridge.
    """
    return _trust_level(tool) in _READ_SAFE_TRUST


def is_read_safe_tool(tool: Any) -> bool:
    """The single read-safe predicate. Used by stubgen (filter) AND bridge
    (re-validate). All three gates must pass:

      ceiling (trust standard) AND name on the allowlist AND name not denied.

    Unknown tools fail closed — they are not on the allowlist, so they are
    excluded by construction.
    """
    name = getattr(tool, "name", None)
    if not isinstance(name, str) or not name:
        return False
    if not passes_trust_ceiling(tool):
        return False
    if name in _MUTATING_DENY_NAMES:
        return False
    return name in READ_SAFE_TOOL_NAMES


def is_read_safe_name(name: str) -> bool:
    """Name-only fast path for the bridge's allowlist membership check.

    The bridge ALSO re-validates the live tool object via
    :func:`is_read_safe_tool` (which re-checks the trust ceiling against the
    registry's actual tool), so this is the cheap first gate, not the only one.
    """
    return (
        isinstance(name, str)
        and bool(name)
        and name in READ_SAFE_TOOL_NAMES
        and name not in _MUTATING_DENY_NAMES
    )


def carries_instinct_pending(result: str | None) -> bool:
    """True when a tool result carries the ``instinct_pending`` sentinel — a
    write that parked for human approval. The bridge rejects such a result so a
    parked write never round-trips into a code-mode script.
    """
    return bool(result) and INSTINCT_PENDING_SENTINEL in str(result)
