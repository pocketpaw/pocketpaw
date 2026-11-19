# ee/pocketpaw_ee/cloud/growth/social/blueprint.py — the Brand Blueprint service
# and the SOLE writer of the Blueprint docs: the per-unit ``growth_blueprint_units``
# and the legacy whole-page ``growth_blueprints`` (read-only now, kept for one
# release). Service-is-repo; the import-linter "Growth" contract keeps every other
# module off both doc classes.
#
# A brand's Blueprint is a set of units (``brand``, ``strategy``, ``plan``,
# ``play:<id>`` for a ``play_catalog`` id), each its own version chain: at most one
# ``approved`` and one ``draft`` per unit, older versions ``superseded``. Unit
# routes (``units``, ``save_unit_draft``, ``approve_unit``, ``unit_diff``) edit one
# unit; a save carries the version it started from and a stale one is a 409
# ``social.blueprint_stale``. A plan phase or play naming a goal the strategy
# (draft, else approved) lacks is a 422 on save; on approve the check is against
# the approved strategy, and approving a strategy that drops a goal a plan or play
# still names is a 422 ``social.blueprint_goal_in_use`` listing those units.
#
# ``approved_for`` (the one read the drafting agents use) stitches the approved
# units back into the legacy ``GrowthBlueprint`` fields (``_to_legacy``): it is None
# unless brand AND strategy are approved, ``version`` is the strategy unit's, and
# each channel's cadence is the sum over its active (core / testing) acting plays.
# The whole-page routes (``get``, ``seed``, ``save_draft``, ``regenerate``,
# ``approve``, ``diff``) are wrappers over the units: a save splits the sections
# sent into drafts of the units they change (``_apply_legacy``), approve approves
# every unit with a draft (strategy last), and diff compares the composed approved
# with the composed draft whatever versions were asked for.
#
# Migration: ``migrate_profile`` splits a brand's legacy approved and draft rows
# into units at their legacy version numbers. Every write migrates the brand first;
# reads of a brand with no unit rows fall back to its legacy rows (never writing).
#
# Numbers are never model-written: pillar weights are owner-entered or an equal
# split computed here, cadences come from the profile or the owner, goals are
# owner-entered only. ``seed`` and ``regenerate`` text must pass
# ``ideas.check_texts`` (no em or en dash, no number the owner did not type); a
# first two-phase roadmap is seeded when there is none. Writes to an existing row
# are conditional on its status; approve marks a draft approved before superseding
# the rest, so a crash leaves two approved (the highest version wins on read),
# never zero; the unique version index stops two writers forking a version.
# Tenancy: every query filters on ``workspace`` and ``profile_id`` (the profile
# comes from ``social.service.require_profile``) except the migration's discovery
# and count reads, marked ``global-read``. Writes carry ``# no-event:`` markers.

from __future__ import annotations

import copy
import inspect
import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from pydantic import ValidationError as PydanticValidationError
from pymongo.errors import DuplicateKeyError

from pocketpaw_ee.cloud._core.context import RequestContext
from pocketpaw_ee.cloud._core.errors import CloudError, ConflictError, NotFound, ValidationError
from pocketpaw_ee.cloud._core.time import iso_utc
from pocketpaw_ee.cloud.growth.social import analyst as social_analyst
from pocketpaw_ee.cloud.growth.social import ideas as social_ideas
from pocketpaw_ee.cloud.growth.social import play_catalog
from pocketpaw_ee.cloud.growth.social.analyst import as_list, typed_items
from pocketpaw_ee.cloud.growth.social.blueprint_units_dto import (
    BODY_MODELS,
    BlueprintUnitDiffResponse,
    BlueprintUnitRowResponse,
    BlueprintUnitsResponse,
    BlueprintUnitStateResponse,
    BranchResponse,
    PlayCatalogResponse,
    PlayResponse,
    SaveUnitDraftRequest,
)
from pocketpaw_ee.cloud.growth.social.domain import (
    BLUEPRINT_CHANNELS,
    BLUEPRINT_SECTIONS,
    MAX_BLUEPRINT_ITEMS,
    MAX_BLUEPRINT_PILLARS,
    MAX_ICP_SENTENCES,
    AnalysisRequest,
    BlueprintAudience,
    BlueprintChannel,
    BlueprintGoal,
    BlueprintIdentity,
    BlueprintPillar,
    BlueprintRoadmap,
    BlueprintVoice,
    GrowthBlueprint,
    RoadmapCadence,
    RoadmapMilestone,
    RoadmapPhase,
    SocialAnalysis,
    SocialProfile,
)
from pocketpaw_ee.cloud.growth.social.dto import (
    MAX_WEEKLY_POST_QUOTA,
    BlueprintAudienceModel,
    BlueprintChannelModel,
    BlueprintDiffResponse,
    BlueprintGoalModel,
    BlueprintIdentityModel,
    BlueprintPillarModel,
    BlueprintRoadmapModel,
    BlueprintSectionDiff,
    BlueprintStateResponse,
    BlueprintVoiceModel,
    GrowthBlueprintResponse,
    RoadmapCadenceModel,
    RoadmapMilestoneModel,
    RoadmapPhaseModel,
    SaveBlueprintDraftRequest,
)
from pocketpaw_ee.cloud.growth.social.play_catalog import PLAY_IDS, PLAYS
from pocketpaw_ee.cloud.models.growth_blueprint import GrowthBlueprint as _BlueprintDoc
from pocketpaw_ee.cloud.models.growth_blueprint_unit import GrowthBlueprintUnit as _UnitDoc

logger = logging.getLogger(__name__)

_ITEM_CHARS = 300
_PILLAR_CHARS = 80


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _strs(value: Any) -> tuple[str, ...]:
    return tuple(v for v in value if isinstance(v, str)) if isinstance(value, list) else ()


def _int(value: Any) -> int:
    return int(value) if isinstance(value, int | float) else 0


