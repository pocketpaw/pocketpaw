# ee/pocketpaw_ee/cloud/mandates/service.py
# Created: 2026-06-11 (feat/belt-mandates, slice 1 — models + CRUD).
#
# Business logic for the MANDATE primitive — the standing Belt JOB. Sole owner
# of writes to MandateDoc / ShiftDoc / SightingDoc (the only module that imports
# those Beanie classes, per the 4-file entity rule).
#
# Public API (module-level ``async def op(workspace_id, user_id, body) -> dict``):
#   slice 1: create_mandate, list_mandates, get_mandate
#   slice 2: file_feedback, list_sightings, run_patrols (deps patrol)
#   slice 4: trigger_shift (foreman → plan gate)
#   slice 5: get_pawprints
#
# Conventions (cloud entity rules): validate body at entry
# (``Schema.model_validate(body)``); tenant filter ``workspace=...`` on EVERY
# find; emit an event on every write (or ``# no-event: <reason>``); errors via
# ``_core.errors`` CloudError subclasses (never HTTPException).

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from pocketpaw_ee.cloud._core.errors import NotFound, ValidationError
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud.mandates import events as mandate_events
from pocketpaw_ee.cloud.mandates.domain import (
    Budget,
    Charter,
    Kpi,
    MandateDoc,
    ShiftDoc,
    SightingDoc,
    Surface,
)
from pocketpaw_ee.cloud.mandates.dto import (
    CreateMandateRequest,
    FeedbackRequest,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mapping helpers — DTO charter ↔ domain charter; doc → wire dict
# ---------------------------------------------------------------------------


def _charter_from_request(req: CreateMandateRequest) -> Charter:
    return Charter(
        goal=req.charter.goal,
        kpis=[Kpi(name=k.name, target=k.target, direction=k.direction) for k in req.charter.kpis],
        says_no=list(req.charter.says_no),
        boundaries=list(req.charter.boundaries),
        budget=Budget(
            max_tasks_per_shift=req.charter.budget.max_tasks_per_shift,
            gate_minutes_per_week=req.charter.budget.gate_minutes_per_week,
        ),
        cadence=req.charter.cadence,
    )


def _charter_to_wire(charter: Charter) -> dict[str, Any]:
    return {
        "goal": charter.goal,
        "kpis": [{"name": k.name, "target": k.target, "direction": k.direction} for k in charter.kpis],
        "says_no": list(charter.says_no),
        "boundaries": list(charter.boundaries),
        "budget": {
            "max_tasks_per_shift": charter.budget.max_tasks_per_shift,
            "gate_minutes_per_week": charter.budget.gate_minutes_per_week,
        },
        "cadence": charter.cadence,
    }


# ---------------------------------------------------------------------------
# Internal fetch helpers — tenant-scoped, raise NotFound on a miss / cross-tenant
# ---------------------------------------------------------------------------


async def _fetch_mandate(workspace_id: str, mandate_id: str) -> MandateDoc:
    """Load a mandate in the caller's workspace, or 404.

    The ``workspace=`` filter is part of the query so a cross-tenant id is a
    clean 404 (we never confirm a foreign mandate exists)."""
    try:
        doc = await MandateDoc.find_one(
            MandateDoc.workspace == workspace_id, MandateDoc.id == _as_object_id(mandate_id)
        )
    except Exception:  # noqa: BLE001 — a malformed id is a 404, not a 500
        doc = None
    if doc is None:
        raise NotFound("mandate", mandate_id)
    return doc


def _as_object_id(raw: str) -> Any:
    """Coerce a string id to a Beanie/Mongo ObjectId. A malformed id raises,
    which the callers translate to a 404."""
    from bson import ObjectId

    return ObjectId(raw)


# ---------------------------------------------------------------------------
# CRUD (slice 1)
# ---------------------------------------------------------------------------


async def create_mandate(workspace_id: str, user_id: str, body: Any) -> dict[str, Any]:
    """Create a new standing mandate. Charter body validated at entry."""
    body = CreateMandateRequest.model_validate(body)

    doc = MandateDoc(
        workspace=workspace_id,
        name=body.name,
        surface=Surface(repo_id=body.surface.repo_id),
        charter=_charter_from_request(body),
        status="active",
        soul_path=body.soul_path,
    )
    await doc.insert()

    await emit(
        mandate_events.MandateCreated(
            data={
                "workspace_id": workspace_id,
                "mandate_id": str(doc.id),
                "name": doc.name,
            }
        )
    )
    logger.info("mandate: created %s (workspace=%s, repo=%s)", doc.id, workspace_id, body.surface.repo_id)
    return await _mandate_detail_wire(doc)


async def list_mandates(workspace_id: str, user_id: str, body: Any = None) -> dict[str, Any]:
    """List the workspace's mandates with a per-mandate health summary.

    Health = last shift state, open gate count (shifts awaiting approval), and
    total sighting count. ``body`` is unused (read path)."""
    # no-event: read-only path; emit only on writes.
    docs = (
        await MandateDoc.find(MandateDoc.workspace == workspace_id)
        .sort("-createdAt")
        .to_list()
    )
    out: list[dict[str, Any]] = []
    for doc in docs:
        mandate_id = str(doc.id)
        last_shift = (
            await ShiftDoc.find(
                ShiftDoc.workspace == workspace_id, ShiftDoc.mandate_id == mandate_id
            )
            .sort("-no")
            .first_or_none()
        )
        open_gate_count = await ShiftDoc.find(
            ShiftDoc.workspace == workspace_id,
            ShiftDoc.mandate_id == mandate_id,
            ShiftDoc.state == "in_gate",
        ).count()
        sighting_count = await SightingDoc.find(
            SightingDoc.workspace == workspace_id, SightingDoc.mandate_id == mandate_id
        ).count()
        out.append(
            {
                "id": mandate_id,
                "name": doc.name,
                "status": doc.status,
                "repo_id": doc.surface.repo_id,
                "cadence": doc.charter.cadence,
                "health": {
                    "last_shift_state": last_shift.state if last_shift else None,
                    "open_gate_count": open_gate_count,
                    "sighting_count": sighting_count,
                },
                "created_at": doc.createdAt,
            }
        )
    return {"mandates": out}


async def get_mandate(workspace_id: str, user_id: str, mandate_id: str) -> dict[str, Any]:
    """Return one mandate's detail — charter, recent shifts, sightings-by-patrol.

    A mandate in another workspace is a 404."""
    # no-event: read-only path; emit only on writes.
    doc = await _fetch_mandate(workspace_id, mandate_id)
    return await _mandate_detail_wire(doc)


async def _mandate_detail_wire(doc: MandateDoc) -> dict[str, Any]:
    """Build the detail wire dict for a mandate doc — recent shifts + sightings
    grouped by patrol."""
    mandate_id = str(doc.id)
    workspace_id = doc.workspace
    recent_shifts = (
        await ShiftDoc.find(
            ShiftDoc.workspace == workspace_id, ShiftDoc.mandate_id == mandate_id
        )
        .sort("-no")
        .limit(10)
        .to_list()
    )
    sightings = await SightingDoc.find(
        SightingDoc.workspace == workspace_id, SightingDoc.mandate_id == mandate_id
    ).to_list()
    by_patrol: dict[str, int] = {}
    for s in sightings:
        by_patrol[s.patrol] = by_patrol.get(s.patrol, 0) + 1

    return {
        "id": mandate_id,
        "name": doc.name,
        "status": doc.status,
        "surface": {"repo_id": doc.surface.repo_id},
        "charter": _charter_to_wire(doc.charter),
        "soul_path": doc.soul_path,
        "recent_shifts": [
            {
                "id": str(s.id),
                "no": s.no,
                "state": s.state,
                "plan_action_id": s.plan_action_id,
                "created_at": s.createdAt,
            }
            for s in recent_shifts
        ],
        "sightings_by_patrol": by_patrol,
        "created_at": doc.createdAt,
    }


# ---------------------------------------------------------------------------
# Patrols + sightings (slice 2)
# ---------------------------------------------------------------------------


async def file_feedback(workspace_id: str, user_id: str, mandate_id: str, body: Any) -> dict[str, Any]:
    """Intake patrol — turn a human's feedback into a Sighting.

    Severity defaults to 3 (mid) when omitted. The sighting's ``patrol`` is
    ``"feedback"`` and the ``source`` rides on the evidence so the foreman can
    weight it."""
    body = FeedbackRequest.model_validate(body)
    # Tenant gate — a mandate in another workspace is a 404.
    await _fetch_mandate(workspace_id, mandate_id)

    severity = body.severity if body.severity is not None else 3
    sighting = SightingDoc(
        workspace=workspace_id,
        mandate_id=mandate_id,
        patrol="feedback",
        severity=severity,
        summary=body.text.strip()[:280],
        evidence={"source": body.source, "filed_by": user_id, "text": body.text.strip()},
    )
    await sighting.insert()

    await emit(
        mandate_events.MandateSightingAdded(
            data={
                "workspace_id": workspace_id,
                "mandate_id": mandate_id,
                "sighting_id": str(sighting.id),
                "patrol": "feedback",
                "severity": severity,
            }
        )
    )
    return _sighting_to_wire(sighting)


async def list_sightings(workspace_id: str, user_id: str, mandate_id: str) -> dict[str, Any]:
    """List a mandate's sightings, newest-first. Cross-tenant mandate → 404."""
    # no-event: read-only path; emit only on writes.
    await _fetch_mandate(workspace_id, mandate_id)
    docs = (
        await SightingDoc.find(
            SightingDoc.workspace == workspace_id, SightingDoc.mandate_id == mandate_id
        )
        .sort("-ts")
        .to_list()
    )
    return {"sightings": [_sighting_to_wire(s) for s in docs]}


def _sighting_to_wire(s: SightingDoc) -> dict[str, Any]:
    return {
        "id": str(s.id),
        "mandate_id": s.mandate_id,
        "patrol": s.patrol,
        "severity": s.severity,
        "summary": s.summary,
        "evidence": dict(s.evidence or {}),
        "ts": s.ts,
    }


def _utcnow() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "create_mandate",
    "file_feedback",
    "get_mandate",
    "list_mandates",
    "list_sightings",
]
