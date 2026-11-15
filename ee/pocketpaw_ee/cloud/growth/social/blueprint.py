# ee/pocketpaw_ee/cloud/growth/social/blueprint.py — the Brand Blueprint service
# and the SOLE owner of GrowthBlueprint doc writes (service-is-repo; the
# import-linter "Growth" contract keeps everything else off the doc class).
#
# A brand profile's blueprint is a chain of versions. At most one ``approved``
# and one ``draft`` exist per (workspace, profile_id); every older version is
# ``superseded``. ``seed`` writes version 1 as a draft from the profile's
# analysis (falling back to what the owner typed), its weekly post quota and the
# workspace's active ICPs, and is a no-op once any version exists.
# ``save_draft`` edits the draft, opening one from the approved when there is
# none. ``regenerate`` runs the social analyst and writes its text into a NEW
# draft version, superseding the old draft and never touching the approved.
# ``approve`` turns the draft into the approved and supersedes the previous one.
#
# Numbers are never model-written. Pillar weights are owner-entered, an equal
# split of 100 computed here, or 0 for a pillar the analyst newly named; channel
# cadences come from the profile or the owner; goals are owner-entered only, and
# regenerate copies channels, goals, the roadmap and learning ids unchanged.
# Regenerated text must pass ``ideas.check_texts`` (no em or en dash, no number
# the owner did not type): a failing item is dropped, and a failing field keeps
# its previous value.
#
# Tenancy: every query filters on ``workspace`` and ``profile_id``, and the
# profile is resolved through ``social.service`` (same NotFound as siblings).
# The one-draft / one-approved rule is checked by queries, not a unique index,
# so two simultaneous approves could race; the unique version index stops two
# writers forking the same version. ``approved_for`` is the read the drafting
# agents use. Writes carry ``# no-event:`` markers: nothing subscribes yet.

from __future__ import annotations

import copy
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from pymongo.errors import DuplicateKeyError

from pocketpaw_ee.cloud._core.context import RequestContext
from pocketpaw_ee.cloud._core.errors import CloudError, ConflictError, NotFound, ValidationError
from pocketpaw_ee.cloud._core.time import iso_utc
from pocketpaw_ee.cloud.growth.social import analyst as social_analyst
from pocketpaw_ee.cloud.growth.social import ideas as social_ideas
from pocketpaw_ee.cloud.growth.social.domain import (
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
    BlueprintVoice,
    GrowthBlueprint,
    SocialAnalysis,
    SocialProfile,
)
from pocketpaw_ee.cloud.growth.social.dto import (
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
    SaveBlueprintDraftRequest,
)
from pocketpaw_ee.cloud.models.growth_blueprint import GrowthBlueprint as _BlueprintDoc

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


def _to_domain(doc: _BlueprintDoc) -> GrowthBlueprint:
    identity = doc.identity or {}
    audience = doc.audience or {}
    voice = doc.voice or {}
    return GrowthBlueprint(
        id=str(doc.id),
        workspace_id=doc.workspace,
        profile_id=doc.profile_id,
        version=doc.version,
        status=doc.status,
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
            for p in _dicts(doc.pillars)
        ),
        channels=tuple(
            BlueprintChannel(
                channel=_str(c.get("channel")),
                posts_per_week=_int(c.get("posts_per_week")),
                sends_per_week=_int(c.get("sends_per_week")),
            )
            for c in _dicts(doc.channels)
        ),
        goals=tuple(
            BlueprintGoal(
                id=_str(g.get("id")),
                metric=_str(g.get("metric")),
                target=float(g.get("target") or 0),
                channel=_str(g.get("channel")) or None,
            )
            for g in _dicts(doc.goals)
        ),
        roadmap={"phases": _dicts((doc.roadmap or {}).get("phases"))},
        learning_ids=tuple(doc.learning_ids or ()),
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
        roadmap=BlueprintRoadmapModel(phases=list(bp.roadmap.get("phases") or [])),
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
# Reads
# ---------------------------------------------------------------------------


