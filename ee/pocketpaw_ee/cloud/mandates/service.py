# ee/pocketpaw_ee/cloud/mandates/service.py — MANDATE business logic.
#
# Sole owner of writes to MandateDoc / ShiftDoc / SightingDoc (the only module
# that imports those Beanie classes, per the 4-file entity rule).
#
# Public API (module-level ``async def op(workspace_id, user_id, body) -> dict``):
# create (the repo must sit inside the workspace's belt allowlist roots)/list/get
# mandates; file_feedback, list_sightings, run_patrols (patrols
# that accept ``workspace_id`` / ``user_id`` / ``upstream`` get them via
# signature inspection; sightings dedup on ``_dedup_signal``); trigger_shift
# (sense → foreman → plan gate); prepare_plan_resolution; get_pawprints;
# set_autopilot; set_crew (the roster; each NEW agent must be one the caller can
# read in this workspace, enabled); digest (the morning report, composed only
# from the read functions above plus the belt runs list).
#
# The CREW: a roster of cloud Agents on the mandate (``MandateDoc.crew``). The
# dispatcher seats each plan task on a dev (``crew_seat_for_task``: live dev
# seats in roster order, ``pick_dev`` round-robin by task index); the headless
# runner then reads that agent's CURRENT model + instructions
# (``crew_worker``) so an edit in the agent editor reaches the next develop.
# Every read is ``agents.service.get_for_viewer`` as the seat's ``seated_by``
# (the admin who seated that agent; a later roster save keeps it), so an agent
# that admin can no longer read is a gone seat. Agents are read through
# ``agents.service`` (never its Beanie doc).
#
# The BACKLOG (``_backlog``): a sighting stays open until a task citing it lands.
# The foreman and the digest both read it by joining the mandate's belt run rows
# (``plan_action_id`` + ``task_index``) to the ``belt_plan`` tasks' evidence refs;
# the shift trigger persists what it finds resolved on the sighting
# (``resolved_by_run``), because the runs list only reaches the newest actions.
# A task landed on the mandate's line is built either way; its ``line`` says
# whether the base holds it yet (``belt.executor.line_merged``).
#
# System/executor reads (no Beanie leaks out): repo_for_mandate,
# charter_for_mandate (the develop station's checks/recipes/goal read),
# crew_seat_for_task, crew_worker, file_station_sighting (the develop station's
# line conflict, deduped like a patrol's), list_autopilot_enabled,
# executor_revalidate, mark_shift, and list_cadence_due — the cadence
# scheduler's cross-workspace read of ACTIVE mandates whose cadence interval
# (daily = 1 day, weekly = 7 days; manual never) has elapsed since their last
# shift (N+1, one shift read per mandate; fine at current mandate counts).
#
# Conventions (cloud entity rules): validate body at entry
# (``Schema.model_validate(body)``); tenant filter ``workspace=...`` on EVERY
# request-path find; emit an event on every write (or ``# no-event: <reason>``);
# errors via ``_core.errors`` CloudError subclasses (never HTTPException). The
# Action-blob back-write goes through ``InstinctStore.update_parameters`` via the
# shared ``cloud/_core/proposals.update_action_blob``.

from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

from pocketpaw_ee.cloud._core.errors import NotFound, ValidationError
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud.mandates import events as mandate_events
from pocketpaw_ee.cloud.mandates.domain import (
    Budget,
    Charter,
    CrewMember,
    Kpi,
    MandateDoc,
    ShiftDoc,
    SightingDoc,
    Surface,
    UpstreamPin,
)
from pocketpaw_ee.cloud.mandates.dto import (
    CreateMandateRequest,
    CrewMemberRequest,
    FeedbackRequest,
    TeachingFeedbackRequest,
)

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


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
        checks=list(req.charter.checks),
        recipes=dict(req.charter.recipes),
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
        "checks": list(charter.checks),
        "recipes": dict(charter.recipes),
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


async def _require_allowed_repo(workspace_id: str, repo_id: str) -> None:
    """A mandate may only bind a repo inside the workspace's belt allowlist
    roots (settings ∪ the workspace's persisted roots, the console's view),
    judged on the resolved path so ``..`` or a symlink can't widen it. The
    develop station re-resolves against the settings allowlist at run time."""
    from pathlib import Path

    from pocketpaw_ee.cloud.belt import service as belt_service

    roots = await belt_service.resolve_allowlist_roots(workspace_id)
    try:
        resolved = Path(repo_id).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        resolved = None
    if resolved is None or not any(resolved.is_relative_to(root) for root in roots):
        raise ValidationError(
            "mandate.repo_not_allowed",
            f"surface.repo_id: {repo_id!r} is outside the workspace's allowed repo roots",
        )


