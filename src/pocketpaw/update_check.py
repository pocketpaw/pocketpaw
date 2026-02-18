"""Startup version check against PyPI + release notes fetching.

Changes:
  - 2026-02-18: Added styled CLI update box, release notes fetching, version seen tracking.
  - 2026-02-16: Initial implementation. Checks PyPI daily, caches result, prints update notice.

Checks once per 24 hours whether a newer version of pocketpaw exists on PyPI.
Cache stored in ~/.pocketpaw/.update_check so the result is shared between
CLI launches and the dashboard API.
"""

import json
import logging
import os
import sys
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

PYPI_URL = "https://pypi.org/pypi/pocketpaw/json"
CACHE_FILENAME = ".update_check"
CACHE_TTL = 86400  # 24 hours
REQUEST_TIMEOUT = 2  # seconds

RELEASE_NOTES_CACHE_DIR = ".release_notes_cache"
RELEASE_NOTES_TTL = 3600  # 1 hour
GITHUB_API_URL = "https://api.github.com/repos/pocketpaw/pocketpaw/releases/tags/v{version}"


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse '0.4.1' into (0, 4, 1)."""
    return tuple(int(x) for x in v.strip().split("."))


def check_for_updates(current_version: str, config_dir: Path) -> dict | None:
    """Check PyPI for a newer version. Returns version info dict or None on error.

    Uses a daily cache file to avoid hitting PyPI on every launch.
    Never raises — all errors are caught and logged at debug level.
    """
    try:
        cache_file = config_dir / CACHE_FILENAME
        now = time.time()

        # Try cache first
        if cache_file.exists():
            try:
                cache = json.loads(cache_file.read_text())
                if now - cache.get("ts", 0) < CACHE_TTL:
                    latest = cache.get("latest", current_version)
                    return {
                        "current": current_version,
                        "latest": latest,
                        "update_available": _parse_version(latest)
                        > _parse_version(current_version),
                    }
            except (json.JSONDecodeError, ValueError):
                pass  # Corrupted cache, re-fetch

        # Fetch from PyPI
        req = urllib.request.Request(PYPI_URL, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read())
        latest = data["info"]["version"]

        # Write cache
        config_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({"ts": now, "latest": latest}))

        return {
            "current": current_version,
            "latest": latest,
            "update_available": _parse_version(latest) > _parse_version(current_version),
        }
    except Exception:
        logger.debug("Update check failed (network or parse error)", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# CLI styled update notice
# ---------------------------------------------------------------------------


def _should_suppress_notice() -> bool:
    """Check if the update notice should be suppressed."""
    if os.environ.get("POCKETPAW_NO_UPDATE_CHECK"):
        return True
    if os.environ.get("CI"):
        return True
    if not sys.stderr.isatty():
        return True
    return False


def print_styled_update_notice(info: dict) -> None:
    """Print a styled, can't-miss update box to stderr.

    Uses box-drawing characters, ANSI colors, and writes to stderr so it
    doesn't pollute piped output. Auto-suppressed in CI, non-TTY, or when
    POCKETPAW_NO_UPDATE_CHECK is set.
    """
    if _should_suppress_notice():
        return

    current = info["current"]
    latest = info["latest"]

    # ANSI color codes
    YELLOW = "\033[33m"
    GREEN = "\033[32m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

    changelog_url = "github.com/pocketpaw/pocketpaw/releases"
    upgrade_cmd = "pip install --upgrade pocketpaw"

    # Build the content lines (without borders) to measure width
    title_line = (
        f"   {BOLD}Update available!{RESET}  {current} {YELLOW}\u2192{RESET} {GREEN}{latest}{RESET}"
    )
    changelog_line = f"   {DIM}Changelog:{RESET} {changelog_url}"
    upgrade_line = f"   {DIM}Run:{RESET}       {upgrade_cmd}"

    # Fixed box width (60 chars inner)
    box_width = 60
    border_h = "\u2500" * box_width
    empty_inner = " " * box_width

    lines = [
        f"{YELLOW}\u250c{border_h}\u2510{RESET}",
        f"{YELLOW}\u2502{RESET}{empty_inner}{YELLOW}\u2502{RESET}",
        f"{YELLOW}\u2502{RESET}{title_line}{' ' * 6}{YELLOW}\u2502{RESET}",
        f"{YELLOW}\u2502{RESET}{empty_inner}{YELLOW}\u2502{RESET}",
        f"{YELLOW}\u2502{RESET}{changelog_line}{' ' * (box_width - 52)}{YELLOW}\u2502{RESET}",
        f"{YELLOW}\u2502{RESET}{upgrade_line}{' ' * (box_width - 46)}{YELLOW}\u2502{RESET}",
        f"{YELLOW}\u2502{RESET}{empty_inner}{YELLOW}\u2502{RESET}",
        f"{YELLOW}\u2514{border_h}\u2518{RESET}",
    ]

    sys.stderr.write("\n" + "\n".join(lines) + "\n\n")


def print_update_notice(info: dict) -> None:
    """Deprecated: use print_styled_update_notice instead.

    Delegates to styled version for backward compatibility.
    """
    print_styled_update_notice(info)


# ---------------------------------------------------------------------------
# Release notes fetching + version seen tracking
# ---------------------------------------------------------------------------


def fetch_release_notes(version: str, config_dir: Path) -> dict | None:
    """Fetch release notes from GitHub for a specific version.

    Returns {version, body, html_url, published_at, name} or None on error.
    Uses per-version cache files with 1h TTL in config_dir/.release_notes_cache/.
    """
    try:
        cache_dir = config_dir / RELEASE_NOTES_CACHE_DIR
        cache_file = cache_dir / f"v{version}.json"
        now = time.time()

        # Try cache first
        if cache_file.exists():
            try:
                cached = json.loads(cache_file.read_text())
                if now - cached.get("ts", 0) < RELEASE_NOTES_TTL:
                    return cached.get("data")
            except (json.JSONDecodeError, ValueError):
                pass

        # Fetch from GitHub
        url = GITHUB_API_URL.format(version=version)
        req = urllib.request.Request(
            url, headers={"Accept": "application/vnd.github.v3+json", "User-Agent": "pocketpaw"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            release = json.loads(resp.read())

        data = {
            "version": version,
            "body": release.get("body", ""),
            "html_url": release.get("html_url", ""),
            "published_at": release.get("published_at", ""),
            "name": release.get("name", f"v{version}"),
        }

        # Write cache
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({"ts": now, "data": data}))

        return data
    except Exception:
        logger.debug("Failed to fetch release notes for v%s", version, exc_info=True)
        return None


def get_last_seen_version(config_dir: Path) -> str | None:
    """Read last_seen_version from the update check cache file."""
    try:
        cache_file = config_dir / CACHE_FILENAME
        if cache_file.exists():
            cache = json.loads(cache_file.read_text())
            return cache.get("last_seen_version")
    except (json.JSONDecodeError, ValueError, OSError):
        pass
    return None


def mark_version_seen(version: str, config_dir: Path) -> None:
    """Write last_seen_version into the update check cache file.

    Preserves existing cache fields (ts, latest) and adds/updates last_seen_version.
    """
    try:
        cache_file = config_dir / CACHE_FILENAME
        cache = {}
        if cache_file.exists():
            try:
                cache = json.loads(cache_file.read_text())
            except (json.JSONDecodeError, ValueError):
                pass
        cache["last_seen_version"] = version
        config_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(cache))
    except OSError:
        logger.debug("Failed to mark version %s as seen", version, exc_info=True)
