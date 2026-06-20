# ee/pocketpaw_ee/cloud/jobs/service.py
# Created: 2026-06-20 (feat/workspace-jobs, pp#1459) — the jobs entity service:
# the ONLY module in the package that reads/writes the `WorkspaceJobDoc` Beanie
# document (import-linter "Jobs" contract). Owns dispatch (registry lookup →
# param validation → queued doc → ARQ enqueue → `WorkspaceJobQueued` emit +
# INFO audit), the tenancy-checked status read, and the lifecycle transitions
# (`mark_running` / `mark_done` / `mark_failed`) the worker drives. `mark_failed`
# emits a WARNING `WorkspaceJobUpdated` + audit; success emits INFO.

"""Workspace-jobs service — Beanie writes + dispatch + lifecycle."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from typing import Any

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from pocketpaw.security.audit import AuditEvent, AuditSeverity, get_audit_logger
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import (
    WorkspaceJobQueued,
    WorkspaceJobUpdated,
)
from pocketpaw_ee.cloud.jobs.domain import JOBS_QUEUE, WORKSPACE_JOB_IDENTITY
from pocketpaw_ee.cloud.jobs.registry import resolve_job, validate_job_params
from pocketpaw_ee.cloud.models.workspace_job import WorkspaceJobDoc

logger = logging.getLogger(__name__)

# Reuse the same lazy-pool pattern as the chat-runs ArqExecutor — one pool per
# process, double-checked lock so concurrent first-dispatches don't leak pools.
_pool: ArqRedis | None = None
_pool_lock = asyncio.Lock()


async def _get_pool() -> ArqRedis:
    global _pool
    if _pool is None:
        async with _pool_lock:
            if _pool is None:
                url = os.environ.get("POCKETPAW_REDIS_URL", "").strip()
                if not url:
                    raise RuntimeError(
                        "POCKETPAW_REDIS_URL is not set — workspace jobs need Redis."
                    )
                _pool = await create_pool(RedisSettings.from_dsn(url))
    return _pool


def _reset_for_tests() -> None:
    global _pool
    _pool = None


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


async def dispatch_job(
    *,
    workspace_id: str,
    pocket_id: str,
    action: str,
    job_name: str,
    params: dict[str, Any],
    triggered_by: str,
) -> dict:
    """Resolve, persist, and enqueue a workspace job.

    Order matters for the security contract:
      1. registry lookup — unknown name raises ``UnknownJobError`` (400)
         BEFORE any doc is written or the pool touched.
      2. param validation — a credential-shaped key raises ``JobParamsError``
         (400), again before any persistence.
      3. create the ``queued`` status doc.
      4. enqueue ``execute_workspace_job(job_id)`` on the jobs queue.
      5. emit ``WorkspaceJobQueued`` + an INFO audit event.

    Returns ``{ok: True, code: "job_enqueued", job_id}`` for the action route.
    """
    # (1) + (2) — gate before any write.
    resolve_job(job_name)
    validate_job_params(params)

    # (3) — persist queued.
    doc = WorkspaceJobDoc(
        workspace=workspace_id,
        pocket_id=pocket_id,
        action=action,
        job_name=job_name,
        params=dict(params),
        triggered_by=triggered_by,
        status="queued",
    )
    await doc.insert()
    job_id = str(doc.id)

    # (4) — enqueue. A pool/enqueue failure must surface (the caller turns it
    # into a 5xx); the queued doc stays so an operator can see the orphan.
    try:
        pool = await _get_pool()
        arq_job = await pool.enqueue_job("execute_workspace_job", job_id, queue=JOBS_QUEUE)
        if arq_job is not None:
            doc.arq_job_id = getattr(arq_job, "job_id", None)
            await doc.save()
    except Exception:
        logger.exception("jobs: enqueue failed for job %s (%s)", job_id, job_name)
        raise

    # (5) — emit + audit (best-effort; emit never raises, audit is wrapped).
    await emit(
        WorkspaceJobQueued(
            data={
                "job_id": job_id,
                "workspace_id": workspace_id,
                "pocket_id": pocket_id,
                "action": action,
                "job_name": job_name,
            }
        )
    )
    _audit(
        severity=AuditSeverity.INFO,
        status="success",
        job_id=job_id,
        workspace_id=workspace_id,
        pocket_id=pocket_id,
        action=action,
        job_name=job_name,
    )
    return {"ok": True, "code": "job_enqueued", "job_id": job_id}


# ---------------------------------------------------------------------------
# Reads (tenancy-enforced)
# ---------------------------------------------------------------------------


async def get_job(workspace_id: str, job_id: str) -> WorkspaceJobDoc | None:
    """Re-fetch a job by id AND re-check its workspace.

    Returns ``None`` when the id is unknown OR belongs to another workspace —
    the status route maps ``None`` to a 404 so a cross-tenant id leak is
    impossible (criterion #8: the poll enforces tenancy).
    """
    doc = await get_job_by_id(job_id)
    if doc is None or doc.workspace != workspace_id:
        return None
    return doc


async def get_job_by_id(job_id: str) -> WorkspaceJobDoc | None:
    """Fetch a job by id with no workspace filter.

    Used by the WORKER, which is handed only the id at enqueue time and trusts
    the doc's own ``workspace`` (written at dispatch by the authenticated
    route) as the tenancy authority. A bogus / non-ObjectId id returns ``None``
    so the worker no-ops instead of raising.
    """
    try:
        return await WorkspaceJobDoc.get(job_id)
    except Exception:
        # An id that isn't a valid ObjectId raises; treat as not-found.
        return None


# ---------------------------------------------------------------------------
# Lifecycle transitions (worker-driven)
# ---------------------------------------------------------------------------


async def mark_running(doc: WorkspaceJobDoc) -> None:
    doc.status = "running"
    doc.started_at = datetime.now(UTC)
    await doc.save()


async def mark_done(doc: WorkspaceJobDoc, *, result: dict[str, Any]) -> None:
    """Mark a job done + emit an INFO ``WorkspaceJobUpdated`` and audit."""
    doc.status = "done"
    doc.result = result
    doc.error = None
    doc.ended_at = datetime.now(UTC)
    await doc.save()
    await _emit_updated(doc)
    _audit(
        severity=AuditSeverity.INFO,
        status="success",
        job_id=str(doc.id),
        workspace_id=doc.workspace,
        pocket_id=doc.pocket_id,
        action=doc.action,
        job_name=doc.job_name,
        outcome="done",
    )


async def mark_failed(doc: WorkspaceJobDoc, *, error: str) -> None:
    """Mark a job failed + emit a WARNING ``WorkspaceJobUpdated`` and audit."""
    doc.status = "failed"
    doc.error = error
    doc.ended_at = datetime.now(UTC)
    await doc.save()
    await _emit_updated(doc)
    _audit(
        severity=AuditSeverity.WARNING,
        status="error",
        job_id=str(doc.id),
        workspace_id=doc.workspace,
        pocket_id=doc.pocket_id,
        action=doc.action,
        job_name=doc.job_name,
        outcome="failed",
        error=error,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _emit_updated(doc: WorkspaceJobDoc) -> None:
    await emit(
        WorkspaceJobUpdated(
            data={
                "job_id": str(doc.id),
                "workspace_id": doc.workspace,
                "pocket_id": doc.pocket_id,
                "action": doc.action,
                "job_name": doc.job_name,
                "status": doc.status,
            }
        )
    )


def _audit(*, severity: AuditSeverity, status: str, **context: Any) -> None:
    """Emit an AuditEvent for a job lifecycle step. Best-effort — a failing
    audit logger must never break a job's status transition."""
    try:
        get_audit_logger().log(
            AuditEvent.create(
                severity=severity,
                actor=WORKSPACE_JOB_IDENTITY,
                action="workspace_job",
                target=f"job:{context.get('job_id', '')}",
                status=status,
                **context,
            )
        )
    except Exception as exc:  # noqa: BLE001 — audit must never block the write
        logger.warning("jobs: audit emit failed (%s)", exc)


__all__ = [
    "dispatch_job",
    "get_job",
    "get_job_by_id",
    "mark_done",
    "mark_failed",
    "mark_running",
]