async def create_mandate(workspace_id: str, user_id: str, body: Any) -> dict[str, Any]:
    """Create a new standing mandate. Charter body validated at entry; the
    bound repo must sit inside the workspace's allowed roots (422 otherwise)."""
    body = CreateMandateRequest.model_validate(body)
    await _require_allowed_repo(workspace_id, body.surface.repo_id)
    crew = await _crew_from_request(workspace_id, user_id, body.crew)

    doc = MandateDoc(
        workspace=workspace_id,
        name=body.name,
        surface=Surface(repo_id=body.surface.repo_id),
        charter=_charter_from_request(body),
        status="active",
        soul_path=body.soul_path,
        patrols=list(body.patrols),
        upstream=[UpstreamPin(repo=u.repo, pin_file=u.pin_file) for u in body.upstream],
        crew=crew,
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
    total sighting count. ``body`` is unused (read path). Three queries in all,
    whatever the mandate count: the mandates, then one shift aggregation and
    one sighting aggregation grouped by mandate, run concurrently."""
    # no-event: read-only path; emit only on writes.
    docs = await MandateDoc.find(MandateDoc.workspace == workspace_id).sort("-createdAt").to_list()
    ids = [str(d.id) for d in docs]
    shift_rows, sighting_rows = await asyncio.gather(
        _aggregate(
            ShiftDoc,
            [
                {"$match": {"workspace": workspace_id, "mandate_id": {"$in": ids}}},
                {"$sort": {"no": -1}},
                {
                    "$group": {
                        "_id": "$mandate_id",
                        "last_state": {"$first": "$state"},
                        "open_gates": {"$sum": {"$cond": [{"$eq": ["$state", "in_gate"]}, 1, 0]}},
                    }
                },
            ],
        ),
        _aggregate(
            SightingDoc,
            [
                {"$match": {"workspace": workspace_id, "mandate_id": {"$in": ids}}},
                {"$group": {"_id": "$mandate_id", "n": {"$sum": 1}}},
            ],
        ),
    )
    shifts = {r["_id"]: r for r in shift_rows}
    sightings = {r["_id"]: r["n"] for r in sighting_rows}
    out: list[dict[str, Any]] = []
    for doc in docs:
        mandate_id = str(doc.id)
        shift = shifts.get(mandate_id) or {}
        open_gate_count = shift.get("open_gates", 0)
        sighting_count = sightings.get(mandate_id, 0)
        out.append(
            {
                "id": mandate_id,
                "name": doc.name,
                "status": doc.status,
                "repo_id": doc.surface.repo_id,
                "cadence": doc.charter.cadence,
                "health": {
                    "last_shift_state": shift.get("last_state"),
                    "open_gate_count": open_gate_count,
                    "sighting_count": sighting_count,
                },
                "autopilot": _autopilot_to_wire(doc),
                "created_at": doc.createdAt,
            }
        )
    return {"mandates": out}


async def _aggregate(model: Any, pipeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run a raw aggregation. Motor's ``aggregate()`` returns a coroutine and
    mongomock-motor's a plain cursor, hence the ``isawaitable`` check (the
    repo's cross-driver idiom; Beanie's ``Document.aggregate`` breaks under the
    test harness)."""
    cursor = model.get_pymongo_collection().aggregate(pipeline)
    if inspect.isawaitable(cursor):
        cursor = await cursor
    return [row async for row in cursor]


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
        "upstream": [u.model_dump() for u in doc.upstream],
        "crew": [m.model_dump() for m in doc.crew],
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

    Generalized across patrols: a patrol that knows its own identity sets
    ``evidence.dedup_key`` (``upstream`` keys on repo + pin + upstream head, so a
    quiet day files nothing new); ``issues`` carry ``evidence.iid`` (stable across
    a retitle), ``deps`` carry ``evidence.package``; anything else falls back to
    the summary. Without the ``iid`` branch an issue retitle would change the
    summary and double-persist the same issue."""
    ev = evidence or {}
    signal = ev.get("dedup_key") or ev.get("iid") or ev.get("package") or summary or ""
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
            if "upstream" in params:
                kwargs["upstream"] = [u.model_dump() for u in doc.upstream]
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


async def file_station_sighting(
    workspace_id: str, mandate_id: str, draft: dict[str, Any]
) -> dict[str, Any] | None:
    """A sighting a factory station files (the develop station's base conflict
    on a mandate's line). ``draft`` is shaped like a patrol's (``patrol``,
    ``severity``, ``summary``, ``evidence``) and dedups like ``run_patrols``:
    ``None`` when the same signal from the same patrol is already on file."""
    await _fetch_mandate(workspace_id, mandate_id)
    patrol = str(draft.get("patrol") or "station")
    evidence = dict(draft.get("evidence") or {})
    summary = str(draft.get("summary") or "")[:280]
    signal = _dedup_signal(evidence, summary)
    existing = await SightingDoc.find(
        SightingDoc.workspace == workspace_id,
        SightingDoc.mandate_id == mandate_id,
        SightingDoc.patrol == patrol,
    ).to_list()
    if any(_dedup_signal(s.evidence, s.summary) == signal for s in existing):
        return None  # no-event: nothing written
    sighting = SightingDoc(
        workspace=workspace_id,
        mandate_id=mandate_id,
        patrol=patrol,
        severity=int(draft.get("severity") or 3),
        summary=summary,
        evidence=evidence,
    )
    await sighting.insert()
    await emit(
        mandate_events.MandateSightingAdded(
            data={
                "workspace_id": workspace_id,
                "mandate_id": mandate_id,
                "sighting_id": str(sighting.id),
                "patrol": patrol,
                "severity": sighting.severity,
            }
        )
    )
    return _sighting_to_wire(sighting)


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
        # With several web processes only the lease holder runs loops; it hears
        # about this start through announce_change below.
        if autopilot_mod.runs_here():
            await autopilot_mod.start_autopilot(
                workspace_id, mandate_id, users, run_immediate=False
            )
    else:  # stop
        await autopilot_mod.stop_autopilot(mandate_id)
        users = doc.autopilot.users if doc.autopilot else 3
        doc.autopilot = Autopilot(on=False, users=users)
        await doc.save()
    autopilot_mod.announce_change(mandate_id)

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


# ---------------------------------------------------------------------------
# Crew — cloud Agents seated on the mandate
# ---------------------------------------------------------------------------


async def _crew_from_request(
    workspace_id: str,
    user_id: str,
    members: list[CrewMemberRequest],
    seated: list[CrewMember] | None = None,
) -> list[CrewMember]:
    """The roster to store, vouched per seat. A seat whose agent is already on
    the stored roster (``seated``) keeps its ``seated_by``: that admin vouched
    for it and develops still read the agent as them, so another admin's edit
    neither re-reads it as themselves nor trips on an agent they can't see (or
    one deleted since). A NEW agent must be one the caller can read, live in
    this workspace (a public agent from another workspace is refused, so a
    leaked id never seats a foreign agent) and be enabled; 422 on a miss. The
    caller is stamped as each new seat's ``seated_by``. Role, concurrency and
    setup always come from the request."""
    from pocketpaw_ee.cloud.agents import service as agents_service

    vouched = {m.agent_id: m.seated_by for m in seated or []}
    for m in members:
        if m.agent_id in vouched:
            continue
        try:
            agent = await agents_service.get_for_viewer(m.agent_id, workspace_id, user_id)
        except NotFound:
            agent = None
        if agent is None or agent.workspace_id != workspace_id:
            raise ValidationError(
                "mandate.crew_agent_not_found",
                f"crew: agent {m.agent_id!r} is not an agent in this workspace",
            )
        if agent.disabled:
            raise ValidationError(
                "mandate.crew_agent_disabled",
                f"crew: agent {m.agent_id!r} is disabled; enable it before seating it",
            )
    return [
        CrewMember(**m.model_dump(), seated_by=vouched.get(m.agent_id, user_id)) for m in members
    ]


async def set_crew(workspace_id: str, user_id: str, mandate_id: str, body: Any) -> dict[str, Any]:
    """Replace a mandate's crew roster. Body: ``{crew: [{agent_id, role,
    concurrency, setup?}]}``. Returns ``{"mandate": <detail>}`` (the autopilot
    envelope). Seats already on the roster carry over as whoever seated them;
    a bad body or a new agent the caller can't read, outside this workspace, or
    disabled is a 422; a cross-tenant mandate a 404. Removing a seat always
    works."""
    from pydantic import ValidationError as PydanticValidationError

    from pocketpaw_ee.cloud.mandates.dto import SetCrewRequest

    try:
        req = SetCrewRequest.model_validate(body)
    except PydanticValidationError as exc:
        raise ValidationError("mandate.crew_invalid", _first_pydantic_msg(exc)) from exc

    doc = await _fetch_mandate(workspace_id, mandate_id)
    doc.crew = await _crew_from_request(workspace_id, user_id, req.crew, doc.crew)
    await doc.save()
    await emit(
        mandate_events.MandateCrewChanged(
            data={
                "workspace_id": workspace_id,
                "mandate_id": mandate_id,
                "crew": [m.model_dump() for m in doc.crew],
            }
        )
    )
    logger.info(
        "mandate: crew set on %s (workspace=%s, %d seat(s))",
        mandate_id,
        workspace_id,
        len(doc.crew),
    )
    return {"mandate": await _mandate_detail_wire(doc)}


def pick_dev(devs: list[_T], index: int) -> _T | None:
    """The SEAT RULE: plan task ``index`` (1-based, the plan's order) goes to
    dev ``(index - 1) % len(devs)`` — round-robin in roster order, so two devs
    split a two-task shift. ``None`` when there are no devs."""
    return devs[(index - 1) % len(devs)] if devs else None


async def _live_agent(workspace_id: str, agent_id: str, seated_by: str) -> Any | None:
    """The agent behind a seat, or ``None`` when it is gone, disabled, no
    longer in this workspace, or no longer readable by ``seated_by`` (the same
    visibility-checked read the roster route used to seat it)."""
    from pocketpaw_ee.cloud.agents import service as agents_service

    try:
        agent = await agents_service.get_for_viewer(agent_id, workspace_id, seated_by or None)
    except NotFound:
        return None
    if agent.workspace_id != workspace_id or agent.disabled:
        return None
    return agent


async def crew_seat_for_task(
    workspace_id: str, mandate_id: str, index: int
) -> dict[str, Any] | None:
    """The dev seat plan task ``index`` runs on: ``{agent_id, name, setup,
    seated_by}``,
    or ``None`` (no crew, or no live dev) so the factory's env defaults apply.
    Dead seats (deleted / disabled agents) are skipped before ``pick_dev``."""
    # no-event: read-only path; emit only on writes.
    try:
        doc = await MandateDoc.find_one(
            MandateDoc.workspace == workspace_id, MandateDoc.id == _as_object_id(mandate_id)
        )
    except Exception:  # noqa: BLE001 — malformed id == miss
        doc = None
    if doc is None:
        return None
    live = []
    for seat in doc.crew:
        if seat.role != "dev":
            continue
        agent = await _live_agent(workspace_id, seat.agent_id, seat.seated_by)
        if agent is not None:
            live.append((seat, agent))
    picked = pick_dev(live, index)
    if picked is None:
        return None
    seat, agent = picked
    return {
        "agent_id": seat.agent_id,
        "name": agent.name,
        "setup": seat.setup or "",
        "seated_by": seat.seated_by,
    }


async def crew_worker(workspace_id: str, agent_id: str, seated_by: str) -> dict[str, str] | None:
    """A seated worker's CURRENT settings for a develop: ``{name, model,
    instructions}`` (the agent's ``config.model`` and ``config.system_prompt``),
    or ``None`` when the agent is gone, disabled or no longer readable by the
    admin who seated it (``seated_by``). Read at develop time, not
    stored on the run, so a private agent's instructions never land on a run
    blob and an editor change applies to the next develop."""
    # no-event: read-only path; emit only on writes.
    agent = await _live_agent(workspace_id, agent_id, seated_by)
    if agent is None:
        return None
    return {
        "name": agent.name,
        "model": agent.config.model,
        "instructions": agent.config.system_prompt,
    }


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
# Backlog — open sightings and the planned tasks that cite them
# ---------------------------------------------------------------------------

# The foreman sees at most this many open sightings (highest severity, then
# oldest); the prompt says how many it left out.
_BACKLOG_CAP = 30

# Task statuses that mean someone is still working the task: the foreman must
# not plan it again.
_IN_FLIGHT = frozenset(
    {"queued", "developing", "approved", "pending at gate", "pending at plan gate"}
)

# A planned task with no run reads its status off the plan Action.
_PLAN_STATUS = {
    "pending": "pending at plan gate",
    "approved": "dispatched",
    "executed": "dispatched",
    "rejected": "plan rejected",
    "failed": "plan failed",
}


def _task_status(run: dict[str, Any]) -> str:
    """A run row's status in the foreman's words. A queued run whose headless
    develop failed waits for a human, so it is ``develop failed``, not in
    flight."""
    status = str(run.get("status") or "")
    if status == "queued":
        if run.get("headless_error"):
            return "develop failed"
        return "developing" if run.get("headless_state") else "queued"
    return "pending at gate" if status == "proposed" else status


async def _line_state(run: dict[str, Any] | None, line_merged: Any) -> str | None:
    """Where a landed run's commit stands when it landed on its mandate's line:
    in the base yet, or on the line awaiting the captain's merge. Either way it
    is built; ``None`` for a run that did not land on a line."""
    run = run or {}
    line, base = str(run.get("branch") or ""), str(run.get("base_branch") or "")
    if not line.startswith("belt/line/"):
        return None
    try:
        merged = await line_merged(
            str(run.get("repo") or ""), base, str(run.get("commit_sha") or "")
        )
    except Exception:  # noqa: BLE001 — an unreadable repo reads as not merged yet
        logger.debug("mandate: line state read failed", exc_info=True)
        merged = False
    return f"merged into {base}" if merged else f"on the line {line}, awaiting merge into {base}"


def _is_gate_teaching(s: SightingDoc) -> bool:
    """A gate rejection/edit filed as a sighting is shift history, not backlog
    work: no task can resolve it. Without a ``shift_no`` it stays in the
    backlog, where the foreman still reads it."""
    ev = s.evidence or {}
    return ev.get("source") == "gate" and ev.get("shift_no") is not None


async def _planned_tasks(
    workspace_id: str, plan_ids: list[str], runs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Every task of the given ``belt_plan`` Actions with its outcome, oldest
    shift first: ``{shift_no, title, evidence_refs, status, in_flight, error,
    run_id}``.

    A task's run is the newest run row carrying its ``(plan_action_id,
    task_index)``; ``task_index`` is 1-based into the plan's CURRENT tasks (the
    executor dispatches the list a resolve kept). A task with no run takes the
    plan Action's status (pending at the plan gate, rejected, ...)."""
    from pocketpaw.stores import get_instinct_store
    from pocketpaw_ee.cloud.belt.executor import line_merged
    from pocketpaw_ee.cloud.mandates.executor import BELT_PLAN_PARAM_KEY

    newest: dict[tuple[str, int], dict[str, Any]] = {}
    for r in runs:  # list_runs is newest-first
        if r.get("plan_action_id") and r.get("task_index") is not None:
            newest.setdefault((str(r["plan_action_id"]), int(r["task_index"])), r)

    # ISO: HTTP / scheduler path (no ContextVar) — scope to the caller.
    store = get_instinct_store(workspace_id=workspace_id or None)
    out: list[dict[str, Any]] = []
    for plan_id in plan_ids:
        try:
            action = await store.get_action(plan_id)
        except Exception:  # noqa: BLE001 — an unreadable plan drops its tasks, never the shift
            logger.debug("mandate: plan read failed for %s", plan_id, exc_info=True)
            continue
        blob = (getattr(action, "parameters", None) or {}).get(BELT_PLAN_PARAM_KEY)
        if not isinstance(blob, dict):
            continue
        plan_status = str(getattr(getattr(action, "status", None), "value", "") or "")
        for i, task in enumerate((blob.get("plan") or {}).get("tasks") or [], start=1):
            run = newest.get((plan_id, i))
            status = (
                _task_status(run)
                if run
                else _PLAN_STATUS.get(plan_status, plan_status or "unknown")
            )
            out.append(
                {
                    "shift_no": int(blob.get("shift_no") or 0),
                    "title": str(task.get("title") or ""),
                    "evidence_refs": [str(ref) for ref in task.get("evidence_refs") or []],
                    "status": status,
                    "in_flight": status in _IN_FLIGHT,
                    "error": (run or {}).get("error"),
                    "run_id": (run or {}).get("action_id"),
                    "line": await _line_state(run, line_merged) if status == "landed" else None,
                }
            )
    out.sort(key=lambda t: t["shift_no"])
    return out


async def _backlog(
    workspace_id: str,
    mandate_id: str,
    runs: list[dict[str, Any]],
    *,
    since: datetime | None = None,
    history_plan_ids: tuple[str, ...] = (),
    persist: bool = False,
) -> dict[str, Any]:
    """The mandate's OPEN sightings and the planned tasks behind them.

    A sighting is resolved once a task citing it LANDS. Resolution is computed
    here by joining this mandate's run rows to their plan tasks, and
    ``persist=True`` (the shift trigger) records it on the sighting, because the
    runs list only reaches the workspace's newest actions and an old landed run
    would otherwise fall out of it and reopen its sightings. A failed, rejected
    or still-running task leaves its sightings open.

    Returns ``{"open": [...], "tasks": [...], "teaching": [...]}``: open entries
    (highest severity, then oldest) are ``{id, patrol, severity, summary, new,
    in_flight, tasks}``, where ``new`` means filed after ``since`` and ``tasks``
    lists the citing tasks (``{shift_no, title, status}``); ``tasks`` covers the
    plans behind this mandate's runs, every plan still at the plan gate (in
    flight, however many shifts ago), and ``history_plan_ids``; ``teaching`` is
    the gate teaching sightings as ``{shift_no, note}`` (shift history, never
    backlog)."""
    sightings = await SightingDoc.find(
        SightingDoc.workspace == workspace_id, SightingDoc.mandate_id == mandate_id
    ).to_list()
    gated = await ShiftDoc.find(
        ShiftDoc.workspace == workspace_id,
        ShiftDoc.mandate_id == mandate_id,
        ShiftDoc.state == "in_gate",
    ).to_list()
    plan_ids = {str(r["plan_action_id"]) for r in runs if r.get("plan_action_id")}
    plan_ids.update(p for p in (*history_plan_ids, *(g.plan_action_id for g in gated)) if p)
    tasks = await _planned_tasks(workspace_id, sorted(plan_ids), runs)

    landed_by: dict[str, str] = {}
    citing: dict[str, list[dict[str, Any]]] = {}
    for t in tasks:
        for ref in t["evidence_refs"]:
            citing.setdefault(ref, []).append(t)
            if t["status"] == "landed" and t["run_id"]:
                landed_by.setdefault(ref, str(t["run_id"]))

    open_docs: list[SightingDoc] = []
    teaching: list[dict[str, Any]] = []
    for s in sightings:
        sid = str(s.id)
        if s.resolved_by_run:
            continue
        if _is_gate_teaching(s):
            ev = s.evidence
            title = f' "{ev["task_title"]}"' if ev.get("task_title") else ""
            note = f"{ev.get('kind', 'note')}{title}: {ev.get('reason') or s.summary}"
            teaching.append({"shift_no": int(ev["shift_no"]), "note": note})
            continue
        if sid in landed_by:
            if persist:
                # no-event: bookkeeping on the sighting row; the landed run
                # already announced itself on belt_run_updated.
                s.resolved_by_run = landed_by[sid]
                s.resolved_at = _utcnow()
                await s.save()
            continue
        open_docs.append(s)
    open_docs.sort(key=lambda s: (-int(s.severity), _aware(s.ts)))

    entries = []
    for s in open_docs:
        cites = [
            {"shift_no": t["shift_no"], "title": t["title"], "status": t["status"]}
            for t in citing.get(str(s.id), [])
        ]
        entries.append(
            {
                "id": str(s.id),
                "patrol": s.patrol,
                "severity": s.severity,
                "summary": s.summary,
                "new": since is None or _aware(s.ts) > _aware(since),
                "in_flight": any(c["status"] in _IN_FLIGHT for c in cites),
                "tasks": cites,
            }
        )
    return {"open": entries, "tasks": tasks, "teaching": teaching}


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

    # 3. JUDGE — assemble the context and make the ONE foreman call. The
    #    foreman reads the open backlog (every sighting no landed task has
    #    resolved), not just what arrived since the last shift.
    from pocketpaw_ee.cloud.belt import service as belt_service
    from pocketpaw_ee.cloud.belt.orient import c4_lines

    charter_wire = _charter_to_wire(doc.charter)
    since = last.createdAt if last else None
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
    runs = [
        r
        for r in (await belt_service.list_runs(workspace_id))["runs"]
        if r.get("mandate_id") == mandate_id
    ]
    backlog = await _backlog(
        workspace_id,
        mandate_id,
        runs,
        since=since,
        history_plan_ids=tuple(h.plan_action_id for h in history_docs if h.plan_action_id),
        persist=True,
    )
    history = [
        {
            "no": h.no,
            "state": h.state,
            "outcome": h.outcome,
            "tasks": [t for t in backlog["tasks"] if t["shift_no"] == h.no],
            "gate": [g["note"] for g in backlog["teaching"] if g["shift_no"] == h.no],
        }
        for h in reversed(history_docs)
    ]
    soul_context = await soul_link.recall_for_planning(
        doc.soul_path, f"{doc.name} {doc.charter.goal}"
    )

    context = foreman_mod.ForemanContext(
        shift_no=shift_no,
        charter=charter_wire,
        sightings=backlog["open"][:_BACKLOG_CAP],
        open_total=len(backlog["open"]),
        history=history,
        line=[
            {"shift_no": t["shift_no"], "title": t["title"], "state": t["line"]}
            for t in backlog["tasks"]
            if t.get("line")
        ],
        soul_context=soul_context,
        architecture=c4_lines(doc.surface.repo_id),
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
    # ISO: HTTP path (no ``current_workspace`` ContextVar) — scope to the caller.
    store = get_instinct_store(workspace_id=workspace_id or None)
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
    correct). Store-API write — the same pattern the belt MCP propose uses.
    Best-effort."""

    from pocketpaw_ee.cloud._core.proposals import update_action_blob
    from pocketpaw_ee.cloud.mandates.executor import BELT_PLAN_PARAM_KEY

    try:
        await update_action_blob(
            store=store,
            action_id=action_id,
            param_key=BELT_PLAN_PARAM_KEY,
            updates={"proposed_event_id": proposed_event_id},
        )
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
    from pydantic import ValidationError as PydanticValidationError

    from pocketpaw.stores import get_instinct_store
    from pocketpaw_ee.cloud.mandates.dto import ResolvePlanRequest
    from pocketpaw_ee.cloud.mandates.executor import BELT_PLAN_PARAM_KEY

    # A bad body (e.g. an empty decisions list) is a 422, not a 500: the cloud
    # error handler only maps CloudError.
    try:
        body = ResolvePlanRequest.model_validate(body)
    except PydanticValidationError as exc:
        raise ValidationError("mandate.plan_resolve_invalid", _first_pydantic_msg(exc)) from exc
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

    # ISO: HTTP path (no ``current_workspace`` ContextVar) — scope to the caller.
    store = get_instinct_store(workspace_id=workspace_id or None)
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
            # ISO: HTTP path (no ContextVar) — scope to the caller's workspace.
            _store = get_instinct_store(workspace_id=workspace_id or None)
            action = await _store.get_action(doc.plan_action_id)
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

    # ISO: HTTP path (no ``current_workspace`` ContextVar) — scope to the caller.
    store = get_instinct_store(workspace_id=workspace_id or None)
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
# Digest — the workspace's morning report over the existing read models
# ---------------------------------------------------------------------------

_DIGEST_TOP_SIGHTINGS = 5


def _ts_utc(value: Any) -> datetime | None:
    """A read-model timestamp (datetime or ISO string, naive = UTC) as an aware
    UTC datetime; ``None`` when it can't be read."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    return _aware(value).astimezone(UTC) if isinstance(value, datetime) else None


def _since(value: Any, since: datetime) -> bool:
    ts = _ts_utc(value)
    return ts is not None and ts >= since


def _run_digest_row(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "action_id": run.get("action_id"),
        "status": run.get("status"),
        "title": str(run.get("title") or run.get("task") or run.get("summary") or "").split(
            "\n", 1
        )[0][:200],
        "pr_url": run.get("pr_url"),
        "branch": run.get("branch"),
        "commit_sha": run.get("commit_sha"),
        "headless_error": run.get("headless_error"),
        "headless_state": run.get("headless_state"),
        "error": run.get("error"),
    }


async def digest(workspace_id: str, user_id: str, body: Any = None) -> dict[str, Any]:
    """The workspace's mandate digest since ``since`` (default 24 hours ago).

    Per mandate: new sightings (count + top 5 by severity), the open backlog
    (every sighting no landed task has resolved, any age: count + top 5 by
    severity then age, each flagged ``in_flight``), shifts and runs
    created since, the gates still waiting on a human (in-gate plans and
    per-diff runs at ``proposed``) and ``stuck`` runs (queued with a
    ``headless_error``, or still marked ``headless_state`` by a background
    develop that never finished) — those two whatever their age. Built only from the
    existing read models — ``list_mandates``, ``get_mandate``, ``shift_wire``,
    ``list_sightings`` and the belt runs list — so the digest can never disagree
    with the console, plus ``_backlog`` (read-only here), so it agrees with what
    the next shift's foreman sees. ``totals`` sums the workspace."""
    # no-event: read-only path; emit only on writes.
    from pydantic import ValidationError as PydanticValidationError

    from pocketpaw_ee.cloud.belt import service as belt_service
    from pocketpaw_ee.cloud.mandates.dto import DigestRequest

    try:
        req = DigestRequest.model_validate(body or {})
    except PydanticValidationError as exc:
        raise ValidationError("mandate.digest_invalid", _first_pydantic_msg(exc)) from exc
    now = _utcnow()
    since = _aware(req.since).astimezone(UTC) if req.since else now - timedelta(days=1)

    mandates = (await list_mandates(workspace_id, user_id))["mandates"]
    runs = (await belt_service.list_runs(workspace_id))["runs"]

    out: list[dict[str, Any]] = []
    totals = {
        "mandates": len(mandates),
        "new_sightings": 0,
        "shifts": 0,
        "runs": 0,
        "landed": 0,
        "failed": 0,
        "gates_waiting": 0,
        "open_backlog": 0,
    }
    for m in mandates:
        mandate_id = m["id"]
        detail = await get_mandate(workspace_id, user_id, mandate_id)
        sightings = (await list_sightings(workspace_id, user_id, mandate_id))["sightings"]
        fresh = [s for s in sightings if _since(s["ts"], since)]
        top = sorted(fresh, key=lambda s: -int(s["severity"]))[:_DIGEST_TOP_SIGHTINGS]

        shifts: list[dict[str, Any]] = []
        plan_gates: list[dict[str, Any]] = []
        for row in detail["recent_shifts"]:
            is_new = _since(row["created_at"], since)
            if not is_new and row["state"] != "in_gate":
                continue
            wire = await shift_wire(workspace_id, row["id"])
            if is_new:
                shifts.append({k: wire[k] for k in ("no", "state", "outcome", "task_count")})
            if wire["state"] == "in_gate":
                plan_gates.append(
                    {
                        "shift_no": wire["no"],
                        "plan_action_id": wire["plan_action_id"],
                        "task_count": wire["task_count"],
                    }
                )

        mine = [r for r in runs if r.get("mandate_id") == mandate_id]
        backlog = (await _backlog(workspace_id, mandate_id, mine))["open"]
        new_runs = [_run_digest_row(r) for r in mine if _since(r.get("created_at"), since)]
        diff_gates = [_run_digest_row(r) for r in mine if r.get("status") == "proposed"]
        # A headless develop that failed (or never finished: a restart drops
        # the in-memory queue) leaves its run queued for a human, so it is
        # reported whatever its age, like a gate.
        stuck = [
            _run_digest_row(r)
            for r in mine
            if r.get("status") == "queued" and (r.get("headless_error") or r.get("headless_state"))
        ]

        totals["new_sightings"] += len(fresh)
        totals["shifts"] += len(shifts)
        totals["runs"] += len(new_runs)
        totals["landed"] += sum(1 for r in new_runs if r["status"] == "landed")
        totals["failed"] += sum(
            1 for r in new_runs if r["status"] == "failed" or r["headless_error"]
        )
        totals["gates_waiting"] += len(plan_gates) + len(diff_gates)
        totals["open_backlog"] += len(backlog)
        out.append(
            {
                "id": mandate_id,
                "name": m["name"],
                "status": m["status"],
                "cadence": m["cadence"],
                "sightings": {
                    "count": len(fresh),
                    "top": [
                        {"title": s["summary"], "severity": s["severity"], "patrol": s["patrol"]}
                        for s in top
                    ],
                },
                "backlog": {
                    "count": len(backlog),
                    "top": [
                        {
                            "title": s["summary"],
                            "severity": s["severity"],
                            "patrol": s["patrol"],
                            "in_flight": s["in_flight"],
                        }
                        for s in backlog[:_DIGEST_TOP_SIGHTINGS]
                    ],
                },
                "shifts": shifts,
                "runs": new_runs,
                "gates": {"plans": plan_gates, "diffs": diff_gates},
                "stuck": stuck,
            }
        )
    return {
        "since": since.isoformat(),
        "generated_at": now.isoformat(),
        "mandates": out,
        "totals": totals,
    }


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


async def charter_for_mandate(workspace_id: str, mandate_id: str) -> dict[str, Any] | None:
    """Read a mandate's charter (wire dict) plus its bound repo, tenant-scoped.

    The headless develop station's read: it needs the charter's ``checks``,
    ``recipes``, ``goal``, ``boundaries`` and ``says_no``. Returns
    ``{"repo": ..., "charter": {...}}`` or ``None`` on a miss / cross-tenant id."""
    # no-event: read-only path; emit only on writes.
    try:
        doc = await MandateDoc.find_one(
            MandateDoc.workspace == workspace_id, MandateDoc.id == _as_object_id(mandate_id)
        )
    except Exception:  # noqa: BLE001 — malformed id == miss
        doc = None
    if doc is None:
        return None
    return {"repo": doc.surface.repo_id, "charter": _charter_to_wire(doc.charter)}


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


# Cadence → due-interval. A mandate is due once its last shift is older than its
# interval; "manual" mandates are never scheduled (no entry → not due).
_CADENCE_INTERVALS: dict[str, timedelta] = {
    "daily": timedelta(days=1),
    "weekly": timedelta(days=7),
}

# The SYSTEM actor a scheduled shift runs as (no human user_id on a cadence fire).
_SCHEDULER_ACTOR = "system:scheduler"


async def list_cadence_due(now: datetime) -> list[dict[str, Any]]:
    """All ACTIVE mandates whose charter cadence is DUE at ``now`` — the cadence
    scheduler's read (``scheduler.run_scheduler_tick``).

    A mandate is DUE when its cadence has a scheduling interval (``"daily"`` → 1
    day, ``"weekly"`` → 7 days) AND its most recent shift's ``createdAt`` is older than
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
    "charter_for_mandate",
    "crew_seat_for_task",
    "crew_worker",
    "create_mandate",
    "digest",
    "executor_revalidate",
    "file_feedback",
    "get_mandate",
    "get_pawprints",
    "list_autopilot_enabled",
    "list_cadence_due",
    "list_mandates",
    "list_sightings",
    "mark_shift",
    "pick_dev",
    "prepare_plan_resolution",
    "repo_for_mandate",
    "run_patrols",
    "set_autopilot",
    "set_crew",
    "shift_wire",
    "trigger_shift",
]
