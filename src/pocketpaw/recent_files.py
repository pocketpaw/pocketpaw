"""Recent files tracker.

Captures file paths from agent tool usage and persists them to disk.
Stores the last N unique file paths with timestamps.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MAX_ENTRIES = 50
_STORE_FILE = Path.home() / ".pocketpaw" / "recent_files.json"

# Tools that operate on file paths and the param key holding the path
_TOOL_PATH_KEYS: dict[str, list[str]] = {
    "Read": ["file_path"],
    "Write": ["file_path"],
    "Edit": ["file_path"],
    "read_file": ["path", "file_path"],
    "write_file": ["path", "file_path"],
    "edit_file": ["path", "file_path"],
    "str_replace_editor": ["path"],
    "Bash": [],  # handled separately via _extract_path_from_bash
}

# Regex to extract file paths from Bash command strings.
# Matches absolute paths and common relative paths with an extension.
_BASH_PATH_RE = re.compile(
    r"(?<!\w)"  # not preceded by a word character
    r"((?:[~/]|\./|\.\./)"  # must start with ~, /, ./ or ../
    r"[^\s;|&<>\"'`]+)"  # followed by non-whitespace/shell-special chars
    r"(?!\w)",  # not followed by a word character
    re.ASCII,
)


def _extract_path_from_bash(command: str) -> str | None:
    """Heuristic: extract the first plausible file path from a Bash command string.

    Looks for absolute paths (starting with / or ~) and relative paths
    starting with ./ or ../ that look like files (contain a dot in the
    final component).  Returns the first match, or None if none found.
    """
    for match in _BASH_PATH_RE.finditer(command):
        candidate = match.group(1).rstrip(".,;)")
        p = Path(candidate).expanduser()
        # Accept if it already exists on disk OR looks like a file (has an extension)
        if p.exists() or "." in p.name:
            return candidate
    return None


def _extract_path_from_tool(tool_name: str, tool_input: dict[str, Any]) -> str | None:
    """Extract a file path from tool input params."""
    keys = _TOOL_PATH_KEYS.get(tool_name)
    if keys is None:
        return None

    # Bash: apply heuristic to the command string
    if tool_name == "Bash":
        command = tool_input.get("command") or tool_input.get("cmd") or ""
        if isinstance(command, str) and command:
            return _extract_path_from_bash(command)
        return None

    for key in keys:
        val = tool_input.get(key)
        if val and isinstance(val, str):
            return val
    return None


class RecentFilesTracker:
    """Tracks recently used file paths from agent tool calls."""

    def __init__(self) -> None:
        self._entries: list[dict[str, Any]] = []
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            if _STORE_FILE.exists():
                self._entries = json.loads(_STORE_FILE.read_text("utf-8"))
        except Exception:
            logger.debug("Failed to load recent files", exc_info=True)
            self._entries = []

    def _save(self) -> None:
        try:
            _STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _STORE_FILE.write_text(json.dumps(self._entries, indent=2), "utf-8")
        except Exception:
            logger.debug("Failed to save recent files", exc_info=True)

    def record_tool_use(self, tool_name: str, tool_input: dict[str, Any]) -> None:
        """Record a file path from a tool invocation."""
        path = _extract_path_from_tool(tool_name, tool_input)
        if not path:
            return

        # Skip non-file paths and very short strings
        if len(path) < 3:
            return

        self._ensure_loaded()

        # Remove existing entry for this path (we'll re-add at top)
        self._entries = [e for e in self._entries if e.get("path") != path]

        # Determine if it's a directory or file
        p = Path(path)
        is_dir = p.is_dir() if p.exists() else False
        name = p.name or path
        ext = p.suffix.lstrip(".").lower() if not is_dir else ""

        self._entries.insert(
            0,
            {
                "path": path,
                "name": name,
                "is_dir": is_dir,
                "extension": ext,
                "timestamp": time.time(),
                "tool": tool_name,
            },
        )

        # Trim to max
        self._entries = self._entries[:_MAX_ENTRIES]
        self._save()

    def get_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return the most recent file entries."""
        self._ensure_loaded()
        return self._entries[:limit]

    def clear(self) -> None:
        """Clear all recent files."""
        self._entries = []
        self._save()


# Singleton
_tracker: RecentFilesTracker | None = None


def get_recent_files_tracker() -> RecentFilesTracker:
    global _tracker
    if _tracker is None:
        _tracker = RecentFilesTracker()
    return _tracker
