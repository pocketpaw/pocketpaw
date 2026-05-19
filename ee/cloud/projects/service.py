# service.py — Projects entity business logic.
# Created: 2026-05-16 — Mission Control backend completion. Sole owner of
#   writes to the ``Project`` Beanie document. Module-level ``async def``
#   API per the ee/cloud Code Rules. Emit-on-every-write per Rule 9.
#   Tenant filter on every read per Rule 7. Soft-unassign on delete:
#   Pockets / Tasks / Cycles in the workspace with the deleted
#   ``project_id`` get their reference cleared but are NOT cascade-deleted
#   themselves — historical work stays alive even when its container is
#   gone.
"""Projects entity — business logic service.

Public API (all module-level ``async def``):

  - :func:`agent_create` — insert a new project.
  - :func:`agent_list` — workspace-scoped filterable list.
  - :func:`agent_get` — single fetch with tenant guard.
  - :func:`agent_update` — partial patch of mutable metadata.
  - :func:`agent_archive` — soft-archive (status='archived').
  - :func:`agent_delete` — hard-delete + cascade unassign on children.

Cascade-unassign rationale: Projects are a grouping primitive, not a
container. Deleting a project shouldn't destroy the underlying work
(pockets carry rippleSpecs, tasks carry audit history, cycles carry
burnup data) — it just removes the grouping. The unassign step uses
the entities' own services so each one emits its own ``*.updated``
event and downstream listeners stay consistent.
"""

from __future__ import annotations

import logging

from beanie import PydanticObjectId

