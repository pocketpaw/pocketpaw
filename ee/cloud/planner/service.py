# Created: 2026-05-17 — pocketpaw#1118 P1. Cloud-side planner service:
#   wraps OSS ``pocketpaw.deep_work.planner.PlannerAgent`` and
#   materializes its output into cloud Projects, Tasks, and FileUpload
#   primitives. NEVER imports anything under ``src/pocketpaw/deep_work/``
#   except via the public OSS API surface (``PlannerAgent``, ``PlannerResult``,
#   ``TaskSpec``, ``AgentSpec``) — the OSS module is sacred and changes go
#   through the OSS PR flow, not through ee/cloud.
"""Planner entity — business logic service.

Public API (all module-level ``async def``):

  - :func:`agent_plan_project` — entry point. Validates the target
    cloud Project, invokes the OSS planner, lands the resulting
    artifacts (PRD, plan.json, goal.md) into the workspace Files
    panel, creates one cloud Task per OSS TaskSpec, and returns a
    :class:`PlanProjectResult` for the FE Plan tab.
  - :func:`get_plan_for_project` — read path. Reconstructs the most
    recent plan summary from cloud primitives (no PlanSession doc
    today — the planner output is the persistent record; we surface
    a summary by listing files + tasks tagged with the project id).

Implementation notes:

  * The OSS ``PlannerAgent.plan(...)`` is the canonical entry — it
    is pure (no MissionControlManager writes), returns a
    ``PlannerResult``, and broadcasts phase events through the OSS
    bus (cosmetic in cloud — we discard them).
  * ``deep_research=True`` upgrades the OSS depth to ``"deep"``
    (extra LLM round-trip); ``False`` uses ``"standard"`` which
    matches the OSS HTTP endpoint default.
  * The cloud Project must already exist before planning starts
    — the operator picked it from the rail / modal. We never
    create a Project here.
  * File writes go through ``uploads.service.write_text_file`` —
    the FileReady event fires per-file so the KB indexer can pull
    the PRD into the workspace knowledge base.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ee.cloud._core.context import RequestContext
from ee.cloud._core.errors import NotFound, ValidationError
from ee.cloud._core.realtime.emit import emit
from ee.cloud._core.realtime.events import PlanGenerated
from ee.cloud.planner.domain import AgentGap, PlanSession
from ee.cloud.planner.dto import (
    AgentGapDTO,
    PlanProjectRequest,
    PlanProjectResult,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def agent_plan_project(ctx: RequestContext, body: PlanProjectRequest) -> PlanProjectResult:
    """Invoke the OSS planner for a cloud Project; materialize outputs.

    Workspace tenancy comes from ``ctx``; never accept ``workspace_id``
    as a function parameter (Rule 5). Validation happens at entry so
    internal callers (the agent tool, bus handlers, jobs) get the same
    safety net as HTTP callers (Rule 6).
    """

    body = PlanProjectRequest.model_validate(body)
    if not ctx.workspace_id:
        raise ValidationError(
            "planner.no_workspace",
            "planning requires an active workspace",
        )

    project = await _load_project_or_404(ctx, body.project_id)
    planner_result = await _run_oss_planner(
        project_id=project.id,
        goal=body.goal,
        deep_research=body.deep_research,
    )

    # Materialize: files first (cheap if planner produced empty content),
    # then tasks (each emits its own task.proposed), then agent-gap
    # detection (read-only). A failure at any step rolls forward — the
    # operator gets a partial plan they can re-trigger, not a silent
    # nothing.
    file_refs = await _write_planner_files(
        ctx=ctx,
        project_id=project.id,
        goal=body.goal,
        planner_result=planner_result,
    )
    task_ids = await _materialize_tasks(
        ctx=ctx,
        project=project,
        planner_result=planner_result,
    )
    agent_gaps = await _detect_agent_gaps(
        workspace_id=ctx.workspace_id,
        planner_result=planner_result,
    )

    session = PlanSession(
        id=project.id,  # OSS planner doesn't mint a separate session id today
        workspace_id=ctx.workspace_id,
        project_id=project.id,
        status="ready",
        prd_file_id=file_refs.get("prd"),
        plan_file_id=file_refs.get("plan"),
        goal_file_id=file_refs.get("goal"),
        task_ids=tuple(task_ids),
        agent_gaps=tuple(agent_gaps),
    )

    await emit(
        PlanGenerated(
            data={
                "workspace_id": ctx.workspace_id,
                "project_id": project.id,
                "plan_session_id": session.id,
                "prd_file_id": session.prd_file_id,
                "task_count": len(session.task_ids),
                "agent_gap_count": len(session.agent_gaps),
            }
        )
    )

    return _session_to_dto(session)


async def get_plan_for_project(ctx: RequestContext, project_id: str) -> PlanProjectResult | None:
    """Return the most recent plan summary for ``project_id``, or ``None``.

    There is no PlanSession Beanie document in v0 — the plan output IS
    the persistent record (files in the Files panel + tasks in
    Mission Control). We reconstruct the summary by:

      1. Verifying the project exists in the caller's workspace
         (tenant check; raises NotFound otherwise).
      2. Looking for the PRD file at the conventional path
         ``/projects/{project_id}/prd.md``. If absent, no plan exists yet.
      3. Surfacing the matching tasks via the existing tasks service.

    Agent-gap detection is intentionally NOT re-run here — it's a
    point-in-time signal from the original plan; re-running it on
    every Plan-tab refresh would hit the agents list for nothing.
    The FE renders the persisted gaps from the most recent
    ``PlanGenerated`` event payload, or shows an empty gap list when
    the route is a cold-start hydration.
    """

    if not ctx.workspace_id:
        return None

    project = await _load_project_or_404(ctx, project_id)

    folder_path = f"/projects/{project.id}"
    files_by_name = await _list_planner_files(
        workspace_id=ctx.workspace_id,
        folder_path=folder_path,
    )
    prd_file_id = files_by_name.get("prd.md")
    if not prd_file_id:
        return None  # No plan generated for this project yet.

    task_ids = await _list_planner_task_ids(ctx=ctx, project_id=project.id)

    session = PlanSession(
        id=project.id,
        workspace_id=ctx.workspace_id,
        project_id=project.id,
        status="ready",
        prd_file_id=prd_file_id,
        plan_file_id=files_by_name.get("plan.json"),
        goal_file_id=files_by_name.get("goal.md"),
        task_ids=tuple(task_ids),
        agent_gaps=(),  # See docstring — gaps are point-in-time
    )
    return _session_to_dto(session)


# ---------------------------------------------------------------------------
# OSS planner adapter
# ---------------------------------------------------------------------------


async def _run_oss_planner(
    *,
    project_id: str,
    goal: str,
    deep_research: bool,
) -> Any:
    """Call ``PlannerAgent.plan`` and return the ``PlannerResult``.

    The OSS PlannerAgent constructor requires a MissionControlManager
    instance but ``plan()`` itself doesn't touch it (manager is only
    used by ``ensure_profile``, which we never call from cloud). Pass
    a lightweight stub so we avoid pulling in the OSS singleton
    machinery and the on-disk MC state it carries.
    """

    from pocketpaw.deep_work.planner import PlannerAgent

    manager_stub = _PlannerManagerStub()
    planner = PlannerAgent(manager_stub)  # type: ignore[arg-type]

    depth = "deep" if deep_research else "standard"
    try:
        return await planner.plan(
            project_description=goal,
            project_id=project_id,
            research_depth=depth,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("OSS planner failed for project_id=%s", project_id)
        raise ValidationError(
            "planner.run_failed",
            f"deep_work planner failed: {exc}",
        ) from exc


class _PlannerManagerStub:
    """Manager-shaped stub passed to OSS ``PlannerAgent``.

    ``PlannerAgent.plan()`` doesn't call any manager methods today
    (verified by grep against deep_work/planner.py). The stub exists
    purely to satisfy the constructor's type expectation without
    pulling in MissionControlManager (which would activate OSS local
    storage). If a future OSS refactor adds a manager call inside
    ``plan()``, this stub will raise AttributeError loudly — which is
    the right failure mode: cloud should not silently hand OSS a
    working LOCAL manager.
    """

    async def get_agent_by_name(self, *_args: Any, **_kwargs: Any) -> None:
        return None


# ---------------------------------------------------------------------------
# Materialization helpers
# ---------------------------------------------------------------------------


async def _write_planner_files(
    *,
    ctx: RequestContext,
    project_id: str,
    goal: str,
    planner_result: Any,
) -> dict[str, str]:
    """Land PRD / goal / plan-json artifacts into the workspace Files panel.

    Returns a ``{logical_name: file_id}`` map for the planner service to
    pin onto the returned PlanSession. Folder layout matches the spec
    from the PR brief:

        /projects/{project_id}/
          ├─ prd.md       ← planner_result.prd_content
          ├─ goal.md      ← the original goal text
          └─ plan.json    ← planner_result.to_dict() (raw, for replay)

    Each write goes through the canonical
    ``uploads.service.write_text_file`` so the FileReady event fires
    and the KB indexer picks the PRD up automatically.
    """

    from ee.cloud.uploads.mongo_store import MongoFileStore
    from ee.cloud.uploads.service import write_text_file

    folder_path = f"/projects/{project_id}"

    # Re-plan safety: soft-delete prior PRD / goal.md / plan.json rows in
    # this folder before writing the new run. Without this, the file
    # store inserts a second row at the same path (no unique constraint
    # on (workspace, folder_path, filename)) and `_list_planner_files`
    # returns the stale first-run id via dict.setdefault — operator opens
    # the old PRD after a re-plan.
    workspace_id = ctx.workspace_id or ""
    if workspace_id:
        store = MongoFileStore()
        try:
            await store.soft_delete_under_prefix(workspace_id, folder_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "planner: soft-delete of %s prior to re-plan failed: %s",
                folder_path,
                exc,
            )

    refs: dict[str, str] = {}

    prd_content = getattr(planner_result, "prd_content", "") or ""
    if prd_content.strip():
        rec = await write_text_file(
            workspace_id=ctx.workspace_id or "",
            owner_id=ctx.user_id,
            folder_path=folder_path,
            filename="prd.md",
            content=prd_content,
            mime="text/markdown",
        )
        refs["prd"] = rec.id

    rec = await write_text_file(
        workspace_id=ctx.workspace_id or "",
        owner_id=ctx.user_id,
        folder_path=folder_path,
        filename="goal.md",
        content=goal,
        mime="text/markdown",
    )
    refs["goal"] = rec.id

    try:
        plan_blob = json.dumps(
            planner_result.to_dict() if hasattr(planner_result, "to_dict") else {},
            indent=2,
            default=str,
        )
    except (TypeError, ValueError) as exc:
        logger.warning("plan.json serialization failed: %s", exc)
        plan_blob = json.dumps({"error": str(exc)})

    rec = await write_text_file(
        workspace_id=ctx.workspace_id or "",
        owner_id=ctx.user_id,
        folder_path=folder_path,
        filename="plan.json",
        content=plan_blob,
        mime="application/json",
    )
    refs["plan"] = rec.id

    return refs


async def _materialize_tasks(
    *,
    ctx: RequestContext,
    project: Any,
    planner_result: Any,
) -> list[str]:
    """Create one cloud Task per OSS TaskSpec.

    Assignee resolution:

      * If the planner left a ``required_specialties`` hint AND we can
        find a cloud Agent in the workspace whose name matches the OSS
        team_recommendation entry that covers any of those specialties,
        assign the task to that agent (kind=agent).
      * Otherwise fall back to the project's lead_id (or the caller)
        as a human assignee — the operator sees the task in their tray
        and can re-route from there.

    OSS TaskSpec → cloud CreateTaskRequest field map:
      - key → ignored (cloud Task id is mongo-generated)
      - title → title
      - description → summary
      - priority → priority (normalized to the cloud's normal/high/etc.)
      - task_type → drives assignee kind (human/agent/review)
      - blocked_by_keys → ignored in v0 (P4 per the brief)
    """

    from ee.cloud.agents import service as agents_service
    from ee.cloud.tasks import service as tasks_service
    from ee.cloud.tasks.dto import AssigneeDTO, CreateTaskRequest, SourceDTO

    # Build a name → cloud Agent lookup once so the per-task loop is
    # O(1). Cloud's ``list_agents`` is workspace-scoped and cheap.
    workspace_id = ctx.workspace_id or ""
    cloud_agents = await agents_service.list_agents(workspace_id)
    by_name = {a.name.lower(): a for a in cloud_agents}

    # Recommended team gives us a name + specialties pair the planner
    # already mapped onto its task graph — we use it to choose the
    # right cloud agent when multiple match a specialty.
    team = list(getattr(planner_result, "team_recommendation", []) or [])
    specialty_to_agent_name: dict[str, str] = {}
    for spec in team:
        for sp in getattr(spec, "specialties", []) or []:
            specialty_to_agent_name.setdefault(sp.lower(), spec.name)

    all_specs = list(getattr(planner_result, "tasks", []) or []) + list(
        getattr(planner_result, "human_tasks", []) or []
    )

    plan_session_ref = project.id  # see PlanSession.id comment in service top
    fallback_assignee_id = getattr(project, "lead_id", None) or ctx.user_id

    task_ids: list[str] = []
    for spec in all_specs:
        assignee_kind, assignee_id, assignee_name = _resolve_assignee(
            spec=spec,
            specialty_to_agent_name=specialty_to_agent_name,
            by_name=by_name,
            fallback_id=fallback_assignee_id,
            fallback_name="",
        )
        priority = _normalize_priority(getattr(spec, "priority", "medium"))

        req = CreateTaskRequest(
            title=spec.title or spec.key or "Untitled task",
            summary=spec.description or "",
            assignee=AssigneeDTO(
                kind=assignee_kind,
                id=assignee_id,
                name=assignee_name,
            ),
            project_id=project.id,
            priority=priority,
            source=SourceDTO(
                type="planner",
                ref_id=plan_session_ref,
                metadata={
                    "planner_task_key": getattr(spec, "key", ""),
                    "task_type": getattr(spec, "task_type", "agent"),
                },
            ),
        )

        try:
            created = await tasks_service.agent_create_task(ctx, req)
        except Exception:  # noqa: BLE001
            logger.exception(
                "task materialization failed for planner key=%s",
                getattr(spec, "key", "?"),
            )
            continue
        task_ids.append(created.id)

    return task_ids


def _resolve_assignee(
    *,
    spec: Any,
    specialty_to_agent_name: dict[str, str],
    by_name: dict[str, Any],
    fallback_id: str,
    fallback_name: str,
) -> tuple[str, str, str]:
    """Map an OSS TaskSpec to a cloud assignee triple.

    Returns ``(kind, id, name)`` suitable for AssigneeDTO. Human falls
    back to the project lead — the cloud tasks service flips human
    tasks straight into ``in_progress`` so the operator sees them.
    """

    task_type = getattr(spec, "task_type", "agent")
    if task_type == "human":
        return ("human", fallback_id, fallback_name)

    specialties = [sp.lower() for sp in getattr(spec, "required_specialties", []) or []]
    for sp in specialties:
        team_name = specialty_to_agent_name.get(sp)
        if not team_name:
            continue
        agent = by_name.get(team_name.lower())
        if agent is not None:
            return ("agent", str(agent.id), agent.name)

    # No specialty match — fall back to ``human`` so the operator
    # explicitly re-routes rather than us silently picking a random
    # cloud agent.
    return ("human", fallback_id, fallback_name)


def _normalize_priority(raw: str) -> str:
    """OSS uses low/medium/high/urgent; cloud uses low/normal/high/urgent.

    Map ``medium`` → ``normal``; pass everything else through. Unknown
    values default to ``normal`` so a planner that emits an out-of-band
    priority value doesn't poison the cloud task.
    """

    raw = (raw or "").lower()
    if raw in {"low", "high", "urgent"}:
        return raw
    if raw == "medium":
        return "normal"
    return "normal"


async def _detect_agent_gaps(
    *,
    workspace_id: str,
    planner_result: Any,
) -> list[AgentGap]:
    """Return one ``AgentGap`` per planner-recommended agent missing
    from the workspace.

    The match is case-insensitive on agent name. We do NOT auto-create
    agents — the operator decides whether each gap is worth a new
    cloud Agent row, an existing-agent rename, or just accepting the
    fallback human assignment.
    """

    from ee.cloud.agents import service as agents_service

    cloud_agents = await agents_service.list_agents(workspace_id)
    existing_names = {a.name.lower() for a in cloud_agents}

    gaps: list[AgentGap] = []
    for spec in getattr(planner_result, "team_recommendation", []) or []:
        name = (getattr(spec, "name", "") or "").strip()
        if not name:
            continue
        if name.lower() in existing_names:
            continue
        gaps.append(
            AgentGap(
                spec_name=name,
                recommended_role=getattr(spec, "role", "") or "",
                specialties=tuple(getattr(spec, "specialties", []) or []),
            )
        )
    return gaps


# ---------------------------------------------------------------------------
# Read-path helpers (used by ``get_plan_for_project``)
# ---------------------------------------------------------------------------


async def _list_planner_files(
    *,
    workspace_id: str,
    folder_path: str,
) -> dict[str, str]:
    """List file_ids for the planner artifacts under ``folder_path``.

    Returns a ``{filename: file_id}`` map. Direct Mongo read is allowed
    here per Rule 7 because we filter on ``workspace`` — but to stay
    inside the 4-file shape we go through the uploads MongoFileStore
    helper rather than touching the document class.
    """

    from ee.cloud.uploads.mongo_store import MongoFileStore

    store = MongoFileStore()
    rows = await store.list_by_workspace(workspace_id, limit=50)
    out: dict[str, str] = {}
    for rec in rows:
        # ``list_by_workspace`` returns FileRecord shapes that don't carry
        # ``folder_path``; we need the doc itself to check. Cheap: the
        # planner folder rarely has more than a handful of files.
        doc = await store.get_doc_scoped(rec.id, workspace=workspace_id)
        if doc is None or (doc.folder_path or "/") != folder_path:
            continue
        out.setdefault(rec.filename, rec.id)
    return out


async def _list_planner_task_ids(
    *,
    ctx: RequestContext,
    project_id: str,
) -> list[str]:
    """Return Task ids created by the planner for ``project_id``.

    Filters on ``project_id`` AND ``source.type='planner'`` so we don't
    count manually-created tasks the operator filed under the same
    project. v0 uses a list+filter pass — small N, and the cloud Tasks
    listing already supports ``project_id`` directly.
    """

    from ee.cloud.tasks import service as tasks_service
    from ee.cloud.tasks.dto import ListTasksRequest

    rows = await tasks_service.agent_list_tasks(
        ctx, ListTasksRequest(project_id=project_id, limit=500)
    )
    return [r.id for r in rows if r.source.type == "planner"]


async def _load_project_or_404(ctx: RequestContext, project_id: str) -> Any:
    """Tenant-checked Project load. Raises ``NotFound`` on missing /
    cross-workspace ids — uniform 404 prevents id enumeration timing
    attacks.
    """

    from ee.cloud.projects import service as projects_service

    try:
        return await projects_service.agent_get(ctx, project_id)
    except NotFound:
        raise


# ---------------------------------------------------------------------------
# DTO mapping
# ---------------------------------------------------------------------------


def _session_to_dto(session: PlanSession) -> PlanProjectResult:
    """Map a domain :class:`PlanSession` to its wire DTO."""

    return PlanProjectResult(
        plan_session_id=session.id,
        project_id=session.project_id,
        status=session.status,
        prd_file_id=session.prd_file_id,
        plan_file_id=session.plan_file_id,
        goal_file_id=session.goal_file_id,
        task_ids=list(session.task_ids),
        agent_gaps=[
            AgentGapDTO(
                spec_name=g.spec_name,
                recommended_role=g.recommended_role,
                specialties=list(g.specialties),
            )
            for g in session.agent_gaps
        ],
    )


__all__ = [
    "agent_plan_project",
    "get_plan_for_project",
]
