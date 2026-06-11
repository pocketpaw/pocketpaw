# ee/pocketpaw_ee/cloud/mandates/events.py
# Created: 2026-06-11 (feat/belt-mandates, slice 1 — models + CRUD).
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


# UI contract — fired when a shift's PlanProposal lands as a pending Instinct
# ``belt_plan`` Action. Payload: {workspace_id, mandate_id, proposal} —
# ``workspace_id`` drives the audience resolver's workspace fan-out (the same
# branch shape as ``belt_run_updated``); the /belt mandates page subscribes to
# exactly the ``belt_plan`` topic and reads {mandate_id, proposal}.
@dataclass
class BeltPlanProposed(Event):
    EVENT_TYPE: ClassVar[str] = "belt_plan"


__all__ = [
    "BeltPlanProposed",
    "MandateCreated",
    "MandateShiftStarted",
    "MandateShiftUpdated",
    "MandateSightingAdded",
]
