# ee/pocketpaw_ee/cloud/models/temporal_sweep_state.py
# Created: 2026-05-28 (feat/wave-3d-temporal-scheduler) — Beanie document
# backing the per-(workspace, pocket, trigger_key, row_id) state row the
# RFC 03 v2 temporal sweeper persists between sweeps. The pure OSS
# ``sweep_temporal_triggers`` library is stateless across calls; the
# caller hands in the prior sweep's truth map and gets back an updated
# map. Wave 3d persists that map per pocket+trigger+row so a rising-edge
# transition (false → true) fires exactly once.
#
# Tenancy: ``workspace`` is required + indexed. Every read in
# ``temporal_sweeps/service.py`` filters by it, so a sweep-state row in
# workspace A is invisible to workspace B. The composite unique index on
# (workspace, pocket_id, trigger_key, row_id) makes the upsert pattern
# safe under concurrent sweep ticks (HA is out of scope for v0, but the
# index pins the invariant for the day it lands).
#
# Storage shape (one row per (workspace, pocket, trigger_key, row_id)):
#   * predicate_value — last evaluated boolean truth value of the
#     trigger's ``when`` CEL on this row.
#   * last_swept_at — wall-clock UTC of the sweep that produced the
#     value. Used for forensic / dashboard debug, not for cadence (the
#     scheduler owns cadence).
#
# Why a separate collection rather than embedding on Pocket: the matrix
# is (triggers × rows), which can be large for a pocket with many rows.
# Keeping it side-table-ish avoids bloating the Pocket doc and lets
# Mongo's index on the composite key serve point lookups efficiently.

"""Beanie document for the RFC 03 v2 temporal-sweep state matrix."""

from __future__ import annotations

from datetime import datetime

from beanie import Indexed
from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class TemporalSweepStateDoc(TimestampedDocument):
    """One persisted (trigger × row) truth value from a temporal sweep.

    A row is written/upserted by ``temporal_sweeps.service.upsert_state``
    on every sweep that produced a state entry for the (trigger, row)
    pair. ``predicate_value`` carries the last evaluated boolean — a
    sweep's rising-edge detection diffs the prior persisted value
    against the new CEL eval to decide whether to dispatch.

    Fields:
        workspace: tenant id. Indexed; every read filters by it.
        pocket_id: pocket the trigger lives on.
        trigger_key: stable key the OSS sweeper synthesizes for the
            trigger — the action name when unique, otherwise
            ``temporal_{action}_{idx}`` / ``temporal_{idx}``.
        row_id: identifier of the row the predicate evaluated against.
        predicate_value: the boolean the CEL ``when`` evaluated to on
            the last sweep. ``True`` means the row currently satisfies
            the trigger; rising-edge dispatches fire when a stored
            ``False`` flips to a current ``True``.
        last_swept_at: wall-clock UTC of the sweep that produced this
            value. Distinct from ``updatedAt`` (which Beanie sets on
            every save) — operators want the sweep timestamp, not the
            Mongo write timestamp.
    """

    workspace: Indexed(str)  # type: ignore[valid-type]
    pocket_id: str
    trigger_key: str
    row_id: str
    predicate_value: bool
    last_swept_at: datetime

    class Settings:
        name = "temporal_sweep_state"
        indexes = [
            # Composite unique key: one row per
            # (workspace, pocket, trigger, row). Upserts target this
            # tuple; HA-safe under future leader election.
            IndexModel(
                [
                    ("workspace", 1),
                    ("pocket_id", 1),
                    ("trigger_key", 1),
                    ("row_id", 1),
                ],
                unique=True,
                name="ws_pocket_trigger_row_uniq",
            ),
            # Per-pocket scan: the dispatcher loads every (trigger, row)
            # state for one pocket at the start of a sweep.
            [("workspace", 1), ("pocket_id", 1)],
        ]


__all__ = ["TemporalSweepStateDoc"]
