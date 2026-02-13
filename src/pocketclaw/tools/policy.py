"""Tool Policy System — controls which tools are available to agent backends.

Profiles define presets of allowed tools. Groups are shorthand for sets of tools.
Explicit allow/deny lists override profiles.

Precedence (highest to lowest):
  1. tools_deny — always wins, blocks even if explicitly allowed
  2. tools_allow — if non-empty, only these tools are available (union with profile)
  3. tool_profile — baseline set of allowed tools

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
    "group:fs": ["read_file", "write_file", "edit_file", "list_dir"],
    "group:shell": ["shell"],
    "group:browser": ["browser"],
    "group:memory": ["remember", "recall", "forget"],
    "group:desktop": ["desktop"],
    "group:search": ["web_search", "url_extract"],
    "group:skills": ["create_skill"],
    "group:gmail": ["gmail_search", "gmail_read", "gmail_send"],
    "group:calendar": ["calendar_list", "calendar_create", "calendar_prep"],
    "group:voice": ["text_to_speech", "speech_to_text"],
    "group:research": ["research"],
    "group:delegation": ["delegate_claude_code"],
    "group:drive": ["drive_list", "drive_download", "drive_upload", "drive_share"],
    "group:docs": ["docs_read", "docs_create", "docs_search"],
    "group:spotify": [
        "spotify_search",
        "spotify_now_playing",
        "spotify_playback",
        "spotify_playlist",
    ],
    "group:media": ["image_generate", "ocr"],
    "group:reddit": ["reddit_search", "reddit_read", "reddit_trending"],
    "group:sessions": [
        "new_session",
        "list_sessions",
        "switch_session",
        "clear_session",
        "rename_session",
        "delete_session",
    ],
    "group:mcp": [],  # Placeholder — MCP tools are dynamic per server
}

# ---------------------------------------------------------------------------
# Built-in profiles — from minimal to full
# ---------------------------------------------------------------------------
TOOL_PROFILES: dict[str, dict] = {
    "minimal": {
        "allow": ["group:memory", "group:sessions"],
    },
    "coding": {
        "allow": ["group:fs", "group:shell", "group:memory"],
    },
    "full": {},  # No restrictions — everything allowed
}


class ToolPolicy:
    """Evaluates whether a tool is allowed based on profile + allow/deny lists."""

    def __init__(
        self,
        profile: str = "full",
        allow: Sequence[str] | None = None,
        deny: Sequence[str] | None = None,
    ):
        self.profile = profile
        self._allow_raw = list(allow) if allow else []
        self._deny_raw = list(deny) if deny else []

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
        """Build the final allowed set from profile + explicit allow list."""
        # Start with the profile
        try:
            profile_set = self.resolve_profile(self.profile)
        except ValueError:
            logger.warning("Unknown profile '%s', falling back to 'full'", self.profile)
            profile_set = set()  # full = no restrictions

        # Merge in explicit allow list
        explicit = self._expand_names(self._allow_raw)
        return profile_set | explicit