def _dicts(value: Any) -> list[dict]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _phase(p: dict) -> RoadmapPhase | None:
    """A stored phase, or None when its dates no longer parse."""
    try:
        return RoadmapPhase(
            id=_str(p.get("id")),
            name=_str(p.get("name")),
            start=date.fromisoformat(p["start"]),
            end=date.fromisoformat(p["end"]),
            cadence=tuple(
                RoadmapCadence(
                    channel=_str(c.get("channel")), posts_per_week=_int(c.get("posts_per_week"))
                )
                for c in _dicts(p.get("cadence"))
            ),
            goal_ids=_strs(p.get("goal_ids")),
            milestones=tuple(
                RoadmapMilestone(
                    id=_str(m.get("id")),
                    title=_str(m.get("title")),
                    date=date.fromisoformat(m["date"]),
                    status=_str(m.get("status")) or "planned",
                    link=_str(m.get("link")) or None,
                )
                for m in _dicts(p.get("milestones"))
            ),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _roadmap(value: Any) -> BlueprintRoadmap:
    phases = (_phase(p) for p in _dicts((value or {}).get("phases")))
    return BlueprintRoadmap(phases=tuple(p for p in phases if p is not None))


def _domain(sections: dict[str, Any], **meta: Any) -> GrowthBlueprint:
    """A domain Blueprint from legacy-shaped ``sections`` plus row metadata."""
    identity = sections.get("identity") or {}
    audience = sections.get("audience") or {}
    voice = sections.get("voice") or {}
    return GrowthBlueprint(
        identity=BlueprintIdentity(
            summary=_str(identity.get("summary")),
            product=_str(identity.get("product")),
            problem=_str(identity.get("problem")),
        ),
        audience=BlueprintAudience(
            segments=_strs(audience.get("segments")),
            icp_sentences=_strs(audience.get("icp_sentences")),
        ),
        voice=BlueprintVoice(tone=_str(voice.get("tone")), avoid=_strs(voice.get("avoid"))),
        pillars=tuple(
            BlueprintPillar(
                name=_str(p.get("name")), weight=_int(p.get("weight")), hooks=_strs(p.get("hooks"))
            )
            for p in _dicts(sections.get("pillars"))
        ),
        channels=tuple(
            BlueprintChannel(
                channel=_str(c.get("channel")),
                posts_per_week=_int(c.get("posts_per_week")),
                sends_per_week=_int(c.get("sends_per_week")),
            )
            for c in _dicts(sections.get("channels"))
        ),
        goals=tuple(
            BlueprintGoal(
                id=_str(g.get("id")),
                metric=_str(g.get("metric")),
                target=float(g.get("target") or 0),
                channel=_str(g.get("channel")) or None,
            )
            for g in _dicts(sections.get("goals"))
        ),
        roadmap=_roadmap(sections.get("roadmap")),
        learning_ids=tuple(sections.get("learning_ids") or ()),
        **meta,
    )


def _to_domain(doc: _BlueprintDoc) -> GrowthBlueprint:
    """A legacy (pre-unit) Blueprint row."""
    return _domain(
        doc.model_dump(),
        id=str(doc.id),
        workspace_id=doc.workspace,
        profile_id=doc.profile_id,
        version=doc.version,
        status=doc.status,
        created_by=doc.created_by,
        approved_by=doc.approved_by,
        approved_at=doc.approved_at,
        created_at=doc.createdAt,
        updated_at=doc.updatedAt,
    )


def _to_response(bp: GrowthBlueprint) -> GrowthBlueprintResponse:
    return GrowthBlueprintResponse(
        id=bp.id,
        workspace_id=bp.workspace_id,
        profile_id=bp.profile_id,
        version=bp.version,
        status=bp.status,
        identity=BlueprintIdentityModel(
            summary=bp.identity.summary, product=bp.identity.product, problem=bp.identity.problem
        ),
        audience=BlueprintAudienceModel(
            segments=list(bp.audience.segments), icp_sentences=list(bp.audience.icp_sentences)
        ),
        voice=BlueprintVoiceModel(tone=bp.voice.tone, avoid=list(bp.voice.avoid)),
        pillars=[
            BlueprintPillarModel(name=p.name, weight=p.weight, hooks=list(p.hooks))
            for p in bp.pillars
        ],
        channels=[
            BlueprintChannelModel(
                channel=c.channel, posts_per_week=c.posts_per_week, sends_per_week=c.sends_per_week
            )
            for c in bp.channels
        ],
        goals=[
            BlueprintGoalModel(id=g.id, metric=g.metric, target=g.target, channel=g.channel)
            for g in bp.goals
        ],
        roadmap=BlueprintRoadmapModel(
            phases=[
                RoadmapPhaseModel(
                    id=p.id,
                    name=p.name,
                    start=p.start,
                    end=p.end,
                    cadence=[
                        RoadmapCadenceModel(channel=c.channel, posts_per_week=c.posts_per_week)
                        for c in p.cadence
                    ],
                    goal_ids=list(p.goal_ids),
                    milestones=[
                        RoadmapMilestoneModel(
                            id=m.id, title=m.title, date=m.date, status=m.status, link=m.link
                        )
                        for m in p.milestones
                    ],
                )
                for p in bp.roadmap.phases
            ]
        ),
        learning_ids=list(bp.learning_ids),
        created_by=bp.created_by,
        approved_by=bp.approved_by,
        approved_at=iso_utc(bp.approved_at),
        created_at=iso_utc(bp.created_at),
        updated_at=iso_utc(bp.updated_at),
    )


def _response(doc: _BlueprintDoc | None) -> GrowthBlueprintResponse | None:
    return _to_response(_to_domain(doc)) if doc is not None else None


# ---------------------------------------------------------------------------
# Units: the store helpers (one version chain per unit)
# ---------------------------------------------------------------------------

_CORE_UNITS = ("brand", "strategy", "plan")
_UNIT_ORDER = {u: i for i, u in enumerate((*_CORE_UNITS, *(f"play:{p.id}" for p in PLAYS)))}
_PLAY = "play:"


def _kind(unit: str) -> str:
    """``brand`` / ``strategy`` / ``plan`` / ``play``; 422 for anything else,
    including a play id the catalog does not list."""
    if unit in _CORE_UNITS:
        return unit
    if unit.startswith(_PLAY) and unit[len(_PLAY) :] in PLAY_IDS:
        return "play"
    raise ValidationError("social.blueprint_unknown_unit", "Not a Blueprint unit", {"unit": unit})


async def _profile(ctx: RequestContext, profile_id: str | None) -> tuple[str, SocialProfile]:
    from pocketpaw_ee.cloud.growth.social import service as social_service

    return await social_service.require_profile(ctx, profile_id)


async def _next_version(workspace_id: str, profile_id: str, unit: str) -> int:
    docs = (
        await _UnitDoc.find({"workspace": workspace_id, "profile_id": profile_id, "unit": unit})
        .sort([("version", -1)])
        .limit(1)
        .to_list()
    )
    return docs[0].version + 1 if docs else 1


async def _rows(workspace_id: str, profile_id: str) -> dict[str, dict[str, _UnitDoc]]:
    """``{unit: {"approved": doc, "draft": doc}}``, the highest version per status."""
    docs = (
        await _UnitDoc.find(
            {
                "workspace": workspace_id,
                "profile_id": profile_id,
                "status": {"$in": ["approved", "draft"]},
            }
        )
        .sort([("version", 1)])
        .to_list()
    )
    out: dict[str, dict[str, _UnitDoc]] = {}
    for doc in docs:  # ascending, so the highest version per status wins
        out.setdefault(doc.unit, {})[doc.status] = doc
    return out


async def _has_units(workspace_id: str, profile_id: str) -> bool:
    found = await _UnitDoc.find_one({"workspace": workspace_id, "profile_id": profile_id})
    return found is not None


def _pick(rows: dict[str, dict[str, _UnitDoc]], status: str) -> dict[str, _UnitDoc] | None:
    """Per unit the row a composed ``status`` Blueprint uses: the approved, or for a
    draft the draft else the approved. None without both brand and strategy, or,
    for a draft, when no unit has one."""
    if status == "draft" and not any("draft" in r for r in rows.values()):
        return None
    picked = {
        unit: (r.get("draft") or r.get("approved")) if status == "draft" else r.get("approved")
        for unit, r in rows.items()
    }
    picked = {unit: doc for unit, doc in picked.items() if doc is not None}
    return picked if "brand" in picked and "strategy" in picked else None


# ---------------------------------------------------------------------------
# Units <-> the legacy (whole-page) sections
# ---------------------------------------------------------------------------
# Each legacy section lives in one unit: identity -> brand.who_we_are, voice ->
# brand.tone_of_voice, audience -> strategy.gtm.icp, pillars ->
# strategy.content_pillars, goals -> strategy.goals.objectives, learning_ids ->
# strategy, roadmap -> plan. A channel's cadence lives on its acting plays: the
# channel's ``posts_per_week`` is the sum over its active (core / testing) post
# plays, ``sends_per_week`` over its send plays. A legacy edit puts a channel's
# number on its primary play (the first catalog play for that channel and
# cadence) minus what its other active plays already carry. A channel that has
# no play (``whatsapp``), or a cadence no play carries (sends on x or reddit,
# posts on email), has nowhere to live and is dropped.


def _primary_play(channel: str, cadence: str) -> str | None:
    return next(
        (p.id for p in PLAYS if p.acts and p.channel == channel and p.cadence == cadence), None
    )


def _active(body: dict[str, Any]) -> bool:
    return body.get("status") in ("core", "testing")


def _normal(unit: str, body: dict[str, Any]) -> dict[str, Any]:
    """``body`` in its model's full shape; a stored body that no longer validates is
    kept as it is rather than lost."""
    try:
        model = BODY_MODELS[_kind(unit)].model_validate(body)
    except PydanticValidationError:
        logger.warning("growth blueprint: %s body no longer validates; kept as stored", unit)
        return body
    return model.model_dump(mode="json")


def _apply_legacy(bodies: dict[str, dict], sections: dict[str, Any]) -> dict[str, dict]:
    """``bodies`` (every core unit present) with the legacy ``sections`` sent laid
    over the fields they map to; every other field is kept."""
    out = {u: copy.deepcopy(b) for u, b in bodies.items()}
    for unit in _CORE_UNITS:
        out[unit] = _normal(unit, out.get(unit) or {})
    brand, strategy, plan = out["brand"], out["strategy"], out["plan"]
    if "identity" in sections:
        identity = sections["identity"] or {}
        brand["who_we_are"] = {k: _str(identity.get(k)) for k in ("summary", "product", "problem")}
    if "voice" in sections:
        voice = sections["voice"] or {}
        brand["tone_of_voice"].update(
            tone=_str(voice.get("tone")), avoid=list(_strs(voice.get("avoid")))
        )
    if "audience" in sections:
        audience = sections["audience"] or {}
        strategy["gtm"]["icp"].update(
            segments=list(_strs(audience.get("segments"))),
            icp_sentences=list(_strs(audience.get("icp_sentences"))),
        )
    if "pillars" in sections:
        strategy["content_pillars"] = copy.deepcopy(_dicts(sections["pillars"]))
    if "goals" in sections:
        known = {o.get("id"): o for o in _dicts(strategy["goals"].get("objectives"))}
        strategy["goals"]["objectives"] = [
            {
                **known.get(_str(g.get("id")), {}),
                "id": _str(g.get("id")),
                "metric": _str(g.get("metric")),
                "target": g.get("target") or 0,
                "channel": _str(g.get("channel")) or None,
            }
            for g in _dicts(sections["goals"])
        ]
    if "learning_ids" in sections:
        strategy["learning_ids"] = list(sections["learning_ids"] or [])
    if "roadmap" in sections:
        plan["roadmap"] = copy.deepcopy(sections["roadmap"] or {"phases": []})
    if "channels" in sections:
        _apply_channels(out, _dicts(sections["channels"]))
    return {u: _normal(u, b) for u, b in out.items()}


def _apply_channels(out: dict[str, dict], channels: list[dict]) -> None:
    sent = {_str(c.get("channel")): c for c in channels}
    for p in PLAYS:
        if not p.acts or _primary_play(p.channel or "", p.cadence or "") != p.id:
            continue
        unit, key = f"{_PLAY}{p.id}", f"{p.cadence}_per_week"
        body = out.get(unit)
        entry = sent.get(p.channel or "")
        if entry is None:
            if body is not None and _active(body):
                body["status"] = "parked"
            continue
        value = _int(entry.get(key))
        # The channel's first play anchors it (kept at 0 so the channel survives);
        # a second cadence on the same channel (LinkedIn sends) only once it is used.
        anchor = next(q.id for q in PLAYS if q.acts and q.channel == p.channel) == p.id
        if body is None and not value and not anchor:
            continue
        others = sum(
            _int(b.get(key))
            for u, b in out.items()
            if u != unit
            and u.startswith(_PLAY)
            and _active(b)
            and (q := play_catalog.play(u[len(_PLAY) :])) is not None
            and q.channel == p.channel
            and q.cadence == p.cadence
        )
        body = dict(body or {})
        body["status"] = body.get("status") if _active(body) else "core"
        # ponytail: another active play already carrying more than the total leaves this at 0.
        body[key] = max(value - others, 0)
        out[unit] = body


def _to_legacy(bodies: dict[str, dict]) -> dict[str, Any]:
    """The legacy sections stitched from unit bodies (the inverse of ``_apply_legacy``)."""
    brand = bodies.get("brand") or {}
    strategy = bodies.get("strategy") or {}
    plan = bodies.get("plan") or {}
    voice = brand.get("tone_of_voice") or {}
    icp = (strategy.get("gtm") or {}).get("icp") or {}
    channels = []
    for name in BLUEPRINT_CHANNELS:
        plays = [
            (q, b)
            for u, b in bodies.items()
            if u.startswith(_PLAY)
            and _active(b)
            and (q := play_catalog.play(u[len(_PLAY) :])) is not None
            and q.acts
            and q.channel == name
        ]
        if plays:
            channels.append(
                {
                    "channel": name,
                    "posts_per_week": sum(
                        _int(b.get("posts_per_week")) for q, b in plays if q.cadence == "posts"
                    ),
                    "sends_per_week": sum(
                        _int(b.get("sends_per_week")) for q, b in plays if q.cadence == "sends"
                    ),
                }
            )
    who = brand.get("who_we_are") or {}
    return {
        "identity": {k: _str(who.get(k)) for k in ("summary", "product", "problem")},
        "audience": {
            "segments": list(_strs(icp.get("segments"))),
            "icp_sentences": list(_strs(icp.get("icp_sentences"))),
        },
        "voice": {"tone": _str(voice.get("tone")), "avoid": list(_strs(voice.get("avoid")))},
        "pillars": copy.deepcopy(_dicts(strategy.get("content_pillars"))),
        "channels": channels,
        "goals": [
            {
                "id": _str(o.get("id")),
                "metric": _str(o.get("metric")),
                "target": o.get("target") or 0,
                "channel": o.get("channel"),
            }
            for o in _dicts((strategy.get("goals") or {}).get("objectives"))
        ],
        "roadmap": copy.deepcopy(plan.get("roadmap") or {"phases": []}),
        "learning_ids": list(strategy.get("learning_ids") or []),
    }


def _compose(
    workspace_id: str, profile_id: str, rows: dict[str, dict[str, _UnitDoc]], status: str
) -> GrowthBlueprint | None:
    picked = _pick(rows, status)
    if picked is None:
        return None
    bodies = {unit: doc.body for unit, doc in picked.items()}
    strategy = picked["strategy"]
    return _domain(
        _to_legacy(bodies),
        id=str(strategy.id),
        workspace_id=workspace_id,
        profile_id=profile_id,
        version=strategy.version,
        status=status,
        created_by=strategy.created_by,
        approved_by=strategy.approved_by,
        approved_at=strategy.approved_at,
        created_at=min(d.createdAt for d in picked.values()),
        updated_at=max(d.updatedAt for d in picked.values()),
        brand=copy.deepcopy(bodies["brand"]),
        strategy=copy.deepcopy(bodies["strategy"]),
        plan=copy.deepcopy(bodies.get("plan") or {}),
        plays={u[len(_PLAY) :]: copy.deepcopy(b) for u, b in bodies.items() if u.startswith(_PLAY)},
        unit_versions={unit: doc.version for unit, doc in picked.items()},
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def _legacy_find(workspace_id: str, profile_id: str, status: str) -> _BlueprintDoc | None:
    """The newest legacy (pre-unit) row in ``status``; read-only, until migrated."""
    match = {"workspace": workspace_id, "profile_id": profile_id}
    docs = (
        await _BlueprintDoc.find({**match, "status": status})
        .sort([("version", -1)])
        .limit(1)
        .to_list()
    )
    return docs[0] if docs else None


async def _state(workspace_id: str, profile_id: str) -> BlueprintStateResponse:
    rows = await _rows(workspace_id, profile_id)
    if not rows and not await _has_units(workspace_id, profile_id):
        return BlueprintStateResponse(
            approved=_response(await _legacy_find(workspace_id, profile_id, "approved")),
            draft=_response(await _legacy_find(workspace_id, profile_id, "draft")),
        )
    approved = _compose(workspace_id, profile_id, rows, "approved")
    draft = _compose(workspace_id, profile_id, rows, "draft")
    return BlueprintStateResponse(
        approved=_to_response(approved) if approved else None,
        draft=_to_response(draft) if draft else None,
    )


async def get(ctx: RequestContext, profile_id: str | None = None) -> BlueprintStateResponse:
    """The profile's approved Blueprint and its open draft (either may be null),
    each composed from the units."""
    workspace_id, profile = await _profile(ctx, profile_id)
    return await _state(workspace_id, profile.id)


async def approved_for(workspace_id: str, profile_id: str) -> GrowthBlueprint | None:
    """The approved Blueprint the drafting agents follow, stitched from the approved
    units; None unless ``brand`` and ``strategy`` are both approved. A brand not yet
    migrated reads its legacy approved row. Never falls back to another profile."""
    rows = await _rows(workspace_id, profile_id)
    if not rows and not await _has_units(workspace_id, profile_id):
        doc = await _legacy_find(workspace_id, profile_id, "approved")
        return _to_domain(doc) if doc is not None else None
    return _compose(workspace_id, profile_id, rows, "approved")


def _unit_row(doc: _UnitDoc | None) -> BlueprintUnitRowResponse | None:
    if doc is None:
        return None
    return BlueprintUnitRowResponse(
        id=str(doc.id),
        unit=doc.unit,
        version=doc.version,
        status=doc.status,
        body=doc.body,
        created_by=doc.created_by,
        approved_by=doc.approved_by,
        approved_at=iso_utc(doc.approved_at),
        created_at=iso_utc(doc.createdAt),
        updated_at=iso_utc(doc.updatedAt),
    )


def _unit_state(unit: str, r: dict[str, _UnitDoc]) -> BlueprintUnitStateResponse:
    return BlueprintUnitStateResponse(
        unit=unit, approved=_unit_row(r.get("approved")), draft=_unit_row(r.get("draft"))
    )


def catalog() -> PlayCatalogResponse:
    return PlayCatalogResponse(
        branches=[
            BranchResponse(
                id=b.id,
                label=b.label,
                purpose=b.purpose,
                groups=list(b.groups),
                leaves=list(b.leaves),
                plan_only=b.plan_only,
            )
            for b in play_catalog.BRANCHES
        ],
        plays=[
            PlayResponse(
                id=p.id,
                label=p.label,
                branch=p.branch,
                leaf=p.leaf,
                platform=p.platform,
                peso=p.peso,
                acts=p.acts,
            )
            for p in PLAYS
        ],
    )


async def units(ctx: RequestContext, profile_id: str | None = None) -> BlueprintUnitsResponse:
    """Every unit's approved and draft rows plus the play catalog. A brand not yet
    migrated lists no units (its first write migrates it)."""
    workspace_id, profile = await _profile(ctx, profile_id)
    rows = await _rows(workspace_id, profile.id)
    return BlueprintUnitsResponse(
        profile_id=profile.id,
        units=[
            _unit_state(u, rows[u]) for u in sorted(rows, key=lambda u: _UNIT_ORDER.get(u, 999))
        ],
        catalog=catalog(),
    )


# ---------------------------------------------------------------------------
# Building sections
# ---------------------------------------------------------------------------


def _items(values: Any, limit: int = MAX_BLUEPRINT_ITEMS, chars: int = _ITEM_CHARS) -> list[str]:
    return list(as_list(values, limit, chars))


def _segments(audience: str) -> list[str]:
    return _items(typed_items(audience))


def _passes(text: str, source: str) -> bool:
    """Non-empty, no em or en dash, and no number the owner did not type."""
    return bool(text) and not social_ideas.check_texts([text], source)


def _checked(values: Any, source: str, limit: int = MAX_BLUEPRINT_ITEMS, chars: int = _ITEM_CHARS):
    return [v for v in _items(values, limit, chars) if _passes(v, source)]


def _pillars(
    names: list[str], hooks: list[str], weights: dict[str, int] | None
) -> list[dict[str, Any]]:
    """Pillars for ``names`` with ``hooks`` dealt round-robin. ``weights`` keeps known
    weights by name (an unknown name gets 0); None splits 100 equally."""
    names = _items(names, MAX_BLUEPRINT_PILLARS, _PILLAR_CHARS)
    if not names:
        return []
    share, extra = divmod(100, len(names))
    pillars = [
        {
            "name": name,
            "weight": (
                weights.get(name.lower(), 0) if weights is not None else share + (i < extra)
            ),
            "hooks": [],
        }
        for i, name in enumerate(names)
    ]
    for i, hook in enumerate(_items(hooks)):
        pillars[i % len(pillars)]["hooks"].append(hook)
    return pillars


async def _icp_sentences(ctx: RequestContext, profile_id: str) -> list[str]:
    """Active ICPs of this brand (legacy unbranded ones get a brand from the backfill)."""
    from pocketpaw_ee.cloud.growth import service as growth_service

    icps = await growth_service.list_icps(ctx, profile_id=profile_id, status="active")
    return _items([icp.criteria for icp in icps], MAX_ICP_SENTENCES)


def _analysis_or_typed(profile: SocialProfile) -> SocialAnalysis:
    return social_analyst.apply_typed_fields(
        profile.analysis or SocialAnalysis(), dict(profile.description)
    )


def _today() -> date:
    """Today in UTC; the seam tests freeze."""
    return datetime.now(UTC).date()


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _first_roadmap(channels: list[dict]) -> dict[str, Any]:
    """Two phases from today, each with the channels' own weekly post counts."""
    today = _today()
    cadence = [
        {
            "channel": _str(c.get("channel")),
            # ponytail: a channel allows 50 posts a week, a cadence 21; cap rather than refuse.
            "posts_per_week": min(_int(c.get("posts_per_week")), MAX_WEEKLY_POST_QUOTA),
        }
        for c in _dicts(channels)
    ]
    return {
        "phases": [
            {
                "id": _new_id(),
                "name": name,
                "start": (today + timedelta(days=first)).isoformat(),
                "end": (today + timedelta(days=last)).isoformat(),
                "cadence": copy.deepcopy(cadence),
                "goal_ids": [],
                "milestones": [],
            }
            for name, first, last in (("This week", 0, 6), ("Next 4 weeks", 7, 34))
        ]
    }


def _with_first_roadmap(sections: dict[str, Any]) -> dict[str, Any]:
    if not _dicts((sections.get("roadmap") or {}).get("phases")):
        sections["roadmap"] = _first_roadmap(sections.get("channels") or [])
    return sections


async def _seed_sections(ctx: RequestContext, profile: SocialProfile) -> dict[str, Any]:
    """Seed text passes the same dash / number check as regenerate; a failing field
    seeds empty and a failing item is dropped."""
    a = _analysis_or_typed(profile)
    source = _source_text(profile)

    def text(value: str) -> str:
        return value if _passes(value, source) else ""

    sections = {
        "identity": {name: text(getattr(a, name)) for name in ("summary", "product", "problem")},
        "audience": {
            "segments": _checked(_segments(a.audience), source),
            "icp_sentences": await _icp_sentences(ctx, profile.id),
        },
        "voice": {"tone": text(a.tone), "avoid": _checked(a.avoid, source)},
        "pillars": _pillars(
            _checked(a.content_pillars, source, MAX_BLUEPRINT_PILLARS, _PILLAR_CHARS),
            _checked(a.hooks, source),
            None,
        ),
        "channels": [
            {"channel": "x", "posts_per_week": profile.weekly_post_quota, "sends_per_week": 0}
        ],
        "goals": [],
        "roadmap": {"phases": []},
    }
    return _with_first_roadmap(sections)


def _carried(sections: dict[str, Any]) -> dict[str, Any]:
    out = {name: copy.deepcopy(sections.get(name)) for name in BLUEPRINT_SECTIONS}
    out["learning_ids"] = list(sections.get("learning_ids") or [])
    return out


def _source_text(profile: SocialProfile) -> str:
    """What the owner typed: the only text a blueprint number may come from."""
    return " ".join([profile.company_name, profile.website or "", *profile.description.values()])


def _regenerated(base: dict[str, Any], a: SocialAnalysis, source: str) -> dict[str, Any]:
    """``base``'s sections with the analyst's text swapped in where it passes the checks."""

    def text(new: str, old: Any) -> str:
        return new if _passes(new, source) else _str(old)

    def items(values: Any, limit: int = MAX_BLUEPRINT_ITEMS, chars: int = _ITEM_CHARS) -> list:
        return _checked(values, source, limit, chars)

    out = _carried(base)
    identity, audience, voice = (base.get(k) or {} for k in ("identity", "audience", "voice"))
    out["identity"] = {
        name: text(getattr(a, name), identity.get(name))
        for name in ("summary", "product", "problem")
    }
    out["audience"] = {
        "segments": items(_segments(a.audience)) or list(_strs(audience.get("segments"))),
        "icp_sentences": list(_strs(audience.get("icp_sentences"))),
    }
    out["voice"] = {
        "tone": text(a.tone, voice.get("tone")),
        "avoid": items(a.avoid) or list(_strs(voice.get("avoid"))),
    }
    names = items(a.content_pillars, MAX_BLUEPRINT_PILLARS, _PILLAR_CHARS)
    if names:
        old = _dicts(base.get("pillars"))
        weights = {_str(p.get("name")).lower(): _int(p.get("weight")) for p in old} if old else None
        pillars = _pillars(names, items(a.hooks), weights)
        if not any(p["hooks"] for p in pillars):
            old_hooks = {_str(p.get("name")).lower(): list(_strs(p.get("hooks"))) for p in old}
            for p in pillars:
                p["hooks"] = old_hooks.get(p["name"].lower(), [])
        out["pillars"] = pillars
    return _with_first_roadmap(out)


# ---------------------------------------------------------------------------
# Writes: store helpers
# ---------------------------------------------------------------------------


def _stale() -> ConflictError:
    return ConflictError(
        "social.blueprint_stale", "This blueprint changed while you were editing. Reload it."
    )


async def _set_if(match: dict[str, Any], fields: dict[str, Any]) -> bool:
    """One conditional write: ``fields`` lands only if a row still matches ``match``."""
    result = await _UnitDoc.get_pymongo_collection().update_one(
        match, {"$set": {**fields, "updatedAt": datetime.now(UTC)}}
    )
    return result.matched_count == 1


async def _supersede_others(
    workspace_id: str, profile_id: str, unit: str, status: str, keep: Any
) -> None:
    await _UnitDoc.get_pymongo_collection().update_many(
        {
            "workspace": workspace_id,
            "profile_id": profile_id,
            "status": status,
            "unit": unit,
            "_id": {"$ne": keep},
        },
        {"$set": {"status": "superseded", "updatedAt": datetime.now(UTC)}},
    )


async def _insert(doc: _UnitDoc) -> None:
    try:
        await doc.insert()
    except DuplicateKeyError as exc:
        raise ConflictError(
            "social.blueprint_version_taken", "Someone else changed this blueprint. Reload it."
        ) from exc


def _draft_match(workspace_id: str, profile_id: str, doc: _UnitDoc) -> dict[str, Any]:
    return {
        "_id": doc.id,
        "workspace": workspace_id,
        "profile_id": profile_id,
        "unit": doc.unit,
        "status": "draft",
    }


async def _write_draft(
    workspace_id: str,
    profile_id: str,
    unit: str,
    draft: _UnitDoc | None,
    body: dict[str, Any],
    user_id: str,
) -> None:
    """Replace the unit's draft body, or open a draft at the unit's next version.
    Conditional on the row still being this unit's draft: an approve or a
    regenerate that landed since the read turns this save into a 409."""
    if draft is None:
        await _insert(
            _UnitDoc(
                workspace=workspace_id,
                profile_id=profile_id,
                unit=unit,
                version=await _next_version(workspace_id, profile_id, unit),
                status="draft",
                created_by=user_id,
                body=body,
            )
        )
    elif not await _set_if(_draft_match(workspace_id, profile_id, draft), {"body": body}):
        raise _stale()


async def _approve_row(workspace_id: str, profile_id: str, draft: _UnitDoc, user_id: str) -> None:
    """Approve first, then supersede the rest: a crash between the two leaves two
    approved (the newer wins on read), never zero."""
    fields = {"status": "approved", "approved_by": user_id, "approved_at": datetime.now(UTC)}
    if not await _set_if(_draft_match(workspace_id, profile_id, draft), fields):
        raise _stale()
    await _supersede_others(workspace_id, profile_id, draft.unit, "approved", draft.id)


# ---------------------------------------------------------------------------
# Writes: cross-unit goal checks
# ---------------------------------------------------------------------------


def _goal_ids(strategy: dict[str, Any] | None) -> set[str]:
    return {
        _str(o.get("id")) for o in _dicts(((strategy or {}).get("goals") or {}).get("objectives"))
    }


def _named_goals(unit: str, body: dict[str, Any]) -> set[str]:
    """The goal ids a plan (phase ``goal_ids``) or a play (``goal_id``) names."""
    if unit == "plan":
        phases = _dicts((body.get("roadmap") or {}).get("phases"))
        return {g for p in phases for g in _strs(p.get("goal_ids"))}
    if unit.startswith(_PLAY):
        return {body["goal_id"]} if body.get("goal_id") else set()
    return set()


def _unknown_goal(goal_ids: set[str]) -> ValidationError:
    return ValidationError(
        "social.blueprint_unknown_goal",
        "A roadmap phase or play names a goal this blueprint does not have",
        {"goal_ids": sorted(goal_ids)},
    )


def _check_goals_in_use(rows: dict[str, dict[str, _UnitDoc]], goals: set[str]) -> None:
    """Approving a strategy may not drop a goal a plan or play (approved or draft)
    still names; the 422 lists each such unit and its missing goals."""
    in_use: dict[str, list[str]] = {}
    for unit, r in rows.items():
        named = set().union(*(_named_goals(unit, d.body) for d in r.values()))
        if named - goals:
            in_use[unit] = sorted(named - goals)
    if in_use:
        raise ValidationError(
            "social.blueprint_goal_in_use",
            "This strategy drops a goal that a plan or play still names",
            {"units": in_use},
        )


def _check_goal_refs(base: dict[str, Any], edit: dict[str, Any], plays: set[str]) -> None:
    """Legacy save: when goals or the roadmap change, every phase ``goal_ids`` entry
    and every play ``goal_id`` (``plays``) must name a goal of the resulting draft."""
    if "goals" not in edit and "roadmap" not in edit:
        return
    goals = {_str(g.get("id")) for g in _dicts(edit.get("goals", base.get("goals")))}
    roadmap = edit.get("roadmap", base.get("roadmap")) or {}
    named = {g for p in _dicts(roadmap.get("phases")) for g in _strs(p.get("goal_ids"))}
    unknown = (named | plays) - goals
    if unknown:
        raise _unknown_goal(unknown)


async def _check_learning_refs(workspace_id: str, profile_id: str, ids: list[str] | None) -> None:
    """Every learning id must name a learning of this workspace and brand; a foreign
    id reads the same as a missing one."""
    if not ids:
        return
    from pocketpaw_ee.cloud.growth.social import service as social_service

    unknown = await social_service.unknown_learning_ids(workspace_id, profile_id, ids)
    if unknown:
        raise ValidationError(
            "social.blueprint_unknown_learning",
            "A learning id does not name a learning of this brand",
            {"learning_ids": unknown},
        )


# ---------------------------------------------------------------------------
# Writes: per unit
# ---------------------------------------------------------------------------


def _unit_body(unit: str, raw: dict[str, Any]) -> dict[str, Any]:
    """The body validated by its unit's model, in full shape, with ids minted for
    objectives, phases and milestones that have none. JSON mode, so dates land as
    ISO strings."""
    try:
        body = BODY_MODELS[_kind(unit)].model_validate(raw).model_dump(mode="json")
    except PydanticValidationError as exc:
        raise ValidationError(
            "social.blueprint_invalid_body",
            "This Blueprint section has a field that is missing or not allowed",
            {
                "errors": [
                    {"loc": [str(x) for x in e["loc"]], "msg": e["msg"]} for e in exc.errors()
                ]
            },
        ) from exc
    for o in _dicts((body.get("goals") or {}).get("objectives")):
        o["id"] = o["id"] or _new_id()
    for phase in _dicts((body.get("roadmap") or {}).get("phases")):
        phase["id"] = phase["id"] or _new_id()
        for m in phase["milestones"]:
            m["id"] = m["id"] or _new_id()
    return body


async def _unit_result(workspace_id: str, profile_id: str, unit: str):
    rows = await _rows(workspace_id, profile_id)
    return _unit_state(unit, rows.get(unit, {}))


async def save_unit_draft(
    ctx: RequestContext,
    unit: str,
    body: SaveUnitDraftRequest,
    profile_id: str | None = None,
) -> BlueprintUnitStateResponse:
    """Replace the unit's draft body (opening a draft when there is none). 409 when
    ``base_version`` is not the unit's current version (draft, else approved, else
    0); 422 when a plan or play names a goal the strategy (draft, else approved)
    lacks."""
    body = SaveUnitDraftRequest.model_validate(body)
    kind = _kind(unit)
    new_body = _unit_body(unit, body.body)
    workspace_id, profile = await _profile(ctx, profile_id)
    await migrate_profile(workspace_id, profile.id)
    rows = await _rows(workspace_id, profile.id)
    if kind in ("plan", "play"):
        strategy = rows.get("strategy", {})
        current = strategy.get("draft") or strategy.get("approved")
        unknown = _named_goals(unit, new_body) - _goal_ids(current.body if current else None)
        if unknown:
            raise _unknown_goal(unknown)
    if kind == "strategy":
        await _check_learning_refs(workspace_id, profile.id, new_body["learning_ids"])
    mine = rows.get(unit, {})
    draft = mine.get("draft")
    base = draft or mine.get("approved")
    if body.base_version != (base.version if base else 0):
        raise _stale()
    await _write_draft(workspace_id, profile.id, unit, draft, new_body, ctx.user_id or "")
    # no-event: Growth › Social has no realtime subscriber; the Blueprint page re-fetches.
    return await _unit_result(workspace_id, profile.id, unit)


async def approve_unit(
    ctx: RequestContext, unit: str, profile_id: str | None = None
) -> BlueprintUnitStateResponse:
    """The unit's draft becomes its approved version. 409 without a draft; 422 when
    a plan or play names a goal the approved strategy lacks, or a strategy drops a
    goal a plan or play still names."""
    kind = _kind(unit)
    workspace_id, profile = await _profile(ctx, profile_id)
    await migrate_profile(workspace_id, profile.id)
    rows = await _rows(workspace_id, profile.id)
    draft = rows.get(unit, {}).get("draft")
    if draft is None:
        raise ConflictError("social.blueprint_no_draft", "There is no draft to approve")
    if kind in ("plan", "play"):
        approved = rows.get("strategy", {}).get("approved")
        unknown = _named_goals(unit, draft.body) - _goal_ids(approved.body if approved else None)
        if unknown:
            raise _unknown_goal(unknown)
    if kind == "strategy":
        _check_goals_in_use(rows, _goal_ids(draft.body))
    await _approve_row(workspace_id, profile.id, draft, ctx.user_id or "")
    # no-event: Growth › Social has no realtime subscriber; the Blueprint page re-fetches.
    return await _unit_result(workspace_id, profile.id, unit)


# ---------------------------------------------------------------------------
# Writes: the whole-page (legacy) routes, as wrappers over the units
# ---------------------------------------------------------------------------


def _current(rows: dict[str, dict[str, _UnitDoc]]) -> dict[str, _UnitDoc]:
    """Per unit the row an edit builds on: the draft, else the approved."""
    out = {unit: r.get("draft") or r.get("approved") for unit, r in rows.items()}
    return {unit: doc for unit, doc in out.items() if doc is not None}


async def seed(ctx: RequestContext, profile_id: str | None = None) -> BlueprintStateResponse:
    """Write version 1 drafts of brand, strategy, plan and the acting plays the
    profile posts on, from the profile. Does nothing once the brand has a Blueprint."""
    workspace_id, profile = await _profile(ctx, profile_id)
    await migrate_profile(workspace_id, profile.id)
    if not await _has_units(workspace_id, profile.id):
        bodies = _apply_legacy({}, await _seed_sections(ctx, profile))
        for unit, body in bodies.items():
            doc = _UnitDoc(
                workspace=workspace_id,
                profile_id=profile.id,
                unit=unit,
                version=1,
                status="draft",
                created_by=ctx.user_id or "",
                body=body,
            )
            try:
                await doc.insert()
            except DuplicateKeyError:
                pass  # a concurrent seed wrote version 1 first; seeding is idempotent
        # no-event: Growth › Social has no realtime subscriber; the Blueprint page re-fetches.
    return await _state(workspace_id, profile.id)


def _draft_edit(body: SaveBlueprintDraftRequest) -> dict[str, Any]:
    """The sections sent, in full shape (nested defaults included) so every stored
    version diffs alike; goals, phases and milestones without an id get one. JSON
    mode, so roadmap dates land as ISO strings."""
    dumped = body.model_dump(mode="json")
    out: dict[str, Any] = {}
    for name in body.model_fields_set:
        value = dumped[name]
        if value is None:
            continue
        if name == "goals":
            value = [{**g, "id": g.get("id") or _new_id()} for g in value]
        if name == "roadmap":
            for phase in value["phases"]:
                phase["id"] = phase["id"] or _new_id()
                for m in phase["milestones"]:
                    m["id"] = m["id"] or _new_id()
        out[name] = value
    return out


async def save_draft(
    ctx: RequestContext, body: SaveBlueprintDraftRequest, profile_id: str | None = None
) -> BlueprintStateResponse:
    """Lay the sections sent over the current Blueprint and write a draft for every
    unit whose body changed (opening one from its approved when there is none).
    404 before the Blueprint is seeded."""
    body = SaveBlueprintDraftRequest.model_validate(body)
    workspace_id, profile = await _profile(ctx, profile_id)
    edit = _draft_edit(body)
    await migrate_profile(workspace_id, profile.id)
    rows = await _rows(workspace_id, profile.id)
    current = _current(rows)
    if not current:
        raise NotFound("social_blueprint")
    bodies = {unit: doc.body for unit, doc in current.items()}
    plays = set().union(*(_named_goals(u, b) for u, b in bodies.items() if u.startswith(_PLAY)))
    _check_goal_refs(_to_legacy(bodies), edit, plays)
    await _check_learning_refs(workspace_id, profile.id, edit.get("learning_ids"))
    for unit, new_body in _apply_legacy(bodies, edit).items():
        if new_body != bodies.get(unit):
            draft = rows.get(unit, {}).get("draft")
            await _write_draft(workspace_id, profile.id, unit, draft, new_body, ctx.user_id or "")
    # no-event: Growth › Social has no realtime subscriber; the Blueprint page re-fetches.
    return await _state(workspace_id, profile.id)


async def regenerate(ctx: RequestContext, profile_id: str | None = None) -> BlueprintStateResponse:
    """Rerun the analyst and write its text into NEW draft versions of ``brand`` and
    ``strategy`` (and ``plan`` when a first roadmap is seeded), superseding their old
    drafts. Approved versions are never touched; numbers and goals are carried
    over, never generated."""
    analyze_fn = social_analyst.resolve_analyze_fn()
    if analyze_fn is None:
        raise CloudError(
            503,
            "social.analyzer_unavailable",
            "Website analysis is not configured on this deployment",
        )
    workspace_id, profile = await _profile(ctx, profile_id)
    await migrate_profile(workspace_id, profile.id)
    if not _current(await _rows(workspace_id, profile.id)):
        raise NotFound("social_blueprint")
    if not profile.website and not profile.has_description():
        raise ValidationError(
            "social.nothing_to_analyze",
            "Add a website or at least one description field before regenerating",
        )
    request = AnalysisRequest(
        workspace_id=workspace_id,
        company_name=profile.company_name,
        website=profile.website,
        description=dict(profile.description),
    )
    try:
        outcome = await analyze_fn(request)
    except Exception:  # noqa: BLE001
        logger.warning("growth social: blueprint regenerate raised for ws=%s", workspace_id)
        outcome = None
    if outcome is None or outcome.analysis is None:
        raise CloudError(
            502,
            "social.blueprint_regenerate_failed",
            (outcome.error if outcome else None) or "The analysis failed. Try again shortly.",
        )

    analysis = social_analyst.apply_typed_fields(outcome.analysis, dict(profile.description))
    # Re-read the base: an owner edit may have landed during the 20-60 s run.
    rows = await _rows(workspace_id, profile.id)
    current = _current(rows)
    if not current:
        raise NotFound("social_blueprint")
    bodies = {unit: doc.body for unit, doc in current.items()}
    new_bodies = _apply_legacy(
        bodies, _regenerated(_to_legacy(bodies), analysis, _source_text(profile))
    )
    targets = ["brand", "strategy"]
    if new_bodies["plan"] != bodies.get("plan"):
        targets.append("plan")
    written: list[_UnitDoc] = []
    for unit in targets:
        doc = _UnitDoc(
            workspace=workspace_id,
            profile_id=profile.id,
            unit=unit,
            version=await _next_version(workspace_id, profile.id, unit),
            status="draft",
            created_by=ctx.user_id or "",
            body=new_bodies[unit],
        )
        await _insert(doc)
        written.append(doc)
    superseded: list[_UnitDoc] = []
    for unit in targets:
        base = rows.get(unit, {}).get("draft")
        if base is None:
            continue
        base_match = _draft_match(workspace_id, profile.id, base)
        if not await _set_if(base_match, {"status": "superseded"}):
            for doc in written:
                await doc.delete()
            for old in superseded:
                await _set_if({"_id": old.id, "status": "superseded"}, {"status": "draft"})
            raise _stale()
        superseded.append(base)
    # A draft opened from the approved meanwhile is superseded too (never the approved).
    for doc in written:
        await _supersede_others(workspace_id, profile.id, doc.unit, "draft", doc.id)
    # no-event: Growth › Social has no realtime subscriber; the Blueprint page re-fetches.
    return await _state(workspace_id, profile.id)


async def approve(ctx: RequestContext, profile_id: str | None = None) -> BlueprintStateResponse:
    """Approve every unit that has a draft; 409 when none has. The goal references
    of the resulting Blueprint are checked first (422), and the strategy is
    approved last."""
    workspace_id, profile = await _profile(ctx, profile_id)
    await migrate_profile(workspace_id, profile.id)
    rows = await _rows(workspace_id, profile.id)
    drafts = {unit: r["draft"] for unit, r in rows.items() if "draft" in r}
    if not drafts:
        raise ConflictError("social.blueprint_no_draft", "There is no draft to approve")
    resulting = _current(rows)
    strategy = resulting.get("strategy")
    goals = _goal_ids(strategy.body if strategy else None)
    named = set().union(*(_named_goals(u, d.body) for u, d in resulting.items()))
    if named - goals:
        raise _unknown_goal(named - goals)
    for unit in sorted(drafts, key=lambda u: u == "strategy"):
        await _approve_row(workspace_id, profile.id, drafts[unit], ctx.user_id or "")
    # no-event: Growth › Social has no realtime subscriber; the Blueprint page re-fetches.
    return await _state(workspace_id, profile.id)


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def _keyed(section: str, value: Any) -> dict[str, Any]:
    """A section as {key: value}: field names for the dict sections (empty values
    dropped, so filling a field reads as added), item identities for the lists."""
    if section == "pillars":
        return {_str(p.get("name")): p for p in _dicts(value)}
    if section == "channels":
        return {_str(c.get("channel")): c for c in _dicts(value)}
    if section == "goals":
        return {_str(g.get("id")): g for g in _dicts(value)}
    if section == "roadmap":
        return {_str(p.get("id")): p for p in _dicts((value or {}).get("phases"))}
    raw = value if isinstance(value, dict) else {}
    return {k: v for k, v in raw.items() if v not in ("", None, [], {})}


def _section_diff(section: str, old: Any, new: Any) -> BlueprintSectionDiff:
    return _key_diff(_keyed(section, old), _keyed(section, new))


def _key_diff(a: dict[str, Any], b: dict[str, Any]) -> BlueprintSectionDiff:
    return BlueprintSectionDiff(
        added=[k for k in b if k not in a],
        removed=[k for k in a if k not in b],
        changed=[k for k in b if k in a and a[k] != b[k]],
    )


async def _legacy_version(workspace_id: str, profile_id: str, version: int) -> _BlueprintDoc:
    doc = await _BlueprintDoc.find_one(
        {"workspace": workspace_id, "profile_id": profile_id, "version": version}
    )
    if doc is None:
        raise NotFound("social_blueprint_version", str(version))
    return doc


async def diff(
    ctx: RequestContext, from_version: int, to_version: int, profile_id: str | None = None
) -> BlueprintDiffResponse:
    """What changed per section between the approved and the draft Blueprint.
    Composed versions span several unit chains, so ``from_version`` and
    ``to_version`` are echoed back but do not pick rows: the diff is always the
    composed approved against the composed draft (either side falling back to the
    other). A brand not yet migrated diffs its legacy versions as before."""
    workspace_id, profile = await _profile(ctx, profile_id)
    rows = await _rows(workspace_id, profile.id)
    if not rows and not await _has_units(workspace_id, profile.id):
        old_doc = await _legacy_version(workspace_id, profile.id, from_version)
        new_doc = await _legacy_version(workspace_id, profile.id, to_version)
        old, new = old_doc.model_dump(), new_doc.model_dump()
    else:
        a, b = _pick(rows, "approved"), _pick(rows, "draft")
        if a is None and b is None:
            raise NotFound("social_blueprint_version", str(from_version))
        old = _to_legacy({u: d.body for u, d in (a or b or {}).items()})
        new = _to_legacy({u: d.body for u, d in (b or a or {}).items()})
    return BlueprintDiffResponse(
        from_version=from_version,
        to_version=to_version,
        sections={
            name: _section_diff(name, old.get(name), new.get(name)) for name in BLUEPRINT_SECTIONS
        },
    )


def _flat(value: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Dotted paths to the non-empty leaves (lists are leaves)."""
    out: dict[str, Any] = {}
    for key, v in value.items():
        if isinstance(v, dict) and v:
            out.update(_flat(v, f"{prefix}{key}."))
        elif v not in ("", None, [], {}):
            out[f"{prefix}{key}"] = v
    return out


async def _unit_version(workspace_id: str, profile_id: str, unit: str, version: int) -> _UnitDoc:
    doc = await _UnitDoc.find_one(
        {"workspace": workspace_id, "profile_id": profile_id, "unit": unit, "version": version}
    )
    if doc is None:
        raise NotFound("social_blueprint_version", f"{unit}@{version}")
    return doc


async def unit_diff(
    ctx: RequestContext,
    unit: str,
    from_version: int,
    to_version: int,
    profile_id: str | None = None,
) -> BlueprintUnitDiffResponse:
    """Field-level diff between two versions of one unit."""
    _kind(unit)
    workspace_id, profile = await _profile(ctx, profile_id)
    old = await _unit_version(workspace_id, profile.id, unit, from_version)
    new = await _unit_version(workspace_id, profile.id, unit, to_version)
    return BlueprintUnitDiffResponse(
        unit=unit,
        from_version=from_version,
        to_version=to_version,
        fields=_key_diff(_flat(old.body), _flat(new.body)),
    )


# ---------------------------------------------------------------------------
# Migration: legacy rows -> units
# ---------------------------------------------------------------------------


def _legacy_sections(doc: _BlueprintDoc) -> dict[str, Any]:
    return {
        **{name: getattr(doc, name) for name in BLUEPRINT_SECTIONS},
        "learning_ids": doc.learning_ids,
    }


async def migrate_profile(
    workspace_id: str, profile_id: str, *, dry_run: bool = False
) -> dict[str, Any]:
    """Split the brand's legacy approved and draft rows into units. The approved row
    becomes every unit's approved version, at the legacy version number; the draft
    becomes a draft (same number) for each unit it changes. Idempotent: a brand
    that has any unit row is skipped, and a concurrent migrate's rows are kept.
    Returns ``{"status": "skipped" | "empty" | "migrated", "units": [...]}``."""
    if await _has_units(workspace_id, profile_id):
        return {"status": "skipped", "units": []}
    approved = await _legacy_find(workspace_id, profile_id, "approved")
    draft = await _legacy_find(workspace_id, profile_id, "draft")
    if approved is None and draft is None:
        return {"status": "empty", "units": []}
    docs: list[_UnitDoc] = []
    base: dict[str, dict] = {}
    if approved is not None:
        base = _apply_legacy({}, _legacy_sections(approved))
        docs += [
            _UnitDoc(
                workspace=workspace_id,
                profile_id=profile_id,
                unit=unit,
                version=approved.version,
                status="approved",
                body=body,
                created_by=approved.created_by,
                approved_by=approved.approved_by,
                approved_at=approved.approved_at,
            )
            for unit, body in base.items()
        ]
    if draft is not None:
        docs += [
            _UnitDoc(
                workspace=workspace_id,
                profile_id=profile_id,
                unit=unit,
                version=draft.version,
                status="draft",
                body=body,
                created_by=draft.created_by,
            )
            for unit, body in _apply_legacy(base, _legacy_sections(draft)).items()
            if body != base.get(unit)
        ]
    report = {"status": "migrated", "units": [(d.unit, d.status, d.version) for d in docs]}
    if dry_run:
        return report
    for doc in docs:
        try:
            await doc.insert()
        except DuplicateKeyError:
            pass  # a concurrent migrate wrote this unit version first; same content
    # no-event: a one-time data move; nothing subscribes.
    return report


async def _all(cursor: Any) -> list[dict]:
    # The async PyMongo driver returns a coroutine; mongomock-motor may not.
    result = cursor.to_list(None)
    return await result if inspect.isawaitable(result) else result


async def legacy_profiles() -> list[tuple[str, str]]:
    """Every (workspace, profile_id) holding a legacy approved or draft row."""
    # global-read: migration discovery across tenants; every write is per brand.
    found = await _all(
        _BlueprintDoc.get_pymongo_collection().find(
            {"status": {"$in": ["approved", "draft"]}}, {"workspace": 1, "profile_id": 1}
        )
    )
    return sorted({(d["workspace"], d["profile_id"]) for d in found})


async def migration_counts() -> tuple[int, int]:
    """(brands with a legacy approved row, brands with approved brand AND strategy
    units). Equal after a correct migration."""
    # global-read: the migration's count check spans every tenant.
    legacy = {
        (d["workspace"], d["profile_id"])
        for d in await _all(
            _BlueprintDoc.get_pymongo_collection().find(
                {"status": "approved"}, {"workspace": 1, "profile_id": 1}
            )
        )
    }
    have: dict[tuple[str, str], set[str]] = {}
    # global-read: as above.
    for d in await _all(
        _UnitDoc.get_pymongo_collection().find(
            {"status": "approved", "unit": {"$in": ["brand", "strategy"]}},
            {"workspace": 1, "profile_id": 1, "unit": 1},
        )
    ):
        have.setdefault((d["workspace"], d["profile_id"]), set()).add(d["unit"])
    return len(legacy), sum(1 for units in have.values() if units == {"brand", "strategy"})


__all__ = [
    "approve",
    "approve_unit",
    "approved_for",
    "catalog",
    "diff",
    "get",
    "legacy_profiles",
    "migrate_profile",
    "migration_counts",
    "regenerate",
    "save_draft",
    "save_unit_draft",
    "seed",
    "unit_diff",
    "units",
]
