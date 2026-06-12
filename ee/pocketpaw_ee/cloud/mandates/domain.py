# ee/pocketpaw_ee/cloud/mandates/domain.py
# Created: 2026-06-11 (feat/belt-mandates, slice 1 — models + CRUD).
#
# Updated: 2026-06-11 (feat/belt-autopilot) — added the ``Autopilot`` embedded
# value object + the ``autopilot`` field on ``MandateDoc``. Autopilot runs
# Foresight-seeded simulated users against the mandate's surface on a background
# cycle, emitting structured feedback sightings the next shift's foreman cites.
# The persisted state is ``{on: bool, users: int}``; the background asyncio task
# itself is process-local (the ``autopilot`` module's registry), never persisted.
#
# The MANDATE primitive's persistence + value objects. A MANDATE is a standing
# JOB the Belt holds over time (an FDE retainer): it senses its surface via
# PATROLS, plans a FEW tasks per SHIFT via a FOREMAN (LLM judgment), routes the
# plan through a PLAN GATE (Instinct ``belt_plan`` proposal), and dispatches
# approved tasks as normal Belt runs.
#
# This module holds BOTH the Beanie documents (MandateDoc / ShiftDoc /
# SightingDoc) AND the frozen domain/charter value objects. Per the 4-file
# entity rule, ONLY ``mandates/service.py`` imports the Beanie doc classes; the
# router/dto layers see only the frozen domain objects the service maps to.
#
# The docs live here (not in cloud/models/) so the entity is self-contained;
# they are registered into ``init_beanie`` via a lazy import in
# ``cloud/models/__init__.get_all_documents`` (same out-of-models pattern the
# calendar docs use). Every doc carries the ``workspace`` tenancy key, indexed.

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from beanie import Indexed
from pydantic import BaseModel, Field

from pocketpaw_ee.cloud.models.base import TimestampedDocument

# ---------------------------------------------------------------------------
# Literals shared across charter + docs
# ---------------------------------------------------------------------------

KpiDirection = Literal["up", "down"]
Cadence = Literal["weekly", "manual"]
MandateStatus = Literal["active", "paused"]
ShiftState = Literal["planning", "in_gate", "executing", "done", "stood_down"]


# ---------------------------------------------------------------------------
# Charter sub-objects — persisted as embedded subdocs on MandateDoc and mirrored
# as frozen value objects on the read path.
# ---------------------------------------------------------------------------


class Kpi(BaseModel):
    """A single tracked KPI on a mandate charter.

    ``direction`` says which way is GOOD — ``"up"`` (e.g. coverage) or
    ``"down"`` (e.g. open CVE count). The foreman names an expected KPI
    direction on every task it plans.
    """

    name: str
    target: float
    direction: KpiDirection


class Budget(BaseModel):
    """Per-shift + per-week budget caps on a mandate charter.

    ``max_tasks_per_shift`` is the hard cap the foreman MUST respect and the
    plan-gate executor RE-validates at approval time. ``gate_minutes_per_week``
    is the human-review time budget the mandate is allowed to consume (advisory
    at demo bar).
    """

    max_tasks_per_shift: int = 3
    gate_minutes_per_week: int = 15


class Charter(BaseModel):
    """The standing instruction set for a mandate — the FDE retainer's brief.

    ``goal`` is the one-line job. ``kpis`` are the tracked outcomes.
    ``says_no`` + ``boundaries`` are hard constraints the foreman must honor
    (a boundary OVERRIDES a KPI opportunity). ``budget`` caps the per-shift
    task count + weekly gate minutes. ``cadence`` is ``"weekly"`` or
    ``"manual"`` (demo bar triggers shifts manually).
    """

    goal: str
    kpis: list[Kpi] = Field(default_factory=list)
    says_no: list[str] = Field(default_factory=list)
    boundaries: list[str] = Field(default_factory=list)
    budget: Budget = Field(default_factory=Budget)
    cadence: Cadence = "weekly"


class Surface(BaseModel):
    """What a mandate senses + acts on. v1 binds one repo (``repo_id``)."""

    repo_id: str


