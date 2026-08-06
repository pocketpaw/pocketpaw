# bridges/calendar.py — Task → Calendar one-way bridge.
# Updated: 2026-08-06 (review follow-up) — slim-payload guard: an emit
#   whose task dict OMITS the due_at key (planner requeue's hand-built
#   payload) no longer deletes a live event; absent != cleared. Also
#   swapped the due_at assert for a real branch (-O strips asserts).
# Created: 2026-08-06 (feat/coupling-tasks-on-calendar, coupling wave T-12).
#   A task with a ``due_at`` now shows up on /calendar so the user plans
#   their day against real deadlines instead of a separate task feed.
#   Modeled directly on ``meetings/bridges/calendar.py`` (the reverse
#   meeting→calendar bridge): synthetic calendar id, ``fabric_object_id``
#   linkage, idempotency via find-before-create, and hard containment so
#   a calendar failure can never break a task write.
"""Task → Calendar bridge.

One direction only (tasks → calendar). When a Task carries a ``due_at``,
mint a CalendarEvent on the synthetic ``tasks`` calendar so the deadline
shows up on /calendar next to meetings and appointments. The event is
stamped with ``fabric_object_id="task:{task_id}"`` — the same linkage
convention the meetings bridge uses (``meeting:{id}``) — so re-emits
converge instead of duplicating, and so other calendar consumers can
recognise bridge-minted rows.

Subscribes the typed Task events on the ``_core.realtime`` bus (the bus
``tasks/service.py`` actually emits on — NOT ``shared.events.event_bus``,
which carries the calendar/meeting topics):

* ``task.proposed``  → task created; mint an event when ``due_at`` is set
* ``task.updated``   → converge: due date moved → move the event; due date
  cleared → delete the event; due date newly set (or task reverted back
  to an active status) → mint the event
* ``task.resolved``  → task completed (``done`` or ``awaiting_approval``)
  → delete the event; the deadline no longer needs day-planning

Every handler runs the same reconciler: *the calendar event for a task
exists iff the task has a ``due_at`` and its status still needs doing*.
That invariant makes each handler idempotent under bus re-delivery and
keeps the two surfaces convergent no matter which event arrives.

Containment: the realtime bus already swallows per-handler exceptions,
and every calendar call here is additionally wrapped — a task write must
always succeed even when the calendar service is down (mirrors the
meetings bridge's ``# bridge must not break meeting create`` posture).

Loop guard: this bridge consumes only ``task.*`` events, so it cannot
re-trigger itself. The calendar events it mints DO fire
``calendar.event.created`` on the shared bus, where the meetings forward
bridge scans descriptions for Zoom/Meet URLs — that handler skips any
event whose ``fabric_object_id`` names a bridge-owned object (``task:``
included), so a task titled with a meeting link can't mint a phantom
Meeting.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from pocketpaw_ee.cloud._core.realtime.bus import get_bus
from pocketpaw_ee.cloud._core.realtime.events import (
    Event,
    TaskProposed,
    TaskResolved,
    TaskUpdated,
)

logger = logging.getLogger(__name__)


# All task-derived calendar events land on one synthetic calendar so they
# group cleanly in /calendar UIs. Per ``calendar.service._load_calendar``,
# no Calendar row needs to exist — the service synthesizes a default
# workspace-public Calendar with this id, owned by the request actor.
# Mirrors the meetings bridge's ``_MEETING_CALENDAR_ID = "meetings"``.
_TASK_CALENDAR_ID = "tasks"

# The linkage prefix stamped into ``fabric_object_id``. Convention shared
# with the meetings bridge (``meeting:{id}``).
_FABRIC_PREFIX = "task:"

# A task's deadline belongs on the calendar only while the work still
# needs doing. ``done`` / ``reverted`` / ``failed`` are terminal;
# ``awaiting_approval`` means the assignee finished and is waiting on
# sign-off — the deadline no longer needs day-planning, and excluding it
# here keeps an unrelated later edit (title tweak while awaiting
# approval) from resurrecting a deleted event.
_CALENDAR_ELIGIBLE_STATUSES = frozenset({"proposed", "in_progress", "blocked"})

# Due dates are points in time; calendar events need a window. Same
# default the meetings bridge uses for events without an end.
_DEFAULT_DURATION = timedelta(minutes=30)


# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------


def _extract_task(event: Event) -> dict | None:
    """Pull the task dict from a task.* event payload, or ``None`` when
    absent. ``task.updated`` fired for a task-comment ships ``task=None``
    on purpose — that emit must be a no-op here, not a crash."""

    data = getattr(event, "data", None) or {}
    task = data.get("task")
    if not isinstance(task, dict):
        return None
    return task


def _parse_due_at(value: Any) -> datetime | None:
    """Coerce the payload's ``due_at`` into an aware UTC datetime.

    The task DTO serialises ``due_at`` via ``iso_utc`` (an ISO-8601
    string); accept a raw ``datetime`` too so internal callers that pass
    domain objects straight through keep working. Anything unparseable
    is treated as absent rather than raising — a malformed field must
    not take down the subscriber chain.
    """

    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _bridge_ctx(workspace_id: str, user_id: str) -> Any:
    """Build the calendar RequestContext carrying the task's workspace.

    Single chokepoint for tenancy: every calendar call this bridge makes
    goes through here, so the event can only ever land in (and be looked
    up from) the task's own workspace.
    """

    from pocketpaw_ee.calendar._context import RequestContext

    return RequestContext(workspace_id=workspace_id, user_id=user_id)


def _format_description(task: dict[str, Any]) -> str:
    """Terse provenance blurb, mirroring the meetings bridge's format."""

    lines = ["Task due"]
    priority = task.get("priority")
    if priority:
        lines.append(f"Priority: {priority}")
    lines.append("— Synced from Tasks")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Reconciler — the single convergence point every handler calls
