# Created: 2026-05-17 — pocketpaw#1118 P1. Request / response DTOs for
#   the planner entity. Distinct *Request and *Response models per
#   ee/cloud Code Rule §4 — request shapes carry the goal + project_id
#   the operator picked, response shapes carry the materialized cloud
#   primitive ids (file_id, task_ids) the FE Plan tab renders.
# Updated: 2026-05-17 (feat/planner-gaps-and-deps) — pocketpaw#1118 P3
#   added ``ResolveGapRequest`` + ``ResolveGapResult`` for
#   ``POST /api/v1/planner/resolve-gap`` — the route the FE calls after
#   the operator creates the agent for a previously-missing spec.
#   pocketpaw#1118 P4 added ``dependency_warnings`` to
#   ``PlanProjectResult`` so the planner can surface unresolved
#   ``TaskSpec.blocked_by_keys`` without failing the whole run.

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


class ResolveGapRequest(BaseModel):
    """Body for ``POST /api/v1/planner/resolve-gap``.

    Called after the operator creates a cloud Agent for a planner-
    recommended spec the workspace was missing. The service finds every
    task in this plan session that fell back to a human assignee for
    this spec and reassigns it to the new agent.

    ``new_agent_id`` is the cloud agent id returned by the existing
    ``POST /api/v1/agents`` endpoint — we don't add a new agent-creation
    surface here; the FE calls agents-create directly and then posts to
    this route with the id it got back.
    """

    plan_session_id: str = Field(min_length=1)
    spec_name: str = Field(min_length=1)
    new_agent_id: str = Field(min_length=1)


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
      - ``dependency_warnings`` to surface TaskSpec.blocked_by_keys
        entries that didn't resolve to a sibling spec (planner output
        bug — task created with empty blocked_by, not aborted).

    ``plan_session_id`` is exposed for round-trip debugging; the FE
    threads it back to the resolve-gap endpoint after the operator
    creates a missing agent.
    """

    plan_session_id: str
    project_id: str
    status: str
    prd_file_id: str | None = None
    plan_file_id: str | None = None
    goal_file_id: str | None = None
    task_ids: list[str] = Field(default_factory=list)
    agent_gaps: list[AgentGapDTO] = Field(default_factory=list)
    dependency_warnings: list[str] = Field(default_factory=list)


class ResolveGapResult(BaseModel):
    """Wire shape returned by ``POST /api/v1/planner/resolve-gap``.

    ``reassigned_task_ids`` lets the FE patch the WorkFeed rows without
    a refetch. ``remaining_gaps`` is the up-to-date list after removing
    the resolved spec so the FE re-renders the gap card stack cleanly.
    """

    plan_session_id: str
    spec_name: str
    new_agent_id: str
    reassigned_task_ids: list[str] = Field(default_factory=list)
    remaining_gaps: list[AgentGapDTO] = Field(default_factory=list)


__all__ = [
    "AgentGapDTO",
    "PlanProjectRequest",
    "PlanProjectResult",
    "ResolveGapRequest",
    "ResolveGapResult",
]