async def _profile(ctx: RequestContext, profile_id: str | None) -> tuple[str, SocialProfile]:
    from pocketpaw_ee.cloud.growth.social import service as social_service

    workspace_id = social_service._require_workspace(ctx)
    doc = await social_service._require_profile_doc(workspace_id, profile_id)
    return workspace_id, social_service._profile_to_domain(doc)


async def _find(workspace_id: str, profile_id: str, status: str) -> _BlueprintDoc | None:
    return await _BlueprintDoc.find_one(
        {"workspace": workspace_id, "profile_id": profile_id, "status": status}
    )


async def _next_version(workspace_id: str, profile_id: str) -> int:
    docs = (
        await _BlueprintDoc.find({"workspace": workspace_id, "profile_id": profile_id})
        .sort([("version", -1)])
        .limit(1)
        .to_list()
    )
    return docs[0].version + 1 if docs else 1


async def _state(workspace_id: str, profile_id: str) -> BlueprintStateResponse:
    return BlueprintStateResponse(
        approved=_response(await _find(workspace_id, profile_id, "approved")),
        draft=_response(await _find(workspace_id, profile_id, "draft")),
    )


async def get(ctx: RequestContext, profile_id: str | None = None) -> BlueprintStateResponse:
    """The profile's approved blueprint and its open draft (either may be null)."""
    workspace_id, profile = await _profile(ctx, profile_id)
    return await _state(workspace_id, profile.id)


async def approved_for(workspace_id: str, profile_id: str) -> GrowthBlueprint | None:
    """The approved blueprint the drafting agents follow, or None. Never falls back
    to another profile."""
    doc = await _find(workspace_id, profile_id, "approved")
    return _to_domain(doc) if doc is not None else None


# ---------------------------------------------------------------------------
# Building sections
# ---------------------------------------------------------------------------


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _items(values: Any, limit: int = MAX_BLUEPRINT_ITEMS, chars: int = _ITEM_CHARS) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        text = _squash(value)[:chars] if isinstance(value, str) else ""
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out[:limit]


def _segments(audience: str) -> list[str]:
    return _items(re.split(r"[\n;]+", audience or ""))


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


async def _icp_sentences(ctx: RequestContext) -> list[str]:
    from pocketpaw_ee.cloud.growth import service as growth_service

    icps = await growth_service.list_icps(ctx, status="active")
    return _items([icp.criteria for icp in icps], MAX_ICP_SENTENCES)


def _analysis_or_typed(profile: SocialProfile) -> SocialAnalysis:
    return social_analyst.apply_typed_fields(
        profile.analysis or SocialAnalysis(), dict(profile.description)
    )


async def _seed_sections(ctx: RequestContext, profile: SocialProfile) -> dict[str, Any]:
    a = _analysis_or_typed(profile)
    return {
        "identity": {"summary": a.summary, "product": a.product, "problem": a.problem},
        "audience": {
            "segments": _segments(a.audience),
            "icp_sentences": await _icp_sentences(ctx),
        },
        "voice": {"tone": a.tone, "avoid": _items(a.avoid)},
        "pillars": _pillars(list(a.content_pillars), list(a.hooks), None),
        "channels": [
            {"channel": "x", "posts_per_week": profile.weekly_post_quota, "sends_per_week": 0}
        ],
        "goals": [],
        "roadmap": {"phases": []},
    }


def _carried(doc: _BlueprintDoc) -> dict[str, Any]:
    out = {name: copy.deepcopy(getattr(doc, name)) for name in BLUEPRINT_SECTIONS}
    out["learning_ids"] = list(doc.learning_ids or [])
    return out


def _source_text(profile: SocialProfile) -> str:
    """What the owner typed: the only text a regenerated number may come from."""
    return " ".join([profile.company_name, profile.website or "", *profile.description.values()])


