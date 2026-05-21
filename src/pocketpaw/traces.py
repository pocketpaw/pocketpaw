"""Trace storage utilities for request-level observability."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pocketpaw.config import get_config_dir

logger = logging.getLogger(__name__)


def _get_trace_dir() -> Path:
    """Get/create the trace storage directory."""
    path = get_config_dir() / "traces"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _trace_started_at(trace: dict[str, Any]) -> str:
    """Best-effort retrieval of trace start timestamp string."""
    started_at = trace.get("started_at")
    if isinstance(started_at, str) and started_at:
        return started_at

    inbound = trace.get("inbound") if isinstance(trace.get("inbound"), dict) else {}
    inbound_ts = inbound.get("timestamp") if isinstance(inbound, dict) else None
    if isinstance(inbound_ts, str) and inbound_ts:
        return inbound_ts

    return datetime.now(tz=UTC).isoformat()


def _trace_cost(trace: dict[str, Any]) -> float:
    total = trace.get("total") if isinstance(trace.get("total"), dict) else {}
    try:
        return float((total or {}).get("total_cost_usd") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _trace_summary(trace: dict[str, Any]) -> dict[str, Any]:
    """Compact view used by list endpoints."""
    total = trace.get("total") if isinstance(trace.get("total"), dict) else {}
    inbound = trace.get("inbound") if isinstance(trace.get("inbound"), dict) else {}
    session_key = str(trace.get("session_key") or "")
    _, _, session_id = session_key.partition(":")
    return {
        "trace_id": trace.get("trace_id", ""),
        "session_key": session_key,
        "session_id": session_id or session_key,
        "channel": inbound.get("channel", "unknown"),
        "started_at": _trace_started_at(trace),
        "ended_at": trace.get("ended_at", ""),
        "status": total.get("status", "ok"),
        "duration_ms": int(total.get("duration_ms") or 0),
        "total_cost_usd": round(_trace_cost(trace), 6),
        "tool_count": int(total.get("tool_count") or 0),
        "llm_call_count": int(total.get("llm_call_count") or 0),
    }


class TraceStore:
    """Append-only trace store with daily JSONL partitioning."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or _get_trace_dir()
        self._lock = threading.Lock()

    @property
    def root(self) -> Path:
        return self._root

    def _file_for_timestamp(self, timestamp: str) -> Path:
        dt = _parse_iso(timestamp) or datetime.now(tz=UTC)
        return self._root / f"{dt.date().isoformat()}.jsonl"

    def _iter_files_newest_first(self) -> list[Path]:
        return sorted(self._root.glob("*.jsonl"), reverse=True)

    def _append_trace_sync(self, trace: dict[str, Any]) -> None:
        path = self._file_for_timestamp(_trace_started_at(trace))
        with self._lock:
            self._root.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(trace, default=str) + "\n")

    async def append_trace(self, trace: dict[str, Any]) -> None:
        """Append one trace document to daily storage."""
        await asyncio.to_thread(self._append_trace_sync, trace)

    def _cleanup_retention_sync(self, retention_days: int) -> int:
        retention_days = max(1, int(retention_days))
        cutoff_date = (datetime.now(tz=UTC) - timedelta(days=retention_days)).date()
        removed = 0

        with self._lock:
            for file_path in self._root.glob("*.jsonl"):
                try:
                    file_date = datetime.strptime(file_path.stem, "%Y-%m-%d").date()
                except ValueError:
                    continue
                if file_date < cutoff_date:
                    file_path.unlink(missing_ok=True)
                    removed += 1
        return removed

    async def cleanup_retention(self, retention_days: int) -> int:
        """Delete trace partitions older than configured retention."""
        return await asyncio.to_thread(self._cleanup_retention_sync, retention_days)

    def _read_file_traces(self, file_path: Path) -> list[dict[str, Any]]:
        traces: list[dict[str, Any]] = []
        try:
            for line in file_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict):
                    traces.append(data)
        except Exception as exc:
            logger.debug("Failed to read trace file %s: %s", file_path, exc)
        return traces

    def _list_traces_sync(
        self,
        *,
        since: str | None,
        limit: int,
        session_id: str,
        min_cost: float,
        summaries_only: bool,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 5000))
        since_dt = _parse_iso(since)
        session_id = session_id.strip()
        results: list[dict[str, Any]] = []

        for file_path in self._iter_files_newest_first():
            traces = self._read_file_traces(file_path)
            for trace in reversed(traces):
                started_dt = _parse_iso(_trace_started_at(trace))
                if since_dt and started_dt and started_dt < since_dt:
                    continue

                trace_session_key = str(trace.get("session_key") or "")
                _, _, trace_session_id = trace_session_key.partition(":")
                if session_id and session_id not in {trace_session_key, trace_session_id}:
                    continue

                if _trace_cost(trace) < max(0.0, min_cost):
                    continue

                results.append(_trace_summary(trace) if summaries_only else trace)
                if len(results) >= limit:
                    return results

        return results

    async def list_traces(
        self,
        *,
        since: str | None = None,
        limit: int = 100,
        session_id: str = "",
        min_cost: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Return filtered trace summaries, newest first."""
        return await asyncio.to_thread(
            self._list_traces_sync,
            since=since,
            limit=limit,
            session_id=session_id,
            min_cost=min_cost,
            summaries_only=True,
        )

    async def get_full_traces(
        self,
        *,
        since: str | None = None,
        limit: int = 1000,
        session_id: str = "",
        min_cost: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Return full trace payloads for aggregation/analytics."""
        return await asyncio.to_thread(
            self._list_traces_sync,
            since=since,
            limit=limit,
            session_id=session_id,
            min_cost=min_cost,
            summaries_only=False,
        )

    def _get_trace_sync(self, trace_id: str) -> dict[str, Any] | None:
        if not trace_id:
            return None

        for file_path in self._iter_files_newest_first():
            traces = self._read_file_traces(file_path)
            for trace in reversed(traces):
                if str(trace.get("trace_id") or "") == trace_id:
                    return trace
        return None

    async def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        """Return full trace payload by trace_id."""
        return await asyncio.to_thread(self._get_trace_sync, trace_id)


_trace_store: TraceStore | None = None


def get_trace_store() -> TraceStore:
    """Global trace store singleton."""
    global _trace_store
    if _trace_store is None:
        _trace_store = TraceStore()

        from pocketpaw.lifecycle import register

        def _reset() -> None:
            global _trace_store
            _trace_store = None

        register("trace_store", reset=_reset)
    return _trace_store
