# ee/pocketpaw_ee/cloud/mandates/events.py — realtime events of the mandates service.
#
# Realtime events the mandates service emits on writes (cloud entity rule:
# emit an event on every write, or annotate ``# no-event``). Thin Event
# subclasses keyed by an ``EVENT_TYPE`` discriminator, mirroring the workspace
# / foresight event shapes. Carried on the workspace realtime bus so a teammate
# with a mandate console open sees a new mandate / shift / sighting land live.

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from pocketpaw_ee.cloud._core.realtime.events import Event


@dataclass
class MandateCreated(Event):
    EVENT_TYPE: ClassVar[str] = "mandate.created"


@dataclass
class MandateSightingAdded(Event):
    EVENT_TYPE: ClassVar[str] = "mandate.sighting_added"


@dataclass
class MandateShiftStarted(Event):
    EVENT_TYPE: ClassVar[str] = "mandate.shift_started"


@dataclass
class MandateShiftUpdated(Event):
    EVENT_TYPE: ClassVar[str] = "mandate.shift_updated"


# Fired when autopilot is started / stopped on a mandate (feat/belt-autopilot).
# Payload: {workspace_id, mandate_id, on, users}. ``workspace_id`` drives the
# audience resolver's workspace fan-out so a teammate with the mandate console
# open sees the autopilot toggle flip live.
@dataclass
class MandateAutopilotChanged(Event):
    EVENT_TYPE: ClassVar[str] = "mandate.autopilot_changed"


# Fired when a mandate's crew roster is replaced. Payload: {workspace_id,
# mandate_id, crew} — the roster as stored ({agent_id, role, concurrency,
# setup}).
@dataclass
class MandateCrewChanged(Event):
    EVENT_TYPE: ClassVar[str] = "mandate.crew_changed"


# Fired when ``PATCH .../charter`` changes a mandate's model or budget caps.
# Payload: {workspace_id, mandate_id, model, budget} (the values after the write).
@dataclass
class MandateCharterChanged(Event):
    EVENT_TYPE: ClassVar[str] = "mandate.charter_changed"


# UI contract — fired when a shift's PlanProposal lands as a pending Instinct
# ``belt_plan`` Action. Payload: {workspace_id, mandate_id, proposal} —
# ``workspace_id`` drives the audience resolver's workspace fan-out (the same
# branch shape as ``belt_run_updated``); the /belt mandates page subscribes to
# exactly the ``belt_plan`` topic and reads {mandate_id, proposal}.
@dataclass
class BeltPlanProposed(Event):
    EVENT_TYPE: ClassVar[str] = "belt_plan"


# UI contract — fired when a shift plan is resolved through
# ``service.resolve_plan`` (the ``plan/resolve`` route and the auto plan gate).
# Payload: {workspace_id, mandate_id, plan_action_id, shift_no, resolved_by,
# approved, rejected} — ``resolved_by`` is the approver id (a user id, or
# ``system:belt-policy``), the counts are tasks kept and dropped.
@dataclass
class BeltPlanResolved(Event):
    EVENT_TYPE: ClassVar[str] = "belt_plan_resolved"


# UI contract — fired on every shift state write (the shift opening at
# ``planning``, then each ``mark_shift``). Payload: {workspace_id, mandate_id,
# shift_no, state}.
@dataclass
class BeltMandateUpdated(Event):
    EVENT_TYPE: ClassVar[str] = "belt_mandate_updated"


__all__ = [
    "BeltMandateUpdated",
    "BeltPlanProposed",
    "BeltPlanResolved",
    "MandateAutopilotChanged",
    "MandateCreated",
    "MandateCharterChanged",
    "MandateCrewChanged",
    "MandateShiftStarted",
    "MandateShiftUpdated",
    "MandateSightingAdded",
]
