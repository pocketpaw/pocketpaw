# Created: 2026-05-17 — pocketpaw#1118 P1. Cloud-side planner service:
#   wraps OSS ``pocketpaw.deep_work.planner.PlannerAgent`` and
#   materializes its output into cloud Projects, Tasks, and FileUpload
#   primitives. NEVER imports anything under ``src/pocketpaw/deep_work/``
#   except via the public OSS API surface (``PlannerAgent``, ``PlannerResult``,
#   ``TaskSpec``, ``AgentSpec``) — the OSS module is sacred and changes go
#   through the OSS PR flow, not through ee/cloud.
# Updated: 2026-05-17 (feat/planner-gaps-and-deps) — pocketpaw#1118 P3 + P4.
#   P3: persist a ``PlanSession`` Beanie doc per run so ``agent_resolve_gap``
#   can look up which tasks fell back to a human for a given missing spec
#   and reassign them after the operator creates the cloud Agent. Tasks
#   that fall back from agent → human now record the wanted spec name on
#   ``assignee.name`` (and on ``source.metadata.wanted_agent_spec_name``)
#   so the resolve flow can filter precisely without a JSON-blob parse.
#   P4: two-pass task materialization — pass 1 inserts each task with
#   ``blocked_by=[]`` and builds a ``spec_key → cloud_task_id`` map; pass
#   2 patches ``blocked_by`` via ``agent_update_task``. Unknown deps
#   surface as ``dependency_warnings`` on the response, not as a fatal
#   error — the planner keeps a partial result the operator can ship.
# Updated: 2026-05-18 (feat/mc-plan-sessions-endpoint) — added
#   ``list_plan_sessions(ctx, status, limit)``: workspace-scoped Beanie
#   read that returns ``PlanSessionSummary`` value objects for the
#   Mission Control Plan tab drafts list. Kept in this entity because
#   ee/cloud Rule 2 forbids any module other than planner.service from
#   importing ``ee.cloud.models.planner`` — Mission Control consumes
#   the typed summaries via the public API.
# Updated: 2026-05-21 (feat/taskspec-success-criteria) —
#   ``_materialize_tasks`` carries ``success_criteria`` and
#   ``preconditions`` from each OSS TaskSpec onto the cloud Task it
#   creates, so completion-time verification (pocketpaw#1162) can read
#   machine-checkable criteria off the materialized Task.
# Updated: 2026-07-02 (feat/svl-5-cloud-verify) — Self-Verifying Loop at
#   the CLOUD planner terminal (SVL-5), behind
#   ``cloud_plan_verify_loop_enabled`` (default False ⇒ byte-for-byte
#   today's behaviour). ``_run_one`` now judges each finished run's
#   output against the Task's ``success_criteria`` via the OSS
#   ``DeterministicVerdictProvider`` BEFORE any DONE side-effect fires
#   (``_verify_plan_task_outcome``): SOLVED / UNKNOWN stamp the verdict
#   on ``Task.verify`` and complete exactly as today; PARTIAL /
#   NOT_SOLVED is REQUEUED — unmet criteria appended to the cumulative
#   ``verify.feedback`` record, a verify counter (SEPARATE from error
#   retries) bumped, status back to ``proposed`` so the EXISTING
#   dispatch path re-runs it (``_execute_ready_plan_tasks`` tail-calls
#   itself when a batch requeued anything; no new dispatch machinery).
#   The re-run's instructions carry a "Why the last attempt was
#   rejected" section mirroring the OSS executor's prompt block. A
#   no-progress guard (unmet-criteria frozenset repeats or count
#   non-decreasing across two consecutive attempts) escalates EARLY;
#   exhausting ``cloud_plan_verify_max_requeues`` escalates too — both
#   terminate as ``failed`` (the existing terminal that already
#   unblocks dependents here) with ``verify.escalation_reason`` stamped
#   (``no_progress`` | ``budget_exhausted``) and emit TaskResolved so
#   the dependent-dispatch cascade still fires. A requeued or escalated
#   attempt never saves its output file, uploads artifacts, or
#   auto-completes.
# Updated: 2026-07-08 (feat/billing-enforce-gate) — ``_execute_ready_plan_tasks``
#   now runs the shared run-start BILLING gate
#   (``credits.guards.over_billing_limit``) after it finds ready tasks and BEFORE
#   the claim + ``pool.run`` batch, so an over-budget tenant makes NO model call
#   on the planner path. Tasks stay ``proposed`` and execute on a later tick once
#   budget frees. Flag-gated no-op unless ``billing_enforced``.
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

import asyncio
import json
import logging
from typing import Any

from pocketpaw_ee.cloud._core.context import RequestContext
from pocketpaw_ee.cloud._core.errors import NotFound, ValidationError
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import PlanGapResolved, PlanGenerated
from pocketpaw_ee.cloud.models.planner import PlanSession as _PlanSessionDoc
from pocketpaw_ee.cloud.models.planner import PlanSessionAgentGap as _PlanSessionAgentGapDoc
from pocketpaw_ee.cloud.planner.domain import AgentGap, PlanSession, PlanSessionSummary
from pocketpaw_ee.cloud.planner.dto import (
    AgentGapDTO,
    PlanProjectRequest,
    PlanProjectResult,
    ResolveGapRequest,
    ResolveGapResult,
)

logger = logging.getLogger(__name__)


