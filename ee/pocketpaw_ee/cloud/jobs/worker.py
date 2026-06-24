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
#
# Updated: 2026-06-20 (review fix IMPORTANT 3) — fail-closed TENANCY RE-CHECK.
# Before any execution or writeback, the worker fetches the pocket the doc
# names and asserts its workspace equals the doc's workspace. `merge_spec`
# fetches by pocket id only and trusts the passed workspace, so without this
# the writeback would write wherever the doc points. On a mismatch the job is
# marked failed with NO writeback (we cannot safely write to a pocket outside
# the doc's workspace). The check sits ONCE near the top so it can't perturb
# the failure path's "un-hang the button" guarantee.
#
# Updated: 2026-06-20 (fix/jobs-arq-enqueue-contract, BUG 2) — the SUCCESS-path
# writeback now HONORS ``merge_spec``'s rejection. ``merge_spec`` returns
# ``{ok: False, warnings: [...]}`` when the catalog / action-wiring gate blocks
# the merge — it persists NOTHING in that case. The worker previously discarded
# that return and marked the job ``done`` regardless, so a rejected writeback
# looked successful (button done, canvas unchanged). Now ``_writeback`` returns
# the result dict and the success path escalates ``ok: False`` into the shared
# failure handling (mark ``failed`` + failed-state writeback). The escalation is
# success-path-only; the failure path's writeback stays best-effort and is never
# re-escalated, so it can't loop.
#
# Updated: 2026-06-22 (fix/jobs-real-builtin-and-status, BUG B) — the WORKER now
# owns the generic ``<action>_status`` flag on BOTH the success and failure
# paths. Previously the success status came from the built-in, which HARDCODED
# ``score_applications_status``, so a status-bound widget + the frontend's
# optimistic ``<action>_status:"running"`` only lined up when the action
# happened to be named ``score_applications``. The failure path already stamped
# ``<action>_status`` (via ``failed_state_writeback(action, ...)``); the success
# path now stamps ``result["state"]["<action>_status"] = "done"`` from
# ``doc.action`` BEFORE the writeback. Built-ins no longer return any status
# key. The stamp happens before the ok:false → failed escalation, so a rejected
# writeback still escalates correctly (the failed-state marker overwrites the
# optimistic "done" the same way the rejection path always has).

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
from pocketpaw_ee.cloud.pockets.service import get_pocket_workspace, merge_spec

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

    # Fail-closed tenancy re-check (review fix IMPORTANT 3). `merge_spec`
    # fetches the pocket by id only and trusts the workspace we hand it, so the
    # writeback layer has no tenancy assertion of its own. Re-fetch the pocket
    # and confirm it lives in the doc's workspace BEFORE we run or write. On a
    # mismatch we can NOT safely write a failed-state partial either — that
    # would be the same cross-workspace write we're refusing — so we mark the
    # job failed with NO writeback and return. Done ONCE here (not in
    # `_writeback`, which both success and failure paths call) so the failure
    # path's "un-hang the button" guarantee stays clean.
    pocket_workspace = await get_pocket_workspace(pocket_id)
    if pocket_workspace != workspace_id:
        logger.error(
            "jobs: TENANCY MISMATCH — job %s names pocket %s in workspace %r but the "
            "pocket lives in workspace %r; refusing writeback and marking failed",
            job_id,
            pocket_id,
            workspace_id,
            pocket_workspace,
        )
        await jobs_service.mark_failed(
            doc, error="tenancy mismatch: pocket is not in the job's workspace"
        )
        return

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
        # NOTE for connector-job authors: ``str(exc)`` lands in broadcast state
        # via ``failed_state_writeback`` and is visible to every viewer of the
        # pocket. Do NOT let raw exception text carry PII or secrets — map your
        # job's failure modes to FIXED, safe strings (e.g. "upstream timeout")
        # rather than surfacing the raw message. (Tracked as a follow-up; the
        # default below is intentionally left as-is for the built-in jobs.)
        message = str(exc) or exc.__class__.__name__
        # Writeback FIRST so the button un-hangs even if the status save races.
        await _writeback(
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            partial=failed_state_writeback(action, message),
        )
        await jobs_service.mark_failed(doc, error=message)
        return

    # Stamp the generic status flag the triggering button reads. The WORKER
    # owns ``<action>_status`` on BOTH paths (the failure path already stamps it
    # via ``failed_state_writeback(action, ...)``): the built-in returns only its
    # domain state (``scored_rows`` etc.), never a status key, so a status-bound
    # widget + the frontend's optimistic ``<action>_status:"running"`` line up
    # for ANY action name — not just one that happens to match the built-in.
    # Done here, before the writeback + the ok:false escalation, so a rejected
    # writeback still overwrites this "done" with the failed-state marker.
    result.setdefault("state", {})
    if isinstance(result["state"], dict):
        result["state"][f"{action}_status"] = "done"

    # Success — write the validated state-only partial back. ``merge_spec``
    # can still REJECT the merge (``ok: False``) when the catalog / action-
    # wiring gate blocks it: in that case it persists NOTHING. Marking the job
    # ``done`` on a write that never landed is a silent partial (the button
    # shows done, the canvas never changed), so escalate a rejected writeback
    # into the SAME failure handling — mark the job failed + write the failed-
    # state marker so the button un-hangs. (The ok-check is success-path-only;
    # the failure path's ``_writeback`` below stays best-effort and is NOT
    # escalated, so it can't loop.)
    try:
        wb = await _writeback(workspace_id=workspace_id, pocket_id=pocket_id, partial=result)
        if wb is not None and wb.get("ok") is False:
            warnings = wb.get("warnings") or []
            raise _WritebackRejectedError(
                "writeback rejected by merge_spec: " + "; ".join(str(w) for w in warnings)
            )
    except Exception as exc:  # noqa: BLE001 — converge on the same failure path
        logger.exception("jobs: job %s (%s) writeback failed/rejected", job_id, doc.job_name)
        message = str(exc) or exc.__class__.__name__
        # Best-effort failed-state writeback so the button stops spinning. This
        # call is NOT ok-checked — a doubly-rejected merge must not loop.
        await _writeback(
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            partial=failed_state_writeback(action, message),
        )
        await jobs_service.mark_failed(doc, error=message)
        return

    await jobs_service.mark_done(doc, result=result)


class _WritebackRejectedError(RuntimeError):
    """Raised when ``merge_spec`` returns ``ok: False`` on the success-path
    writeback (catalog / action-wiring gate blocked the merge), so the success
    path converges on the shared failure handling."""


async def _writeback(*, workspace_id: str, pocket_id: str, partial: dict) -> dict | None:
    """Merge a partial spec into the pocket under the synthetic job identity.

    Always ``user_id=system:workspace_job`` — never the triggering user. The
    merge fires ``PocketUpdated``; from the worker that routes over xproc to
    the web bus, so open canvases update live.

    Returns ``merge_spec``'s result dict (carrying ``ok`` + ``warnings``) so the
    SUCCESS path can detect a gate rejection (``ok: False``) and escalate to the
    failure handling. The failure path ignores the return (best-effort).
    """
    return await merge_spec(
        workspace_id,
        WORKSPACE_JOB_IDENTITY,
        pocket_id,
        {"merge": partial},
    )


__all__ = ["execute_workspace_job"]
