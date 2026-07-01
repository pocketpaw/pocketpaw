# ee/pocketpaw_ee/cloud/models/workspace_job.py
# Created: 2026-06-20 (feat/workspace-jobs, pp#1459) — Beanie document backing
# one workspace-job run. A job is dispatched from `POST /pockets/{id}/actions/run`
# when the action's `kind == "job"`, runs in the ARQ worker under the synthetic
# `system:workspace_job` identity, and writes its result back to the pocket's
# rippleSpec `state`. This doc is the durable status record the status-poll
# endpoint reads and the worker re-fetches (with a tenancy re-check) before it
# touches anything.
#
# Tenancy: `workspace` is required + indexed. Every read filters by it; the
# worker re-fetches by id AND re-checks `workspace` before writeback, and the
# poll route re-checks the path workspace against the doc → 404 on mismatch.
# Sibling shape: mirrors `ChatRunDoc` (status enum + arq_job_id + error +
# timestamps) since both are ARQ-backed run records.

"""Beanie document for one workspace-job run."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from beanie import Document, Indexed
from pydantic import Field
from pymongo import IndexModel

from pocketpaw_ee.cloud.jobs.domain import JobStatus


def _utcnow() -> datetime:
    return datetime.now(UTC)


class WorkspaceJobDoc(Document):
    """One dispatched workspace job and its lifecycle status.

    Fields:
        workspace: tenant id. Indexed; every read filters by it.
        pocket_id: the pocket the triggering action ran on (writeback target).
        action: the `rippleSpec.actions` key that triggered the job — used to
            key the failed-state writeback (``<action>_status`` / ``_error``).
        job_name: the registered job name (registry lookup key).
        params: the merged params dict the job ran with (credential-shaped keys
            already rejected at dispatch).
        triggered_by: the VIEWER who clicked the action. Audit only — the job
            itself runs under the synthetic ``system:workspace_job`` identity,
            never this user's session.
        status: queued → running → done | failed.
        arq_job_id: the ARQ job id the enqueue returned (None until enqueued /
            on enqueue failure). Forensic — lets an operator correlate with the
            worker log.
        result: the validated state-only partial spec the job produced on
            success (``{}`` otherwise).
        error: the failure message on a failed job (None on success).
    """

    workspace: Indexed(str)  # type: ignore[valid-type]
    pocket_id: str
    action: str
    job_name: str
    params: dict[str, Any] = Field(default_factory=dict)
    triggered_by: str
    status: JobStatus = "queued"
    arq_job_id: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    createdAt: datetime = Field(default_factory=_utcnow)
    started_at: datetime | None = None
    ended_at: datetime | None = None

    class Settings:
        name = "workspace_jobs"
        indexes = [
            # Point lookup by id is Mongo's `_id`; the status poll + worker
            # re-fetch ALSO filter by workspace for tenancy, so index the pair
            # the way the sibling run/sweep docs index their tenancy reads.
            IndexModel([("workspace", 1), ("createdAt", -1)], name="ws_created"),
            # Per-pocket history scan (operator debugging / future per-pocket
            # job feed).
            IndexModel(
                [("workspace", 1), ("pocket_id", 1), ("createdAt", -1)],
                name="ws_pocket_created",
            ),
        ]


__all__ = ["WorkspaceJobDoc"]
