# ee/pocketpaw_ee/cloud/jobs/router.py
# Created: 2026-06-20 (feat/workspace-jobs, pp#1459) — FastAPI router for the
# workspace-jobs status surface. Thin adapter: it resolves identity + tenancy
# and delegates the read to `jobs.service.get_job`, which re-checks the
# workspace and returns None on a cross-tenant id. Any-member read (no role
# check) via `require_membership`, which reads `workspace_id` from the path.
# There is NO dispatch route here — jobs are triggered through the existing
# `POST /pockets/{id}/actions/run` (kind=="job") branch.

"""Workspace-jobs status-poll router."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud.jobs import service as jobs_service
from pocketpaw_ee.cloud.jobs.dto import JobStatusResponse
from pocketpaw_ee.cloud.shared.deps import require_membership

router = APIRouter(prefix="/workspaces", tags=["Workspace Jobs"])


@router.get(
    "/{workspace_id}/jobs/{job_id}",
    dependencies=[Depends(require_membership)],
)
async def get_job_status(workspace_id: str, job_id: str) -> JobStatusResponse:
    """Return one job's lifecycle status.

    Tenancy: the service re-fetches by id AND re-checks ``workspace_id`` — a
    job belonging to another workspace reads as ``None`` here and 404s, so a
    member of workspace A can never poll workspace B's job by id. The job
    RESULT is not returned (it already merged into the pocket's `state`); the
    client polls only to learn when to stop the spinner.
    """
    doc = await jobs_service.get_job(workspace_id, job_id)
    if doc is None:
        raise NotFound("workspace_job", job_id)
    return JobStatusResponse(
        job_id=str(doc.id),
        status=doc.status,
        created_at=doc.createdAt,
        started_at=doc.started_at,
        ended_at=doc.ended_at,
        error=doc.error,
    )


__all__ = ["router"]
