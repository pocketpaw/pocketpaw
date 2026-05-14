# ee/cloud/activity/buffer.py
# Created: 2026-05-13 (feat/mission-control-facade) — per-workspace activity
# ring buffer. Subscribes to agent.thinking / agent.tool_use / agent.tool_start
# / agent.stream_end on the in-process bus and feeds Mission Control's live
# activity ticker. Bounded (~200 entries per workspace) with a 1-hour TTL so
# memory stays flat on a long-running process. Durability lives in Pawprints
# (Instinct audit); this module is decoration.
"""Per-workspace in-memory activity buffer.

Mission Control's status bar + Activity tab show "what the agent is doing
right now": tool calls, thinking notes, completions. That stream is high-
volume and low-value-per-entry — persisting every line to Mongo would
swamp the journal for a feature that operators consume in seconds. So we
buffer in-memory with eviction by both count and age, and rebuild from
zero on process restart.

The audit doc (`docs/internal/2026-05-mission-control-backend-audit.md`,
section "C. Cross-cutting: activity buffer") commits to this trade-off:
"Pawprints is the durable record; activity is the live decoration."

Wiring:
  1. ``register_activity_listeners()`` runs once at app boot from
     ``mount_cloud`` (after ``init_realtime`` installs the singleton bus).
  2. Each subscribed bus event maps to an ``ActivityEvent`` and gets
     pushed onto the workspace's deque via :meth:`Buffer.push`.
  3. The push also calls ``push_sse_event("activity.recorded", ...)`` so
     SSE-streaming consumers see entries without polling.
  4. ``ee.cloud.mission_control.service.agent_list_activity`` reads via
     ``get_buffer().get_recent(workspace_id, limit)``.

Concurrency:
  All deque operations run inside ``asyncio`` handlers on the cloud event
  loop — no thread pool, no locks needed. If we ever hand the buffer to a
  worker thread we'll need to revisit.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ee.cloud._core.realtime.bus import get_bus
from ee.cloud._core.realtime.events import Event

logger = logging.getLogger(__name__)


_MAX_ENTRIES_PER_WORKSPACE = 200
_TTL_SECONDS = 60 * 60  # 1 hour


@dataclass(frozen=True)
class ActivityEvent:
    """One entry in the workspace activity feed.

    The shape is intentionally lossy compared to the source bus event —
    the Mission Control consumer only needs the kind, agent, summary,
    and pocket reference to render the ticker. The fully-detailed
    reasoning trace stays on the originating ``AgentToolUse`` /
    ``AgentThinking`` payload for callers that want it.
    """

    workspace_id: str
    kind: str  # tool_call | thinking | completed | waiting
    agent_id: str | None
    summary: str
    pocket_id: str | None
    ts: float  # unix seconds — used for TTL pruning + display
    extra: dict[str, Any] = field(default_factory=dict)


class Buffer:
    """Per-workspace deques of :class:`ActivityEvent` with TTL pruning.

    Singleton-per-process: the module exposes one instance via
    :func:`get_buffer`. Tests that need isolation call ``reset()`` between
    cases.
    """

    def __init__(
        self,
        *,
        max_per_workspace: int = _MAX_ENTRIES_PER_WORKSPACE,
        ttl_seconds: int = _TTL_SECONDS,
    ) -> None:
        self._max = max_per_workspace
        self._ttl = ttl_seconds
        self._deques: dict[str, deque[ActivityEvent]] = {}
        # Local fan-out handlers so unit tests can assert without spinning
        # up an SSE stream. Mission Control's SSE consumer also registers
        # one here.
        self._subscribers: list[Any] = []

    def push(self, ev: ActivityEvent) -> None:
        """Append ``ev`` to its workspace deque, prune expired neighbours.

        Eviction order: TTL first (cheap O(k) scan from the front), then
        the deque's built-in maxlen handles the overflow.
        """
        if not ev.workspace_id:
            logger.debug("activity push without workspace_id; dropping kind=%s", ev.kind)
            return
        d = self._deques.get(ev.workspace_id)
        if d is None:
            d = deque(maxlen=self._max)
            self._deques[ev.workspace_id] = d
        # TTL prune from the left
        cutoff = ev.ts - self._ttl
        while d and d[0].ts < cutoff:
            d.popleft()
        d.append(ev)
        for fn in list(self._subscribers):
            try:
                fn(ev)
            except Exception:
                logger.debug("activity subscriber failed; continuing", exc_info=True)

    def get_recent(self, workspace_id: str, limit: int = 30) -> list[ActivityEvent]:
        """Return the most-recent entries for ``workspace_id``, newest first.

        Applies TTL pruning before returning so callers always see a
        consistent view even if no recent push triggered an eviction.
        """
        d = self._deques.get(workspace_id)
        if not d:
            return []
        cutoff = time.time() - self._ttl
        while d and d[0].ts < cutoff:
            d.popleft()
        # newest first
        return list(reversed(list(d)))[: max(0, limit)]

    def subscribe(self, fn: Any) -> None:
        """Register a synchronous fan-out callback. Used by Mission Control's
        SSE bridge and by unit tests that assert delivery."""
        self._subscribers.append(fn)

    def unsubscribe(self, fn: Any) -> None:
        try:
            self._subscribers.remove(fn)
        except ValueError:
            pass

    def reset(self) -> None:
        """Empty every workspace deque and drop subscribers. Tests only."""
        self._deques.clear()
        self._subscribers.clear()


_buffer: Buffer | None = None


def get_buffer() -> Buffer:
    """Return the module-level singleton (lazy-construct on first access)."""
    global _buffer
    if _buffer is None:
        _buffer = Buffer()
    return _buffer


# ---------------------------------------------------------------------------
# Bus handlers
# ---------------------------------------------------------------------------


def _extract_kind(event_type: str) -> str:
    """Map a bus event type to the four buckets Mission Control surfaces.

    Bus emits a richer vocabulary (``agent.tool_start`` /
    ``agent.tool_result`` / ``agent.stream_end`` etc). Mission Control's
    ticker only needs the operator-facing kind: ``tool_call``,
    ``thinking``, ``completed``, ``waiting``. Unknown events fall back to
    the raw type string so we don't silently drop new bus traffic.
    """
    if event_type in ("agent.tool_start", "agent.tool_use", "agent.tool_call"):
        return "tool_call"
    if event_type == "agent.thinking":
        return "thinking"
    if event_type in ("agent.stream_end", "agent.completed"):
        return "completed"
    if event_type in ("agent.waiting", "agent.tool_result"):
        return "waiting"
    return event_type


def _summarise(event: Event) -> str:
    """Best-effort one-line summary from the event payload.

    Different agent events stash different fields — fish out the most
    likely "what just happened" string. Falls back to the event type so
    the ticker never renders an empty row.
    """
    data = event.data or {}
    for key in ("summary", "message", "tool", "tool_name", "thought", "name"):
        v = data.get(key)
        if isinstance(v, str) and v:
            return v
    return event.type


async def _handle_agent_event(event: Event) -> None:
    """Bus handler — translates an agent event into an ActivityEvent.

    Defensive: a malformed bus payload (missing workspace_id, weird
    types) drops the entry rather than crashing the bus. The bus already
    wraps each handler in try/except but we want the failure mode to
    surface as a debug log, not a stacktrace.
    """
    data = event.data or {}
    workspace_id = data.get("workspace_id") or data.get("workspace") or ""
    if not workspace_id:
        return
    activity = ActivityEvent(
        workspace_id=str(workspace_id),
        kind=_extract_kind(event.type),
        agent_id=data.get("agent_id"),
        summary=_summarise(event),
        pocket_id=data.get("pocket_id"),
        ts=time.time(),
        extra={"event_type": event.type, "raw": {k: v for k, v in data.items() if k != "raw"}},
    )
    get_buffer().push(activity)
    # SSE fan-out: best-effort, no-op when there's no active stream in scope.
    try:
        from ee.cloud.chat.agent_service import push_sse_event

        push_sse_event(
            "activity.recorded",
            {
                "workspace_id": activity.workspace_id,
                "kind": activity.kind,
                "agent_id": activity.agent_id,
                "summary": activity.summary,
                "pocket_id": activity.pocket_id,
                "ts": activity.ts,
            },
        )
    except Exception:
        logger.debug("activity SSE push failed; continuing", exc_info=True)


_SUBSCRIBED_EVENT_TYPES = (
    "agent.thinking",
    "agent.tool_start",
    "agent.tool_use",
    "agent.tool_result",
    "agent.stream_end",
    # Forward-compat names — if a future bus event lands with these
    # names we'll pick it up without changing the buffer.
    "agent.tool_call",
    "agent.completed",
    "agent.waiting",
)


def register_activity_listeners() -> None:
    """Wire the agent-event subscribers onto the singleton bus.

    Idempotent at the framework level: ``mount_cloud`` calls this once.
    Calling twice would register the handler twice, which would double
    every entry — protect callers by checking the per-process flag.
    """
    global _registered
    if _registered:
        return
    bus = get_bus()
    for evt_type in _SUBSCRIBED_EVENT_TYPES:
        bus.subscribe(evt_type, _handle_agent_event)
    _registered = True


_registered = False


__all__ = [
    "ActivityEvent",
    "Buffer",
    "get_buffer",
    "register_activity_listeners",
]
