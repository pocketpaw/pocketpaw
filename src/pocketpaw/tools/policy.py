"""Tool Policy System — controls which tools are available to agent backends.

Profiles define presets of allowed tools. Groups are shorthand for sets of tools.
Explicit allow/deny lists override profiles.

Precedence (highest to lowest):
  1. tools_deny — always wins, blocks even if explicitly allowed
  2. tools_allow — if non-empty, only these tools are available (union with profile)
  3. tool_profile — baseline set of allowed tools

Updated: 2026-05-21 — Added ``is_mcp_server_explicitly_allowed`` so built-in
  in-process MCP servers (e.g. the planner) can be gated as opt-in rather
  than ambient. The opt-in is driven by a dedicated ``mcp_servers_allow``
  frozenset, kept orthogonal to ``tools_allow``: putting an ``mcp:*`` entry
  in ``tools_allow`` would make ``_allowed_set`` non-empty and flip the
  policy into allow-list mode, silently disabling every other tool. Also
  added ``OPT_IN_MCP_SERVERS`` — the single source of truth for which
  in-process servers are opt-in, imported by AgentPool and ClaudeSDKBackend.

Inspired by OpenClaw's tool-policy.ts.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool groups — named sets of related tools
# ---------------------------------------------------------------------------
TOOL_GROUPS: dict[str, list[str]] = {
    "group:fs": ["read_file", "write_file", "edit_file", "list_dir", "directory_tree"],
    "group:shell": ["shell", "run_python"],
    "group:packages": ["install_package"],
    "group:browser": ["browser"],
    "group:memory": ["remember", "recall", "forget"],
    "group:desktop": ["desktop", "system_info"],
    "group:search": ["web_search", "url_extract"],
    "group:skills": ["create_skill", "skill"],
    "group:gmail": ["gmail_search", "gmail_read", "gmail_send"],
    "group:calendar": ["calendar_list", "calendar_create", "calendar_prep"],
    "group:voice": ["text_to_speech", "speech_to_text"],
    "group:research": ["research"],
    "group:delegation": ["delegate_claude_code", "delegate_to_a2a_agent"],
    "group:drive": ["drive_list", "drive_download", "drive_upload", "drive_share"],
    "group:docs": ["docs_read", "docs_create", "docs_search"],
    "group:spotify": [
        "spotify_search",
        "spotify_now_playing",
        "spotify_playback",
        "spotify_playlist",
    ],
    "group:media": ["image_generate", "ocr", "deliver_artifact"],
    "group:translate": ["translate"],
    "group:reddit": ["reddit_search", "reddit_read", "reddit_trending"],
    "group:sessions": [
        "new_session",
        "list_sessions",
        "switch_session",
        "clear_session",
        "rename_session",
        "delete_session",
    ],
    "group:explorer": ["open_in_explorer"],
    "group:discord": ["discord_cli"],
    "group:mcp": [],  # Placeholder — MCP tools are dynamic per server
}

# ---------------------------------------------------------------------------
# Built-in profiles — from minimal to full
# ---------------------------------------------------------------------------
TOOL_PROFILES: dict[str, dict] = {
    "minimal": {
        "allow": ["group:memory", "group:sessions", "group:explorer"],
    },
    "coding": {
        "allow": ["group:fs", "group:shell", "group:packages", "group:memory", "group:explorer"],
    },
    "full": {},  # No restrictions — everything allowed
}

# ---------------------------------------------------------------------------
# Opt-in in-process MCP servers
# ---------------------------------------------------------------------------
# Built-in in-process MCP servers that are opt-in, not ambient. Every other
# in-process server registers under allow-by-default policy; a server named
# here registers only when the agent's tool policy explicitly opts in via
# ``is_mcp_server_explicitly_allowed`` (backed by ``mcp_servers_allow``).
#
# Single source of truth for the opt-in set. ``AgentPool._build`` reads it to
# turn a cloud agent's ``tools`` entries into ``mcp_servers_allow``, and
# ``ClaudeSDKBackend`` reads it to gate both server registration and the tool
# allowlist. A new opt-in server is added here once.
OPT_IN_MCP_SERVERS: frozenset[str] = frozenset({"pocketpaw_planner"})


class ToolPolicy:
    """Evaluates whether a tool is allowed based on profile + allow/deny lists."""

    def __init__(
        self,
        profile: str = "full",
        allow: Sequence[str] | None = None,
        deny: Sequence[str] | None = None,
        mcp_servers_allow: frozenset[str] | None = None,
    ):
        self.profile = profile
        self._allow_raw = list(allow) if allow else []
        self._deny_raw = list(deny) if deny else []

        # Built-in in-process MCP servers opted in for this policy. Kept
        # separate from ``allow`` on purpose: an ``mcp:*`` entry in
        # ``allow`` makes ``_allowed_set`` non-empty and flips the policy
        # into allow-list mode, silently disabling every other tool. This
        # frozenset is read only by ``is_mcp_server_explicitly_allowed``.
        self._mcp_servers_allow: frozenset[str] = mcp_servers_allow or frozenset()

        # Pre-resolve for fast lookups
        self._allowed_set = self._resolve()
        self._denied_set = self._expand_names(self._deny_raw)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_tool_allowed(self, tool_name: str) -> bool:
        """Return True if *tool_name* passes the policy."""
        # Deny always wins
        if tool_name in self._denied_set:
            logger.debug("Tool '%s' blocked by deny list", tool_name)
            return False

        # If the profile is 'full' and there's no explicit allow list,
        # everything not denied is allowed.
        if not self._allowed_set:
            return True

        allowed = tool_name in self._allowed_set
        if not allowed:
            logger.debug("Tool '%s' not in allowed set", tool_name)
        return allowed

    def filter_tool_names(self, names: Sequence[str]) -> list[str]:
        """Return only the names that pass the policy."""
        return [n for n in names if self.is_tool_allowed(n)]

    def is_mcp_server_allowed(self, server_name: str) -> bool:
        """Return True if an MCP server is allowed by the policy.

        MCP servers use the naming convention ``mcp:<server>:*``.
        A server is blocked if:
        - ``mcp:<server>:*`` or ``group:mcp`` is in the deny list
        A server is allowed if:
        - the profile is 'full' and there's no explicit allow list, OR
        - ``mcp:<server>:*`` or ``group:mcp`` is in the allow/profile set
        """
        wildcard = f"mcp:{server_name}:*"
        # Check deny
        if wildcard in self._denied_set or "group:mcp" in self._denied_set:
            logger.debug("MCP server '%s' blocked by deny list", server_name)
            return False
        # Full profile with no allow list → permit all
        if not self._allowed_set:
            return True
        # Check allow
        if wildcard in self._allowed_set or "group:mcp" in self._allowed_set:
            return True
        logger.debug("MCP server '%s' not in allowed set", server_name)
        return False

    def is_mcp_server_explicitly_allowed(self, server_name: str) -> bool:
        """Return True only when an MCP server is *explicitly* opted in.

        Unlike :meth:`is_mcp_server_allowed`, this does NOT treat the
        allow-by-default fallthrough as permission. It returns True only
        when ``server_name`` is in the dedicated ``mcp_servers_allow``
        frozenset passed to the constructor. ``tools_allow`` is never
        consulted here — see ``__init__`` for why the two are orthogonal.
        Deny still wins.

        Use this to gate built-in in-process MCP servers that must be
        opt-in rather than ambient on every agent run (e.g. the planner).
        """
        wildcard = f"mcp:{server_name}:*"
        # Deny always wins.
        if wildcard in self._denied_set or "group:mcp" in self._denied_set:
            return False
        return server_name in self._mcp_servers_allow

    def is_mcp_tool_allowed(self, server_name: str, tool_name: str) -> bool:
        """Return True if a specific MCP tool is allowed.

        Checks ``mcp:<server>:<tool>``, ``mcp:<server>:*``, and ``group:mcp``.
        """
        specific = f"mcp:{server_name}:{tool_name}"
        wildcard = f"mcp:{server_name}:*"
        # Check deny (specific first, then wildcard, then group)
        if (
            specific in self._denied_set
            or wildcard in self._denied_set
            or "group:mcp" in self._denied_set
        ):
            return False
        # Full profile with no allow list → permit all
        if not self._allowed_set:
            return True
        return (
            specific in self._allowed_set
            or wildcard in self._allowed_set
            or "group:mcp" in self._allowed_set
        )

    # ------------------------------------------------------------------
    # Resolution helpers
    # ------------------------------------------------------------------

    @staticmethod
    def resolve_profile(profile_name: str) -> set[str]:
        """Expand a profile name into a concrete set of tool names.

        Returns an empty set for the 'full' profile (meaning no restrictions).
        Raises ValueError for unknown profiles.
        """
        if profile_name not in TOOL_PROFILES:
            raise ValueError(
                f"Unknown tool profile '{profile_name}'. Available: {', '.join(TOOL_PROFILES)}"
            )
        cfg = TOOL_PROFILES[profile_name]
        raw = cfg.get("allow", [])
        return ToolPolicy._expand_names(raw)

    @staticmethod
    def _expand_names(raw: Sequence[str]) -> set[str]:
        """Expand a list that may contain group references into tool names.

        Dynamic groups (like ``group:mcp``) with empty tool lists are kept
        as sentinel values so that ``is_mcp_server_allowed`` can check them.
        """
        result: set[str] = set()
        for item in raw:
            if item.startswith("group:") and item in TOOL_GROUPS:
                members = TOOL_GROUPS[item]
                if members:
                    result.update(members)
                else:
                    # Keep the group sentinel (e.g. group:mcp with no static tools)
                    result.add(item)
            else:
                result.add(item)
        return result

    def _resolve(self) -> set[str]:
        """Build the final allowed set from profile + explicit allow list.

        Raises ValueError when the profile name is not recognised. The
        previous silent fallback to ``set()`` (equivalent to the ``full``
        profile) meant a typo in ``tool_profile`` lifted all restrictions —
        see issue #889.
        """
        profile_set = self.resolve_profile(self.profile)
        explicit = self._expand_names(self._allow_raw)
        return profile_set | explicit
