# ee/pocketpaw_ee/cloud/jobs/worker.py
# Created: 2026-06-20 (feat/workspace-jobs, pp#1459) — the ARQ entrypoint that
# executes one workspace job. Registered into the SHARED chat-runs
# `WorkerSettings.functions` (default #1: same worker process, not a new
# deploy artifact). Runs in the worker, where `xproc.set_role("worker")` is
# already pinned, so the `merge_spec` writeback's `emit(PocketUpdated(...))`
# routes over the cross-process bridge to the web's bus and open canvases
# update live — the same infra resumable chat runs use.
#
# Identity is HARDCODED to `system:workspace_job`; the merge_spec writeback
# always runs under it, never the triggering user. The job result is validated
# (state-only) before writeback; any rejection / exception / timeout marks the
# job failed AND writes a failed-state partial so the button never hangs.
# `merge_spec` is imported at module scope so tests can monkeypatch it.

"""ARQ entrypoint: execute one workspace job + write the result back."""

from __future__ import annotations

import logging
from typing import Any

# Module-scope import so tests can ``monkeypatch.setattr(worker, "merge_spec", …)``.
from pocketpaw_ee.cloud.jobs import service as jobs_service
from pocketpaw_ee.cloud.jobs.domain import (
    WORKSPACE_JOB_IDENTITY,
    failed_state_writeback,
)
from pocketpaw_ee.cloud.jobs.registry import resolve_job, validate_job_result
from pocketpaw_ee.cloud.pockets.service import merge_spec

logger = logging.getLogger(__name__)


async def execute_workspace_job(ctx: dict[str, Any], job_id: str) -> None:
    """ARQ job: run the registered callable and write its result back.

    ``ctx`` is the ARQ worker context (unused — the doc carries everything).
    The job_id is the ``WorkspaceJobDoc`` id. The worker:
      1. re-fetches the doc — a missing doc is a no-op (it was deleted, or the
         id is bogus); we do NOT write anything.
      2. marks it running.
      3. resolves + runs the registered job under the synthetic identity.
      4. validates the result (state-only) and writes it back via merge_spec.
      5. on ANY failure (unknown job / raise / validation reject / timeout)
         marks the job failed AND writes a failed-state partial so the
         triggering button stops spinning.

    The worker does not know the caller's workspace independently, so it trusts
    the doc's ``workspace`` (written at dispatch by the authenticated route) and
    re-fetches under it — the tenancy invariant is "the doc owns its workspace".
    """
    doc = await jobs_service.get_job_by_id(job_id)
    if doc is None:
        logger.warning("jobs: execute_workspace_job got unknown job id %s — no-op", job_id)
        return

    workspace_id = doc.workspace
    pocket_id = doc.pocket_id
    action = doc.action

    await jobs_service.mark_running(doc)

    try:
        job = resolve_job(doc.job_name)
        raw = await job(
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            job_id=job_id,
            params=dict(doc.params),
        )
        # State-only contract — raises JobResultError on a template-owned write.
        result = validate_job_result(raw)
    except Exception as exc:  # noqa: BLE001 — every failure path converges here
        logger.exception("jobs: job %s (%s) failed", job_id, doc.job_name)
        message = str(exc) or exc.__class__.__name__
        # Writeback FIRST so the button un-hangs even if the status save races.
        await _writeback(
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            partial=failed_state_writeback(action, message),
        )
        await jobs_service.mark_failed(doc, error=message)
        return

    # Success — write the validated state-only partial back.
    await _writeback(workspace_id=workspace_id, pocket_id=pocket_id, partial=result)
    await jobs_service.mark_done(doc, result=result)


async def _writeback(*, workspace_id: str, pocket_id: str, partial: dict) -> None:
    """Merge a partial spec into the pocket under the synthetic job identity.

    Always ``user_id=system:workspace_job`` — never the triggering user. The
    merge fires ``PocketUpdated``; from the worker that routes over xproc to
    the web bus, so open canvases update live.
    """
    await merge_spec(
        workspace_id,
        WORKSPACE_JOB_IDENTITY,
        pocket_id,
        {"merge": partial},
    )


__all__ = ["execute_workspace_job"]
