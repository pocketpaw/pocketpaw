"""Cycles domain — business logic service.

Sole owner of writes to the ``Cycle`` Beanie document. Module-level
``async def`` functions, ``ctx: RequestContext`` first, validate-at-entry,
emit-on-every-write.

Composition with the Tasks entity (``ee.cloud.tasks.service``) is via lazy
import — kept lazy so cycles can still operate when the host branch
hasn't merged Tasks yet. Once both entities are present (the default on
``ee`` post-PR-2), the composition runs at full fidelity; the
lazy-import path stays as a safety net for trunk forks.

Public API:
- ``agent_create_cycle(ctx, body)``
- ``agent_list_cycles(ctx)``
- ``agent_get_cycle(ctx, cycle_id)``
- ``agent_update_cycle(ctx, cycle_id, body)``
- ``agent_close_cycle(ctx, cycle_id)``
- ``agent_list_cycle_items(ctx, cycle_id)``

Internal:
- ``_snapshot_cycle_daily(ctx, cycle_id)`` — idempotent within a day.
  Weekend flag is computed against the UTC date; deployments in
  non-UTC timezones may observe ±12h drift on the weekend boundary.
  Pass an explicit ``today`` param to align with local time.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any

from beanie import PydanticObjectId

from ee.cloud._core.context import RequestContext
from ee.cloud._core.errors import ConflictError, Forbidden, NotFound, ValidationError
from ee.cloud._core.realtime.emit import emit
from ee.cloud._core.realtime.events import (
    CycleClosed,
    CycleCreated,
    CycleSnapshotted,
    CycleUpdated,
)
from ee.cloud._core.time import iso_utc
from ee.cloud.cycles.domain import Cycle, CycleDailyPoint
from ee.cloud.cycles.dto import (
    CreateCycleRequest,
    CycleDailyPointResponse,
    CycleListItemResponse,
    CycleResponse,
    UpdateCycleRequest,
)
from ee.cloud.models.cycle import Cycle as _CycleDoc
from ee.cloud.models.cycle import CycleDailyPoint as _CycleDailyPointDoc

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Private mapping helpers
# ---------------------------------------------------------------------------


def _daily_to_domain(p: _CycleDailyPointDoc) -> CycleDailyPoint:
    return CycleDailyPoint(
        date=p.date,
        scope=p.scope,
        started=p.started,
        completed=p.completed,
        is_weekend=p.is_weekend,
    )


def _to_domain(doc: _CycleDoc) -> Cycle:
    return Cycle(
        id=str(doc.id),
        workspace_id=doc.workspace,
        name=doc.name,
        description=doc.description,
        pocket_id=doc.pocket_id,
        start=doc.start,
        end=doc.end,
        status=doc.status,  # type: ignore[arg-type]
        scope=doc.scope,
        started=doc.started,
        completed=doc.completed,
        daily=tuple(_daily_to_domain(p) for p in doc.daily),
        created_by=doc.created_by,
        created_at=getattr(doc, "createdAt", None),
        updated_at=getattr(doc, "updatedAt", None),
    )


def _date_str(d: date | None) -> str | None:
    return d.isoformat() if d is not None else None


def _daily_to_dto(p: CycleDailyPoint) -> CycleDailyPointResponse:
    return CycleDailyPointResponse(
        date=p.date.isoformat(),
        scope=p.scope,
        started=p.started,
        completed=p.completed,
        is_weekend=p.is_weekend,
    )


def _to_list_response(c: Cycle) -> CycleListItemResponse:
    return CycleListItemResponse(
        id=c.id,
        workspace_id=c.workspace_id,
        name=c.name,
        description=c.description,
        pocket_id=c.pocket_id,
        start=c.start.isoformat(),
        end=c.end.isoformat(),
        status=c.status,
        scope=c.scope,
        started=c.started,
        completed=c.completed,
        created_by=c.created_by,
        created_at=iso_utc(c.created_at),
        updated_at=iso_utc(c.updated_at),
    )


def _to_response(c: Cycle) -> CycleResponse:
    base = _to_list_response(c)
    return CycleResponse(
        **base.model_dump(),
        daily=[_daily_to_dto(p) for p in c.daily],
    )


# ---------------------------------------------------------------------------
# Tenancy + access helpers
# ---------------------------------------------------------------------------


def _require_workspace(ctx: RequestContext) -> str:
    """Cycles always operate in a workspace; routes that bypass an active
    workspace should never reach the service. Raise a Forbidden so the
    caller gets a clean 403 rather than a 500."""
    if not ctx.workspace_id:
        raise Forbidden("cycle.no_workspace", "Active workspace required for cycle operations")
    return ctx.workspace_id


async def _fetch_in_workspace(workspace_id: str, cycle_id: str) -> _CycleDoc:
    """Fetch a cycle scoped to the caller's workspace; raise NotFound if
    the id is malformed, the doc is missing, or it lives in another
    workspace (so we don't leak existence across tenants)."""
    try:
        oid = PydanticObjectId(cycle_id)
    except Exception:
        raise NotFound("cycle", cycle_id) from None
    doc = await _CycleDoc.find_one({"_id": oid, "workspace": workspace_id})
    if doc is None:
        raise NotFound("cycle", cycle_id)
    return doc


# ---------------------------------------------------------------------------
# Tasks composition — lazy import
# ---------------------------------------------------------------------------


async def _tasks_for_cycle(ctx: RequestContext, cycle_id: str) -> list[Any] | None:
    """Return the list of tasks attached to a cycle, or ``None`` when the
    Tasks entity isn't available on this branch.

    The cycles entity composes with ``ee.cloud.tasks.service.agent_list_tasks``
    once PR 2 of the Mission Control series lands. Until then we lazy-import
    inside this helper and degrade silently — the 4-file rule forbids
    inlining a Beanie query against the Tasks collection from here, so the
    fallback is "no data" rather than "wrong data".
    """
    try:
        from ee.cloud.tasks import service as tasks_service
        from ee.cloud.tasks.dto import ListTasksRequest
    except Exception:
        logger.debug(
            "cycles: tasks entity not available; returning None for cycle %s items",
            cycle_id,
        )
        return None

    try:
        body = ListTasksRequest(cycle_id=cycle_id)  # type: ignore[call-arg]
        return await tasks_service.agent_list_tasks(ctx, body)  # type: ignore[attr-defined]
    except Exception:
        logger.warning(
            "cycles: tasks.agent_list_tasks failed for cycle %s; treating as empty",
            cycle_id,
            exc_info=True,
        )
        return None


def _counters_from_tasks(tasks: list[Any]) -> tuple[int, int, int]:
    """Project (scope, started, completed) from a tasks list.

    Tasks status vocabulary lives in PR 2; we use the documented values from
    the audit doc: ``proposed | in_progress | done | blocked | failed`` (the
    last three are terminal). "Started" is anything past ``proposed``;
    "completed" is ``done`` only. Anything else counts towards scope only.
    """
    scope = len(tasks)
    started = 0
    completed = 0
    for t in tasks:
        status = getattr(t, "status", None) or (t.get("status") if isinstance(t, dict) else None)
        if status in ("in_progress", "done", "blocked", "failed"):
            started += 1
        if status == "done":
            completed += 1
    return scope, started, completed


# ---------------------------------------------------------------------------
# Overlap check
# ---------------------------------------------------------------------------


async def _has_active_overlap(
    workspace_id: str,
    pocket_id: str | None,
    start: date,
    end: date,
    exclude_id: str | None = None,
) -> bool:
    """Return True if another active cycle on the same pocket overlaps the
    proposed range.

    Decision (v1): we only block overlap on the **same pocket** — workspaces
    routinely have multiple engagements in flight on different pockets at
    the same time. Cycles with no ``pocket_id`` collide only with other
    no-pocket cycles. Relaxing the rule entirely (allow any overlap) is
    tracked as a follow-up if operators push back.
    """
    query: dict[str, Any] = {
        "workspace": workspace_id,
        "status": "active",
        "pocket_id": pocket_id,
    }
    if exclude_id is not None:
        try:
            query["_id"] = {"$ne": PydanticObjectId(exclude_id)}
        except Exception:
            pass
    async for doc in _CycleDoc.find(query):
        # Two ranges [a, b] and [c, d] overlap iff a <= d and c <= b.
        if doc.start <= end and start <= doc.end:
            return True
    return False


# ---------------------------------------------------------------------------
# Public service API
# ---------------------------------------------------------------------------


async def agent_create_cycle(ctx: RequestContext, body: CreateCycleRequest) -> CycleResponse:
    """Create a new cycle in the caller's workspace.

    Validates dates (start < end via the DTO model_validator) and rejects
    overlap with another active cycle on the same pocket. The cycle is
    persisted with denormalized counters at zero and an empty daily series;
    the first call to ``agent_get_cycle`` / ``_snapshot_cycle_daily`` after
    Tasks are attached will populate them.
    """
    body = CreateCycleRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)

    if body.status == "active" and await _has_active_overlap(
        workspace_id, body.pocket_id, body.start, body.end
    ):
        raise ConflictError(
            "cycle.overlap",
            "Another active cycle on the same pocket overlaps this date range",
        )

    doc = _CycleDoc(
        workspace=workspace_id,
        name=body.name,
        description=body.description,
        pocket_id=body.pocket_id,
        start=body.start,
        end=body.end,
        status=body.status,
        created_by=ctx.user_id,
    )
    await doc.insert()

    response = _to_response(_to_domain(doc))
    await emit(CycleCreated(data=response.model_dump()))
    return response


