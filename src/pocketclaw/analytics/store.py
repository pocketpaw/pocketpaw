"""File-based analytics persistence.

Stores an AnalyticsSnapshot as JSON in ``~/.pocketclaw/analytics/analytics.json``.
Uses atomic write (temp file + rename) to avoid corruption on crash.

Created: 2026-02-13
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from pocketclaw.analytics.models import AnalyticsSnapshot
from pocketclaw.config import get_config_dir

logger = logging.getLogger(__name__)

_ANALYTICS_DIR_NAME = "analytics"
_ANALYTICS_FILE = "analytics.json"


def _get_analytics_path() -> Path:
    """Return the path to the analytics JSON file, creating dirs if needed."""
    analytics_dir = get_config_dir() / _ANALYTICS_DIR_NAME
    analytics_dir.mkdir(parents=True, exist_ok=True)
    return analytics_dir / _ANALYTICS_FILE


class AnalyticsStore:
    """Read/write AnalyticsSnapshot to disk."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _get_analytics_path()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def save(self, snapshot: AnalyticsSnapshot) -> None:
        """Persist a snapshot atomically."""
        data = snapshot.model_dump_json(indent=2)
        # Atomic write: write to temp file in same directory, then rename
        parent = self._path.parent
        parent.mkdir(parents=True, exist_ok=True)
        try:
            fd, tmp = tempfile.mkstemp(dir=parent, suffix=".tmp")
            with open(fd, "w") as f:
                f.write(data)
            Path(tmp).replace(self._path)
            logger.debug("Analytics saved to %s", self._path)
        except Exception:
            logger.warning("Failed to save analytics", exc_info=True)
            # Clean up temp file on failure
            try:
                Path(tmp).unlink(missing_ok=True)
            except Exception:
                pass

    def load(self) -> AnalyticsSnapshot | None:
        """Load a previously saved snapshot. Returns None if no data."""
        if not self._path.exists():
            return None
        try:
            raw = self._path.read_text()
            return AnalyticsSnapshot.model_validate_json(raw)
        except Exception:
            logger.warning("Failed to load analytics from %s", self._path, exc_info=True)
            return None

    def delete(self) -> None:
        """Remove the stored analytics file."""
        try:
            self._path.unlink(missing_ok=True)
        except Exception:
            pass
