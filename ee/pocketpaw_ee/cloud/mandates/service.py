# ee/pocketpaw_ee/cloud/mandates/service.py
# Created: 2026-06-11 (feat/belt-mandates, slice 1 — models + CRUD).
#
# Updated: 2026-06-13 (feat/patrol-engine) — added ``list_cadence_due(now)``, the
#   cadence scheduler's read: all ACTIVE mandates whose charter cadence interval
#   has elapsed since their last shift (or that never shifted). "manual" mandates
#   are never returned. Also: ``run_patrols`` now passes ``workspace_id`` to any
#   patrol whose signature accepts it (the LIVE ``issues`` patrol needs it to
#   reach its connector) via signature inspection — the legacy ``deps_patrol``
#   ``(repo_id)`` shape is unchanged. Additive only.
# Updated: 2026-06-13 (PR #1463 review) — ``run_patrols`` also threads ``user_id``
#   to patrols that accept it (audit attribution); the sighting dedup key is
#   generalized to ``_dedup_signal`` (issues key on ``evidence.iid``, deps on
#   ``evidence.package``, else summary) so a retitled issue no longer
#   double-persists; ``list_cadence_due`` carries an explicit N+1 demo-bar
#   concession note (TODO: single aggregation pipeline).
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
#   autopilot (feat/belt-autopilot): set_autopilot (start/stop Foresight-seeded
#     simulated users feeding the feedback patrol) + repo_for_mandate (the
#     dispatcher/autopilot surface read).
#
# Conventions (cloud entity rules): validate body at entry
# (``Schema.model_validate(body)``); tenant filter ``workspace=...`` on EVERY
# find; emit an event on every write (or ``# no-event: <reason>``); errors via
# ``_core.errors`` CloudError subclasses (never HTTPException).

from __future__ import annotations

import inspect
import logging
from datetime import UTC, datetime, timedelta
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
    TeachingFeedbackRequest,
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
        "kpis": [
            {"name": k.name, "target": k.target, "direction": k.direction} for k in charter.kpis
        ],
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


def _first_pydantic_msg(exc: Any) -> str:
    """Render the first error off a Pydantic ``ValidationError`` as a single
    user-facing message ``"<field>: <reason>"`` for a 422 CloudError."""
    try:
        err = exc.errors()[0]
        loc = ".".join(str(p) for p in err.get("loc", ())) or "body"
        return f"{loc}: {err.get('msg', 'invalid value')}"
    except Exception:  # noqa: BLE001 — fall back to the str form
        return str(exc)


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
        patrols=list(body.patrols),
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
    logger.info(
        "mandate: created %s (workspace=%s, repo=%s)", doc.id, workspace_id, body.surface.repo_id
    )
    # UI contract — the create response wraps the detail in a ``mandate``
    # envelope; GET /belt/mandates/{id} stays bare.
    return {"mandate": await _mandate_detail_wire(doc)}


async def list_mandates(workspace_id: str, user_id: str, body: Any = None) -> dict[str, Any]:
    """List the workspace's mandates with a per-mandate health summary.

    Health = last shift state, open gate count (shifts awaiting approval), and
    total sighting count. ``body`` is unused (read path)."""
    # no-event: read-only path; emit only on writes.
    docs = await MandateDoc.find(MandateDoc.workspace == workspace_id).sort("-createdAt").to_list()
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
                "autopilot": _autopilot_to_wire(doc),
                "created_at": doc.createdAt,
            }
        )
    return {"mandates": out}


def _autopilot_to_wire(doc: MandateDoc) -> dict[str, Any]:
    """Render a mandate's persisted autopilot state for the wire ({on, users}).
    A mandate predating the autopilot field reads as off/3 (the doc default)."""
    ap = doc.autopilot
    return {"on": bool(ap.on), "users": int(ap.users)} if ap else {"on": False, "users": 3}


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
        await ShiftDoc.find(ShiftDoc.workspace == workspace_id, ShiftDoc.mandate_id == mandate_id)
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
        "patrols": list(doc.patrols),
        "autopilot": _autopilot_to_wire(doc),
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


async def file_feedback(
    workspace_id: str, user_id: str, mandate_id: str, body: Any
) -> dict[str, Any]:
    """Intake patrol — turn a human's feedback into a Sighting.

    Two body shapes (UI contract), discriminated on the presence of ``kind``:

    * GENERAL — ``{text, severity?, source}`` (autopilot / integrations).
      Severity defaults to 3 (mid). Returns the sighting wire dict.
    * TEACHING — ``{kind: reject|edit|plan, reason, shift_no?, task_title?}``
      (the gate UI's human-teaching channel from rejections/edits). Returns
      ``{"ok": true}``.

    Both shapes persist a ``patrol="feedback"`` Sighting so the foreman's next
    digest sees the signal; teaching items carry the gate context on the
    evidence."""
    raw = dict(body or {})
    if "kind" in raw:
        teaching = TeachingFeedbackRequest.model_validate(raw)
        # Tenant gate — a mandate in another workspace is a 404.
        await _fetch_mandate(workspace_id, mandate_id)
        summary = f"[gate {teaching.kind}] {teaching.reason.strip()}"[:280]
        sighting = SightingDoc(
            workspace=workspace_id,
            mandate_id=mandate_id,
            patrol="feedback",
            severity=3,
            summary=summary,
            evidence={
                "source": "gate",
                "kind": teaching.kind,
                "filed_by": user_id,
                "reason": teaching.reason.strip(),
                "shift_no": teaching.shift_no,
                "task_title": teaching.task_title,
            },
        )
        await sighting.insert()
        await emit(
            mandate_events.MandateSightingAdded(
                data={
                    "workspace_id": workspace_id,
                    "mandate_id": mandate_id,
                    "sighting_id": str(sighting.id),
                    "patrol": "feedback",
                    "severity": 3,
                }
            )
        )
        return {"ok": True}

    body = FeedbackRequest.model_validate(raw)
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