async def agent_list_cycles(ctx: RequestContext) -> list[CycleListItemResponse]:
    """List cycles in the caller's workspace.

    Sorted by status (active → upcoming → completed) then ``start`` date
    descending — the Mission Control left list expects active engagements
    on top, then upcoming, then the historical archive most-recent-first.
    """
    workspace_id = _require_workspace(ctx)
    docs = await _CycleDoc.find({"workspace": workspace_id}).to_list()
    domains = [_to_domain(d) for d in docs]

    status_rank = {"active": 0, "upcoming": 1, "completed": 2}
    domains.sort(key=lambda c: (status_rank.get(c.status, 9), -c.start.toordinal()))
    return [_to_list_response(c) for c in domains]


async def agent_get_cycle(ctx: RequestContext, cycle_id: str) -> CycleResponse:
    """Fetch a single cycle with its full daily series.

    Refreshes the denormalized counters from the Tasks collection at read
    time so the detail panel reflects the latest state. The persisted
    counters are still useful for cheap list reads — the freshness gap
    closes whenever this endpoint is called.
    """
    workspace_id = _require_workspace(ctx)
    doc = await _fetch_in_workspace(workspace_id, cycle_id)

    tasks = await _tasks_for_cycle(ctx, str(doc.id))
    if tasks is not None:
        scope, started, completed = _counters_from_tasks(tasks)
        if (doc.scope, doc.started, doc.completed) != (scope, started, completed):
            doc.scope = scope
            doc.started = started
            doc.completed = completed
            await doc.save()

    # no-event: silent counter sync on read — avoids CycleUpdated churn on every
    # cycle detail fetch. The authoritative ``CycleUpdated`` emits live on writes
    # (agent_update_cycle, agent_close_cycle); the daily snapshot job emits
    # ``CycleSnapshotted`` for chart updates.
    return _to_response(_to_domain(doc))