def _regenerated(base: _BlueprintDoc, a: SocialAnalysis, source: str) -> dict[str, Any]:
    """``base``'s sections with the analyst's text swapped in where it passes the checks."""

    def ok(text: str) -> bool:
        return bool(text) and not social_ideas.check_texts([text], source)

    def text(new: str, old: Any) -> str:
        return new if ok(new) else _str(old)

    def items(values: Any, limit: int = MAX_BLUEPRINT_ITEMS, chars: int = _ITEM_CHARS) -> list:
        return [v for v in _items(values, limit, chars) if ok(v)]

    out = _carried(base)
    identity, audience, voice = base.identity or {}, base.audience or {}, base.voice or {}
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
        old = _dicts(base.pillars)
        weights = {_str(p.get("name")).lower(): _int(p.get("weight")) for p in old} if old else None
        pillars = _pillars(names, items(a.hooks), weights)
        if not any(p["hooks"] for p in pillars):
            old_hooks = {_str(p.get("name")).lower(): list(_strs(p.get("hooks"))) for p in old}
            for p in pillars:
                p["hooks"] = old_hooks.get(p["name"].lower(), [])
        out["pillars"] = pillars
    return out


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


async def _insert(doc: _BlueprintDoc) -> None:
    try:
        await doc.insert()
    except DuplicateKeyError as exc:
        raise ConflictError(
            "social.blueprint_version_taken", "Someone else changed this blueprint. Reload it."
        ) from exc


async def seed(ctx: RequestContext, profile_id: str | None = None) -> BlueprintStateResponse:
    """Write the first draft from the profile. Does nothing once any version exists."""
    workspace_id, profile = await _profile(ctx, profile_id)
    existing = await _BlueprintDoc.find_one({"workspace": workspace_id, "profile_id": profile.id})
    if existing is None:
        doc = _BlueprintDoc(
            workspace=workspace_id,
            profile_id=profile.id,
            version=1,
            status="draft",
            created_by=ctx.user_id or "",
            **await _seed_sections(ctx, profile),
        )
        try:
            await doc.insert()
        except DuplicateKeyError:
            pass  # a concurrent seed wrote version 1 first; seeding is idempotent
        # no-event: Growth › Social has no realtime subscriber; the Blueprint page re-fetches.
    return await _state(workspace_id, profile.id)


def _apply_draft_edit(doc: _BlueprintDoc, body: SaveBlueprintDraftRequest) -> None:
    # Full section shapes (nested defaults included), so every stored version diffs alike.
    dumped = body.model_dump()
    for name in body.model_fields_set:
        value = dumped[name]
        if value is None:
            continue
        if name == "goals":
            value = [{**g, "id": g.get("id") or uuid.uuid4().hex[:12]} for g in value]
        setattr(doc, name, value)


async def save_draft(
    ctx: RequestContext, body: SaveBlueprintDraftRequest, profile_id: str | None = None
) -> BlueprintStateResponse:
    """Replace the sections sent on the draft, opening a draft from the approved when
    there is none. 404 before the blueprint is seeded."""
    body = SaveBlueprintDraftRequest.model_validate(body)
    workspace_id, profile = await _profile(ctx, profile_id)
    draft = await _find(workspace_id, profile.id, "draft")
    if draft is None:
        approved = await _find(workspace_id, profile.id, "approved")
        if approved is None:
            raise NotFound("social_blueprint")
        draft = _BlueprintDoc(
            workspace=workspace_id,
            profile_id=profile.id,
            version=await _next_version(workspace_id, profile.id),
            status="draft",
            created_by=ctx.user_id or "",
            **_carried(approved),
        )
        _apply_draft_edit(draft, body)
        await _insert(draft)
    else:
        _apply_draft_edit(draft, body)
        draft.updatedAt = datetime.now(UTC)
        await draft.save()
    # no-event: Growth › Social has no realtime subscriber; the Blueprint page re-fetches.
    return await _state(workspace_id, profile.id)


