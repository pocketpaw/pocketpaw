# ee/pocketpaw_ee/cloud/jobs/dto.py
# Created: 2026-06-20 (feat/workspace-jobs, pp#1459) — wire model for the job
# status-poll endpoint. The job RESULT is not returned here — it already
# landed in the pocket's rippleSpec `state` via the writeback, so the poll
# only reports lifecycle (status + timestamps + error). Pure Pydantic — no
# Beanie import (import-linter "Jobs" contract).

"""Wire models for the workspace-jobs status surface."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class JobStatusResponse(BaseModel):
    """Body for ``GET /workspaces/{ws}/jobs/{job_id}``.

    Reports the job's lifecycle only. The result already merged into the
    pocket's `state`, so a client polls this purely to know when to stop
    showing a spinner (``done`` / ``failed``) and to surface ``error`` on a
    failure.
    """

    job_id: str
    status: str
    created_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    error: str | None = None


__all__ = ["JobStatusResponse"]
