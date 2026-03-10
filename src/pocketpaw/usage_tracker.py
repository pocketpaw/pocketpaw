# Usage tracker — persistent token/cost tracking across sessions.
# Created: 2026-03-09
#
# Stores per-request usage records as append-only JSONL in ~/.pocketpaw/usage.jsonl.
# Provides aggregation helpers for the /api/v1/metrics/usage endpoint.

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Pricing (per 1M tokens, USD) ──
# Updated 2025-05. Adjust as providers change pricing.
_PRICING: dict[str, dict[str, float]] = {
    # Anthropic
    "claude-sonnet-4-20250514": {"input": 3.0, "output": 15.0, "cached_input": 0.30},
    "claude-opus-4-20250514": {"input": 15.0, "output": 75.0, "cached_input": 1.50},
    "claude-haiku-4-20250506": {"input": 0.80, "output": 4.0, "cached_input": 0.08},
    "claude-3-5-sonnet-20241022": {"input": 3.0, "output": 15.0, "cached_input": 0.30},
    "claude-3-5-haiku-20241022": {"input": 0.80, "output": 4.0, "cached_input": 0.08},
    "claude-3-opus-20240229": {"input": 15.0, "output": 75.0, "cached_input": 1.50},
    # OpenAI
    "gpt-4o": {"input": 2.50, "output": 10.0},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4.1": {"input": 2.0, "output": 8.0},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "gpt-4.1-nano": {"input": 0.10, "output": 0.40},
    "o3": {"input": 2.0, "output": 8.0},
    "o3-mini": {"input": 1.10, "output": 4.40},
    "o4-mini": {"input": 1.10, "output": 4.40},
    "codex-mini-latest": {"input": 1.50, "output": 6.0},
    # Google
    "gemini-2.5-pro": {"input": 1.25, "output": 10.0},
    "gemini-2.5-flash": {"input": 0.15, "output": 0.60},
    "gemini-2.0-flash": {"input": 0.10, "output": 0.40},
}


def _estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
) -> float | None:
    """Estimate USD cost. Returns None if model pricing is unknown."""
    pricing = _PRICING.get(model)
    if not pricing:
        # Try prefix match in both directions:
        # "gpt-4o-2024-11-20" starts with "gpt-4o" (dated variant -> base key)
        # "claude-sonnet-4-6" is a prefix of "claude-sonnet-4-6-20250514" (alias -> dated key)
        for key, p in _PRICING.items():
            if model.startswith(key) or key.startswith(model):
                pricing = p
                break
    if not pricing:
        return None

    cost = (
        max(0, input_tokens - cached_input_tokens) * pricing["input"]
        + output_tokens * pricing["output"]
        + cached_input_tokens * pricing.get("cached_input", pricing["input"])
    ) / 1_000_000
    return round(cost, 6)


@dataclass
class UsageRecord:
    """Single usage record for one agent turn."""

    timestamp: str
    backend: str
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None
    session_id: str = ""


@dataclass
class UsageSummary:
    """Aggregated usage stats."""

    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cached_input_tokens: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    request_count: int = 0
    by_model: dict = field(default_factory=dict)
    by_backend: dict = field(default_factory=dict)


class UsageTracker:
    """Append-only usage tracker with JSONL persistence."""

    def __init__(self, path: Path | None = None):
        if path is None:
            from pocketpaw.config import get_config_dir

            path = get_config_dir() / "usage.jsonl"
        self._path = path
        self._lock = threading.Lock()

    def record(
        self,
        backend: str,
        model: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_input_tokens: int = 0,
        session_id: str = "",
        total_cost_usd: float | None = None,
    ) -> UsageRecord:
        """Record a usage entry and persist to disk.

        If total_cost_usd is provided (e.g. from Claude Agent SDK's
        ResultMessage), it is used as the authoritative cost. Otherwise
        we estimate from the pricing table.
        """
        total = input_tokens + output_tokens
        cost = (
            total_cost_usd
            if total_cost_usd is not None
            else _estimate_cost(model, input_tokens, output_tokens, cached_input_tokens)
        )

        record = UsageRecord(
            timestamp=datetime.now(tz=UTC).isoformat(),
            backend=backend,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            total_tokens=total,
            cost_usd=cost,
            session_id=session_id,
        )

        try:
            with self._lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._path, "a") as f:
                    f.write(json.dumps(asdict(record)) + "\n")
        except Exception as e:
            logger.warning("Failed to write usage record: %s", e)

        return record

    def get_records(self, limit: int = 100) -> list[UsageRecord]:
        """Read recent records (newest first)."""
        if not self._path.exists():
            return []
        records: list[UsageRecord] = []
        try:
            lines = self._path.read_text().strip().split("\n")
            for line in reversed(lines):
                if len(records) >= limit:
                    break
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    records.append(UsageRecord(**data))
                except Exception:
                    pass
        except Exception as e:
            logger.warning("Failed to read usage records: %s", e)
        return records

    def get_summary(self, since: str | None = None) -> dict:
        """Get aggregated usage summary, optionally filtered by timestamp."""
        records = self.get_records(limit=10_000)
        if since:
            records = [r for r in records if r.timestamp >= since]

        summary = UsageSummary()
        for r in records:
            summary.total_input_tokens += r.input_tokens
            summary.total_output_tokens += r.output_tokens
            summary.total_cached_input_tokens += r.cached_input_tokens
            summary.total_tokens += r.total_tokens
            if r.cost_usd is not None:
                summary.total_cost_usd += r.cost_usd
            summary.request_count += 1

            # By model
            if r.model:
                m = summary.by_model.setdefault(
                    r.model, {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "count": 0}
                )
                m["input_tokens"] += r.input_tokens
                m["output_tokens"] += r.output_tokens
                if r.cost_usd is not None:
                    m["cost_usd"] += r.cost_usd
                m["count"] += 1

            # By backend
            b = summary.by_backend.setdefault(
                r.backend, {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "count": 0}
            )
            b["input_tokens"] += r.input_tokens
            b["output_tokens"] += r.output_tokens
            if r.cost_usd is not None:
                b["cost_usd"] += r.cost_usd
            b["count"] += 1

        summary.total_cost_usd = round(summary.total_cost_usd, 6)
        return asdict(summary)

    def clear(self) -> None:
        """Clear all usage records."""
        try:
            with self._lock:
                if self._path.exists():
                    self._path.write_text("")
        except Exception as e:
            logger.warning("Failed to clear usage records: %s", e)


# Singleton
_tracker: UsageTracker | None = None


def get_usage_tracker() -> UsageTracker:
    global _tracker
    if _tracker is None:
        _tracker = UsageTracker()
    return _tracker
