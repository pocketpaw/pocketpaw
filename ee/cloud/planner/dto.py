# Created: 2026-05-17 — pocketpaw#1118 P1. Request / response DTOs for
#   the planner entity. Distinct *Request and *Response models per
#   ee/cloud Code Rule §4 — request shapes carry the goal + project_id
#   the operator picked, response shapes carry the materialized cloud
#   primitive ids (file_id, task_ids) the FE Plan tab renders.
"""Planner entity — request/response DTOs."""

from __future__ import annotations

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class PlanProjectRequest(BaseModel):
    """Body for ``POST /api/v1/planner/run``.

    ``goal`` is the operator's natural-language project description that
    the OSS planner decomposes into a PRD + tasks + recommended team.
    ``deep_research`` upgrades the underlying planner's research phase
    from the default "standard" depth to "deep" — adds an extra LLM
    round-trip per call but produces more thorough source material in
    the PRD. We expose a boolean rather than a free-form depth string
    so the FE toggle stays a binary control; if we ever need the
    "quick" or "none" depths we add a separate field.
    """

    project_id: str = Field(min_length=1)
    goal: str = Field(min_length=10, max_length=5000)
    deep_research: bool = False


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class AgentGapDTO(BaseModel):
    """Wire shape for a single missing-agent diagnostic."""

    spec_name: str
    recommended_role: str
    specialties: list[str] = Field(default_factory=list)


class PlanProjectResult(BaseModel):
    """Wire shape returned by ``POST /api/v1/planner/run`` and
    ``GET /api/v1/planner/by-project/{project_id}``.

    The FE Plan tab consumes:
      - ``prd_file_id`` to render the "Open PRD" link via the existing
        Files panel download route.
      - ``task_ids`` to scroll the WorkFeed to the newly-created tasks.
      - ``agent_gaps`` to drive the "Create missing agents" CTA.

    ``plan_session_id`` is exposed for round-trip debugging; the FE does
    not render it in the v0 surface.
    """

    plan_session_id: str
    project_id: str
    status: str
    prd_file_id: str | None = None
    plan_file_id: str | None = None
    goal_file_id: str | None = None
    task_ids: list[str] = Field(default_factory=list)
    agent_gaps: list[AgentGapDTO] = Field(default_factory=list)


__all__ = [
    "AgentGapDTO",
    "PlanProjectRequest",
    "PlanProjectResult",
]
