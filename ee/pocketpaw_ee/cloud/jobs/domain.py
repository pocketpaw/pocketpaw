# ee/pocketpaw_ee/cloud/jobs/domain.py
# Created: 2026-06-20 (feat/workspace-jobs, pp#1459) — pure domain constants
# and value types for the workspace jobs primitive. A "pocket job" is a named,
# server-side async callable run under the WORKSPACE service identity (not the
# triggering user's session) inside the ARQ worker, so a final
# `emit(PocketUpdated(...))` reaches the bus even after the user's socket is
# gone. This module holds NO Beanie / FastAPI / I/O imports so every other
# module in the package (and the import-linter contract) can depend on it
# freely.

"""Pure domain types + constants for the workspace jobs primitive."""

from __future__ import annotations

import os
from typing import Literal

# ---------------------------------------------------------------------------
# Identity — every job runs under this synthetic, hardcoded actor. It is NOT
# user-assignable and never derived from a session: a job's blast radius must
# be auditable to "the workspace ran this", never "user X ran this as the
# workspace". Connector calls inside a job use the workspace's STORED creds
# (the source-read path), never tokens supplied through `params`.
# ---------------------------------------------------------------------------
WORKSPACE_JOB_IDENTITY = "system:workspace_job"

# ARQ queue the jobs run on — distinct from the resumable-chat-run queue so a
# burst of jobs can't starve interactive runs (they share the same worker
# PROCESS, just different logical queues).
JOBS_QUEUE = "paw:jobs"

# Job lifecycle. `queued` on enqueue, `running` once the worker picks it up,
# terminal as `done` / `failed`.
JobStatus = Literal["queued", "running", "done", "failed"]

# ARQ per-job timeout (DEFAULT #4 = 900s). Read from the environment so the
# knob stays EE-side (no `src/pocketpaw/config.py` edit). A timed-out job is
# treated exactly like a raised exception: marked `failed` + a failed-state
# writeback so the triggering button never hangs.
_DEFAULT_JOB_TIMEOUT_SECONDS = 900


def job_timeout_seconds() -> int:
    """Resolve the ARQ ``job_timeout`` for workspace jobs.

    ``POCKETPAW_JOB_TIMEOUT_SECONDS`` overrides the 900s default. A malformed
    value falls back to the default rather than crashing worker boot.
    """
    raw = os.environ.get("POCKETPAW_JOB_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_JOB_TIMEOUT_SECONDS
    try:
        val = int(raw)
    except ValueError:
        return _DEFAULT_JOB_TIMEOUT_SECONDS
    return val if val > 0 else _DEFAULT_JOB_TIMEOUT_SECONDS


def failed_state_writeback(action: str, message: str) -> dict:
    """Build the partial-spec writeback for a failed job.

    A failed job MUST still write back so the triggering button stops
    spinning. The shape mirrors the success writeback (state-only) and is
    keyed by the action name: ``<action>_status`` / ``<action>_error``.
    """
    return {"state": {f"{action}_status": "failed", f"{action}_error": message}}


__all__ = [
    "JOBS_QUEUE",
    "JobStatus",
    "WORKSPACE_JOB_IDENTITY",
    "failed_state_writeback",
    "job_timeout_seconds",
]