async def agent_update_cycle(
    ctx: RequestContext, cycle_id: str, body: UpdateCycleRequest
) -> CycleResponse:
    """Patch ``name``, ``description``, or ``start`` / ``end`` dates.

    Editable only while the cycle is ``upcoming`` — once a cycle goes
    active, its boundaries are part of the historical record. Status
    transitions go through ``agent_close_cycle`` instead.
    """
    body = UpdateCycleRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)
    doc = await _fetch_in_workspace(workspace_id, cycle_id)

    if doc.status != "upcoming":
        raise Forbidden(
            "cycle.not_upcoming",
            "Only upcoming cycles can have their name, description, or dates edited",
        )

    new_start = body.start if body.start is not None else doc.start
    new_end = body.end if body.end is not None else doc.end
    if new_start >= new_end:
        raise ValidationError("cycle.invalid_range", "start must be before end")

    if body.name is not None:
        doc.name = body.name
    if body.description is not None:
        doc.description = body.description
    if body.start is not None:
        doc.start = body.start
    if body.end is not None:
        doc.end = body.end
    await doc.save()

    response = _to_response(_to_domain(doc))
    await emit(CycleUpdated(data=response.model_dump()))
    return response


async def agent_close_cycle(ctx: RequestContext, cycle_id: str) -> CycleResponse:
    """Mark a cycle ``completed`` and roll incomplete tasks forward.

    Matches Linear's behavior: every Task with ``cycle_id == this`` and
    ``status != done`` is reassigned to the next active cycle on the same
    pocket (if any); otherwise the task's ``cycle_id`` is cleared so it
    surfaces back in the unscheduled list.

    Rolling is a no-op when the Tasks entity isn't available on the branch —
    the cycle still closes, and the rollover runs implicitly once PR 2 is
    merged (newly-created tasks won't pick the closed cycle as their
    default).
    """
    workspace_id = _require_workspace(ctx)
    doc = await _fetch_in_workspace(workspace_id, cycle_id)

    if doc.status == "completed":
        raise ConflictError("cycle.already_closed", "This cycle is already completed")

    rolled = await _roll_incomplete_tasks(ctx, doc)

    doc.status = "completed"
    await doc.save()

    response = _to_response(_to_domain(doc))
    payload = {**response.model_dump(), "rolled_count": rolled}
    await emit(CycleClosed(data=payload))
    return response


