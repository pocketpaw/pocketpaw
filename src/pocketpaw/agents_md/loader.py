"""AGENTS.md loader — reads and parses AGENTS.md files from repositories.

Supports:
- Searching for AGENTS.md starting from a given directory and walking up
  to the filesystem root (git-aware: stops at the repo root when a .git
  directory is found).
- Parsing the raw Markdown content into a structured ``AgentsMd`` object.
- Caching parsed results by (path, mtime) so repeated calls are free.

Usage::

    loader = AgentsMdLoader()
    agents_md = loader.find_and_load("/path/to/working/dir")
    if agents_md:
        print(agents_md.constraints_block)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_FILENAME = "AGENTS.md"
# Hard cap on how many parent directories to walk up (prevents runaway)
_MAX_WALK_DEPTH = 20
# Maximum raw file size we'll accept (prevent huge prompt injections)
_MAX_BYTES = 32_768  # 32 KiB


@dataclass(frozen=True)
class AgentsMd:
    """Parsed representation of an AGENTS.md file."""

    path: Path
    raw_content: str
    # Sections extracted from the Markdown
    sections: dict[str, str] = field(default_factory=dict)

    @property
    def constraints_block(self) -> str:
        """Return the full AGENTS.md content formatted for prompt injection."""
        lines = [
            f"# Project AGENTS.md Constraints (from {self.path})",
            "",
            self.raw_content.strip(),
        ]
        return "\n".join(lines)

    @property
    def preview(self) -> str:
        """Short preview for dashboard display (first 200 chars)."""
        text = self.raw_content.strip()
        if len(text) <= 200:
            return text
        return text[:197] + "…"


# ---------------------------------------------------------------------------
# Simple (path, mtime) cache so repeated calls don't hit the filesystem
# ---------------------------------------------------------------------------
@dataclass
class _CacheEntry:
    content: str
    mtime: float


_cache: dict[str, _CacheEntry] = {}


def _read_cached(path: Path) -> str | None:
    """Read file content with mtime-based caching. Returns None if unreadable."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    key = str(path)
    entry = _cache.get(key)
    if entry and entry.mtime == mtime:
        return entry.content
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) > _MAX_BYTES:
        logger.warning(
            "AGENTS.md at %s exceeds %d bytes — truncating to prevent oversized prompts",
            path,
            _MAX_BYTES,
        )
        raw = raw[:_MAX_BYTES]
    content = raw.decode("utf-8", errors="replace")
    _cache[key] = _CacheEntry(content=content, mtime=mtime)
    return content


def _parse_sections(content: str) -> dict[str, str]:
    """Parse Markdown headings into a {heading: body} dict (best-effort)."""
    sections: dict[str, str] = {}
    current_heading: str | None = None
    current_lines: list[str] = []

    for line in content.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            if current_heading is not None:
                sections[current_heading] = "\n".join(current_lines).strip()
            # Extract heading text (strip leading # and whitespace)
            current_heading = stripped.lstrip("#").strip()
            current_lines = []
        else:
            current_lines.append(line)

    if current_heading is not None:
        sections[current_heading] = "\n".join(current_lines).strip()

    return sections


class AgentsMdLoader:
    """Finds and loads AGENTS.md files from a directory tree.

    Search strategy (mirrors how tools like git find .gitignore):
    1. Start at ``start_dir``.
    2. Look for ``AGENTS.md`` in the current directory.
    3. If found, stop and return it.
    4. Walk up to the parent directory.
    5. Stop early if a ``.git`` directory is found in the current level
       (we've reached the repo root) or if we've walked ``_MAX_WALK_DEPTH``
       levels.
    """

    def find_and_load(self, start_dir: str | Path) -> AgentsMd | None:
        """Find the nearest AGENTS.md and return a parsed ``AgentsMd``.

        Returns ``None`` if no AGENTS.md is found.
        """
        directory = Path(start_dir).expanduser().resolve()
        depth = 0

        while depth < _MAX_WALK_DEPTH:
            candidate = directory / _FILENAME
            content = _read_cached(candidate)
            if content is not None:
                logger.debug("Found AGENTS.md at %s", candidate)
                sections = _parse_sections(content)
                return AgentsMd(path=candidate, raw_content=content, sections=sections)

            # Stop at filesystem root or after finding a .git boundary
            parent = directory.parent
            if parent == directory:
                # Filesystem root reached
                break
            # If we found a .git at this level, this is the repo root —
            # we already checked for AGENTS.md here, so stop.
            if (directory / ".git").exists():
                break

            directory = parent
            depth += 1

        return None