def _record_deep_work_audit(
    workspace_id: str,
    actor_id: str,
    action: str,
    target_type: str,
    target_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Fire-and-forget audit recording for deep work operations.

    Never raises — failures are logged and swallowed so an audit outage
    cannot block a legitimate operation.
    """
    import asyncio

    from pocketpaw_ee.cloud.deep_work_log import service as _dw_log_service

    asyncio.ensure_future(
        _dw_log_service.record(
            workspace_id=workspace_id,
            actor_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            metadata=metadata or {},
        )
    )


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
    task_ids, dependency_warnings = await _materialize_tasks(
        ctx=ctx,
        project=project,
        planner_result=planner_result,
    )
    agent_gaps = await _detect_agent_gaps(
        workspace_id=ctx.workspace_id,
        planner_result=planner_result,
    )

    session_id = await _persist_plan_session(
        workspace_id=ctx.workspace_id,
        project_id=project.id,
        file_refs=file_refs,
        task_ids=task_ids,
        agent_gaps=agent_gaps,
        dependency_warnings=dependency_warnings,
    )

    session = PlanSession(
        id=session_id,
        workspace_id=ctx.workspace_id,
        project_id=project.id,
        status="ready",
        prd_file_id=file_refs.get("prd"),
        plan_file_id=file_refs.get("plan"),
        goal_file_id=file_refs.get("goal"),
        task_ids=tuple(task_ids),
        agent_gaps=tuple(agent_gaps),
        dependency_warnings=tuple(dependency_warnings),
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

    # Kick off execution of unblocked agent tasks asynchronously.
    # Cascade dispatch on task.resolved is handled by the subscriber
    # in cloud/tasks/listeners.py.
    try:
        asyncio.ensure_future(
            _execute_ready_plan_tasks(
                workspace_id=ctx.workspace_id,
                project_id=project.id,
            )
        )
    except Exception:
        logger.exception("failed to kick off plan task execution")

    _record_deep_work_audit(
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user_id or "system",
        action="deep_work.plan.generated",
        target_type="plan_session",
        target_id=session.id,
        metadata={
            "project_id": project.id,
            "project_name": project.name,
            "task_count": len(session.task_ids),
            "agent_gap_count": len(session.agent_gaps),
            "prd_file_id": session.prd_file_id,
            "deep_research": body.deep_research,
        },
    )

    return _session_to_dto(session)


async def agent_resolve_gap(ctx: RequestContext, body: ResolveGapRequest) -> ResolveGapResult:
    """Reassign human-fallback tasks for a missing agent spec to the
    newly-created cloud Agent.

    Called after the operator creates an Agent (via ``POST /api/v1/agents``)
    for a spec the planner originally wanted but the workspace was
    missing. The service:

      1. Loads the PlanSession doc — workspace tenant-checked. Unknown
         session id → ``NotFound``.
      2. Loads the new cloud Agent — workspace tenant-checked via
         ``agents_service.get`` + a workspace cross-check. Unknown agent
         id or cross-workspace → ``NotFound`` (uniform 404 to prevent
         id enumeration).
      3. Finds every task in the session whose ``assignee.kind == 'human'``
         AND ``assignee.name == spec_name`` (the fallback marker set in
         ``_resolve_assignee``). The ``source.metadata.wanted_agent_spec_name``
         column is the defensive cross-check.
      4. Reassigns each matching task to the new agent via
         ``tasks_service.agent_reassign_task``.
      5. Pops the resolved spec out of the PlanSession's ``agent_gaps``.
      6. Emits ``PlanGapResolved`` so the FE Plan tab can patch the
         gap card stack without a refetch.
    """

    body = ResolveGapRequest.model_validate(body)
    if not ctx.workspace_id:
        raise ValidationError(
            "planner.no_workspace",
            "resolving a plan gap requires an active workspace",
        )

    session_doc = await _load_plan_session_or_404(ctx, body.plan_session_id)
    new_agent = await _load_agent_for_gap_or_404(
        workspace_id=ctx.workspace_id,
        agent_id=body.new_agent_id,
    )

    from pocketpaw_ee.cloud.tasks import service as tasks_service
    from pocketpaw_ee.cloud.tasks.dto import ListTasksRequest, ReassignTaskRequest

    # The list of tasks belonging to this plan session is canonical on
    # the PlanSession doc — we filter the session-scoped slice down to
    # human-fallback rows matching the spec name. The wanted-spec
    # metadata is the safe cross-check (an operator could have renamed
    # the assignee in the meantime; the source metadata is immutable).
    rows = await tasks_service.agent_list_tasks(
        ctx, ListTasksRequest(project_id=session_doc.project_id, limit=500)
    )
    spec_name_lower = body.spec_name.lower()
    # O(n) membership check per row instead of O(n²) — relevant once a
    # plan session has more than a few dozen tasks.
    session_task_id_set = set(session_doc.task_ids)
    targets = [
        r
        for r in rows
        if r.id in session_task_id_set
        and r.assignee.kind == "human"
        and (
            (r.assignee.name or "").lower() == spec_name_lower
            or (r.source.metadata or {}).get("wanted_agent_spec_name", "").lower()
            == spec_name_lower
        )
    ]

    reassign_body = ReassignTaskRequest(
        assignee_kind="agent",
        assignee_id=body.new_agent_id,
        assignee_name=new_agent.name,
    )
    reassigned_task_ids: list[str] = []
    for task_row in targets:
        try:
            await tasks_service.agent_reassign_task(ctx, task_row.id, reassign_body)
        except Exception:  # noqa: BLE001
            logger.exception(
                "resolve_gap reassignment failed for task=%s spec=%s",
                task_row.id,
                body.spec_name,
            )
            continue
        reassigned_task_ids.append(task_row.id)

    # Strip the resolved spec out of the persisted gap list. Match is
    # case-insensitive on spec_name to mirror the planner's detection
    # path (``_detect_agent_gaps`` lowercases for the existing-name set).
    remaining_gaps_docs = [
        g for g in session_doc.agent_gaps if (g.spec_name or "").lower() != spec_name_lower
    ]
    session_doc.agent_gaps = remaining_gaps_docs
    if reassigned_task_ids:
        # Refresh task_ids only if we actually wrote — keeps the doc
        # stable when the resolve hits an empty target set.
        pass  # task_ids unchanged: same tasks, different assignee.
    await session_doc.save()

    remaining_dto = [
        AgentGapDTO(
            spec_name=g.spec_name,
            recommended_role=g.recommended_role,
            specialties=list(g.specialties),
        )
        for g in remaining_gaps_docs
    ]

    await emit(
        PlanGapResolved(
            data={
                "workspace_id": ctx.workspace_id,
                "project_id": session_doc.project_id,
                "plan_session_id": str(session_doc.id),
                "spec_name": body.spec_name,
                "new_agent_id": body.new_agent_id,
                "reassigned_task_count": len(reassigned_task_ids),
                "remaining_gap_count": len(remaining_gaps_docs),
            }
        )
    )

    return ResolveGapResult(
        plan_session_id=str(session_doc.id),
        spec_name=body.spec_name,
        new_agent_id=body.new_agent_id,
        reassigned_task_ids=reassigned_task_ids,
        remaining_gaps=remaining_dto,
    )


async def get_plan_for_project(ctx: RequestContext, project_id: str) -> PlanProjectResult | None:
    """Return the most recent plan summary for ``project_id``, or ``None``.

    P3 added a persisted PlanSession Beanie doc — when it exists we
    return its persisted shape (so plan_session_id, agent_gaps, and
    dependency_warnings round-trip across refreshes). Pre-P3 plans (and
    forks that disabled the doc) still reconstruct from the files panel
    so the read path stays backwards-compatible.

    Reconstruction steps:

      1. Verify the project exists in the caller's workspace
         (tenant check; raises NotFound otherwise).
      2. If a PlanSession doc exists, hydrate from it directly.
      3. Otherwise look for the PRD file at
         ``/projects/{project_id}/prd.md``; absent → no plan yet.
      4. Surface the matching tasks via the existing tasks service.

    Agent-gap detection is intentionally NOT re-run here — it's a
    point-in-time signal from the original plan; re-running it on
    every Plan-tab refresh would hit the agents list for nothing.
    """

    if not ctx.workspace_id:
        return None

    project = await _load_project_or_404(ctx, project_id)

    # P3 happy path — persisted doc carries everything the FE needs.
    persisted = await _PlanSessionDoc.find_one(
        {"workspace": ctx.workspace_id, "project_id": project.id}
    )
    if persisted is not None:
        session = PlanSession(
            id=str(persisted.id),
            workspace_id=ctx.workspace_id,
            project_id=project.id,
            status=persisted.status,
            prd_file_id=persisted.prd_file_id,
            plan_file_id=persisted.plan_file_id,
            goal_file_id=persisted.goal_file_id,
            task_ids=tuple(persisted.task_ids),
            agent_gaps=tuple(
                AgentGap(
                    spec_name=g.spec_name,
                    recommended_role=g.recommended_role,
                    specialties=tuple(g.specialties),
                )
                for g in persisted.agent_gaps
            ),
            dependency_warnings=tuple(persisted.dependency_warnings),
        )
        return _session_to_dto(session)

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


async def list_plan_sessions(
    ctx: RequestContext,
    *,
    status: str | None = None,
    limit: int = 50,
) -> list[PlanSessionSummary]:
    """List persisted plan sessions for the active workspace.

    Powers the Mission Control Plan tab drafts list — the façade calls
    this and DTO-maps the summaries to the wire shape. We keep the
    Beanie read here (per ee/cloud Rule 2: only ``planner.service`` may
    import ``ee.cloud.models.planner``) so Mission Control stays a
    pure consumer of typed value objects.

    Tenancy:
      - Workspace filter is mandatory; an empty / missing
        ``ctx.workspace_id`` returns ``[]`` rather than raising, mirroring
        the audit service's defensive empty-response pattern. The
        router-level dep guard already 400s before we get here when the
        user has no active workspace.

    Status filter:
      - The doc-level vocabulary is ``ready`` | ``stale``; the wire
        vocabulary (``draft`` | ``active`` | ``archived``) is mapped at
        the DTO boundary. Callers passing an unknown ``status`` get an
        empty list — we don't 400, because the wire-side enum is
        DTO-validated upstream of this call.

    Name resolution:
      - PlanSession docs don't carry a name field of their own. We
        batch-load the linked Project docs once and inject the project
        name into each summary. Projects in a different workspace
        (shouldn't happen given the workspace filter, but defensive)
        are skipped — the session shows an empty name rather than
        leaking the other tenant's project label.

    # no-event: read-only path per Rule 9.
    """

    if not ctx.workspace_id:
        return []

    query: dict[str, Any] = {"workspace": ctx.workspace_id}
    if status is not None:
        query["status"] = status

    docs = (
        await _PlanSessionDoc.find(query)
        .sort("-updatedAt")
        .limit(max(1, min(limit, 200)))
        .to_list()
    )
    if not docs:
        return []

    # Batch-load project names so the listing doesn't N+1. We only
    # surface projects that live in the caller's workspace — the
    # ``projects.service.agent_get`` chokepoint enforces this anyway, so
    # we go straight to the Beanie collection via the projects entity's
    # public ``exists_in_workspace`` + ``agent_get`` pair.
    from pocketpaw_ee.cloud.projects import service as projects_service

    project_ids = sorted({d.project_id for d in docs})
    name_by_id: dict[str, str] = {}
    for pid in project_ids:
        try:
            proj = await projects_service.agent_get(ctx, pid)
        except NotFound:
            # Project gone (deleted) — leave name empty. The drafts list
            # row still renders so the operator can audit / clean up.
            continue
        name_by_id[pid] = proj.name

    summaries: list[PlanSessionSummary] = []
    for d in docs:
        summaries.append(
            PlanSessionSummary(
                id=str(d.id),
                workspace_id=ctx.workspace_id,
                project_id=d.project_id,
                name=name_by_id.get(d.project_id, ""),
                status=d.status,
                task_count=len(d.task_ids),
                created_at=d.createdAt,
                updated_at=d.updatedAt,
            )
        )
    return summaries


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

    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore
    from pocketpaw_ee.cloud.uploads.service import write_text_file

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
) -> tuple[list[str], list[str]]:
    """Create one cloud Task per OSS TaskSpec via two passes.

    Pass 1 inserts every Task with ``blocked_by=[]`` so we have cloud
    ids to point at; pass 2 patches ``blocked_by`` once the
    ``spec_key → cloud_task_id`` map is fully populated. This handles
    forward references — a TaskSpec may depend on a sibling that wasn't
    created yet at the time of its own insert.

    Assignee resolution:

      * If the planner left a ``required_specialties`` hint AND we can
        find a cloud Agent in the workspace whose name matches the OSS
        team_recommendation entry that covers any of those specialties,
        assign the task to that agent (kind=agent).
      * Otherwise fall back to the project's lead_id (or the caller)
        as a human assignee, recording the planner-wanted spec name on
        ``assignee.name`` so the resolve-gap flow can find the row.

    Returns ``(task_ids, dependency_warnings)`` — warnings carry any
    ``blocked_by_keys`` entries that didn't resolve to a sibling spec
    (planner bug — we skip the unknown dep but still create the task).
    """

    from pocketpaw_ee.cloud.agents import service as agents_service
    from pocketpaw_ee.cloud.tasks import service as tasks_service
    from pocketpaw_ee.cloud.tasks.dto import (
        AssigneeDTO,
        CreateTaskRequest,
        SourceDTO,
        UpdateTaskRequest,
    )

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

    # Pass 1 — create every task with empty blocked_by, build the
    # spec_key → cloud_task_id map for pass 2.
    task_ids: list[str] = []
    spec_key_to_task_id: dict[str, str] = {}
    created_pairs: list[tuple[Any, str]] = []  # (spec, task_id) for pass 2

    for spec in all_specs:
        assignee_kind, assignee_id, assignee_name, wanted_spec_name = _resolve_assignee(
            spec=spec,
            specialty_to_agent_name=specialty_to_agent_name,
            by_name=by_name,
            fallback_id=fallback_assignee_id,
            fallback_name="",
        )
        priority = _normalize_priority(getattr(spec, "priority", "medium"))

        source_metadata = {
            "planner_task_key": getattr(spec, "key", ""),
            "task_type": getattr(spec, "task_type", "agent"),
        }
        if wanted_spec_name:
            # Resolve-gap filters on this key — the planner wanted spec
            # X but no cloud Agent matched, so we recorded the want
            # alongside the fallback assignment.
            source_metadata["wanted_agent_spec_name"] = wanted_spec_name

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
                metadata=source_metadata,
            ),
            blocked_by=[],
            # Machine-verifiable criteria the planner emitted on the
            # TaskSpec. Carried onto the cloud Task so completion-time
            # verification (pocketpaw#1162) has them. ``getattr`` keeps
            # the materializer tolerant of an older TaskSpec shape.
            success_criteria=list(getattr(spec, "success_criteria", None) or []),
            preconditions=list(getattr(spec, "preconditions", None) or []),
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
        spec_key = getattr(spec, "key", "") or ""
        if spec_key:
            spec_key_to_task_id[spec_key] = created.id
        created_pairs.append((spec, created.id))

    # Pass 2 — wire dependencies. Each TaskSpec's ``blocked_by_keys``
    # references sibling spec keys; we translate to the cloud task ids
    # we minted in pass 1 and patch the row via agent_update_task. Any
    # unresolved name surfaces as a warning rather than aborting.
    dependency_warnings: list[str] = []
    for spec, task_id in created_pairs:
        dep_keys = list(getattr(spec, "blocked_by_keys", []) or [])
        if not dep_keys:
            continue
        resolved_ids: list[str] = []
        for dep_key in dep_keys:
            cloud_id = spec_key_to_task_id.get(dep_key)
            if cloud_id is None:
                warning = (
                    f"task {getattr(spec, 'key', '?')!r} depends on unknown "
                    f"spec {dep_key!r}; skipping"
                )
                logger.warning(warning)
                dependency_warnings.append(dep_key)
                continue
            resolved_ids.append(cloud_id)
        if not resolved_ids:
            continue
        try:
            await tasks_service.agent_update_task(
                ctx, task_id, UpdateTaskRequest(blocked_by=resolved_ids)
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "dependency wiring failed for task %s spec=%s",
                task_id,
                getattr(spec, "key", "?"),
            )

    return task_ids, dependency_warnings


def _resolve_assignee(
    *,
    spec: Any,
    specialty_to_agent_name: dict[str, str],
    by_name: dict[str, Any],
    fallback_id: str,
    fallback_name: str,
) -> tuple[str, str, str, str]:
    """Map an OSS TaskSpec to a cloud assignee 4-tuple.

    Returns ``(kind, id, name, wanted_spec_name)``. ``wanted_spec_name``
    is non-empty only when the planner wanted an agent we couldn't
    resolve — it records which team-recommendation spec we fell back
    from so the resolve-gap flow can match the rows precisely. For
    human-typed specs the wanted name stays empty (the planner
    explicitly asked for a human; there's no "missing agent" to resolve).

    Human fallback assignees carry ``assignee.name = wanted_spec_name``
    so the operator UI can render "wanted: events-coordinator (fallback
    to lead)" without a separate field, and so the resolve-gap filter
    matches on a single column.
    """

    task_type = getattr(spec, "task_type", "agent")
    if task_type == "human":
        return ("human", fallback_id, fallback_name, "")

    specialties = [sp.lower() for sp in getattr(spec, "required_specialties", []) or []]
    wanted_team_name = ""
    for sp in specialties:
        team_name = specialty_to_agent_name.get(sp)
        if not team_name:
            continue
        # Remember the first team-recommendation name we tried so the
        # fallback path can record it even when no cloud Agent matched.
        if not wanted_team_name:
            wanted_team_name = team_name
        agent = by_name.get(team_name.lower())
        if agent is not None:
            return ("agent", str(agent.id), agent.name, "")

    # No specialty match — fall back to ``human`` so the operator
    # explicitly re-routes rather than us silently picking a random
    # cloud agent. Record the wanted spec name on the assignee so the
    # resolve-gap flow can find the row by ``assignee.name``.
    fallback_display_name = wanted_team_name or fallback_name
    return ("human", fallback_id, fallback_display_name, wanted_team_name)


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

    from pocketpaw_ee.cloud.agents import service as agents_service

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

    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

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

    from pocketpaw_ee.cloud.tasks import service as tasks_service
    from pocketpaw_ee.cloud.tasks.dto import ListTasksRequest

    rows = await tasks_service.agent_list_tasks(
        ctx, ListTasksRequest(project_id=project_id, limit=500)
    )
    return [r.id for r in rows if r.source.type == "planner"]


async def _load_project_or_404(ctx: RequestContext, project_id: str) -> Any:
    """Tenant-checked Project load. Raises ``NotFound`` on missing /
    cross-workspace ids — uniform 404 prevents id enumeration timing
    attacks.
    """

    from pocketpaw_ee.cloud.projects import service as projects_service

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
        dependency_warnings=list(session.dependency_warnings),
    )


# ---------------------------------------------------------------------------
# PlanSession persistence + load helpers
# ---------------------------------------------------------------------------


async def _persist_plan_session(
    *,
    workspace_id: str,
    project_id: str,
    file_refs: dict[str, str],
    task_ids: list[str],
    agent_gaps: list[AgentGap],
    dependency_warnings: list[str] | None = None,
) -> str:
    """Insert (or replace) the PlanSession doc for ``project_id``.

    We re-run by deleting the prior doc rather than upserting because
    Beanie docs carry a generated ObjectId we don't want to retain
    across re-plans (the previous run's id would still flow through
    PlanGapResolved events for the new run). One doc per workspace +
    project enforces "one active plan session per project".
    """

    existing = await _PlanSessionDoc.find(
        {"workspace": workspace_id, "project_id": project_id}
    ).to_list()
    for prior in existing:
        await prior.delete()

    doc = _PlanSessionDoc(
        workspace=workspace_id,
        project_id=project_id,
        status="ready",
        prd_file_id=file_refs.get("prd"),
        plan_file_id=file_refs.get("plan"),
        goal_file_id=file_refs.get("goal"),
        task_ids=list(task_ids),
        agent_gaps=[
            _PlanSessionAgentGapDoc(
                spec_name=g.spec_name,
                recommended_role=g.recommended_role,
                specialties=list(g.specialties),
            )
            for g in agent_gaps
        ],
        dependency_warnings=list(dependency_warnings or []),
    )
    await doc.insert()
    return str(doc.id)


async def _load_plan_session_or_404(ctx: RequestContext, plan_session_id: str) -> _PlanSessionDoc:
    """Tenant-checked PlanSession load. Uniform NotFound on miss /
    cross-workspace ids (id enumeration mitigation, matching the tasks
    service ``_fetch_task`` pattern).
    """

    from beanie import PydanticObjectId

    try:
        oid = PydanticObjectId(plan_session_id)
    except Exception as exc:  # noqa: BLE001
        raise NotFound("plan_session", plan_session_id) from exc
    doc = await _PlanSessionDoc.get(oid)
    if doc is None or doc.workspace != ctx.workspace_id:
        raise NotFound("plan_session", plan_session_id)
    return doc


async def _load_agent_for_gap_or_404(*, workspace_id: str, agent_id: str) -> Any:
    """Load a cloud Agent + cross-check workspace tenancy.

    ``agents_service.get`` doesn't take a workspace argument (it's a
    by-id loader that returns the agent regardless of workspace) so we
    enforce the tenancy check here. Uniform NotFound on either branch
    so the operator can't probe whether a given agent id exists in
    another workspace.
    """

    from pocketpaw_ee.cloud.agents import service as agents_service

    try:
        agent = await agents_service.get(agent_id)
    except NotFound:
        raise NotFound("agent", agent_id) from None
    if agent.workspace_id != workspace_id:
        raise NotFound("agent", agent_id)
    return agent


__all__ = [
    "agent_plan_project",
    "agent_resolve_gap",
    "get_plan_for_project",
    "list_plan_sessions",
    "on_plan_task_resolved",
]


# ---------------------------------------------------------------------------
# Plan task execution — claims unblocked agent tasks and runs via AgentPool
# ---------------------------------------------------------------------------


async def _execute_ready_plan_tasks(
    *,
    workspace_id: str,
    project_id: str,
) -> None:
    """Find and execute unblocked agent tasks for a plan project.

    If tasks were assigned to a human fallback (the planner recommended
    an agent missing from the workspace), auto-creates the missing agent,
    reassigns the task, then executes it via AgentPool.
    """
    from pocketpaw_ee.cloud.models.task import Task as _TaskDoc

    docs = await _TaskDoc.find(
        {
            "workspace_id": workspace_id,
            "project_id": project_id,
        }
    ).to_list()
    if not docs:
        logger.info("_execute_ready_plan_tasks: no tasks for project %s", project_id)
        return

    resolved_ids = {str(d.id) for d in docs if d.status in ("done", "failed", "reverted")}

    # Log every task
    for d in docs:
        meta = d.source.metadata or {}
        wanted = meta.get("wanted_agent_spec_name", "")
        logger.info(
            "task_check: id=%s status=%s title=%s assignee=(%s,%s) wanted=%s blocked_by=%s",
            str(d.id),
            d.status,
            d.title[:60],
            d.assignee.kind,
            d.assignee.id[:12] if d.assignee.id else "none",
            wanted,
            list(d.blocked_by or []),
        )
    logger.info("dep_check: total=%d resolved=%s", len(docs), sorted(resolved_ids))

    # Find proposed agent-assigned tasks with no unresolved blockers
    ready = []
    for d in docs:
        if d.status != "proposed":
            continue
        blocked = list(d.blocked_by or [])
        if blocked and not all(bid in resolved_ids for bid in blocked):
            continue
        # Auto-create missing agent if needed
        if d.assignee.kind != "agent":
            meta = d.source.metadata or {}
            wanted = meta.get("wanted_agent_spec_name", "")
            if not wanted:
                continue
            aid = await _ensure_agent(workspace_id, wanted)
            if not aid:
                continue
            await _reassign_task(workspace_id, str(d.id), aid, wanted)
            refreshed = await _TaskDoc.get(str(d.id))
            if refreshed:
                ready.append(refreshed)
        else:
            ready.append(d)

    if not ready:
        logger.info(
            "_execute_ready_plan_tasks: no ready tasks (total=%d, resolved=%d)",
            len(docs),
            len(resolved_ids),
        )
        return
    logger.info("_execute_ready_plan_tasks: %d ready task(s)", len(ready))

    # Run-start BILLING gate — the planner is a background (non-HTTP) executor, so
    # reject BEFORE claiming/running any task rather than starting work an
    # over-budget tenant can't pay for. Gating here (after we know there is work,
    # before the claim + ``pool.run`` batch) means NO model call fires while over
    # budget; the tasks stay ``proposed`` and execute on a later tick once budget
    # frees. Flag-gated no-op unless ``billing_enforced``; there is no user-facing
    # stream on this path, so a warning log is the terminal signal.
    from pocketpaw_ee.cloud.credits.guards import over_billing_limit

    billing_reject = await over_billing_limit(workspace_id)
    if billing_reject is not None:
        logger.warning(
            "_execute_ready_plan_tasks: workspace %s over billing limit (%s) — "
            "skipping execution of %d ready task(s) for project %s",
            workspace_id,
            billing_reject.code,
            len(ready),
            project_id,
        )
        return

    from types import SimpleNamespace

    from pocketpaw.agents.pool import get_agent_pool
    from pocketpaw.config import get_settings
    from pocketpaw_ee.cloud.tasks.dto import ClaimTaskRequest
    from pocketpaw_ee.cloud.tasks.service import agent_claim_task as cloud_claim_task

    ctx = SimpleNamespace(workspace_id=workspace_id)
    pool = get_agent_pool()

    # Claim all concurrently
    async def _try_claim(d):
        tid = str(d.id)
        aid = d.assignee.id
        if not aid:
            return None
        try:
            r = await cloud_claim_task(ctx, tid, ClaimTaskRequest(agent_id=aid))
            if r.get("ok"):
                return (tid, aid, d)
            logger.warning("claim_task failed %s reason=%s", tid, r.get("reason"))
        except Exception as e:
            logger.warning("claim_task threw for %s: %s", tid, e)
        return None

    batch = []
    for res in await asyncio.gather(*[_try_claim(d) for d in ready]):
        if res:
            batch.append(res)

    if not batch:
        logger.info("no tasks could be claimed")
        return
    logger.info("claimed %d task(s), spawning parallel execution", len(batch))

    # Run all concurrently
    async def _run_one(tid, aid, d):
        instr = "You are executing a task from a project plan.\\n\\n"
        instr += "## Task: " + d.title + "\\n\\n"
        instr += d.summary + "\\n\\n"
        if d.success_criteria:
            instr += "## Success Criteria\\n"
            for i, sc in enumerate(d.success_criteria, 1):
                instr += str(i) + ". " + sc + "\\n"
        # SVL-5: if a prior attempt was rejected by the verify loop,
        # surface the SPECIFIC unmet criteria — cumulative, most recent
        # first — so the re-dispatched agent sees exactly what to fix
        # rather than a bare "try again". Mirrors the OSS executor's
        # ``_build_task_prompt`` "Why the last attempt was rejected"
        # block. Flag-gated so a disabled loop renders today's
        # instructions byte-for-byte even if old feedback is on the doc.
        if get_settings().cloud_plan_verify_loop_enabled:
            feedback = (getattr(d, "verify", None) or {}).get("feedback") or []
            if feedback:
                instr += "\\n## Why the last attempt was rejected\\n"
                instr += (
                    "A previous attempt did NOT meet the success criteria below. "
                    "Address each unmet criterion in your work this time:\\n"
                )
                for record in reversed(feedback):
                    attempt = record.get("attempt")
                    summary = record.get("summary", "")
                    if attempt:
                        instr += "Attempt " + str(attempt) + " (" + summary + "):\\n"
                    else:
                        instr += summary + ":\\n"
                    for item in record.get("unmet_criteria", []):
                        line = "- " + item.get("criterion", "")
                        if item.get("detail"):
                            line += " — " + item.get("detail", "")
                        instr += line + "\\n"
        instr += "\\n## Workflow\\n"
        instr += "1. Review the task and create the required output.\\n"
        instr += "2. When done, call the MCP tool complete_task\\n"
        instr += "3. Prefix created files with FILE: /path/to/file.\n"
        output_chunks: list[str] = []
        from pocketpaw.agents.protocol import AgentEvent

        try:
            async for ev in pool.run(
                agent_id=aid,
                message=instr,
                session_key="planner:task:" + project_id + ":" + tid,
                instructions="Execute the task and call complete_task when done.",
            ):
                try:
                    if isinstance(ev, AgentEvent) and ev.type == "message":
                        output_chunks.append(str(ev.content or ""))
                except Exception:
                    pass
            full_output = "".join(output_chunks)[:50000] if output_chunks else ""
            logger.info("agent done task=%s output_len=%d", tid, len(full_output))

            # SVL-5 (Self-Verifying Loop, cloud planner terminal): judge
            # the run's output against the Task's success_criteria BEFORE
            # any DONE side-effect fires. "done" = SOLVED / UNKNOWN (or
            # flag off) — fall through to today's completion path
            # untouched. "requeued" / "escalated" = the verify helper
            # already moved the task (back to ``proposed`` / to
            # ``failed``) and stamped why; a rejected attempt must NOT
            # save its output file, upload artifacts, or auto-complete —
            # returning here is what enforces that invariant.
            resolution = "done"
            if get_settings().cloud_plan_verify_loop_enabled:
                resolution = await _verify_plan_task_outcome(
                    workspace_id=workspace_id,
                    project_id=project_id,
                    task_id=tid,
                    full_output=full_output,
                )
            if resolution != "done":
                return resolution

            if full_output:
                await _save_task_output_file(workspace_id, tid, d.title, full_output, project_id)
                uf = await _scan_and_upload_agent_files(
                    workspace_id=workspace_id,
                    project_id=project_id,
                    task_id=tid,
                    task_title=d.title,
                    agent_output=full_output,
                )
                if uf:
                    logger.info("uploaded %d file(s) from task %s", len(uf), tid)
            await _auto_complete_task(
                workspace_id,
                tid,
                project_id,
                result_summary=full_output[:2000],
            )
            return "done"
        except Exception as e:
            logger.exception("agent exec failed for task %s: %s", tid, e)
            return "error"

    results = await asyncio.gather(*[_run_one(tid, aid, d) for tid, aid, d in batch])

    # SVL-5: a verify-requeued task is back to ``proposed`` with feedback
    # stamped, but nothing emits TaskResolved for it — so re-enter the
    # EXISTING dispatch path (this very function, same as the
    # ``on_plan_task_resolved`` cascade does) to claim and re-run it.
    # Bounded: each pass either solves, requeues within the budget, or
    # escalates to ``failed``, so the recursion depth is capped by
    # ``cloud_plan_verify_max_requeues``. Flag off ⇒ every result is
    # "done"/"error" and this never fires.
    if any(r == "requeued" for r in results):
        await _execute_ready_plan_tasks(
            workspace_id=workspace_id,
            project_id=project_id,
        )


# ---------------------------------------------------------------------------
# Helpers for auto-creating missing agents and reassigning tasks
# ---------------------------------------------------------------------------


async def _ensure_agent(workspace_id: str, spec_name: str) -> str | None:
    """Find or create a cloud agent for the given planner spec name."""
    from types import SimpleNamespace

    from pocketpaw_ee.cloud.agents import service as agents_service
    from pocketpaw_ee.cloud.agents.dto import CreateAgentRequest

    # Check if already exists by listing agents and matching name
    try:
        existing_list = await agents_service.list_agents(workspace_id)
        for a in existing_list:
            if a.name.lower() == spec_name.lower():
                logger.info("_ensure_agent: found existing agent %s for %s", str(a.id), spec_name)
                return str(a.id)
    except Exception:
        pass

    # Create agent — slugify the name
    import re

    slug = re.sub(r"[^a-z0-9-]", "-", spec_name.lower()).strip("-") or "planner-agent"
    ctx = SimpleNamespace(workspace_id=workspace_id, user_id="")

    try:
        created = await agents_service.create(
            ctx,
            workspace_id,
            CreateAgentRequest(
                name=spec_name,
                slug=slug,
                description=f"Auto-created for planner spec: {spec_name}",
                backend="claude_agent_sdk",
            ),
        )
        logger.info("_ensure_agent: created agent %s for %s", str(created.id), spec_name)
        return str(created.id)
    except Exception as e:
        logger.warning("_ensure_agent: failed to create agent %s: %s", spec_name, e)
        return None


async def _reassign_task(workspace_id: str, task_id: str, agent_id: str, agent_name: str) -> bool:
    """Reassign a human-fallback task to the specified agent."""
    from types import SimpleNamespace

    from pocketpaw_ee.cloud.tasks import service as tasks_service
    from pocketpaw_ee.cloud.tasks.dto import ReassignTaskRequest

    ctx = SimpleNamespace(workspace_id=workspace_id)
    try:
        await tasks_service.agent_reassign_task(
            ctx,
            task_id,
            ReassignTaskRequest(
                assignee_kind="agent",
                assignee_id=agent_id,
                assignee_name=agent_name,
            ),
        )
        logger.info("_reassign_task: reassigned %s to agent %s", task_id, agent_name)
        return True
    except Exception as e:
        logger.warning("_reassign_task: failed for %s: %s", task_id, e)
        return False


def _plan_task_event_payload(doc: Any, workspace_id: str, project_id: str) -> dict:
    """Event payload for a planner-terminal task write (SVL-5).

    Same shape ``_auto_complete_task`` hand-builds for TaskResolved —
    ``cascade_plan_tasks`` reads ``workspace_id`` + ``task.project_id``
    off it — with ``status`` taken from the doc's CURRENT state so a
    requeue ships ``proposed`` and an escalation ships ``failed``.
    """
    return {
        "task_id": str(doc.id),
        "workspace_id": workspace_id,
        "project_id": project_id,
        "task": {
            "id": str(doc.id),
            "workspace_id": workspace_id,
            "project_id": project_id,
            "title": doc.title,
            "summary": doc.summary,
            "status": doc.status,
            "assignee": {
                "kind": doc.assignee.kind,
                "id": doc.assignee.id,
                "name": doc.assignee.name,
            },
        },
    }


async def _verify_plan_task_outcome(
    *,
    workspace_id: str,
    project_id: str,
    task_id: str,
    full_output: str,
) -> str:
    """SVL-5 verify hook — judge a finished plan-task run before completion.

    Called from ``_run_one`` only when ``cloud_plan_verify_loop_enabled``
    is on, after the agent run produced ``full_output`` and BEFORE any
    DONE side-effect. The verifier is the OSS
    ``DeterministicVerdictProvider`` — external to the agent that
    produced the output. Returns the resolution the caller must honour:

      - ``"done"`` — SOLVED / UNKNOWN (or the task doc vanished): a
        passing or uncheckable result is NEVER requeued or mutated. The
        verdict is stamped on ``Task.verify['verdict']``; the caller
        completes exactly as today.
      - ``"requeued"`` — PARTIAL / NOT_SOLVED, still progressing and
        within budget: this attempt's unmet criteria (criterion +
        detail) are APPENDED to the cumulative ``verify['feedback']``
        record, the verify counter (``verify['requeue_count']``,
        SEPARATE from any error-retry counter) is bumped, and status
        goes back to ``proposed`` so the existing claim/dispatch path
        re-runs it.
      - ``"escalated"`` — the no-progress guard tripped (this attempt's
        unmet-criteria frozenset equals the previous attempt's, or its
        count is non-decreasing) OR the requeue budget is exhausted:
        status flips to ``failed`` — the existing terminal that already
        unblocks dependents in ``_execute_ready_plan_tasks`` — with
        ``verify['escalation_reason']`` stamped (``no_progress`` |
        ``budget_exhausted``). Emits TaskResolved so the same cascade
        that dispatches dependents after a completion fires here too.

    Mirrors the OSS executor's SVL-2/SVL-3 semantics at this seam.
    """
    from pocketpaw.config import get_settings
    from pocketpaw.instinct.models import OutcomeStatus
    from pocketpaw.instinct.verdict_provider import DeterministicVerdictProvider
    from pocketpaw_ee.cloud._core.realtime.emit import emit
    from pocketpaw_ee.cloud._core.realtime.events import TaskResolved, TaskUpdated
    from pocketpaw_ee.cloud.models.task import Task as _TaskDoc

    doc = await _TaskDoc.get(task_id)
    if not doc or doc.workspace_id != workspace_id:
        # Tenant guard on the read. A vanished / foreign doc falls
        # through to the caller's normal completion path, which
        # re-checks existence itself — same posture as today.
        return "done"

    verdict = DeterministicVerdictProvider().verify(full_output, list(doc.success_criteria or []))
    verify_state = dict(doc.verify or {})
    verify_state["verdict"] = verdict.model_dump()
    logger.info(
        "task %s verify verdict: %s (%s)",
        task_id,
        verdict.status,
        verdict.summary,
    )

    if verdict.status not in (OutcomeStatus.PARTIAL, OutcomeStatus.NOT_SOLVED):
        # SOLVED / UNKNOWN — stamp the verdict and pass through.
        doc.verify = verify_state
        await doc.save()
        # no-event: the TaskResolved emitted by _auto_complete_task
        # moments later announces this same completion; a separate
        # TaskUpdated here would double-fire the feed for one logical
        # completion write.
        return "done"

    unmet = [cr for cr in verdict.criteria_results if not cr.met]
    n = int(verify_state.get("requeue_count", 0))
    max_requeues = get_settings().cloud_plan_verify_max_requeues

    # No-progress / oscillation guard (mirrors OSS SVL-3), checked
    # BEFORE the budget: compare THIS attempt's unmet set against the
    # PREVIOUS attempt's. Exact same criteria still failing, or the
    # unmet count not shrinking — escalate early instead of burning the
    # whole requeue budget circling a dead end.
    prior_feedback = verify_state.get("feedback") or []
    no_progress = False
    if prior_feedback:
        current = frozenset(cr.criterion for cr in unmet)
        prev = frozenset(item["criterion"] for item in prior_feedback[-1].get("unmet_criteria", []))
        no_progress = current == prev or len(current) >= len(prev)

    if not no_progress and n < max_requeues:
        # REQUEUE: append this attempt's rejections to the CUMULATIVE
        # feedback log (attempt N's instructions carry 1..N-1), bump the
        # verify counter, and put the task back where the existing
        # dispatch machinery picks it up.
        verify_state["feedback"] = [
            *prior_feedback,
            {
                "attempt": n + 1,
                "status": verdict.status.value,
                "summary": verdict.summary,
                "unmet_criteria": [
                    {"criterion": cr.criterion, "detail": cr.detail} for cr in unmet
                ],
            },
        ]
        verify_state["requeue_count"] = n + 1
        doc.verify = verify_state
        doc.status = "proposed"
        await doc.save()
        await emit(TaskUpdated(data=_plan_task_event_payload(doc, workspace_id, project_id)))
        logger.info(
            "task %s failed verify (%s); requeue %d/%d with %d unmet criterion(s)",
            task_id,
            verdict.status,
            n + 1,
            max_requeues,
            len(unmet),
        )
        return "requeued"

    # ESCALATE: ``failed`` is the existing terminal — it already
    # unblocks dependents in ``_execute_ready_plan_tasks``'s resolved
    # set, and it surfaces in the operator's Snags. Stamp WHY so the
    # operator sees the cause, and emit TaskResolved so the
    # dependent-dispatch cascade fires exactly as it would after a
    # completion.
    reason = "no_progress" if no_progress else "budget_exhausted"
    verify_state["escalation_reason"] = reason
    doc.verify = verify_state
    doc.status = "failed"
    await doc.save()
    await emit(TaskResolved(data=_plan_task_event_payload(doc, workspace_id, project_id)))
    logger.info(
        "task %s failed verify (%s) after %d requeue(s); escalating to failed (%s)",
        task_id,
        verdict.status,
        n,
        reason,
    )
    return "escalated"


async def _auto_complete_task(
    workspace_id: str,
    task_id: str,
    project_id: str,
    result_summary: str = "",
) -> None:
    """Mark a plan task as done in the cloud DB and emit TaskResolved."""

    from pocketpaw_ee.cloud._core.realtime.emit import emit
    from pocketpaw_ee.cloud._core.realtime.events import TaskResolved
    from pocketpaw_ee.cloud.models.task import Task as _TaskDoc

    doc = await _TaskDoc.get(task_id)
    if not doc:
        logger.warning("auto-complete: task %s not found", task_id)
        return
    if doc.status in ("done", "failed", "reverted"):
        logger.debug("auto-complete: task %s already %s", task_id, doc.status)
        return

    if result_summary:
        if doc.summary:
            doc.summary = (doc.summary + "\n\n---\n\n" + result_summary).strip()
        else:
            doc.summary = result_summary

    old_status = doc.status
    doc.status = "done"
    await doc.save()
    logger.info("auto-completed task %s (%s) — %s -> done", task_id, doc.title, old_status)

    # Build event payload and emit so cascade fires.
    payload = {
        "task_id": task_id,
        "workspace_id": workspace_id,
        "project_id": project_id,
        "task": {
            "id": task_id,
            "workspace_id": workspace_id,
            "project_id": project_id,
            "title": doc.title,
            "summary": doc.summary,
            "status": "done",
            "assignee": {
                "kind": doc.assignee.kind,
                "id": doc.assignee.id,
                "name": doc.assignee.name,
            },
        },
    }
    try:
        await emit(TaskResolved(data=payload))
        logger.info("emitted TaskResolved for task %s", task_id)
    except Exception as e:
        logger.warning("auto-complete: emit failed for %s: %s", task_id, e)


async def _save_task_output_file(
    workspace_id: str,
    task_id: str,
    task_title: str,
    output: str,
    project_id: str,
) -> None:
    """Save agent task output as .md file in project folder."""
    import re as _r

    from pocketpaw_ee.cloud.uploads.service import write_text_file

    slug = _r.sub(r"[^a-z0-9-]", "-", task_title.lower())[:40] or "task-output"
    filename = f"task-{task_id[-8:]}-{slug}.md"
    try:
        rec = await write_text_file(
            workspace_id=workspace_id,
            owner_id="",
            folder_path=f"/projects/{project_id}",
            filename=filename,
            content=output[:100000],
            mime="text/markdown",
        )
        logger.info("saved task output: %s (id=%s, len=%d)", filename, rec.id, len(output))
    except Exception as e:
        logger.warning("failed to save task output: %s", e)


async def _scan_and_upload_agent_files(
    *,
    workspace_id: str,
    project_id: str,
    task_id: str,
    task_title: str,
    agent_output: str,
) -> list[str]:
    """Scan agent output for FILE: /path references and upload files to S3."""
    import os

    from pocketpaw_ee.cloud.uploads.service import write_text_file

    uploaded: list[str] = []
    seen: set[str] = set()
    for _line in agent_output.split("\n"):
        idx = _line.find("FILE:")
        if idx == -1:
            continue
        path = _line[idx + 5 :].strip().split()[0] if _line[idx + 5 :].strip() else ""
        if not path or not path.startswith("/") or path in seen:
            continue
        seen.add(path)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                fc = fh.read()
        except Exception:
            continue
        basename = os.path.basename(path)
        if not basename:
            continue
        try:
            rec = await write_text_file(
                workspace_id=workspace_id,
                owner_id="",
                folder_path=f"/projects/{project_id}",
                filename=basename,
                content=fc[:500000],
            )
            uploaded.append(rec.id)
            logger.info("uploaded agent file: %s (id=%s)", basename, rec.id)
        except Exception as e:
            logger.warning("upload failed %s: %s", basename, e)
    return uploaded


async def on_plan_task_resolved(workspace_id: str, project_id: str | None) -> None:
    """Cascade hook — called when a plan task completes.

    Dispatches any newly unblocked dependents.
    """
    if not project_id:
        return
    logger.info("on_plan_task_resolved: cascading for project %s", project_id)
    await _execute_ready_plan_tasks(
        workspace_id=workspace_id,
        project_id=project_id,
    )
