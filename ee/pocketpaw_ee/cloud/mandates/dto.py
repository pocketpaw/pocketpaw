# ee/pocketpaw_ee/cloud/mandates/dto.py
# Created: 2026-06-11 (feat/belt-mandates, slice 1 — models + CRUD).
#
# Request/Response schemas for the MANDATE primitive. Separate Request and
# Response models per the cloud entity rule (never reuse one model for both
# directions). The Request models are the ``body`` the service ``model_validate``s
# at entry; the Response models are the wire dicts the service returns.
#
# Updated: 2026-06-11 (slice 2 — patrols) — added FeedbackRequest /
# SightingResponse / SightingsListResponse for the feedback-intake patrol and
# the sightings read.
# Updated: 2026-06-11 (slice 4 — plan gate) — added ShiftResponse for the
# manual-shift trigger.
# Updated: 2026-06-11 (slice 5 — pawprints) — added PawprintResponse /
# PawprintsListResponse for the past-tense event feed.

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from pocketpaw_ee.cloud.mandates.domain import (
    Cadence,
    KpiDirection,
    MandateStatus,
    ShiftState,
)

# ---------------------------------------------------------------------------
# Charter request sub-schemas
# ---------------------------------------------------------------------------


class KpiRequest(BaseModel):
    name: str = Field(min_length=1)
    target: float
    direction: KpiDirection


class BudgetRequest(BaseModel):
    max_tasks_per_shift: int = Field(default=3, ge=1, le=20)
    gate_minutes_per_week: int = Field(default=15, ge=0)


class SurfaceRequest(BaseModel):
    repo_id: str = Field(min_length=1)


class CharterRequest(BaseModel):
    goal: str = Field(min_length=1)
    kpis: list[KpiRequest] = Field(default_factory=list)
    says_no: list[str] = Field(default_factory=list)
    boundaries: list[str] = Field(default_factory=list)
    budget: BudgetRequest = Field(default_factory=BudgetRequest)
    cadence: Cadence = "weekly"


# ---------------------------------------------------------------------------
# Mandate create + read
# ---------------------------------------------------------------------------


class CreateMandateRequest(BaseModel):
    """Body for ``POST /belt/mandates``. The charter is the standing brief.
    ``patrols`` (UI contract) is the charter composer's senses toggles — which
    patrols sense this mandate's surface."""

    name: str = Field(min_length=1)
    surface: SurfaceRequest
    charter: CharterRequest
    soul_path: str | None = None
    patrols: list[str] = Field(default_factory=lambda: ["deps", "feedback"])


class MandateHealth(BaseModel):
    """The health summary on the list view — last shift state, open gate count,
    sighting count."""

    last_shift_state: ShiftState | None = None
    open_gate_count: int = 0
    sighting_count: int = 0


class MandateSummaryResponse(BaseModel):
    """One mandate on the list view (charter omitted; health summarized)."""

    id: str
    name: str
    status: MandateStatus
    repo_id: str
    cadence: Cadence
    health: MandateHealth
    created_at: datetime


class MandateListResponse(BaseModel):
    mandates: list[MandateSummaryResponse] = Field(default_factory=list)


class ShiftSummaryResponse(BaseModel):
    """A recent shift on the mandate detail view."""

    id: str
    no: int
    state: ShiftState
    plan_action_id: str | None = None
    created_at: datetime


class MandateDetailResponse(BaseModel):
    """Full mandate detail — charter, recent shifts, sightings-count-by-patrol."""

    id: str
    name: str
    status: MandateStatus
    surface: SurfaceRequest
    charter: CharterRequest
    soul_path: str | None = None
    patrols: list[str] = Field(default_factory=lambda: ["deps", "feedback"])
    recent_shifts: list[ShiftSummaryResponse] = Field(default_factory=list)
    sightings_by_patrol: dict[str, int] = Field(default_factory=dict)
    created_at: datetime


# ---------------------------------------------------------------------------
# Patrols (slice 2) — feedback intake + sightings read
# ---------------------------------------------------------------------------


class FeedbackRequest(BaseModel):
    """Body for ``POST /belt/mandates/{id}/feedback`` — a human-filed signal
    (the GENERAL shape; autopilot and integrations use this).

    ``text`` is the feedback. ``severity`` defaults to 3 (mid) when omitted.
    ``source`` names where it came from (e.g. ``"slack"``, ``"support"``)."""

    text: str = Field(min_length=1)
    severity: int | None = Field(default=None, ge=1, le=5)
    source: str = Field(min_length=1)


