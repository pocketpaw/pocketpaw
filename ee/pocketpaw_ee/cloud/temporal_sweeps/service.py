# ee/pocketpaw_ee/cloud/temporal_sweeps/service.py
# Created: 2026-05-28 (feat/wave-3d-temporal-scheduler) — sole Beanie
# writer for the ``TemporalSweepStateDoc`` collection (RFC 03 v2). Module-
# level ``async def`` API per EE cloud rule 5. Every state-mutating
# function:
#   * validates at entry via ``<Request>.model_validate(body)`` (rule 6)
#     where the function accepts a body (state-write APIs here take
#     typed primitive arguments and need no re-parse).
#   * filters reads by ``workspace=workspace_id`` (rule 7)
#   * raises ``CloudError`` subclasses, never ``HTTPException`` (rule 10)
#   * emits an event on the way out (rule 9) — ``upsert_state`` fires
#     one ``TemporalSweepCompleted`` per per-pocket call.
#
# The sweep state-table is a side-table whose row count is bounded by
# (triggers × rows × pockets × workspaces); upserts target the
# composite ``(workspace, pocket, trigger_key, row_id)`` unique key.
# ``load_last_seen`` is the per-pocket scan the dispatcher calls at the
# top of each sweep; ``upsert_state`` is the per-pocket write at the
# bottom. ``record_errors`` audits per-row CEL eval failures so an
# operator can see which rows failed (the sweeper continues past them
# per the OSS contract).

