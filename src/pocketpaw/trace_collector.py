"""TraceCollector subscribes to system events and persists request traces."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pocketpaw.bus.events import SystemEvent
from pocketpaw.traces import TraceStore, get_trace_store

logger = logging.getLogger(__name__)

_MAX_SUMMARY_CHARS = 240


@dataclass
class _PendingToolCall:
    tool_call_id: str
    name: str
    started_at: str
    started_monotonic: float
    input_summary: str


@dataclass
class _ActiveTrace:
    trace_id: str
    session_key: str
    started_at: str
    started_monotonic: float
    inbound: dict[str, Any]
    agent_start: dict[str, Any] | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    llm_calls: list[dict[str, Any]] = field(default_factory=list)
    pending_tool_calls: dict[str, _PendingToolCall] = field(default_factory=dict)
    outbound: dict[str, Any] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    chunk_count: int = 0


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def _to_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _summarize(value: Any, max_chars: int = _MAX_SUMMARY_CHARS) -> str:
    """Create a compact summary string from arbitrary structured data."""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, default=str, sort_keys=True)
        except Exception:
            text = str(value)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


class TraceCollector:
    """Collect end-to-end request traces from message-bus system events."""

    def __init__(self, store: TraceStore | None = None) -> None:
        self._store = store or get_trace_store()
        self._active: dict[str, _ActiveTrace] = {}
        self._subscribed = False
        self._lock = asyncio.Lock()

    async def subscribe(self) -> None:
        """Subscribe to system events on the global message bus."""
        if self._subscribed:
            return
        from pocketpaw.bus import get_message_bus

        bus = get_message_bus()
        bus.subscribe_system(self._on_event)
        self._subscribed = True
        logger.info("TraceCollector subscribed to message bus")

    async def unsubscribe(self) -> None:
        """Unsubscribe from system events."""
        if not self._subscribed:
            return
        from pocketpaw.bus import get_message_bus

        bus = get_message_bus()
        bus.unsubscribe_system(self._on_event)
        self._subscribed = False

    async def cleanup_retention(self, retention_days: int) -> int:
        """Run retention cleanup for persisted traces."""
        return await self._store.cleanup_retention(retention_days)

    def snapshot(self) -> dict[str, Any]:
        """Minimal runtime state for diagnostics."""
        return {
            "subscribed": self._subscribed,
            "active_traces": len(self._active),
        }

    async def _on_event(self, event: SystemEvent) -> None:
        """Handle bus events and aggregate trace documents."""
        data = event.data or {}
        trace_id = data.get("trace_id")
        if not isinstance(trace_id, str) or not trace_id:
            return

        async with self._lock:
            if event.event_type == "trace_start":
                self._active[trace_id] = _ActiveTrace(
                    trace_id=trace_id,
                    session_key=str(data.get("session_key") or ""),
                    started_at=str(data.get("started_at") or _now_iso()),
                    started_monotonic=time.monotonic(),
                    inbound=dict(data.get("inbound") or {}),
                )
                return

            active = self._active.get(trace_id)
            if active is None:
                return

            if event.event_type == "agent_start":
                active.agent_start = {
                    "timestamp": str(data.get("timestamp") or _now_iso()),
                    "backend": str(data.get("backend") or ""),
                    "model": str(data.get("model") or ""),
                    "session_key": active.session_key,
                }
                return

            if event.event_type == "tool_start":
                tool_call_id = str(data.get("tool_call_id") or uuid.uuid4().hex)
                active.pending_tool_calls[tool_call_id] = _PendingToolCall(
                    tool_call_id=tool_call_id,
                    name=str(data.get("name") or data.get("tool") or "unknown"),
                    started_at=str(data.get("timestamp") or _now_iso()),
                    started_monotonic=time.monotonic(),
                    input_summary=_summarize(data.get("params") or data.get("input") or {}),
                )
                return

            if event.event_type == "tool_result":
                self._record_tool_result(active, data)
                return

            if event.event_type == "token_usage":
                input_tokens = _to_int(data.get("input_tokens", data.get("input", 0)), 0)
                output_tokens = _to_int(data.get("output_tokens", data.get("output", 0)), 0)
                cached_input_tokens = _to_int(
                    data.get("cached_input_tokens", data.get("cached_tokens", 0)),
                    0,
                )
                active.llm_calls.append(
                    {
                        "timestamp": str(data.get("timestamp") or _now_iso()),
                        "backend": str(data.get("backend") or ""),
                        "model": str(data.get("model") or ""),
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "cached_input_tokens": cached_input_tokens,
                        "total_tokens": input_tokens + output_tokens + cached_input_tokens,
                        "cost_usd": round(
                            _to_float(data.get("total_cost_usd", data.get("cost_usd", 0.0))),
                            6,
                        ),
                        "latency_ms": _to_int(data.get("latency_ms"), 0),
                    }
                )
                return

            if event.event_type == "error":
                active.errors.append(
                    {
                        "timestamp": str(data.get("timestamp") or _now_iso()),
                        "message": str(data.get("message") or "Unknown error"),
                        "source": str(data.get("source") or ""),
                    }
                )
                return

            if event.event_type == "trace_chunk":
                active.chunk_count += max(1, _to_int(data.get("count"), 1))
                return

            if event.event_type == "trace_end":
                await self._finalize_trace(active, data)
                self._active.pop(trace_id, None)
                return

    def _record_tool_result(self, active: _ActiveTrace, data: dict[str, Any]) -> None:
        tool_call_id = str(data.get("tool_call_id") or "")
        pending = active.pending_tool_calls.pop(tool_call_id, None)

        if pending is None:
            tool_name = str(data.get("name") or data.get("tool") or "unknown")
            for candidate_id, candidate in active.pending_tool_calls.items():
                if candidate.name == tool_name:
                    pending = candidate
                    active.pending_tool_calls.pop(candidate_id, None)
                    break

        if pending is None:
            pending = _PendingToolCall(
                tool_call_id=tool_call_id or uuid.uuid4().hex,
                name=str(data.get("name") or data.get("tool") or "unknown"),
                started_at=str(data.get("timestamp") or _now_iso()),
                started_monotonic=time.monotonic(),
                input_summary="{}",
            )

        status = str(data.get("status") or "success").lower()
        success = status not in {"error", "failed", "failure"}
        duration_ms = max(0, int((time.monotonic() - pending.started_monotonic) * 1000))
        active.tool_calls.append(
            {
                "tool_call_id": pending.tool_call_id,
                "name": pending.name,
                "started_at": pending.started_at,
                "duration_ms": duration_ms,
                "success": success,
                "input_summary": pending.input_summary,
                "output_summary": _summarize(data.get("result") or ""),
                "status": status,
            }
        )

    async def _finalize_trace(self, active: _ActiveTrace, data: dict[str, Any]) -> None:
        for pending in sorted(
            active.pending_tool_calls.values(), key=lambda item: item.started_monotonic
        ):
            duration_ms = max(0, int((time.monotonic() - pending.started_monotonic) * 1000))
            active.tool_calls.append(
                {
                    "tool_call_id": pending.tool_call_id,
                    "name": pending.name,
                    "started_at": pending.started_at,
                    "duration_ms": duration_ms,
                    "success": False,
                    "input_summary": pending.input_summary,
                    "output_summary": "No tool_result event captured",
                    "status": "missing_result",
                }
            )

        outbound = dict(data.get("outbound") or {})
        if "chunks_count" not in outbound:
            outbound["chunks_count"] = active.chunk_count
        if "timestamp" not in outbound:
            outbound["timestamp"] = _now_iso()
        if "channel" not in outbound:
            outbound["channel"] = active.inbound.get("channel", "unknown")

        ended_at = str(data.get("ended_at") or _now_iso())
        total_cost = round(sum(_to_float(call.get("cost_usd")) for call in active.llm_calls), 6)
        duration_ms = max(0, int((time.monotonic() - active.started_monotonic) * 1000))
        status = str(data.get("status") or "ok")

        if status == "ok" and active.errors:
            status = "error"

        trace_document = {
            "trace_id": active.trace_id,
            "session_key": active.session_key,
            "started_at": active.started_at,
            "ended_at": ended_at,
            "inbound": active.inbound,
            "agent_start": active.agent_start,
            "tool_calls": active.tool_calls,
            "llm_calls": active.llm_calls,
            "outbound": outbound,
            "errors": active.errors,
            "total": {
                "status": status,
                "reason": str(data.get("reason") or ""),
                "duration_ms": duration_ms,
                "total_cost_usd": total_cost,
                "tool_count": len(active.tool_calls),
                "llm_call_count": len(active.llm_calls),
            },
        }

        try:
            await self._store.append_trace(trace_document)
        except Exception:
            logger.debug("Failed to persist trace %s", active.trace_id, exc_info=True)
