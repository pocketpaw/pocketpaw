# ee/instinct/trace_collector.py — Async context manager that captures reasoning
# inputs for a single proposal.
# Created: 2026-04-13 (Move 2 PR-A) — Subscribes to SystemEvents on the bus,
# aggregates fabric_query / soul_recall / kb_inject / tool_start+tool_end events
# into a ReasoningTrace, and exposes the finished trace for persistence by the
# caller. No global state: a fresh collector per proposal keeps traces isolated.

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

from ee.instinct.trace import ReasoningTrace, ToolCallRef

logger = logging.getLogger(__name__)

_PREVIEW_CHARS = 200
_TOOL_EVENT_START = "tool_start"
_TOOL_EVENT_END = "tool_end"
_TOOL_EVENT_RESULT = "tool_result"
_FABRIC_EVENT = "fabric_query"
_SOUL_EVENT = "soul_recall"
_KB_EVENT = "kb_inject"


class TraceCollector:
    """Async context manager that captures reasoning events on the message bus.

    Usage:
        async with TraceCollector(bus) as trace:
            action = await agent.propose(...)
        # trace now holds the captured ReasoningTrace

    The collector subscribes to `subscribe_system` on enter and unsubscribes on
    exit — always, even if the body raises. It aggregates:

    - Fabric queries (event_type="fabric_query", data["object_id"])
    - Soul recalls (event_type="soul_recall", data["memory_id"])
    - KB injections (event_type="kb_inject", data["article_id"])
    - Tool calls (event_type="tool_start"/"tool_end" with matching tool name)

    Unknown event types are ignored. Duplicate IDs within a single trace are
    preserved in insertion order but the `fabric_queries` / `soul_memories` /
    `kb_articles` lists are deduplicated on exit so the trace body stays
    compact.
    """

    def __init__(
        self,
        bus: Any,
        *,
        prompt_version: str = "",
        backend: str = "",
        model: str = "",
    ) -> None:
        self._bus = bus
        self.trace = ReasoningTrace(
            prompt_version=prompt_version,
            backend=backend,
            model=model,
        )
        self._pending_tool_starts: dict[str, float] = {}
        self._callback: Any = None

    async def __aenter__(self) -> TraceCollector:
        self._callback = self._on_event
        self._bus.subscribe_system(self._callback)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._callback is not None:
            try:
                self._bus.unsubscribe_system(self._callback)
            except Exception:
                logger.debug("TraceCollector unsubscribe failed")
            self._callback = None
        # Deduplicate the reference lists while preserving insertion order.
        self.trace.fabric_queries = _dedupe(self.trace.fabric_queries)
        self.trace.soul_memories = _dedupe(self.trace.soul_memories)
        self.trace.kb_articles = _dedupe(self.trace.kb_articles)

    async def _on_event(self, event: Any) -> None:
        event_type = getattr(event, "event_type", None)
        data = getattr(event, "data", {}) or {}
        if event_type == _FABRIC_EVENT:
            oid = data.get("object_id")
            if isinstance(oid, str):
                self.trace.fabric_queries.append(oid)
        elif event_type == _SOUL_EVENT:
            mid = data.get("memory_id")
            if isinstance(mid, str):
                self.trace.soul_memories.append(mid)
        elif event_type == _KB_EVENT:
            aid = data.get("article_id")
            if isinstance(aid, str):
                self.trace.kb_articles.append(aid)
        elif event_type == _TOOL_EVENT_START:
            tool = data.get("tool")
            if isinstance(tool, str):
                self._pending_tool_starts[tool] = time.monotonic()
        elif event_type in (_TOOL_EVENT_END, _TOOL_EVENT_RESULT):
            self._record_tool_end(data)

    def _record_tool_end(self, data: dict[str, Any]) -> None:
        tool = data.get("tool")
        if not isinstance(tool, str):
            return
        args = data.get("args", {})
        result = data.get("result", "")
        started = self._pending_tool_starts.pop(tool, None)
        duration_ms = int((time.monotonic() - started) * 1000) if started is not None else 0

        args_hash = _hash_args(args)
        preview = str(result)
        if len(preview) > _PREVIEW_CHARS:
            preview = preview[: _PREVIEW_CHARS - 3] + "..."

        # Dedupe identical tool+args pairs within a single trace.
        for existing in self.trace.tool_calls:
            if existing.tool == tool and existing.args_hash == args_hash:
                return

        self.trace.tool_calls.append(
            ToolCallRef(
                tool=tool,
                args_hash=args_hash,
                result_preview=preview,
                duration_ms=duration_ms,
            ),
        )


def _hash_args(args: Any) -> str:
    try:
        serialized = json.dumps(args, sort_keys=True, default=str)
    except Exception:
        serialized = str(args)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


def _dedupe(values: list[str]) -> list[str]:
    """Dedupe while preserving the order of first appearance."""
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out