def _dedup_signal(evidence: dict[str, Any] | None, summary: str | None) -> str:
    """The stable per-patrol dedup signal for a sighting / draft.

    Generalized across patrols: ``issues`` carry ``evidence.iid`` (stable across a
    retitle), ``deps`` carry ``evidence.package``; anything else falls back to the
    summary. Without the ``iid`` branch an issue retitle would change the summary
    and double-persist the same issue."""
    ev = evidence or {}
    signal = ev.get("iid") or ev.get("package") or summary or ""
    return str(signal)


async def run_patrols(workspace_id: str, user_id: str, mandate_id: str) -> dict[str, Any]:
    """Run every registered patrol over the mandate's surface and persist the
    resulting Sightings.

    Dedup: a draft whose dedup signal (``evidence.iid`` for issues,
    ``evidence.package`` for deps, else the summary) already has a sighting from
    the same patrol on this mandate is skipped — repeated shift triggers must not
    spam the foreman with identical signals. Returns the NEW sightings only."""
    from pocketpaw_ee.cloud.mandates.patrols import PATROLS

    doc = await _fetch_mandate(workspace_id, mandate_id)

    existing = await SightingDoc.find(
        SightingDoc.workspace == workspace_id, SightingDoc.mandate_id == mandate_id
    ).to_list()
    seen_keys = {(s.patrol, _dedup_signal(s.evidence, s.summary)) for s in existing}

    created: list[SightingDoc] = []
    enabled = set(doc.patrols or [])
    for patrol_name, patrol in PATROLS.items():
        # UI contract — the mandate's ``patrols`` toggles scope the sense loop;
        # an un-toggled patrol never runs. (The "feedback" intake endpoint is a
        # human channel and stays open regardless — it has no sense callable.)
        if patrol_name not in enabled:
            continue
        try:
            # Patrols take ``repo_id`` positionally; a LIVE patrol (e.g. ``issues``)
            # also needs the workspace to reach its connector, and the actor to
            # attribute the connector call to, so pass ``workspace_id`` / ``user_id``
            # ONLY when the patrol's signature accepts them. This keeps the legacy
            # ``deps_patrol(repo_id)`` shape working unchanged.
            kwargs: dict[str, Any] = {}
            params = inspect.signature(patrol).parameters
            if "workspace_id" in params:
                kwargs["workspace_id"] = workspace_id
            if "user_id" in params:
                kwargs["user_id"] = user_id
            drafts = await patrol(doc.surface.repo_id, **kwargs)
        except Exception:  # noqa: BLE001 — a broken patrol must not wedge the shift
            logger.warning("mandate: patrol %r raised — skipping", patrol_name, exc_info=True)
            continue
        for draft in drafts:
            key = (
                str(draft.get("patrol") or patrol_name),
                _dedup_signal(draft.get("evidence"), draft.get("summary")),
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            sighting = SightingDoc(
                workspace=workspace_id,
                mandate_id=mandate_id,
                patrol=str(draft.get("patrol") or patrol_name),
                severity=int(draft.get("severity") or 3),
                summary=str(draft.get("summary") or "")[:280],
                evidence=dict(draft.get("evidence") or {}),
            )
            await sighting.insert()
            created.append(sighting)

    for sighting in created:
        await emit(
            mandate_events.MandateSightingAdded(
                data={
                    "workspace_id": workspace_id,
                    "mandate_id": mandate_id,
                    "sighting_id": str(sighting.id),
                    "patrol": sighting.patrol,
                    "severity": sighting.severity,
                }
            )
        )
    return {"sightings": [_sighting_to_wire(s) for s in created]}


async def set_autopilot(
    workspace_id: str, user_id: str, mandate_id: str, body: Any
) -> dict[str, Any]:
    """Start or stop AUTOPILOT on a mandate — Foresight-seeded simulated users
    feeding the feedback patrol. Body: ``{action: "start"|"stop", users?: int}``.

    START: persist ``autopilot={on: True, users: N}``, run ONE cycle SYNCHRONOUSLY
    (so this response already reflects the first cycle's sightings — the brief's
    "run ONE cycle immediately on start"), then spawn the background loop for the
    subsequent every-interval cycles. STOP: cancel the background task, persist
    ``autopilot.on=False``. Either way, emit ``MandateAutopilotChanged`` and
    return the mandate detail wire dict (envelope-free, matching GET detail).

    A cross-tenant mandate is a 404. Autopilot must never crash a shift or the
    app — the immediate cycle is failure-swallowing by construction
    (``autopilot.run_autopilot_cycle`` never raises)."""
    from pydantic import ValidationError as PydanticValidationError

    from pocketpaw_ee.cloud.mandates import autopilot as autopilot_mod
    from pocketpaw_ee.cloud.mandates.domain import Autopilot
    from pocketpaw_ee.cloud.mandates.dto import AutopilotRequest

    # Validate at entry — a bad body (unknown action / out-of-range users) is a
    # 422 ``ValidationError`` CloudError, not a 500 (the cloud error handler only
    # maps CloudError; a raw Pydantic error would escape as a 500).
    try:
        req = AutopilotRequest.model_validate(body)
    except PydanticValidationError as exc:
        raise ValidationError("mandate.autopilot_invalid", _first_pydantic_msg(exc)) from exc

    doc = await _fetch_mandate(workspace_id, mandate_id)

    if req.action == "start":
        users = max(1, min(10, req.users))
        doc.autopilot = Autopilot(on=True, users=users)
        await doc.save()
        # Run the first cycle SYNCHRONOUSLY so the response/assertions see its
        # sightings, THEN start the loop (which skips its own immediate cycle so
        # the first cycle isn't double-filed). The cycle never raises.
        await autopilot_mod.run_autopilot_cycle(workspace_id, mandate_id, users=users)
        await autopilot_mod.start_autopilot(workspace_id, mandate_id, users, run_immediate=False)
    else:  # stop
        await autopilot_mod.stop_autopilot(mandate_id)
        users = doc.autopilot.users if doc.autopilot else 3
        doc.autopilot = Autopilot(on=False, users=users)
        await doc.save()

    await emit(
        mandate_events.MandateAutopilotChanged(
            data={
                "workspace_id": workspace_id,
                "mandate_id": mandate_id,
                "on": doc.autopilot.on,
                "users": doc.autopilot.users,
            }
        )
    )
    logger.info(
        "mandate: autopilot %s for %s (workspace=%s, users=%d)",
        "started" if doc.autopilot.on else "stopped",
        mandate_id,
        workspace_id,
        doc.autopilot.users,
    )
    # UI contract — the autopilot response wraps the detail in a ``mandate``
    # envelope (same shape as create), so the console can re-render the row.
    return {"mandate": await _mandate_detail_wire(doc)}


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


# ---------------------------------------------------------------------------
# Shift trigger — foreman → plan gate (slice 4)
# ---------------------------------------------------------------------------


async def trigger_shift(workspace_id: str, user_id: str, mandate_id: str) -> dict[str, Any]:
    """Run one SHIFT: sense (patrols) → judge (foreman, ONE LLM call) →
    machine-validate → route the plan through the Instinct PLAN GATE as a
    ``belt_plan`` proposal, or stand the shift down on an empty plan.

    DEMO BAR: manual trigger only (``cadence`` scheduling is a later PR).

    Terminal shapes:
      * tasks planned   → ShiftDoc(state="in_gate", plan_action_id=...) and a
        pending Instinct Action; the chain opened with ``agent.proposed`` —
        the approve/reject paths close it (executor / router).
      * empty plan      → ShiftDoc(state="stood_down") — a SUCCESS state. The
        chain opens AND closes here (``agent.proposed`` →
        ``decision.completed(passed=True, action_outcome="stood_down")``) —
        exactly ONE terminal; no human gate for a no-op.
      * validation fail → ValidationError (422); the shift stays ``planning``
        with the violations recorded on its outcome. # no-event on the raise
        path beyond the shift-started emit: the shift row carries the state.
    """
    from uuid import uuid4

    from pocketpaw_ee.cloud.mandates import foreman as foreman_mod
    from pocketpaw_ee.cloud.mandates import soul_link

    doc = await _fetch_mandate(workspace_id, mandate_id)
    if doc.status != "active":
        raise ValidationError(
            "mandate.paused", "This mandate is paused — resume it before running a shift"
        )

    # 1. SENSE — run the patrols so the foreman sees fresh signals.
    await run_patrols(workspace_id, user_id, mandate_id)

    # 2. Open the shift row (state=planning) — the durable record that a
    #    judgment was attempted, even if the foreman fails.
    last = (
        await ShiftDoc.find(ShiftDoc.workspace == workspace_id, ShiftDoc.mandate_id == mandate_id)
        .sort("-no")
        .first_or_none()
    )
    shift_no = (last.no if last else 0) + 1
    shift = ShiftDoc(workspace=workspace_id, mandate_id=mandate_id, no=shift_no, state="planning")
    await shift.insert()
    await emit(
        mandate_events.MandateShiftStarted(
            data={
                "workspace_id": workspace_id,
                "mandate_id": mandate_id,
                "shift_id": str(shift.id),
                "no": shift_no,
            }
        )
    )

    # 3. JUDGE — assemble the context and make the ONE foreman call.
    charter_wire = _charter_to_wire(doc.charter)
    since = last.createdAt if last else None
    all_sightings = (
        await SightingDoc.find(
            SightingDoc.workspace == workspace_id, SightingDoc.mandate_id == mandate_id
        )
        .sort("-ts")
        .to_list()
    )
    digest = [
        {"id": str(s.id), "patrol": s.patrol, "severity": s.severity, "summary": s.summary}
        for s in all_sightings
        if since is None or _aware(s.ts) > _aware(since)
    ]
    history_docs = (
        await ShiftDoc.find(
            ShiftDoc.workspace == workspace_id,
            ShiftDoc.mandate_id == mandate_id,
            ShiftDoc.no < shift_no,
        )
        .sort("-no")
        .limit(3)
        .to_list()
    )
    history = [{"no": h.no, "state": h.state, "outcome": h.outcome} for h in reversed(history_docs)]
    soul_context = await soul_link.recall_for_planning(
        doc.soul_path, f"{doc.name} {doc.charter.goal}"
    )

    context = foreman_mod.ForemanContext(
        shift_no=shift_no,
        charter=charter_wire,
        sightings=digest,
        history=history,
        soul_context=soul_context,
    )
    try:
        plan = await foreman_mod.plan_shift(context)
    except Exception as exc:  # noqa: BLE001 — surface a clean upstream failure
        from pocketpaw_ee.cloud._core.errors import CloudError

        # Failure path keeps the state UNCHANGED ("planning") and only records
        # the failure on ``outcome`` — the shift never advances on a bad call.
        await mark_shift(
            workspace_id=workspace_id,
            shift_id=str(shift.id),
            state="planning",
            outcome=f"foreman call failed: {exc}",
        )
        raise CloudError(
            502, "mandate.foreman_failed", f"The foreman's judgment call failed: {exc}"
        ) from exc

    # 4. MACHINE VALIDATION — action fields + structure ONLY (never ``why``).
    violations = foreman_mod.validate_plan(plan, charter_wire)
    if violations:
        await mark_shift(
            workspace_id=workspace_id,
            shift_id=str(shift.id),
            state="planning",
            outcome="plan refused by machine validation: " + "; ".join(violations),
        )
        raise ValidationError("mandate.plan_invalid", "; ".join(violations))

    # 5a. EMPTY PLAN — stand the shift down. A success, not an error: the
    #     chain opens and closes here with exactly ONE terminal.
    if plan.no_action:
        correlation_id = uuid4()
        proposed_event_id = _emit_agent_proposed_plan(
            correlation_id=correlation_id,
            workspace_id=workspace_id,
            user_id=user_id,
            mandate_name=doc.name,
            shift_no=shift_no,
            task_count=0,
            no_action=True,
        )
        _emit_stood_down_close(
            correlation_id=correlation_id,
            workspace_id=workspace_id,
            user_id=user_id,
            reason=plan.no_action_reason or "",
            causation_id=proposed_event_id,
        )
        outcome = f"stood down: {plan.no_action_reason or 'no action needed'}"
        await mark_shift(
            workspace_id=workspace_id,
            shift_id=str(shift.id),
            state="stood_down",
            outcome=outcome,
        )
        await soul_link.remember_shift(
            doc.soul_path, f"Mandate '{doc.name}' shift {shift_no} {outcome}"
        )
        # UI contract — the shift response rides a ``shift`` envelope.
        return {
            "shift": {
                "shift_id": str(shift.id),
                "no": shift_no,
                "state": "stood_down",
                "plan_action_id": None,
                "task_count": 0,
                "no_action_reason": plan.no_action_reason,
            }
        }

    # 5b. TASK PLAN — propose through the Instinct PLAN GATE as a ``belt_plan``
    #     Action. Mirrors the belt MCP propose: mint the chain correlation_id
    #     BEFORE building the blob; confirm the Action is durable; THEN open the
    #     chain and back-write the proposed event id.
    from pocketpaw.instinct.models import ActionCategory, ActionPriority, ActionTrigger
    from pocketpaw.stores import get_instinct_store
    from pocketpaw_ee.cloud.mandates.executor import BELT_PLAN_PARAM_KEY, BELT_PLAN_SCHEMA

    correlation_id = uuid4()
    blob: dict[str, Any] = {
        "kind": "belt_plan",
        "schema": BELT_PLAN_SCHEMA,
        "mandate_id": mandate_id,
        "shift_id": str(shift.id),
        "shift_no": shift_no,
        "plan": plan.model_dump(),
        # Budget snapshot — the executor re-validates it is UNCHANGED at
        # approval time.
        "budget_max_tasks": doc.charter.budget.max_tasks_per_shift,
        "soul_path": doc.soul_path,
        "workspace_id": workspace_id,
        "requested_by": user_id,
        "correlation_id": str(correlation_id),
        "proposed_event_id": None,
    }

    titles = "; ".join(t.title for t in plan.tasks)
    title = f"Shift plan — {doc.name} (shift {shift_no})"
    recommendation = (
        f"Approve to dispatch {len(plan.tasks)} task(s) as Belt runs for mandate "
        f"'{doc.name}': {titles[:400]}"
    )
    trigger = ActionTrigger(
        type="agent",
        source="belt:mandate-foreman",
        reason="shift plan proposed by the mandate foreman — requires human approval",
    )
    store = get_instinct_store()
    try:
        action = await store.propose(
            pocket_id=workspace_id,
            title=title,
            description=recommendation,
            recommendation=recommendation,
            trigger=trigger,
            category=ActionCategory.EXTERNAL,
            priority=ActionPriority.HIGH,
            parameters={BELT_PLAN_PARAM_KEY: blob},
            assignee=user_id,
            workspace_id=workspace_id,
        )
    except Exception as exc:  # noqa: BLE001
        from pocketpaw_ee.cloud._core.errors import CloudError

        await mark_shift(
            workspace_id=workspace_id,
            shift_id=str(shift.id),
            state="planning",
            outcome=f"gate proposal failed: {exc}",
        )
        raise CloudError(
            502, "mandate.gate_propose_failed", f"could not propose the plan: {exc}"
        ) from exc

    # NO phantom success — confirm the Action is durably readable.
    stored = await store.get_action(action.id)
    if stored is None:
        from pocketpaw_ee.cloud._core.errors import CloudError

        raise CloudError(502, "mandate.gate_propose_failed", "the plan was not stored — retry")

    proposed_event_id = _emit_agent_proposed_plan(
        correlation_id=correlation_id,
        workspace_id=workspace_id,
        user_id=user_id,
        mandate_name=doc.name,
        shift_no=shift_no,
        task_count=len(plan.tasks),
        no_action=False,
        action_id=str(action.id),
    )
    if proposed_event_id is not None:
        await _persist_plan_chain_ids(
            store=store,
            action_id=str(action.id),
            proposed_event_id=str(proposed_event_id),
        )

    await mark_shift(
        workspace_id=workspace_id,
        shift_id=str(shift.id),
        state="in_gate",
        plan_action_id=str(action.id),
    )
    # UI contract — announce the new plan proposal on the ``belt_plan`` topic
    # (workspace fan-out, mirroring how belt_run_updated rides the bus). The
    # page subscribing to this topic reads {mandate_id, proposal}.
    await emit(
        mandate_events.BeltPlanProposed(
            data={
                "workspace_id": workspace_id,
                "mandate_id": mandate_id,
                "proposal": {
                    "plan_action_id": str(action.id),
                    "shift_id": str(shift.id),
                    "shift_no": shift_no,
                    **plan.model_dump(),
                },
            }
        )
    )
    logger.info(
        "mandate: shift %s of %s proposed %d task(s) via belt_plan action %s (correlation_id=%s)",
        shift_no,
        mandate_id,
        len(plan.tasks),
        action.id,
        correlation_id,
    )
    # UI contract — the shift response rides a ``shift`` envelope.
    return {
        "shift": {
            "shift_id": str(shift.id),
            "no": shift_no,
            "state": "in_gate",
            "plan_action_id": str(action.id),
            "task_count": len(plan.tasks),
            "no_action_reason": None,
        }
    }


def _aware(ts: datetime) -> datetime:
    """Normalize a possibly-naive datetime to UTC-aware for comparisons
    (mongomock round-trips naive datetimes)."""
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def _emit_agent_proposed_plan(
    *,
    correlation_id: Any,
    workspace_id: str,
    user_id: str,
    mandate_name: str,
    shift_no: int,
    task_count: int,
    no_action: bool,
    action_id: str | None = None,
) -> Any | None:
    """Open the Decision-Graph chain for a shift plan (``agent.proposed``).

    Returns the emitted event id for causation chaining, or None when the emit
    raised — best-effort per RFC 09 (the reconciler picks up orphans)."""
    from soul_protocol.spec.journal import Actor

    from pocketpaw_ee.cloud.decisions.journal_writer import record_agent_proposed

    actor = Actor(
        kind="agent",
        id=f"user:{user_id or 'unknown'}",
        scope_context=[f"workspace:{workspace_id}"],
    )
    intent = f"shift {shift_no} of mandate '{mandate_name}' — " + (
        "stand down (no action)" if no_action else f"plan {task_count} task(s)"
    )
    payload: dict[str, Any] = {
        "intent": intent,
        "action": "belt_plan",
        "pocket_id": workspace_id,
        "inputs": [],
        "proposal_kind": "belt_plan",
        "shift_no": shift_no,
        "task_count": task_count,
        "no_action": no_action,
    }
    if action_id:
        payload["action_id"] = action_id
    try:
        entry = record_agent_proposed(
            correlation_id=correlation_id,
            actor=actor,
            scope=[f"workspace:{workspace_id}"],
            payload=payload,
        )
        return entry.id
    except Exception:  # noqa: BLE001 — chain emit is best-effort
        logger.warning(
            "mandate agent.proposed emit failed for correlation_id=%s — reconciler will catch up",
            correlation_id,
            exc_info=True,
        )
        return None


def _emit_stood_down_close(
    *,
    correlation_id: Any,
    workspace_id: str,
    user_id: str,
    reason: str,
    causation_id: Any | None,
) -> None:
    """Close a stood-down shift's chain with its ONE terminal —
    ``decision.completed(passed=True, action_outcome="stood_down")``. A
    stood-down shift is a SUCCESS (the foreman judged no action was needed)."""
    from soul_protocol.spec.journal import Actor

    from pocketpaw_ee.cloud.decisions.journal_writer import record_decision_completed

    actor = Actor(
        kind="agent",
        id=f"user:{user_id or 'unknown'}",
        scope_context=[f"workspace:{workspace_id}"],
    )
    payload: dict[str, Any] = {
        "passed": True,
        "action_outcome": "stood_down",
        "task_count": 0,
    }
    if reason:
        payload["reason"] = reason
    try:
        record_decision_completed(
            correlation_id=correlation_id,
            actor=actor,
            scope=[f"workspace:{workspace_id}"],
            payload=payload,
            causation_id=causation_id,
        )
    except Exception:  # noqa: BLE001 — chain close is best-effort
        logger.warning(
            "mandate stood_down chain close failed for correlation_id=%s — "
            "reconciler will catch up",
            correlation_id,
            exc_info=True,
        )


async def _persist_plan_chain_ids(*, store: Any, action_id: str, proposed_event_id: str) -> None:
    """Back-write ``proposed_event_id`` onto the persisted ``_belt_plan`` blob
    (the correlation_id was minted before the blob was built, so it's already
    correct). Direct SQL update — the same pattern the belt MCP propose uses.
    Best-effort."""
    import json as _json

    import aiosqlite

    from pocketpaw_ee.cloud.mandates.executor import BELT_PLAN_PARAM_KEY

    try:
        action = await store.get_action(action_id)
        if action is None:
            return
        params = dict(getattr(action, "parameters", None) or {})
        blob = params.get(BELT_PLAN_PARAM_KEY)
        if not isinstance(blob, dict):
            return
        blob = dict(blob)
        blob["proposed_event_id"] = proposed_event_id
        params[BELT_PLAN_PARAM_KEY] = blob

        async with aiosqlite.connect(store._db_path) as db:
            await db.execute(
                "UPDATE instinct_actions SET parameters = ?,"
                " updated_at = datetime('now') WHERE id = ?",
                (_json.dumps(params), action_id),
            )
            await db.commit()
    except Exception:  # noqa: BLE001 — write-back is best-effort
        logger.warning(
            "mandate: failed to persist chain ids onto action %s — human.corrected "
            "will emit without causation_id",
            action_id,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Plan resolve (UI contract) — map per-task gate verdicts onto the Instinct path
# ---------------------------------------------------------------------------


async def prepare_plan_resolution(
    workspace_id: str, user_id: str, mandate_id: str, body: Any
) -> dict[str, Any]:
    """Validate a console plan-resolution and translate it into ONE Instinct
    transition the router then performs through the REAL approve/reject path.

    The instinct gate stays the single authority: an approved/edited subset
    becomes an APPROVE-WITH-EDITS (the blob's task list filtered + retitled —
    the same Corrections machinery every instinct edit uses), and an all-reject
    becomes a plain REJECT. The chain therefore closes exactly once, in the
    same code paths the Tray uses. Rejected tasks are recorded as teaching
    sightings (the feedback patrol) so the foreman's next digest learns.

    Rules: the shift must be ``in_gate`` with a plan Action still PENDING;
    decision ``index`` is 0-BASED into the plan's tasks array; EVERY task must
    carry exactly one decision (explicit beats implicit at a human gate).

    Returns the router's marching orders:
    ``{action_id, shift_id, mode: "approve"|"reject", parameters?, edited,
    reject_reason?}`` — ``parameters`` (approve mode) is the full edited
    parameters dict for ``ApproveRequest``; ``edited`` says whether any task
    was edited/dropped (drives the corrections path)."""
    from pocketpaw.stores import get_instinct_store
    from pocketpaw_ee.cloud.mandates.dto import ResolvePlanRequest
    from pocketpaw_ee.cloud.mandates.executor import BELT_PLAN_PARAM_KEY

    body = ResolvePlanRequest.model_validate(body)
    await _fetch_mandate(workspace_id, mandate_id)

    shift = await ShiftDoc.find_one(
        ShiftDoc.workspace == workspace_id,
        ShiftDoc.mandate_id == mandate_id,
        ShiftDoc.no == body.shift_no,
    )
    if shift is None:
        raise NotFound("shift", str(body.shift_no))
    if shift.state != "in_gate" or not shift.plan_action_id:
        raise ValidationError(
            "mandate.shift_not_in_gate",
            f"shift {body.shift_no} is {shift.state!r} — only an in_gate shift can be resolved",
        )

    store = get_instinct_store()
    action = await store.get_action(shift.plan_action_id)
    params = dict(getattr(action, "parameters", None) or {}) if action else {}
    blob = params.get(BELT_PLAN_PARAM_KEY)
    if action is None or not isinstance(blob, dict):
        raise ValidationError(
            "mandate.plan_missing", "the shift's plan Action is missing or malformed"
        )
    status = str(getattr(getattr(action, "status", None), "value", "") or "")
    if status != "pending":
        raise ValidationError("mandate.plan_already_resolved", f"the plan is already {status}")

    tasks = list((blob.get("plan") or {}).get("tasks") or [])
    by_index: dict[int, Any] = {}
    for d in body.decisions:
        if d.index >= len(tasks):
            raise ValidationError(
                "mandate.bad_decision_index",
                f"decision index {d.index} is out of range (plan has {len(tasks)} tasks; "
                "indices are 0-based)",
            )
        if d.index in by_index:
            raise ValidationError(
                "mandate.duplicate_decision", f"task index {d.index} has two decisions"
            )
        if d.decision == "edit" and not (d.edited_title or "").strip():
            raise ValidationError(
                "mandate.edit_without_title", f"edit decision on task {d.index} needs edited_title"
            )
        by_index[d.index] = d
    missing = [i for i in range(len(tasks)) if i not in by_index]
    if missing:
        raise ValidationError(
            "mandate.incomplete_decisions",
            f"every task needs a decision; missing indices {missing} (0-based)",
        )

    kept: list[dict[str, Any]] = []
    edited = False
    rejected: list[tuple[int, Any]] = []
    for i, task in enumerate(tasks):
        d = by_index[i]
        if d.decision == "reject":
            rejected.append((i, d))
            edited = True
            continue
        t = dict(task)
        if d.decision == "edit":
            t["title"] = d.edited_title.strip()
            edited = True
        kept.append(t)

    # Rejected tasks become teaching sightings — the foreman's next digest
    # reads the human's reasons. (Each insert emits MandateSightingAdded.)
    for i, d in rejected:
        await file_feedback(
            workspace_id,
            user_id,
            mandate_id,
            {
                "kind": "reject",
                "reason": (d.reason or "rejected at the gate").strip(),
                "shift_no": body.shift_no,
                "task_title": str(tasks[i].get("title") or ""),
            },
        )

    if not kept:
        reasons = "; ".join((d.reason or "").strip() for _, d in rejected if d.reason)
        return {
            "action_id": str(shift.plan_action_id),
            "shift_id": str(shift.id),
            "mode": "reject",
            "edited": True,
            "reject_reason": reasons or "all tasks rejected at the gate",
        }

    new_blob = dict(blob)
    new_plan = dict(blob.get("plan") or {})
    new_plan["tasks"] = kept
    new_blob["plan"] = new_plan
    new_params = dict(params)
    new_params[BELT_PLAN_PARAM_KEY] = new_blob
    return {
        "action_id": str(shift.plan_action_id),
        "shift_id": str(shift.id),
        "mode": "approve",
        "edited": edited,
        "parameters": new_params,
    }


async def shift_wire(workspace_id: str, shift_id: str) -> dict[str, Any]:
    """Refresh one shift's wire dict (the resolve response's ``shift``).

    Shape-matched to ``trigger_shift``'s ``shift`` payload (shift_id, no,
    state, plan_action_id, task_count, no_action_reason) so POST /shift and
    POST /plan/resolve return the same shape; ``outcome`` rides along as a
    resolve-path extra (the dispatch/rejection text the console can show).
    ``task_count`` reads the plan Action's CURRENT task list, so a resolve
    that dropped tasks reports the kept count."""
    # no-event: read-only path; emit only on writes.
    try:
        doc = await ShiftDoc.find_one(
            ShiftDoc.workspace == workspace_id, ShiftDoc.id == _as_object_id(shift_id)
        )
    except Exception:  # noqa: BLE001 — malformed id == 404
        doc = None
    if doc is None:
        raise NotFound("shift", shift_id)

    task_count = 0
    if doc.plan_action_id:
        from pocketpaw.stores import get_instinct_store
        from pocketpaw_ee.cloud.mandates.executor import BELT_PLAN_PARAM_KEY

        try:
            action = await get_instinct_store().get_action(doc.plan_action_id)
            blob = (getattr(action, "parameters", None) or {}).get(BELT_PLAN_PARAM_KEY)
            if isinstance(blob, dict):
                task_count = len((blob.get("plan") or {}).get("tasks") or [])
        except Exception:  # noqa: BLE001 — count degrades to 0, never breaks the read
            logger.debug("mandate: shift_wire task count read failed", exc_info=True)

    no_action_reason = None
    if doc.state == "stood_down" and doc.outcome:
        no_action_reason = doc.outcome.removeprefix("stood down: ")

    return {
        "shift_id": str(doc.id),
        "no": doc.no,
        "state": doc.state,
        "plan_action_id": doc.plan_action_id,
        "task_count": task_count,
        "no_action_reason": no_action_reason,
        "outcome": doc.outcome,
    }


# ---------------------------------------------------------------------------
# Pawprints — the past-tense event feed (slice 5)
# ---------------------------------------------------------------------------


async def get_pawprints(workspace_id: str, user_id: str, mandate_id: str) -> dict[str, Any]:
    """Walk the mandate's shift history + decision chains into a past-tense
    event feed (UI contract item shape: ``{id, mandate_id, shift_no, kind,
    summary, evidence_refs, ts}``).

    Kinds: the UI consumes ``executed`` / ``rejected`` / ``edited`` /
    ``stood_down``; the feed also emits ``proposed`` / ``approved`` /
    ``failed`` / ``planning`` with the same shape (a documented superset).
    ``edited`` fires instead of ``approved`` when the approval carried human
    edits (the action has Corrections recorded).

    Sources: the ShiftDoc rows (state + outcome) and each shift's ``belt_plan``
    Instinct Action (status + the plan blob's evidence refs) — the same records
    the decision chain folded from, read through the store instead of replaying
    the journal at demo bar."""
    # no-event: read-only path; emit only on writes.
    await _fetch_mandate(workspace_id, mandate_id)
    shifts = (
        await ShiftDoc.find(ShiftDoc.workspace == workspace_id, ShiftDoc.mandate_id == mandate_id)
        .sort("+no")
        .to_list()
    )

    from pocketpaw.stores import get_instinct_store
    from pocketpaw_ee.cloud.mandates.executor import BELT_PLAN_PARAM_KEY

    store = get_instinct_store()
    prints: list[dict[str, Any]] = []

    def _item(shift: Any, kind: str, summary: str, refs: list[str], ts: Any) -> dict[str, Any]:
        return {
            "id": f"{shift.id}:{kind}",
            "mandate_id": mandate_id,
            "shift_no": shift.no,
            "kind": kind,
            "summary": summary,
            "evidence_refs": refs,
            "ts": ts,
        }

    for shift in shifts:
        if shift.state == "stood_down":
            reason = (shift.outcome or "no action needed").removeprefix("stood down: ")
            prints.append(
                _item(
                    shift,
                    "stood_down",
                    f"Shift {shift.no}: the foreman stood down — {reason}",
                    [],
                    shift.updatedAt,
                )
            )
            continue
        if shift.state == "planning":
            summary = f"Shift {shift.no}: planning"
            if shift.outcome:
                summary = f"Shift {shift.no}: plan did not reach the gate — {shift.outcome}"
            prints.append(_item(shift, "planning", summary, [], shift.updatedAt))
            continue

        # in_gate / executing / done — read the plan Action for status + refs.
        action = None
        if shift.plan_action_id:
            try:
                action = await store.get_action(shift.plan_action_id)
            except Exception:  # noqa: BLE001 — a store hiccup degrades the feed
                logger.debug("mandate: pawprints action read failed", exc_info=True)
        blob = (
            (getattr(action, "parameters", None) or {}).get(BELT_PLAN_PARAM_KEY) if action else None
        )
        tasks = (blob or {}).get("plan", {}).get("tasks", []) if isinstance(blob, dict) else []
        refs = sorted({r for t in tasks for r in (t.get("evidence_refs") or [])})
        task_count = len(tasks)

        prints.append(
            _item(
                shift,
                "proposed",
                f"Shift {shift.no}: the foreman proposed {task_count} task(s) "
                "through the plan gate",
                refs,
                shift.createdAt,
            )
        )
        status = str(getattr(getattr(action, "status", None), "value", "") or "")
        if status in ("approved", "executed", "failed"):
            # ``edited`` when the human approved WITH edits (Corrections exist
            # on the action); plain ``approved`` otherwise.
            approve_kind = "approved"
            try:
                if shift.plan_action_id and await store.get_corrections_for_action(
                    shift.plan_action_id
                ):
                    approve_kind = "edited"
            except Exception:  # noqa: BLE001 — corrections lookup degrades to approved
                logger.debug("mandate: pawprints corrections read failed", exc_info=True)
            verb = "approved" if approve_kind == "approved" else "approved with edits"
            prints.append(
                _item(
                    shift,
                    approve_kind,
                    f"Shift {shift.no}: a human {verb} the plan at the gate",
                    refs,
                    shift.updatedAt,
                )
            )
        if status == "rejected":
            prints.append(
                _item(
                    shift,
                    "rejected",
                    f"Shift {shift.no}: a human rejected the plan at the gate"
                    + (f" — {shift.outcome}" if shift.outcome else ""),
                    refs,
                    shift.updatedAt,
                )
            )
        elif status == "executed":
            prints.append(
                _item(
                    shift,
                    "executed",
                    f"Shift {shift.no}: "
                    + (shift.outcome or f"dispatched {task_count} task(s) as belt runs"),
                    refs,
                    shift.updatedAt,
                )
            )
        elif status == "failed":
            prints.append(
                _item(
                    shift,
                    "failed",
                    f"Shift {shift.no}: the approved plan failed to dispatch"
                    + (f" — {shift.outcome}" if shift.outcome else ""),
                    refs,
                    shift.updatedAt,
                )
            )
    return {"pawprints": prints}


# ---------------------------------------------------------------------------
# Executor-facing helpers (the executor never imports Beanie models)
# ---------------------------------------------------------------------------


async def repo_for_mandate(workspace_id: str, mandate_id: str) -> str | None:
    """Read a mandate's bound repo path (the surface ``repo_id``), tenant-scoped.

    Used by the ``StationTaskDispatcher`` to pre-bind the ``/belt`` station to
    the mandate's repo on a queued station run. Returns ``None`` on a miss /
    cross-tenant id (a malformed id is a clean miss, not a 500)."""
    # no-event: read-only path; emit only on writes.
    try:
        doc = await MandateDoc.find_one(
            MandateDoc.workspace == workspace_id, MandateDoc.id == _as_object_id(mandate_id)
        )
    except Exception:  # noqa: BLE001 — malformed id == miss
        doc = None
    return doc.surface.repo_id if doc is not None else None


async def list_autopilot_enabled() -> list[dict[str, Any]]:
    """All ACTIVE mandates whose persisted ``autopilot.on`` is True — the
    startup reconciler's read (``autopilot.reconcile_autopilot_tasks``).

    DELIBERATELY cross-workspace: this is a SYSTEM boot read that re-derives the
    process-local background loops from the persisted flags, the same posture as
    the stale-run sweeper's all-workspace scan — not a user-facing query (the
    tenant-filter rule applies to request-path finds). Paused mandates are
    excluded: a paused mandate is inert, so its loop is not restarted (it
    resumes when autopilot is started again via the endpoint).

    Returns ``[{workspace_id, mandate_id, users}]``."""
    # no-event: read-only path; emit only on writes.
    docs = await MandateDoc.find(
        MandateDoc.autopilot.on == True,  # noqa: E712 — Beanie expression syntax
        MandateDoc.status == "active",
    ).to_list()
    return [
        {
            "workspace_id": d.workspace,
            "mandate_id": str(d.id),
            "users": int(d.autopilot.users) if d.autopilot else 3,
        }
        for d in docs
    ]


# Cadence → due-interval. A "weekly" mandate is due once its last shift is older
# than this; "manual" mandates are never scheduled (no entry → not due). Kept as a
# table so a later cadence value ("daily", etc.) only adds a row here.
_CADENCE_INTERVALS: dict[str, timedelta] = {"weekly": timedelta(days=7)}

# The SYSTEM actor a scheduled shift runs as (no human user_id on a cadence fire).
_SCHEDULER_ACTOR = "system:scheduler"


async def list_cadence_due(now: datetime) -> list[dict[str, Any]]:
    """All ACTIVE mandates whose charter cadence is DUE at ``now`` — the cadence
    scheduler's read (``scheduler.run_scheduler_tick``).

    A mandate is DUE when its cadence has a scheduling interval (currently only
    ``"weekly"`` → 7 days) AND its most recent shift's ``createdAt`` is older than
    that interval before ``now`` (a mandate that has NEVER shifted is always due).
    ``"manual"`` mandates have no interval, so they are never returned — the demo
    bar's manual-trigger path is untouched.

    DELIBERATELY cross-workspace: this is a SYSTEM sweeper read (same posture as
    ``list_autopilot_enabled`` / the stale-run sweeper), not a request-path find.
    Paused mandates are excluded — a paused mandate is inert. Returns
    ``[{workspace_id, mandate_id, user_id}]`` (``user_id`` is the SYSTEM actor the
    scheduler triggers shifts as).

    ``now`` is injected so the scheduler can pass a deterministic clock — tests
    never depend on wall time."""
    # no-event: read-only path; emit only on writes.
    # DEMO-BAR CONCESSION (N+1): one MandateDoc.find + one ShiftDoc.find per active
    # mandate. Fine at demo-bar mandate counts; at scale this should be a SINGLE
    # aggregation pipeline ($lookup the latest shift per mandate + a $match on the
    # cadence window). TODO: single aggregation pipeline before this runs against a
    # real tenant population.
    docs = await MandateDoc.find(MandateDoc.status == "active").to_list()
    due: list[dict[str, Any]] = []
    for doc in docs:
        interval = _CADENCE_INTERVALS.get(doc.charter.cadence)
        if interval is None:
            continue  # manual (or an unscheduled cadence) — never auto-fired
        last = (
            await ShiftDoc.find(
                ShiftDoc.workspace == doc.workspace, ShiftDoc.mandate_id == str(doc.id)
            )
            .sort("-no")
            .first_or_none()
        )
        if last is not None and _aware(last.createdAt) > (now - interval):
            continue  # shifted inside the cadence window — not due yet
        due.append(
            {
                "workspace_id": doc.workspace,
                "mandate_id": str(doc.id),
                "user_id": _SCHEDULER_ACTOR,
            }
        )
    return due


async def executor_revalidate(workspace_id: str, mandate_id: str) -> dict[str, Any]:
    """Approve-time re-validation read for the plan executor: does the mandate
    still exist, is it active, what is the CURRENT budget."""
    # no-event: read-only path; emit only on writes.
    try:
        doc = await MandateDoc.find_one(
            MandateDoc.workspace == workspace_id, MandateDoc.id == _as_object_id(mandate_id)
        )
    except Exception:  # noqa: BLE001 — malformed id == gone
        doc = None
    if doc is None:
        return {"exists": False, "active": False, "budget_max_tasks": 0}
    return {
        "exists": True,
        "active": doc.status == "active",
        "budget_max_tasks": doc.charter.budget.max_tasks_per_shift,
    }


async def mark_shift(
    *,
    workspace_id: str,
    shift_id: str,
    state: str,
    outcome: str | None = None,
    plan_action_id: str | None = None,
) -> None:
    """State-transition a shift row (tenant-scoped). Used by the trigger path
    and (via a best-effort wrapper) the plan executor + the router's reject
    hook. Unknown shift ids are a logged no-op — the Instinct outcome stays the
    source of truth."""
    try:
        doc = await ShiftDoc.find_one(
            ShiftDoc.workspace == workspace_id, ShiftDoc.id == _as_object_id(shift_id)
        )
    except Exception:  # noqa: BLE001
        doc = None
    if doc is None:
        logger.warning("mandate: shift %s not found for state write %s", shift_id, state)
        return
    doc.state = state  # type: ignore[assignment]
    if outcome is not None:
        doc.outcome = outcome
    if plan_action_id is not None:
        doc.plan_action_id = plan_action_id
    await doc.save()
    await emit(
        mandate_events.MandateShiftUpdated(
            data={
                "workspace_id": workspace_id,
                "mandate_id": doc.mandate_id,
                "shift_id": shift_id,
                "no": doc.no,
                "state": state,
            }
        )
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "create_mandate",
    "executor_revalidate",
    "file_feedback",
    "get_mandate",
    "get_pawprints",
    "list_autopilot_enabled",
    "list_mandates",
    "list_sightings",
    "mark_shift",
    "prepare_plan_resolution",
    "repo_for_mandate",
    "run_patrols",
    "set_autopilot",
    "shift_wire",
    "trigger_shift",
]