from ee.cloud._core.context import RequestContext
from ee.cloud._core.errors import NotFound, ValidationError
from ee.cloud._core.realtime.emit import emit
from ee.cloud._core.realtime.events import (
    ProjectArchived,
    ProjectCreated,
    ProjectDeleted,
    ProjectUpdated,
)
from ee.cloud.models.project import Project as _ProjectDoc
from ee.cloud.projects.domain import Project, ProjectId
from ee.cloud.projects.dto import (
    CreateProjectRequest,
    ListProjectsRequest,
    ProjectResponse,
    UpdateProjectRequest,
    project_to_dto,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Private mapping + access helpers
# ---------------------------------------------------------------------------


def _to_domain(doc: _ProjectDoc) -> Project:
    """Beanie document → frozen domain :class:`Project`."""
    return Project(
        id=ProjectId(str(doc.id)),
        workspace_id=doc.workspace,
        name=doc.name,
        description=doc.description,
        color=doc.color,
        lead_id=doc.lead_id,
        status=doc.status,  # type: ignore[arg-type]
        created_by=doc.created_by,
        created_at=getattr(doc, "createdAt", None),
        updated_at=getattr(doc, "updatedAt", None),
    )


async def _fetch_project(ctx: RequestContext, project_id: str) -> _ProjectDoc:
    """Load a Project by id, enforce workspace tenancy, raise NotFound on
    miss or on a cross-workspace mismatch. The uniform NotFound (not
    Forbidden) on tenant mismatch prevents callers in another workspace
    from enumerating project ids by 404-vs-403 timing.
    """
    try:
        oid = PydanticObjectId(project_id)
    except Exception as exc:  # noqa: BLE001
        raise NotFound("project", project_id) from exc
    doc = await _ProjectDoc.get(oid)
    if doc is None or doc.workspace != ctx.workspace_id:
        raise NotFound("project", project_id)
    return doc


def _event_payload(project: Project) -> dict:
    """Build the realtime event payload for a project mutation.

    Workspace-wide broadcast: every workspace member sees the project
    list (it's a workspace-level picker), so the audience is the whole
    workspace. The audience resolver fans out via ``workspace_id``.
    """
    return {
        "project_id": str(project.id),
        "project": project_to_dto(project).model_dump(),
        "workspace_id": project.workspace_id,
    }


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


async def agent_create(ctx: RequestContext, body: CreateProjectRequest) -> ProjectResponse:
    """Create a new project in the caller's workspace."""

    body = CreateProjectRequest.model_validate(body)
    if not ctx.workspace_id:
        raise ValidationError(
            "project.no_workspace",
            "creating a project requires an active workspace",
        )

    doc = _ProjectDoc(
        workspace=ctx.workspace_id,
        name=body.name,
        description=body.description,
        color=body.color,
        lead_id=body.lead_id,
        status=body.status,
        created_by=ctx.user_id,
    )
    await doc.insert()
    project = _to_domain(doc)
    await emit(ProjectCreated(data=_event_payload(project)))
    return project_to_dto(project)


async def agent_list(
    ctx: RequestContext, body: ListProjectsRequest | dict | None = None
) -> list[ProjectResponse]:
    """List projects in the caller's workspace, filtered.

    Default filter: ``active`` only. Pass ``status='archived'`` to see
    the archive bin, or pass ``status=None`` (the default on the dict
    path) to list everything.
    """

    body = ListProjectsRequest.model_validate(body or {})
    if not ctx.workspace_id:
        return []

    query: dict = {"workspace": ctx.workspace_id}
    if body.status is not None:
        query["status"] = body.status

    docs = await _ProjectDoc.find(query).limit(body.limit).to_list()
    domains = [_to_domain(d) for d in docs]
    # Stable order: active first, then archived; within each, newest first.
    status_rank = {"active": 0, "archived": 1}
    domains.sort(
        key=lambda p: (
            status_rank.get(p.status, 9),
            -(p.created_at.timestamp() if p.created_at else 0),
        )
    )
    return [project_to_dto(p) for p in domains]


async def agent_get(ctx: RequestContext, project_id: str) -> ProjectResponse:
    """Single fetch with tenant check."""
    doc = await _fetch_project(ctx, project_id)
    return project_to_dto(_to_domain(doc))


async def agent_update(
    ctx: RequestContext, project_id: str, body: UpdateProjectRequest
) -> ProjectResponse:
    """Partial update of mutable Project metadata."""

    body = UpdateProjectRequest.model_validate(body)
    doc = await _fetch_project(ctx, project_id)

    if body.name is not None:
        doc.name = body.name
    if body.description is not None:
        doc.description = body.description
    if body.color is not None:
        doc.color = body.color
    if body.lead_id is not None:
        doc.lead_id = body.lead_id
    if body.status is not None:
        doc.status = body.status

    await doc.save()
    project = _to_domain(doc)
    await emit(ProjectUpdated(data=_event_payload(project)))
    return project_to_dto(project)


async def agent_archive(ctx: RequestContext, project_id: str) -> ProjectResponse:
    """Soft-archive a project. Children keep their ``project_id`` reference
    so historical pockets/tasks/cycles still resolve back to the archived
    bin in the UI.
    """
    doc = await _fetch_project(ctx, project_id)
    if doc.status == "archived":
        # Idempotent: return the current state without re-emitting.
        return project_to_dto(_to_domain(doc))
    doc.status = "archived"
    await doc.save()
    project = _to_domain(doc)
    await emit(ProjectArchived(data=_event_payload(project)))
    return project_to_dto(project)


async def agent_delete(ctx: RequestContext, project_id: str) -> None:
    """Hard-delete a project + soft-unassign children.

    Order matters: unassign children FIRST so their ``project_id`` is
    cleared before the project row goes away. ``_unassign_project``
    raises on any cascade failure (other than a missing child entity at
    import time), which aborts the delete and leaves both the project
    row and its children intact — the caller can retry safely.
    """
    doc = await _fetch_project(ctx, project_id)
    payload = {
        "project_id": str(doc.id),
        "workspace_id": doc.workspace,
    }
    await _unassign_project(ctx, str(doc.id))
    await doc.delete()
    await emit(ProjectDeleted(data=payload))


# ---------------------------------------------------------------------------
# Cascade helper
# ---------------------------------------------------------------------------


async def _unassign_project(ctx: RequestContext, project_id: str) -> None:
    """Soft-unassign all Pockets / Tasks / Cycles in the caller's
    workspace that reference ``project_id``.

    Uses each entity's own service so per-row events fire (search index,
    Mission Control, etc. stay consistent). The 4-file rule forbids
    inlining a Beanie write against another entity's collection from
    here — we delegate.

    Lazy imports keep startup cheap and let forks that ship without one
    of the child entities still install the projects module. ImportError
    is swallowed for that reason; anything else propagates so a transient
    bulk-update failure cannot leave the project row deleted while its
    children keep dangling ``project_id`` values.
    """
    if not ctx.workspace_id:
        return

    # --- Pockets -----------------------------------------------------------
    try:
        from ee.cloud.pockets import service as pockets_service
    except ImportError:
        logger.warning("projects.unassign: pockets entity not installed; skipping")
    else:
        unassign_pocket = getattr(pockets_service, "unassign_project_on_pockets", None)
        if unassign_pocket is not None:
            await unassign_pocket(ctx.workspace_id, project_id)

    # --- Tasks -------------------------------------------------------------
    try:
        from ee.cloud.tasks import service as tasks_service
    except ImportError:
        logger.warning("projects.unassign: tasks entity not installed; skipping")
    else:
        unassign_task = getattr(tasks_service, "unassign_project_on_tasks", None)
        if unassign_task is not None:
            await unassign_task(ctx.workspace_id, project_id)

    # --- Cycles ------------------------------------------------------------
    try:
        from ee.cloud.cycles import service as cycles_service
    except ImportError:
        logger.warning("projects.unassign: cycles entity not installed; skipping")
    else:
        unassign_cycle = getattr(cycles_service, "unassign_project_on_cycles", None)
        if unassign_cycle is not None:
            await unassign_cycle(ctx.workspace_id, project_id)


async def exists_in_workspace(workspace_id: str, project_id: str) -> bool:
    """Return True if the project exists in the given workspace.

    Used by sibling services (Pockets, Tasks, Cycles) to validate that a
    supplied ``project_id`` points at a real project in the same
    workspace before they accept it. Returns False on malformed ids so
    callers can map the failure into a domain-appropriate error.
    """
    if not workspace_id or not project_id:
        return False
    try:
        oid = PydanticObjectId(project_id)
    except Exception:
        return False
    doc = await _ProjectDoc.find_one({"_id": oid, "workspace": workspace_id})
    return doc is not None


__all__ = [
    "agent_archive",
    "agent_create",
    "agent_delete",
    "agent_get",
    "agent_list",
    "agent_update",
    "exists_in_workspace",
]