async def regenerate(ctx: RequestContext, profile_id: str | None = None) -> BlueprintStateResponse:
    """Rerun the analyst and write its text into a new draft version. The approved
    version is never touched; numbers and goals are carried over, never generated."""
    analyze_fn = social_analyst.resolve_analyze_fn()
    if analyze_fn is None:
        raise CloudError(
            503,
            "social.analyzer_unavailable",
            "Website analysis is not configured on this deployment",
        )
    workspace_id, profile = await _profile(ctx, profile_id)
    base = await _find(workspace_id, profile.id, "draft") or await _find(
        workspace_id, profile.id, "approved"
    )
    if base is None:
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
    base = await _find(workspace_id, profile.id, "draft") or await _find(
        workspace_id, profile.id, "approved"
    )
    if base is None:
        raise NotFound("social_blueprint")
    doc = _BlueprintDoc(
        workspace=workspace_id,
        profile_id=profile.id,
        version=await _next_version(workspace_id, profile.id),
        status="draft",
        created_by=ctx.user_id or "",
        **_regenerated(base, analysis, _source_text(profile)),
    )
    await _insert(doc)
    await _BlueprintDoc.find(
        {
            "workspace": workspace_id,
            "profile_id": profile.id,
            "status": "draft",
            "_id": {"$ne": doc.id},
        }
    ).update({"$set": {"status": "superseded", "updatedAt": datetime.now(UTC)}})
    # no-event: Growth › Social has no realtime subscriber; the Blueprint page re-fetches.
    return await _state(workspace_id, profile.id)


async def approve(ctx: RequestContext, profile_id: str | None = None) -> BlueprintStateResponse:
    """The draft becomes the approved blueprint; the previous approved is superseded.
    409 when there is no draft."""
    workspace_id, profile = await _profile(ctx, profile_id)
    draft = await _find(workspace_id, profile.id, "draft")
    if draft is None:
        raise ConflictError("social.blueprint_no_draft", "There is no draft to approve")
    now = datetime.now(UTC)
    await _BlueprintDoc.find(
        {"workspace": workspace_id, "profile_id": profile.id, "status": "approved"}
    ).update({"$set": {"status": "superseded", "updatedAt": now}})
    draft.status = "approved"
    draft.approved_by = ctx.user_id or ""
    draft.approved_at = now
    draft.updatedAt = now
    await draft.save()
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
    raw = value if isinstance(value, dict) else {}
    return {k: v for k, v in raw.items() if v not in ("", None, [], {})}


def _section_diff(section: str, old: Any, new: Any) -> BlueprintSectionDiff:
    a, b = _keyed(section, old), _keyed(section, new)
    return BlueprintSectionDiff(
        added=[k for k in b if k not in a],
        removed=[k for k in a if k not in b],
        changed=[k for k in b if k in a and a[k] != b[k]],
    )


async def _version(workspace_id: str, profile_id: str, version: int) -> _BlueprintDoc:
    doc = await _BlueprintDoc.find_one(
        {"workspace": workspace_id, "profile_id": profile_id, "version": version}
    )
    if doc is None:
        raise NotFound("social_blueprint_version", str(version))
    return doc


async def diff(
    ctx: RequestContext, from_version: int, to_version: int, profile_id: str | None = None
) -> BlueprintDiffResponse:
    """What changed per section between two versions of the profile's blueprint."""
    workspace_id, profile = await _profile(ctx, profile_id)
    old = await _version(workspace_id, profile.id, from_version)
    new = await _version(workspace_id, profile.id, to_version)
    return BlueprintDiffResponse(
        from_version=from_version,
        to_version=to_version,
        sections={
            name: _section_diff(name, getattr(old, name), getattr(new, name))
            for name in BLUEPRINT_SECTIONS
        },
    )


__all__ = ["approve", "approved_for", "diff", "get", "regenerate", "save_draft", "seed"]