"""Service for the temporal-sweep state matrix."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import TemporalSweepCompleted
from pocketpaw_ee.cloud.models.temporal_sweep_state import TemporalSweepStateDoc as _StateDoc
from pocketpaw_ee.cloud.temporal_sweeps.domain import (
    SweepDispatchResult,
    TemporalSweepState,
)
from pocketpaw_ee.cloud.temporal_sweeps.dto import state_to_wire_dict

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Private mapping helper — Beanie doc → domain
# ---------------------------------------------------------------------------


def _to_domain(doc: _StateDoc) -> TemporalSweepState:
    return TemporalSweepState(
        workspace_id=doc.workspace,
        pocket_id=doc.pocket_id,
        trigger_key=doc.trigger_key,
        row_id=doc.row_id,
        predicate_value=doc.predicate_value,
        last_swept_at=doc.last_swept_at,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def load_last_seen(
    workspace_id: str,
    pocket_id: str,
) -> dict[tuple[str, str], bool]:
    """Return the persisted ``(trigger_key, row_id) → bool`` map for one pocket.

    Tenant-filtered: a ``(workspace, pocket_id)`` pair only sees its own
    rows. A pocket with no prior sweep returns an empty dict — the OSS
    sweeper treats that as "every currently-true row is a rising edge",
    which is the documented first-sweep semantic.
    """
    query = {"workspace": workspace_id, "pocket_id": pocket_id}
    out: dict[tuple[str, str], bool] = {}
    async for doc in _StateDoc.find(query):
        out[(doc.trigger_key, doc.row_id)] = bool(doc.predicate_value)
    return out


async def upsert_state(
    workspace_id: str,
    pocket_id: str,
    new_state: dict[tuple[str, str], bool],
    *,
    dispatch_result: SweepDispatchResult | None = None,
) -> None:
    """Persist a sweep's ``new_state`` map for one pocket.

    Iterates the ``new_state`` mapping and upserts each entry. The
    composite unique index on ``(workspace, pocket, trigger_key,
    row_id)`` makes the upsert pattern safe under future leader
    election (HA is out of scope for v0 but the index pins the
    invariant).

    Emits one ``TemporalSweepCompleted`` event per call with the
    dispatch tally — listeners (audit, dashboards) key off this single
    event rather than N per-row events, the same shape
    ``BulkActionDispatched`` uses (rule 9). Passing
    ``dispatch_result=None`` is permitted for callers that want to
    persist state without firing the completion event (rare; the
    dispatcher always supplies one).
    """
    if not workspace_id:
        raise ValueError("workspace_id is required to upsert temporal sweep state")
    if not pocket_id:
        raise ValueError("pocket_id is required to upsert temporal sweep state")

    now = datetime.now(UTC)
    for (trigger_key, row_id), value in new_state.items():
        await _StateDoc.find_one(
            {
                "workspace": workspace_id,
                "pocket_id": pocket_id,
                "trigger_key": trigger_key,
                "row_id": row_id,
            },
        ).upsert(
            {
                "$set": {
                    "predicate_value": bool(value),
                    "last_swept_at": now,
                },
            },
            on_insert=_StateDoc(
                workspace=workspace_id,
                pocket_id=pocket_id,
                trigger_key=trigger_key,
                row_id=row_id,
                predicate_value=bool(value),
                last_swept_at=now,
            ),
        )

    # rule 9 — emit on every write. The dispatcher always supplies a
    # SweepDispatchResult; only the rare library-direct caller (e.g. a
    # backfill) omits it.
    if dispatch_result is not None:
        await emit(
            TemporalSweepCompleted(
                data={
                    "workspace_id": workspace_id,
                    "pocket_id": pocket_id,
                    "edges_fired": dispatch_result.edges_fired,
                    "blocked": dispatch_result.blocked,
                    "escalated": dispatch_result.escalated,
                    "errors": dispatch_result.errors,
                    "sweep_duration_ms": dispatch_result.sweep_duration_ms,
                }
            )
        )


async def record_errors(
    workspace_id: str,
    pocket_id: str,
    errors: list[Any],
) -> None:
    """Audit-log per-row CEL eval failures from a sweep.

    Wave 3d-scope: write one audit-log line per error so the operator
    can see which rows failed. The audit-log writer is the same
    facility the action_executor uses; we tag the entry with category
    ``pocket_backend_config`` and severity WARNING so existing audit
    dashboards pick it up without a new category.

    A failure to audit must NEVER break the sweep — the call is wrapped
    so a bus / audit hiccup doesn't propagate back into the dispatcher.
    """
    if not errors:
        return
    try:
        from pocketpaw.security.audit import AuditEvent, AuditSeverity, get_audit_logger

        logger_inst = get_audit_logger()
        for err in errors:
            # ``err`` is a ``TemporalSweepError`` (Pydantic) — extract
            # via attribute access; tolerate dict shape for callers
            # that pass a wire dict.
            row_id = getattr(err, "row_id", None) or (
                err.get("row_id") if isinstance(err, dict) else None
            )
            message = (
                getattr(err, "message", None)
                or (err.get("message") if isinstance(err, dict) else "")
                or ""
            )
            action = getattr(err, "action", None) or (
                err.get("action") if isinstance(err, dict) else None
            )
            logger_inst.log(
                AuditEvent.create(
                    severity=AuditSeverity.WARNING,
                    actor="system:temporal-sweeper",
                    action="pocket.temporal_sweep.row_error",
                    target=pocket_id,
                    status="error",
                    category="pocket_backend_config",
                    workspace_id=workspace_id,
                    pocket_id=pocket_id,
                    pocket_action=action,
                    row_id=row_id or "",
                    message=message,
                )
            )
    except Exception:  # noqa: BLE001 — audit must never break the sweep
        logger.warning("temporal sweep error audit-log write failed", exc_info=True)


async def list_state_for_pocket(
    workspace_id: str,
    user_id: str,  # noqa: ARG001 — viewer context for future per-user filtering
    pocket_id: str,
    *,
    limit: int = 500,
) -> list[dict]:
    """List the persisted (trigger, row) state rows for one pocket.

    Tenant-filtered: returns only rows whose ``workspace`` matches the
    caller's ``workspace_id``. Used by the GET inspect endpoint so
    operators / dashboards can debug a sweep without re-reading Mongo
    directly.
    """
    query = {"workspace": workspace_id, "pocket_id": pocket_id}
    cursor = _StateDoc.find(query).limit(limit)
    return [state_to_wire_dict(_to_domain(doc)) async for doc in cursor]


__all__ = [
    "list_state_for_pocket",
    "load_last_seen",
    "record_errors",
    "upsert_state",
]