async def _roll_incomplete_tasks(ctx: RequestContext, doc: _CycleDoc) -> int:
    """Reassign incomplete tasks attached to this cycle to the next active
    cycle on the same pocket (or clear ``cycle_id`` if none exists).

    Returns the number of tasks that were rolled. Composes via
    ``ee.cloud.tasks.service`` — degrades to 0 when Tasks isn't available.
    """
    tasks = await _tasks_for_cycle(ctx, str(doc.id))
    if not tasks:
        return 0

    try:
        from ee.cloud.tasks import service as tasks_service
    except Exception:
        return 0

    # Pick the next active cycle on the same pocket as the rollover target.
    # If none exists, ``next_id`` stays None and the task drops back into
    # the unscheduled list.
    next_id: str | None = None
    async for candidate in _CycleDoc.find(
        {
            "workspace": doc.workspace,
            "status": "active",
            "pocket_id": doc.pocket_id,
            "_id": {"$ne": doc.id},
        }
    ).sort([("start", 1)]):
        next_id = str(candidate.id)
        break

    rolled = 0
    for task in tasks:
        status = getattr(task, "status", None) or (
            task.get("status") if isinstance(task, dict) else None
        )
        if status == "done":
            continue
        task_id = getattr(task, "id", None) or (task.get("id") if isinstance(task, dict) else None)
        if not task_id:
            continue
        reassign = getattr(tasks_service, "agent_reassign_task_cycle", None)
        if reassign is None:
            # PR 2's task service hasn't exposed the rollover method yet —
            # log and continue so the cycle still closes cleanly.
            logger.info(
                "cycles.close: tasks.agent_reassign_task_cycle not yet available; skipping roll"
            )
            break
        try:
            await reassign(ctx, task_id, next_id)
            rolled += 1
        except Exception:
            logger.warning(
                "cycles.close: failed to roll task %s to cycle %s", task_id, next_id, exc_info=True
            )
    return rolled


async def agent_list_cycle_items(ctx: RequestContext, cycle_id: str) -> list[Any]:
    """Return the tasks attached to a cycle.

    Composes via ``ee.cloud.tasks.service.agent_list_tasks`` and returns the
    list directly so the frontend's existing TaskResponse shape passes
    through unchanged. When Tasks isn't available on this branch, returns
    ``[]`` — the Cycles tab's items list shows an empty state until PR 2
    merges into ``ee``.

    Inlining a Beanie query against the tasks collection here would violate
    the 4-file rule (only ``tasks/service.py`` may import the Beanie task
    document), so we accept the temporary empty state as the cost of clean
    layering.
    """
    workspace_id = _require_workspace(ctx)
    # Tenant-check by fetching the cycle — this also gives 404 on bad ids.
    await _fetch_in_workspace(workspace_id, cycle_id)
    tasks = await _tasks_for_cycle(ctx, cycle_id)
    return list(tasks) if tasks is not None else []


