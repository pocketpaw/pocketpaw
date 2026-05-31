# cost_tracker.py — Rough monthly spend tracker for embedding calls.
# Created: 2026-04-30 — Phase 2 of "Files as Knowledge" plan, Stage 2.D.
# Persists at ~/.pocketpaw/embedding_cost.json so a process restart inside
# the same calendar month keeps the running total. NOT billing-grade —
# cap is a soft guard so a runaway loop can't drain the embedding budget.
"""Rough monthly spend tracker.

The listener calls :meth:`can_spend` before each embedding call and
:meth:`record` after a successful one. When ``can_spend`` says no, the
listener falls back to extraction-only (text still goes to kb-go, vector
ingest is skipped) and logs the cap hit at INFO so the captain sees it.

Persistence is a tiny JSON file at ``~/.pocketpaw/embedding_cost.json``:

    {"month": "2026-05", "spent_usd": 0.4231}

Any deserialisation error resets the file to a fresh state for the
current month. We trade strict accounting for never crashing the
listener over a corrupted ledger.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)

_DEFAULT_PATH = Path.home() / ".pocketpaw" / "embedding_cost.json"


class CostTracker:
    """Soft monthly cap for embedding spend.

    Thread-safe via an internal lock — callers from the FastAPI worker
    pool may share a single tracker (``get_cost_tracker``) so a parallel
    upload burst can't double-count or race the rollover check.
    """

    def __init__(
        self,
        monthly_cap_usd: float,
        path: Path | None = None,
    ) -> None:
        self._cap = monthly_cap_usd
        self._path = path or _DEFAULT_PATH
        self._lock = Lock()
        self._state = self._load()

    # --- public API ------------------------------------------------------

    @property
    def cap_usd(self) -> float:
        return self._cap

    @property
    def spent_this_month(self) -> float:
        with self._lock:
            self._roll_if_new_month()
            return float(self._state.get("spent_usd", 0.0))

    def can_spend(self, estimated_cost: float) -> bool:
        """Return False when adding ``estimated_cost`` would exceed the cap.

        A non-positive ``estimated_cost`` is treated as zero — pre-call
        estimates are best-effort and a bad guess shouldn't block the
        whole pipeline.
        """
        if self._cap <= 0:
            return True  # disabled cap: always allow
        with self._lock:
            self._roll_if_new_month()
            spent = float(self._state.get("spent_usd", 0.0))
            return spent + max(0.0, float(estimated_cost)) <= self._cap

    def record(self, cost: float) -> None:
        """Add ``cost`` to this month's running total. Negative costs ignored."""
        if cost <= 0:
            return
        with self._lock:
            self._roll_if_new_month()
            current = float(self._state.get("spent_usd", 0.0))
            self._state["spent_usd"] = current + float(cost)
            self._save()

    # --- internals -------------------------------------------------------

    @staticmethod
    def _current_month_key() -> str:
        # UTC keeps the rollover unambiguous across hosts in different
        # timezones. Real billing ledgers use UTC for the same reason.
        return datetime.now(UTC).strftime("%Y-%m")

    def _roll_if_new_month(self) -> None:
        """Reset the running total when the calendar month rolls over."""
        current_key = self._current_month_key()
        if self._state.get("month") != current_key:
            self._state = {"month": current_key, "spent_usd": 0.0}
            self._save()

    def _load(self) -> dict:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"month": self._current_month_key(), "spent_usd": 0.0}
        except OSError as exc:
            logger.warning("cost tracker read failed (%s); resetting", exc)
            return {"month": self._current_month_key(), "spent_usd": 0.0}
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except (ValueError, json.JSONDecodeError) as exc:
            logger.warning("cost tracker parse failed (%s); resetting", exc)
            return {"month": self._current_month_key(), "spent_usd": 0.0}

        # If the persisted month doesn't match today, treat the file as
        # rolled over. The first record() call will rewrite it.
        if data.get("month") != self._current_month_key():
            return {"month": self._current_month_key(), "spent_usd": 0.0}
        # Coerce bad floats to zero rather than blow up on disk corruption.
        try:
            data["spent_usd"] = float(data.get("spent_usd", 0.0))
        except (TypeError, ValueError):
            data["spent_usd"] = 0.0
        return data

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._state, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            # Persistence failure shouldn't crash the listener. The cap
            # will reset on next process start — accept the loss.
            logger.warning("cost tracker write failed (%s)", exc)


_tracker: CostTracker | None = None


def get_cost_tracker(settings) -> CostTracker:
    """Return the process-wide tracker, building it lazily from settings.

    The listener calls this on every event so we cache the instance to
    avoid re-reading the JSON ledger from disk per upload.
    """
    global _tracker  # noqa: PLW0603 — process-wide singleton
    if _tracker is None or _tracker.cap_usd != settings.embedding_monthly_cap_usd:
        _tracker = CostTracker(monthly_cap_usd=settings.embedding_monthly_cap_usd)
    return _tracker


def reset_cost_tracker_for_tests() -> None:
    """Test helper: clear the singleton so each test gets a fresh tracker.

    Production paths shouldn't call this — the singleton is the whole
    point of the soft cap. Tests use it to swap the on-disk path
    between fixtures.
    """
    global _tracker  # noqa: PLW0603
    _tracker = None


__all__ = ["CostTracker", "get_cost_tracker", "reset_cost_tracker_for_tests"]
