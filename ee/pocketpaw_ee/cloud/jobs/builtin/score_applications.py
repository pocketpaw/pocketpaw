# ee/pocketpaw_ee/cloud/jobs/builtin/score_applications.py
# Created: 2026-06-20 (feat/workspace-jobs, pp#1459) — the first built-in
# workspace job. V1 is deliberately a thin, side-effect-free reference: it
# scores a batch of applications and projects each row through a PII allowlist
# before the result becomes broadcast `state`. The allowlist is the security
# point of the built-in — because a job result is merged into the pocket spec
# and fanned out over the realtime bus to every open canvas, a job MUST strip
# email / phone (and any non-allowlisted field) so PII never lands in the
# broadcast/audit path. Enforced by the built-in's unit test.
#
# A real connector-backed implementation (reading the batch from the
# workspace's stored creds via the source-read path, never params tokens) is a
# follow-up; the contract + the PII transform are what V1 pins.

"""Built-in `score_applications` job + its PII allowlist transform."""

from __future__ import annotations

from typing import Any

# The ONLY fields a scored row may broadcast. Everything else — notably any
# email / phone / free-text contact field — is dropped before the row reaches
# `state`. Allowlist (not denylist) so a new raw field is dropped by default.
_ALLOWED_ROW_FIELDS = ("id", "name", "score", "stage")


def project_row(row: dict[str, Any]) -> dict[str, Any]:
    """Project one scored row to the broadcast-safe allowlist.

    Drops every field not in :data:`_ALLOWED_ROW_FIELDS`, so PII like
    ``email`` / ``phone`` can never reach the broadcast `state`.
    """
    return {k: row[k] for k in _ALLOWED_ROW_FIELDS if k in row}


def _score_row(row: dict[str, Any]) -> dict[str, Any]:
    """Assign a toy score. Real scoring is a connector-backed follow-up; V1
    pins the contract + the PII allowlist, not a scoring model."""
    name = str(row.get("name", ""))
    scored = {**row, "score": len(name), "stage": "scored"}
    return project_row(scored)


class ScoreApplicationsJob:
    """Score a batch of applications and write the scored rows to `state`.

    Runs under the workspace service identity. Returns a STATE-ONLY partial
    spec — `scored_rows` (PII-stripped) and a status flag the triggering
    button reads to stop spinning.
    """

    name = "score_applications"

    async def __call__(
        self, *, workspace_id: str, pocket_id: str, job_id: str, params: dict
    ) -> dict:
        # V1: score the rows passed in `params["rows"]` (a real impl reads the
        # batch from the workspace's stored connector creds). Bounded by
        # `batch_size` so a job can't run unbounded.
        batch_size = int(params.get("batch_size", 20))
        rows = list(params.get("rows", []))[: max(0, batch_size)]
        scored = [_score_row(r) for r in rows if isinstance(r, dict)]
        return {
            "state": {
                "scored_rows": scored,
                "score_applications_status": "done",
                "score_applications_scored_count": len(scored),
            }
        }


__all__ = ["ScoreApplicationsJob", "project_row"]