# ---------------------------------------------------------------------------


async def _sync_task_calendar(task: dict[str, Any]) -> None:
    """Converge the calendar to the task's current state.

    Invariant: an event with ``fabric_object_id="task:{id}"`` exists on
    the ``tasks`` calendar iff the task has a ``due_at`` and an eligible
    (still-needs-doing) status. Create / move / delete whatever is needed
    to restore the invariant; do nothing when it already holds — which is
    what makes bus re-delivery safe.
    """

    task_id = task.get("id")
    workspace_id = task.get("workspace_id")
    if not (task_id and workspace_id):
        return

    status = task.get("status") or ""

    # Slim-payload guard: not every task.* emit ships the full DTO. The
    # planner terminal's requeue path (``_plan_task_event_payload``)
    # hand-builds ``data.task`` WITHOUT a ``due_at`` key while flipping a
    # still-live, still-due-dated task back to ``proposed``. Key ABSENT
    # means "unknown", not "cleared" — deleting on unknown would wrongly
    # remove a live deadline. Leave the event untouched and let the next
    # full-DTO emit reconcile. An explicit ``due_at: None`` (full DTO,
    # deadline cleared) still falls through and deletes as intended, and
    # terminal-status emits still delete regardless of the key.
    if "due_at" not in task and status in _CALENDAR_ELIGIBLE_STATUSES:
        return

    # Late imports — the calendar package is enterprise-only and its
    # absence must disable the bridge, not break task writes. Mirrors the
    # meetings bridge exactly.
    try:
        from pocketpaw_ee.calendar.dto import CreateEventRequest, UpdateEventRequest

        # Read-only linkage lookup, same precedent as the meetings
        # forward bridge reading _EventDoc directly. All WRITES go
        # through the calendar service (policy + events fire there).
        from pocketpaw_ee.calendar.models import _EventDoc
        from pocketpaw_ee.calendar.service import create_event, delete_event, update_event
    except ImportError:
        logger.warning("calendar package unavailable — task calendar bridge disabled")
        return

    due_at = _parse_due_at(task.get("due_at"))
    should_exist = due_at is not None and status in _CALENDAR_ELIGIBLE_STATUSES

    fabric_id = f"{_FABRIC_PREFIX}{task_id}"
    doc = await _EventDoc.find_one({"workspace": workspace_id, "fabric_object_id": fabric_id})

    if not should_exist:
        if doc is None:
            return  # invariant already holds
        # Act as the event's creator so ``policy.check_event_modify``
        # passes regardless of which workspace member touched the task.
        actor = getattr(doc, "created_by_user_id", None) or task.get("creator_id") or "system"
        ctx = _bridge_ctx(workspace_id, actor)
        try:
            await delete_event(ctx, str(doc.id))
        except Exception:  # noqa: BLE001 — bridge must not break the task flow
            logger.exception(
                "task calendar bridge: failed to delete CalendarEvent=%s for task=%s",
                doc.id,
                task_id,
            )
        return

    if due_at is None:  # narrowed by should_exist already; never trips.
        # Kept as a real branch, not an assert — asserts strip under -O.
        return
    title = task.get("title") or "Untitled task"
    starts_at = due_at
    ends_at = due_at + _DEFAULT_DURATION

    if doc is None:
        ctx = _bridge_ctx(workspace_id, task.get("creator_id") or "system")
        body = CreateEventRequest(
            calendar_id=_TASK_CALENDAR_ID,
            title=title,
            starts_at=starts_at,
            ends_at=ends_at,
            timezone="UTC",
            description=_format_description(task),
            # Linkage + loop-prevention marker — the meetings forward
            # bridge skips events carrying a bridge-owned fabric link.
            fabric_object_id=fabric_id,
        )
        try:
            await create_event(ctx, body)
        except Exception:  # noqa: BLE001 — bridge must not break the task flow
            logger.exception(
                "task calendar bridge: failed to mint CalendarEvent for task=%s", task_id
            )
            return
        logger.info("task calendar bridge: minted CalendarEvent for task=%s", task_id)
        return

    # Event exists and should exist — move/retitle it only when something
    # material changed (idempotency under bus re-delivery).
    doc_starts = getattr(doc, "starts_at", None)
    if doc_starts is not None and doc_starts.tzinfo is None:
        doc_starts = doc_starts.replace(tzinfo=UTC)
    if doc_starts == starts_at and getattr(doc, "title", None) == title:
        return

    actor = getattr(doc, "created_by_user_id", None) or task.get("creator_id") or "system"
    ctx = _bridge_ctx(workspace_id, actor)
    body = UpdateEventRequest(title=title, starts_at=starts_at, ends_at=ends_at)
    try:
        await update_event(ctx, str(doc.id), body)
    except Exception:  # noqa: BLE001 — bridge must not break the task flow
        logger.exception(
            "task calendar bridge: failed to update CalendarEvent=%s for task=%s",
            doc.id,
            task_id,
        )
        return
    logger.info("task calendar bridge: moved CalendarEvent=%s for task=%s", doc.id, task_id)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _on_task_proposed(event: Event) -> None:
    """Task created — mint the calendar event when ``due_at`` is set."""

    task = _extract_task(event)
    if task is None:
        return
    try:
        await _sync_task_calendar(task)
    except Exception:  # noqa: BLE001 — belt and braces on top of the bus's own swallow
        logger.warning("task.proposed → calendar sync failed", exc_info=True)


