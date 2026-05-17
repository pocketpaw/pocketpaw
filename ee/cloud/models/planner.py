# planner.py — Beanie document for the Planner entity.
# Created: 2026-05-17 — pocketpaw#1118 P3. Persists the cloud-side
#   record of one OSS planner run (PRD + plan.json + goal.md + tasks +
#   the agent-gap diagnostic). v0 stored nothing — the plan was
#   reconstructed from files on every read. P3 needs a queryable record
#   so ``planner.service.agent_resolve_gap`` can look up which tasks a
#   plan session created and which gaps it surfaced without parsing
#   plan.json.
"""PlanSession document — one OSS planner run materialized into cloud.

Embedded sub-document :class:`PlanSessionAgentGap` mirrors the wire
``AgentGapDTO`` shape so the read path is a 1:1 mapping. The document is
workspace-scoped (Indexed on ``workspace``) and pinned to the cloud
``project_id`` so a re-plan replaces the prior session for that project
rather than accumulating duplicates.

Only ``ee.cloud.planner.service`` may import this module — enforced by
the ``import-linter`` contract in ``pyproject.toml``.
"""

from __future__ import annotations

from beanie import Indexed
from pydantic import BaseModel, Field

from ee.cloud.models.base import TimestampedDocument


class PlanSessionAgentGap(BaseModel):
    """Embedded shape for one planner-recommended agent missing from the
    workspace. Carries the spec_name + role + specialties the planner
    suggested so the operator UI can render the gap card without a
    follow-up fetch."""

    spec_name: str
    recommended_role: str = ""
    specialties: list[str] = Field(default_factory=list)


class PlanSession(TimestampedDocument):
    """One materialized OSS planner run.

    ``project_id`` pins the session to a cloud Project; we look the doc
    up by ``(workspace, project_id)`` for the resolve-gap flow rather
    than relying on the OSS planner's own session id (which today is
    just the project_id verbatim).
    """

    workspace: Indexed(str)  # type: ignore[valid-type]
    project_id: str
    status: str = Field(default="ready", pattern="^(ready|stale)$")
    prd_file_id: str | None = None
    plan_file_id: str | None = None
    goal_file_id: str | None = None
    task_ids: list[str] = Field(default_factory=list)
    agent_gaps: list[PlanSessionAgentGap] = Field(default_factory=list)
    # Soft warnings emitted by the planner when a TaskSpec dependency
    # couldn't be resolved (unknown name, cyclic ref). Carried across
    # GET refreshes so the operator sees the signal until they
    # re-plan — vanished on cold hydration before this field landed.
    dependency_warnings: list[str] = Field(default_factory=list)

    class Settings:
        name = "plan_sessions"
        indexes = [
            [("workspace", 1), ("project_id", 1)],
        ]


__all__ = ["PlanSession", "PlanSessionAgentGap"]
