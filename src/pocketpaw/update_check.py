"""Startup version check against PyPI.

Changes:
  - 2026-02-16: Initial implementation. Checks PyPI daily, caches result, prints update notice.

Checks once per 24 hours whether a newer version of pocketpaw exists on PyPI.
Cache stored in ~/.pocketpaw/.update_check so the result is shared between
CLI launches and the dashboard API.
"""

import json
import logging
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

PYPI_URL = "https://pypi.org/pypi/pocketpaw/json"
CACHE_FILENAME = ".update_check"
CACHE_TTL = 86400  # 24 hours
REQUEST_TIMEOUT = 2  # seconds


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


def print_update_notice(info: dict) -> None:
    """Print a one-line update notice to the terminal."""
    current = info["current"]
    latest = info["latest"]
    print(
        f"\n  Update available: {current} \u2192 {latest} \u2014 pip install --upgrade pocketpaw\n"
    )