async def _on_task_updated(event: Event) -> None:
    """Task metadata changed — converge the calendar to the new state."""

    task = _extract_task(event)
    if task is None:
        return  # task-comment emits ship task=None on purpose
    try:
        await _sync_task_calendar(task)
    except Exception:  # noqa: BLE001
        logger.warning("task.updated → calendar sync failed", exc_info=True)


async def _on_task_resolved(event: Event) -> None:
    """Task completed — the reconciler deletes the now-stale event."""

    task = _extract_task(event)
    if task is None:
        return
    try:
        await _sync_task_calendar(task)
    except Exception:  # noqa: BLE001
        logger.warning("task.resolved → calendar sync failed", exc_info=True)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_task_calendar_listeners() -> None:
    """Wire the Task → Calendar bridge onto the realtime bus.

    Called once from ``mount_cloud`` after ``init_realtime`` has set the
    singleton — same lifecycle as ``register_task_listeners``.
    """

    bus = get_bus()
    bus.subscribe(TaskProposed.EVENT_TYPE, _on_task_proposed)
    bus.subscribe(TaskUpdated.EVENT_TYPE, _on_task_updated)
    bus.subscribe(TaskResolved.EVENT_TYPE, _on_task_resolved)
    logger.info("registered task → calendar bridge")


__all__ = ["register_task_calendar_listeners"]