# ---------------------------------------------------------------------------
# Daily snapshot — feeds the burnup chart
# ---------------------------------------------------------------------------


async def _snapshot_cycle_daily(
    ctx: RequestContext, cycle_id: str, *, today: date | None = None
) -> CycleDailyPointResponse | None:
    """Append today's (scope, started, completed) point to a cycle's daily
    series, or return ``None`` if today's point already exists.

    Idempotent within a single calendar day. The job runs once per 24h
    per workspace; if it runs twice (manual trigger + scheduled), the
    second call is a no-op.

    Caps the daily array at 100 entries — a cycle longer than ~14 weeks
    is unusual but possible. Beyond the cap we downgrade to a weekly
    cadence by only appending when the most recent point is at least 7
    days old. Documented in the model docstring.

    Weekend flag is computed against the UTC date; deployments in non-UTC
    timezones may observe ±12h drift on the weekend boundary. Pass an
    explicit ``today`` param to align with local time.

    Returns the newly-appended point as a DTO so the snapshot job can emit
    ``CycleSnapshotted`` with a payload the frontend's burnup chart can
    consume directly.
    """
    workspace_id = _require_workspace(ctx)
    doc = await _fetch_in_workspace(workspace_id, cycle_id)
    if doc.status == "completed":
        return None

    today = today or datetime.now(UTC).date()

    # Idempotency: if today's point exists, skip.
    if any(p.date == today for p in doc.daily):
        return None

    # Cap rule: once we hit 100 points, only append weekly thereafter.
    if len(doc.daily) >= 100:
        latest = max((p.date for p in doc.daily), default=None)
        if latest is not None and (today - latest).days < 7:
            return None

    tasks = await _tasks_for_cycle(ctx, cycle_id)
    if tasks is None:
        # Tasks entity not available — log and bail. The cycle's daily
        # array stays empty until PR 2 lands and the next scheduled run
        # picks up.
        logger.info(
            "cycles.snapshot: tasks entity unavailable; skipping snapshot for cycle %s", cycle_id
        )
        return None

    scope, started, completed = _counters_from_tasks(tasks)
    point = _CycleDailyPointDoc(
        date=today,
        scope=scope,
        started=started,
        completed=completed,
        is_weekend=today.weekday() >= 5,
    )
    doc.daily.append(point)
    doc.scope = scope
    doc.started = started
    doc.completed = completed
    await doc.save()

    dto = _daily_to_dto(_daily_to_domain(point))
    await emit(
        CycleSnapshotted(
            data={
                "cycle_id": cycle_id,
                "workspace_id": workspace_id,
                "daily_point": dto.model_dump(),
            }
        )
    )
    return dto


async def list_active_cycle_ids(workspace_id: str) -> list[str]:
    """Return the ids of every active cycle in a workspace.

    Helper for the snapshot job — kept inside the service so the 4-file
    rule holds (only service.py may touch ``models.cycle``). The job
    iterates these ids and calls ``_snapshot_cycle_daily`` per cycle.
    Workspace is passed explicitly (not via a RequestContext) because
    the snapshot job runs as a system actor without a user identity.
    """
    if not workspace_id:
        return []
    ids: list[str] = []
    async for doc in _CycleDoc.find({"workspace": workspace_id, "status": "active"}):
        ids.append(str(doc.id))
    return ids


__all__ = [
    "agent_close_cycle",
    "agent_create_cycle",
    "agent_get_cycle",
    "agent_list_cycle_items",
    "agent_list_cycles",
    "agent_update_cycle",
    "list_active_cycle_ids",
    "_snapshot_cycle_daily",
]
