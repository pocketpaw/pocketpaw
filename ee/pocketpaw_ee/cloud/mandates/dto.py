# ee/pocketpaw_ee/cloud/mandates/dto.py — MANDATE request/response schemas.
#
# Separate Request and Response models per the cloud entity rule (never reuse one
# model for both directions). Request models are the ``body`` the service
# ``model_validate``s at entry; Response models are the wire dicts the service
# returns. ``command_refusal`` is the factory's argv[0] allowlist for charter
# checks/recipes (the develop station re-checks it before exec). Covers mandate
# create/read (charter incl. checks + recipes, and the
# ``upstream`` patrol's pinned-dependency watch list), feedback
# intake + sightings, the shift trigger, plan resolution, pawprints, the
# autopilot toggle, and the digest query.

from __future__ import annotations

import os
import re
import shlex
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

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


_GITHUB_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class UpstreamPinRequest(BaseModel):
    """One ``upstream`` patrol watch: a GitHub ``owner/name`` and the TOML file
    (relative to the bound repo) that pins its ``rev``. ``repo`` lands in a
    ``gh api`` URL path, so it is held to the owner/name charset."""

    repo: str
    pin_file: str = Field(min_length=1)

    @field_validator("repo")
    @classmethod
    def _repo_shape(cls, v: str) -> str:
        if not _GITHUB_REPO.match(v) or ".." in v:
            raise ValueError("repo must be a GitHub owner/name")
        return v

    @field_validator("pin_file")
    @classmethod
    def _pin_file_relative(cls, v: str) -> str:
        if v.startswith(("/", "\\")) or ".." in v.replace("\\", "/").split("/"):
            raise ValueError("pin_file must be a path inside the bound repo")
        return v


class CharterRequest(BaseModel):
    goal: str = Field(min_length=1)
    kpis: list[KpiRequest] = Field(default_factory=list)
    says_no: list[str] = Field(default_factory=list)
    boundaries: list[str] = Field(default_factory=list)
    budget: BudgetRequest = Field(default_factory=BudgetRequest)
    cadence: Cadence = "weekly"
    # Factory hooks — argv strings (shlex-split, never a shell). ``checks`` must
    # pass before a headless diff is attached; ``recipes`` are named
    # deterministic commands a plan task can run instead of an LLM develop.
    # Each argv[0] must be an operator-allowed command (``command_refusal``).
    checks: list[str] = Field(default_factory=list)
    recipes: dict[str, str] = Field(default_factory=dict)

    @field_validator("checks")
    @classmethod
    def _checks_parse(cls, v: list[str]) -> list[str]:
        for cmd in v:
            _require_argv(cmd)
        return v

    @field_validator("recipes")
    @classmethod
    def _recipes_parse(cls, v: dict[str, str]) -> dict[str, str]:
        for name, cmd in v.items():
            if not name.strip():
                raise ValueError("recipe names must be non-empty")
            _require_argv(cmd)
        return v


def _require_argv(cmd: str) -> None:
    """A check/recipe command must split into a non-empty argv whose program
    the operator allows."""
    try:
        argv = shlex.split(cmd)
    except ValueError as exc:
        raise ValueError(f"command {cmd!r} does not parse: {exc}") from None
    if not argv:
        raise ValueError("commands must be non-empty")
    refusal = command_refusal(argv[0])
    if refusal:
        raise ValueError(refusal)


# Programs a charter check/recipe may start, by basename. No shells, ``env``,
# ``sudo``, downloaders, or ``git`` (a check-run git would honour hooks and
# config the agent can plant). Operators override the whole list with
# ``POCKETPAW_FACTORY_ALLOWED_COMMANDS`` (comma-separated basenames).
_DEFAULT_ALLOWED_COMMANDS = "uv,uvx,bun,bunx,node,npm,pnpm,python,python3,pytest,cargo,make,go"


def command_refusal(program: str) -> str | None:
    """Why ``program`` (a charter command's argv[0]) may not run, or ``None``.

    Read per call so an operator's env change applies. A bare name or an
    absolute path is judged by its basename; a relative path (``./x``,
    ``node_modules/.bin/x``) is always refused — it resolves inside the
    agent-editable worktree. The develop station re-checks right before exec."""
    raw = os.environ.get("POCKETPAW_FACTORY_ALLOWED_COMMANDS") or _DEFAULT_ALLOWED_COMMANDS
    allowed = {c.strip() for c in raw.split(",") if c.strip()}
    if "/" in program and not program.startswith("/"):
        return f"command {program!r} is a relative path; use an allowed command name"
    name = program.rsplit("/", 1)[-1]
    if name not in allowed:
        return (
            f"command {name!r} is not allowed (allowed: {', '.join(sorted(allowed))}; "
            "operators set POCKETPAW_FACTORY_ALLOWED_COMMANDS)"
        )
    return None


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
    # The ``upstream`` patrol's watch list (enable it with "upstream" in patrols).
    upstream: list[UpstreamPinRequest] = Field(default_factory=list)


class AutopilotState(BaseModel):
    """The persisted autopilot state on a mandate — Foresight-seeded sim users.

    ``on`` is whether the background autopilot cycle is running; ``users`` is the
    persona count per cycle (1-10). Rides on the mandate detail + list + the
    autopilot endpoint's response so the console can render the toggle."""

    on: bool = False
    users: int = Field(default=3, ge=1, le=10)


class AutopilotRequest(BaseModel):
    """Body for ``POST /belt/mandates/{id}/autopilot``.

    ``action`` starts or stops the background autopilot cycle. ``users`` (start
    only; clamped 1-10, default 3) is the persona count per cycle."""

    action: str = Field(pattern="^(start|stop)$")
    users: int = Field(default=3, ge=1, le=10)


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
    autopilot: AutopilotState = Field(default_factory=AutopilotState)
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
    autopilot: AutopilotState = Field(default_factory=AutopilotState)
    upstream: list[UpstreamPinRequest] = Field(default_factory=list)
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
# Digest — the workspace's morning report
# ---------------------------------------------------------------------------


class DigestRequest(BaseModel):
    """Query for ``GET /belt/mandates/digest``. ``since`` is an ISO-8601
    instant (a naive value reads as UTC); omitted means 24 hours ago."""

    since: datetime | None = None


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
    "AutopilotRequest",
    "AutopilotState",
    "BudgetRequest",
    "CharterRequest",
    "command_refusal",
    "CreateMandateRequest",
    "DigestRequest",
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
    "UpstreamPinRequest",
]
