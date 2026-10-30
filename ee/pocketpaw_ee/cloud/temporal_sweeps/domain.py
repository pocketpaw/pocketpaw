# ee/pocketpaw_ee/cloud/temporal_sweeps/domain.py
# Created: 2026-05-28 (feat/wave-3d-temporal-scheduler) — domain value
# objects for the RFC 03 v2 temporal-sweep state matrix. Frozen
# dataclasses with REQUIRED tenancy fields (workspace_id has no default)
# — constructing one without tenancy is a type error. EE rule 3.

"""Domain value objects for ``temporal_sweeps``."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class TemporalSweepState:
    """One persisted (workspace, pocket, trigger, row) truth value.

    The OSS ``sweep_temporal_triggers`` library hands the EE caller a
    ``new_state`` mapping keyed by ``(trigger_key, row_id)``; the EE
    service persists each entry as one of these. ``predicate_value`` is
    the last CEL ``when`` evaluation result; a rising edge fires when
    the stored ``False`` flips to a current ``True``.

    Frozen so downstream readers (dashboard / audit) cannot mutate the
    object after the service hands it back. Tenancy fields
    (``workspace_id``) are required positional so the type system
    catches a missing tenancy at construction time — the EE cloud rule
    3 invariant.
    """

    workspace_id: str
    pocket_id: str
    trigger_key: str
    row_id: str
    predicate_value: bool
    last_swept_at: datetime


@dataclass(frozen=True)
class SweepDispatchResult:
    """Aggregate outcome of one ``sweep_pocket`` invocation.

    Returned by ``temporal_dispatcher.sweep_pocket`` so callers (the
    scheduler tick, library callers, ad-hoc tooling) can record the
    tally. The same shape rides on the ``TemporalSweepCompleted`` bus
    event the service emits after persisting new state.

    ``edges_fired`` counts dispatches that reached the executor's HTTP
    call (``EXECUTE`` / ``NOTIFY_AND_EXECUTE``). ``blocked`` and
    ``escalated`` are honored gates that short-circuited the dispatch;
    they still represent a rising-edge transition that the runtime
    decided not to fire. ``errors`` is per-row CEL eval failures.
    """

    pocket_id: str
    edges_fired: int
    blocked: int
    escalated: int
    errors: int
    sweep_duration_ms: int


__all__ = ["SweepDispatchResult", "TemporalSweepState"]