class Autopilot(BaseModel):
    """Autopilot state on a mandate — Foresight-seeded simulated users.

    ``on`` is whether a background autopilot cycle is running; ``users`` is how
    many personas each cycle builds (1-10). The persisted state is the
    SOURCE OF TRUTH for whether autopilot SHOULD be running; the live asyncio
    task lives in the ``autopilot`` module's process-local registry (a process
    restart re-derives the task from this persisted ``on`` flag — see the
    autopilot module). DEFAULT off."""

    on: bool = False
    users: int = 3


# ---------------------------------------------------------------------------
# Beanie documents — service.py is the SOLE importer.
# ---------------------------------------------------------------------------


class MandateDoc(TimestampedDocument):
    """A standing Belt mandate. One row per mandate; ``workspace`` tenancy key.

    The charter + surface ride as embedded subdocs. ``status`` gates whether
    shifts may run (``paused`` mandates are inert). ``soul_path`` optionally
    binds a soul file the foreman recalls before planning + appends a shift
    summary to after.
    """

    workspace: Indexed(str)  # type: ignore[valid-type]
    name: str
    surface: Surface
    charter: Charter
    status: MandateStatus = "active"
    soul_path: str | None = None
    # UI contract — the charter composer's senses toggles. Scopes which SENSE
    # patrols run on a shift trigger ("feedback" intake stays open as a human
    # channel regardless; the list gates the automated sense loop).
    patrols: list[str] = Field(default_factory=lambda: ["deps", "feedback"])
    # Autopilot — Foresight-seeded simulated users feeding the feedback patrol.
    # Persisted so a restart re-derives the running task; default off.
    autopilot: Autopilot = Field(default_factory=Autopilot)

    class Settings:
        name = "mandates"


class ShiftDoc(TimestampedDocument):
    """One SHIFT of a mandate — a single plan→gate→dispatch cycle.

    ``no`` is the monotonic shift number within a mandate (1-based). ``state``
    walks ``planning → in_gate → executing → done`` on the happy path, or lands
    on ``stood_down`` when the foreman returned an empty plan (a SUCCESS state,
    not an error). ``plan_action_id`` is the Instinct ``belt_plan`` Action id the
    plan was proposed through (None until the gate proposal lands). ``outcome``
    is the free-text result of the shift (what landed / was rejected / why the
    foreman stood down) — the foreman's history context reads the last 3.
    """

    workspace: Indexed(str)  # type: ignore[valid-type]
    mandate_id: Indexed(str)  # type: ignore[valid-type]
    no: int
    state: ShiftState = "planning"
    plan_action_id: str | None = None
    outcome: str | None = None

    class Settings:
        name = "mandate_shifts"


class SightingDoc(TimestampedDocument):
    """One SIGHTING — a thing a PATROL (or human feedback) flagged on a surface.

    ``patrol`` is the producing patrol name (``"deps"`` / ``"feedback"``).
    ``severity`` is 1-5 (5 = most urgent). ``summary`` is the one-line headline;
    ``evidence`` carries patrol-specific detail (package name, CVE id, feedback
    source). Sightings are the foreman's input signal between shifts.
    """

    workspace: Indexed(str)  # type: ignore[valid-type]
    mandate_id: Indexed(str)  # type: ignore[valid-type]
    patrol: str
    severity: int
    summary: str
    evidence: dict = Field(default_factory=dict)
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "mandate_sightings"


# ---------------------------------------------------------------------------
# Frozen read-path value objects — what consumers outside the service see.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MandateView:
    """Read-path view of a mandate. ``workspace`` + ``id`` are required tenancy
    / identity fields."""

    id: str
    workspace: str
    name: str
    surface: Surface
    charter: Charter
    status: MandateStatus
    soul_path: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ShiftView:
    """Read-path view of a shift."""

    id: str
    workspace: str
    mandate_id: str
    no: int
    state: ShiftState
    plan_action_id: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class SightingView:
    """Read-path view of a sighting."""

    id: str
    workspace: str
    mandate_id: str
    patrol: str
    severity: int
    summary: str
    evidence: dict = field(default_factory=dict)
    ts: datetime = field(default_factory=lambda: datetime.now(UTC))


__all__ = [
    "Autopilot",
    "Budget",
    "Cadence",
    "Charter",
    "Kpi",
    "KpiDirection",
    "MandateDoc",
    "MandateStatus",
    "MandateView",
    "ShiftDoc",
    "ShiftState",
    "ShiftView",
    "SightingDoc",
    "SightingView",
    "Surface",
]