class TeachingFeedbackRequest(BaseModel):
    """The TEACHING shape of ``POST /belt/mandates/{id}/feedback`` — the human
    teaching channel the gate UI files from rejections/edits. Discriminated
    from :class:`FeedbackRequest` by the presence of ``kind``.

    ``kind`` names the gate action (``reject``/``edit``/``plan``); ``reason``
    is the human's explanation; ``shift_no``/``task_title`` tie it to the plan
    item it teaches about. Returns ``{"ok": true}`` on the wire."""

    kind: str = Field(pattern="^(reject|edit|plan)$")
    reason: str = Field(min_length=1)
    shift_no: int | None = None
    task_title: str | None = None


class SightingResponse(BaseModel):
    id: str
    mandate_id: str
    patrol: str
    severity: int
    summary: str
    evidence: dict = Field(default_factory=dict)
    ts: datetime


class SightingsListResponse(BaseModel):
    sightings: list[SightingResponse] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Shift trigger (slice 4)
# ---------------------------------------------------------------------------


class ShiftResponse(BaseModel):
    """Response from ``POST /belt/mandates/{id}/shift`` — the shift the foreman
    planned + how the plan gate resolved.

    ``state`` is the shift state after planning: ``in_gate`` when the foreman
    proposed tasks (awaiting human approval), ``stood_down`` when the foreman
    returned an empty plan (a SUCCESS — quiet surface, healthy KPIs).
    ``plan_action_id`` is the Instinct ``belt_plan`` Action id (None for a
    stood-down shift). ``task_count`` is how many tasks the plan proposed."""

    shift_id: str
    no: int
    state: ShiftState
    plan_action_id: str | None = None
    task_count: int = 0
    no_action_reason: str | None = None


# ---------------------------------------------------------------------------
# Plan resolve (UI contract) — the console's authoritative gate action
# ---------------------------------------------------------------------------


class PlanDecision(BaseModel):
    """One per-task verdict in a plan resolution.

    ``index`` is the 0-BASED position in the proposed plan's ``tasks`` array
    (the order the UI rendered). ``edit`` applies ``edited_title`` and keeps
    the task; ``reject`` drops it and records ``reason`` as teaching feedback."""

    index: int = Field(ge=0)
    decision: str = Field(pattern="^(approve|reject|edit)$")
    edited_title: str | None = None
    reason: str | None = None


class ResolvePlanRequest(BaseModel):
    """Body for ``POST /belt/mandates/{id}/plan/resolve`` — the console's gate
    action. Every task in the shift's plan must carry exactly one decision
    (explicit beats implicit at a human gate)."""

    shift_no: int
    decisions: list[PlanDecision] = Field(min_length=1)


# ---------------------------------------------------------------------------
# Pawprints (slice 5) — past-tense event feed
# ---------------------------------------------------------------------------


class PawprintResponse(BaseModel):
    """One past-tense event in a mandate's history (UI contract shape).

    ``kind`` is the event class — the UI consumes ``executed`` / ``rejected`` /
    ``edited`` / ``stood_down``; the feed also emits ``proposed`` / ``approved``
    / ``failed`` / ``planning`` (a superset, same shape). ``summary`` is the
    human-readable past-tense line. ``id`` is a stable per-item key
    (``<shift_id>:<kind>``); ``evidence_refs`` lists the sighting ids the
    underlying plan cited."""

    id: str
    mandate_id: str
    shift_no: int | None = None
    kind: str
    summary: str
    evidence_refs: list[str] = Field(default_factory=list)
    ts: datetime | None = None


class PawprintsListResponse(BaseModel):
    pawprints: list[PawprintResponse] = Field(default_factory=list)


__all__ = [
    "BudgetRequest",
    "CharterRequest",
    "CreateMandateRequest",
    "FeedbackRequest",
    "KpiRequest",
    "MandateDetailResponse",
    "MandateHealth",
    "MandateListResponse",
    "MandateSummaryResponse",
    "PlanDecision",
    "PawprintResponse",
    "PawprintsListResponse",
    "ResolvePlanRequest",
    "ShiftResponse",
    "ShiftSummaryResponse",
    "SightingResponse",
    "SightingsListResponse",
    "SurfaceRequest",
    "TeachingFeedbackRequest",
]
