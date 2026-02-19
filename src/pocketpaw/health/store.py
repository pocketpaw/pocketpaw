# ErrorStore — persistent error log for PocketPaw health engine.
# Created: 2026-02-17
# Persists errors to ~/.pocketpaw/health/errors.jsonl (append-only JSONL).

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pocketpaw.config import get_config_dir

logger = logging.getLogger(__name__)

_MAX_ROTATION_FILES = 5
_DEFAULT_MAX_SIZE_MB = 10


def _get_health_dir() -> Path:
    """Get/create the health data directory."""
    d = get_config_dir() / "health"
    d.mkdir(exist_ok=True)
    return d


class ErrorStore:
    """Append-only JSONL store for persistent error logging.

    Errors survive page refresh and server restart.
    """

    def __init__(self, path: Path | None = None):
        self._path = path or (_get_health_dir() / "errors.jsonl")

    @property
    def path(self) -> Path:
        return self._path

    def record(
        self,
        message: str,
        source: str = "unknown",
        severity: str = "error",
        traceback: str = "",
        context: dict | None = None,
    ) -> str:
        """Append an error entry. Returns the generated error ID."""
        error_id = uuid.uuid4().hex[:12]
        entry = {
            "id": error_id,
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "source": source,
            "severity": severity,
            "message": message,
            "traceback": traceback,
            "context": context or {},
        }
        try:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception as e:
            logger.warning("ErrorStore.record failed: %s", e)
        return error_id

    def get_recent(self, limit: int = 20, search: str = "") -> list[dict]:
        """Tail-read recent errors, optionally filtering by search string."""
        if not self._path.exists():
            return []

        try:
            lines = self._path.read_text(encoding="utf-8").strip().splitlines()
        except Exception as e:
            logger.warning("ErrorStore.get_recent failed: %s", e)
            return []

        # Parse from newest to oldest
        results: list[dict] = []
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            if search:
                haystack = (
                    entry.get("message", "") + entry.get("source", "") + entry.get("traceback", "")
                ).lower()
                if search.lower() not in haystack:
                    continue

            results.append(entry)
            if len(results) >= limit:
                break

        return results

    def rotate_if_needed(self, max_size_mb: float = _DEFAULT_MAX_SIZE_MB) -> bool:
        """Rotate errors.jsonl if it exceeds max_size_mb.

        Rotates to errors.jsonl.1 ... errors.jsonl.5.
        Returns True if rotation happened.
        """
        if not self._path.exists():
            return False

        size_mb = self._path.stat().st_size / (1024 * 1024)
        if size_mb <= max_size_mb:
            return False

        # Shift existing rotated files
        for i in range(_MAX_ROTATION_FILES, 0, -1):
            old = self._path.with_suffix(f".jsonl.{i}")
            new = self._path.with_suffix(f".jsonl.{i + 1}")
            if i == _MAX_ROTATION_FILES and old.exists():
                old.unlink()
            elif old.exists():
                old.rename(new)

        # Move current to .1
        self._path.rename(self._path.with_suffix(".jsonl.1"))
        return True

    def clear(self) -> None:
        """Remove all stored errors (for testing)."""
        if self._path.exists():
            self._path.unlink()
