# ee/pocketpaw_ee/cloud/growth/social/blueprint_units_dto.py — the wire and body
# models of the per-unit Blueprint (``/blueprint/units...``).
#
# One body model per unit kind: ``BrandBody`` (``brand``), ``StrategyBody``
# (``strategy``), ``PlanBody`` (``plan``) and ``PlayBody`` (every ``play:<id>``).
# Every field defaults empty, so a body may be saved half-filled and a stored
# body that predates a field still validates. Caps follow the legacy Blueprint
# (``MAX_BLUEPRINT_ITEMS`` items of 300 chars, 2000-char text, 8 pillars, 20
# goals). Objective ``baseline`` and ``deadline`` are optional because legacy
# goals carry neither. An objective or play KPI may name a ``metric_key`` (one of
# ``METRIC_KEYS``) that ``live`` knows how to measure; ``metric`` stays the label.
# A play's ``formats`` are ``FormatEntry`` (name + winning / testing / dropped); a
# stored plain string reads as a testing entry, so old rows need no migration
# (``format_entries`` is the same tolerant read over a raw stored body). The play
# decision fields (KPI ``pass_mark`` / ``expand_at``, ``decide_by``) are owner-only
# and left out of a dump when unset, so an untouched old body dumps as it did.
# A strategy keeps one audience (``gtm.icp.target`` plus ``not_for`` and
# ``triggers``) and what people use now in ``positioning.alternatives``; a body in
# the old shape folds on validation (``fold_strategy``, appending, never erasing).
# Creator extras require a disclosure and usage rights (FTC).
# ``BODY_MODELS`` maps a unit kind to its model; ``blueprint.py`` validates a body
# against it and checks cross-unit goal references there (they need the store).
# Request and response classes are distinct (cloud rule 4).

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Annotated, Any, Literal, get_args
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_serializer,
    model_validator,
)

from pocketpaw_ee.cloud.growth.social.domain import (
    MAX_BLUEPRINT_GOALS,
    MAX_BLUEPRINT_ITEMS,
    MAX_BLUEPRINT_LEARNINGS,
    MAX_BLUEPRINT_PILLARS,
    BlueprintChannelName,
    BlueprintStatus,
)
from pocketpaw_ee.cloud.growth.social.dto import (
    MAX_BLUEPRINT_POSTS_PER_WEEK,
    MAX_BLUEPRINT_SENDS_PER_WEEK,
    BlueprintPillarModel,
    BlueprintRoadmapModel,
    BlueprintSectionDiff,
    _BlueprintItem,
    _BlueprintItems,
    _BlueprintText,
    _clean_items,
    _unique,
    fold_icp,
)

UnitStatus = BlueprintStatus
PlayStatus = Literal["core", "testing", "parked"]
MAX_KEY_MESSAGES = 3
MAX_CREATIVE_ANGLES = 3
MetricKey = Literal[
    "impressions_per_week", "engagement_per_post", "link_clicks_per_week", "github_stars"
]
METRIC_KEYS: tuple[str, ...] = get_args(MetricKey)
FormatStatus = Literal["winning", "testing", "dropped"]
FORMAT_STATUSES: tuple[str, ...] = get_args(FormatStatus)

_Short = Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)]
_Id = Annotated[str, StringConstraints(strip_whitespace=True, max_length=40)]
_Amount = Annotated[float, Field(ge=0)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- brand ---------------------------------------------------------------------------------


class Vision(_Body):
    why: _BlueprintText = ""
    vision: _BlueprintText = ""


class ToneOfVoice(_Body):
    tone: _BlueprintText = ""
    do: _BlueprintItems = Field(default_factory=list)
    avoid: _BlueprintItems = Field(default_factory=list)


class WhoWeAre(_Body):
    summary: _BlueprintText = ""
    product: _BlueprintText = ""
    problem: _BlueprintText = ""


class BrandAssets(_Body):
    tagline: _BlueprintText = ""
    colors: _BlueprintItems = Field(default_factory=list)
    names: _BlueprintItems = Field(default_factory=list)


class BrandBody(_Body):
    vision: Vision = Field(default_factory=Vision)
    brand_story: _BlueprintText = ""
    messaging_framework: Annotated[list[_BlueprintText], Field(max_length=MAX_KEY_MESSAGES)] = (
        Field(default_factory=list)
    )
    tone_of_voice: ToneOfVoice = Field(default_factory=ToneOfVoice)
    archetype: _Short = ""
    who_we_are: WhoWeAre = Field(default_factory=WhoWeAre)
    proof: _BlueprintItems = Field(default_factory=list)
    assets: BrandAssets = Field(default_factory=BrandAssets)


# --- strategy ------------------------------------------------------------------------------


class MarketResearch(_Body):
    main_challenge: _BlueprintText = ""
    competitor_notes: _BlueprintItems = Field(default_factory=list)
    interview_notes: _BlueprintItems = Field(default_factory=list)
    trends: _BlueprintItems = Field(default_factory=list)
    baseline: _BlueprintText = ""
    tam_sam_som: _BlueprintText = ""


class Positioning(_Body):
    job_to_be_done: _BlueprintText = ""
    value_proposition: _BlueprintText = ""
    usp: _BlueprintText = ""
    category: _BlueprintText = ""
    statement: _BlueprintText = ""
    # What people use now (moved here from ``market_research``).
    alternatives: _BlueprintItems = Field(default_factory=list)


class Icp(_Body):
    target: _BlueprintText = ""
    not_for: _BlueprintItems = Field(default_factory=list)
    triggers: _BlueprintItems = Field(default_factory=list)


class Gtm(_Body):
    icp: Icp = Field(default_factory=Icp)
    pricing_notes: _BlueprintText = ""
    launch_plan: _BlueprintText = ""


class Objective(_Body):
    """An owner-entered objective; ``id`` is minted by the service when empty."""

    id: _Id = ""
    metric: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    baseline: _Amount | None = None
    target: _Amount
    deadline: date | None = None
    channel: BlueprintChannelName | None = None
    metric_key: MetricKey | None = None


class Goals(_Body):
    business_goal: _BlueprintText = ""
    objectives: Annotated[list[Objective], Field(max_length=MAX_BLUEPRINT_GOALS)] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def _distinct(self) -> Goals:
        _unique([o.id for o in self.objectives if o.id], "objective ids")
        return self


class Choices(_Body):
    guiding_policy: _BlueprintText = ""
    growth_engine: _BlueprintText = ""
    core_play: _BlueprintText = ""
    tests: _BlueprintItems = Field(default_factory=list)
    not_doing: _BlueprintItems = Field(default_factory=list)


def fold_strategy(body: Any) -> Any:
    """A copy of a strategy ``body`` in the current shape: ``gtm.icp``'s retired lists
    folded into ``target`` (``fold_icp``) and ``market_research.alternatives`` moved
    onto the end of ``positioning.alternatives``. Only non-empty old values fold;
    they append (an item already there, ignoring case, is skipped) and never
    replace. Idempotent; the input is not touched. Kept one release."""
    if not isinstance(body, dict):
        return body
    out = dict(body)
    gtm = out.get("gtm")
    if isinstance(gtm, dict) and isinstance(gtm.get("icp"), dict):
        out["gtm"] = {**gtm, "icp": fold_icp(gtm["icp"])}
    research = out.get("market_research")
    if isinstance(research, dict) and "alternatives" in research:
        research = dict(research)
        moved = _clean_items(research.pop("alternatives"))
        out["market_research"] = research
        positioning = out.get("positioning")
        if moved and isinstance(positioning, dict | None):
            positioning = dict(positioning or {})
            have = positioning.get("alternatives")
            have = list(have) if isinstance(have, list) else []
            seen = {s.strip().lower() for s in have if isinstance(s, str)}
            for item in moved:
                if item.lower() not in seen:
                    have.append(item)
                    seen.add(item.lower())
            # ponytail: past the item cap the oldest win; the migration dump keeps the rest.
            positioning["alternatives"] = have[:MAX_BLUEPRINT_ITEMS]
            out["positioning"] = positioning
    return out


class StrategyBody(_Body):
    market_research: MarketResearch = Field(default_factory=MarketResearch)
    positioning: Positioning = Field(default_factory=Positioning)
    gtm: Gtm = Field(default_factory=Gtm)
    goals: Goals = Field(default_factory=Goals)
    choices: Choices = Field(default_factory=Choices)
    content_pillars: Annotated[
        list[BlueprintPillarModel], Field(max_length=MAX_BLUEPRINT_PILLARS)
    ] = Field(default_factory=list)
    learning_ids: Annotated[
        list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]],
        Field(max_length=MAX_BLUEPRINT_LEARNINGS),
    ] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _fold_old_shape(cls, v: Any) -> Any:
        return fold_strategy(v)

    @model_validator(mode="after")
    def _distinct(self) -> StrategyBody:
        _unique([p.name for p in self.content_pillars], "pillar names")
        _unique(self.learning_ids, "learning ids")
        return self


# --- plan ----------------------------------------------------------------------------------


class Budget(_Body):
    money_per_month: _Amount | None = None
    hours_per_week: _Amount | None = None


class PlanBody(_Body):
    roadmap: BlueprintRoadmapModel = Field(default_factory=BlueprintRoadmapModel)
    budget: Budget = Field(default_factory=Budget)


# --- play ----------------------------------------------------------------------------------


class PillarShare(_Body):
    pillar: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    weight: int = Field(default=0, ge=0, le=100)


def _drop_unset(out: Any, keys: tuple[str, ...]) -> Any:
    """A dump without ``keys`` whose value is None (works on pydantic 2.10, which has
    no ``exclude_if``), so a body that never set them dumps as it did before."""
    if isinstance(out, dict):
        for k in keys:
            if out.get(k) is None:
                out.pop(k, None)
    return out


def format_key(name: str) -> str:
    """A format name as the fixed table spells it: lower case, runs of spaces,
    hyphens or underscores as one ``_`` (``X Thread`` and ``x-thread`` are ``x_thread``)."""
    return re.sub(r"[\s\-_]+", "_", name.strip().lower())


class Kpi(_Body):
    metric: _Short = ""
    baseline: _Amount | None = None
    target: _Amount | None = None
    metric_key: MetricKey | None = None
    # Owner-only decision marks: the play passes its test at ``pass_mark`` and earns
    # a second core play at ``expand_at``.
    pass_mark: _Amount | None = None
    expand_at: _Amount | None = None

    @model_serializer(mode="wrap")
    def _dump(self, handler: Any) -> Any:
        return _drop_unset(handler(self), ("pass_mark", "expand_at"))


class FormatEntry(_Body):
    name: _BlueprintItem
    status: FormatStatus = "testing"

    @model_validator(mode="before")
    @classmethod
    def _plain_string(cls, v: Any) -> Any:
        return {"name": v} if isinstance(v, str) else v


def format_entries(raw: Any) -> list[tuple[str, str]]:
    """``(name, status)`` per entry of a stored ``formats`` list, which may hold plain
    strings (old rows) or ``{name, status}`` dicts; anything unreadable is skipped."""
    out = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str) and item.strip():
            out.append((item.strip(), "testing"))
        elif isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"].strip():
            status = item.get("status")
            out.append((item["name"].strip(), status if status in FORMAT_STATUSES else "testing"))
    return out


class SeoExtras(_Body):
    topic_clusters: _BlueprintItems = Field(default_factory=list)
    article_queue: _BlueprintItems = Field(default_factory=list)
    ai_search_targets: _BlueprintItems = Field(default_factory=list)


class PaidExtras(_Body):
    objective: _BlueprintText = ""
    audience: _BlueprintText = ""
    daily_budget: _Amount | None = None
    creative_angles: Annotated[list[_BlueprintText], Field(max_length=MAX_CREATIVE_ANGLES)] = Field(
        default_factory=list
    )
    landing_url: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] = ""

    @field_validator("landing_url")
    @classmethod
    def _http(cls, v: str) -> str:
        if v:
            parts = urlsplit(v)
            if parts.scheme not in ("http", "https") or not parts.netloc:
                raise ValueError("landing_url must be an http(s) URL")
        return v


_Required = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class CreatorExtras(_Body):
    """Disclosure and usage rights are required: a creator post must say it is paid."""

    program_type: _BlueprintText = ""
    creator_profile: _BlueprintText = ""
    payment: _BlueprintText = ""
    brief: _BlueprintText = ""
    disclosure: _Required
    usage_rights: _Required


class PlayBody(_Body):
    status: PlayStatus = "testing"
    role: _BlueprintText = ""
    goal_id: _Id | None = None
    audience: _BlueprintText = ""
    formats: Annotated[list[FormatEntry], Field(max_length=MAX_BLUEPRINT_ITEMS)] = Field(
        default_factory=list
    )
    pillar_mix: Annotated[list[PillarShare], Field(max_length=MAX_BLUEPRINT_PILLARS)] = Field(
        default_factory=list
    )
    posts_per_week: int = Field(default=0, ge=0, le=MAX_BLUEPRINT_POSTS_PER_WEEK)
    sends_per_week: int = Field(default=0, ge=0, le=MAX_BLUEPRINT_SENDS_PER_WEEK)
    voice_tweak: _BlueprintText = ""
    cta: _BlueprintText = ""
    repurpose_from: _Id | None = None
    kpi: Kpi = Field(default_factory=Kpi)
    budget: _Amount | None = None
    hours: _Amount | None = None
    rules: _BlueprintItems = Field(default_factory=list)
    autonomy: _BlueprintText = ""
    seo: SeoExtras | None = None
    paid: PaidExtras | None = None
    creator: CreatorExtras | None = None
    decide_by: date | None = None

    @model_validator(mode="after")
    def _distinct(self) -> PlayBody:
        _unique([format_key(f.name) for f in self.formats], "format names")
        return self

    @model_serializer(mode="wrap")
    def _dump(self, handler: Any) -> Any:
        return _drop_unset(handler(self), ("decide_by",))


BODY_MODELS: dict[str, type[_Body]] = {
    "brand": BrandBody,
    "strategy": StrategyBody,
    "plan": PlanBody,
    "play": PlayBody,
}


# --- requests / responses ------------------------------------------------------------------


class SaveUnitDraftRequest(BaseModel):
    """``PUT /blueprint/units/{unit}/draft``: the unit's whole body plus the
    ``version`` and ``rev`` of the row the editor started from (the draft, else the
    approved, else 0 / 0). Either one stale is a 409."""

    model_config = ConfigDict(extra="forbid")

    body: dict
    base_version: int = Field(ge=0)
    base_rev: int = Field(ge=0)


class BlueprintUnitRowResponse(BaseModel):
    id: str
    unit: str
    version: int
    rev: int
    status: UnitStatus
    body: dict
    created_by: str
    approved_by: str | None
    approved_at: str | None
    created_at: str | None
    updated_at: str | None


class BlueprintUnitStateResponse(BaseModel):
    """``warnings``: plain-text notes that never block the write (a second core
    play while another is under its ``expand_at``)."""

    unit: str
    approved: BlueprintUnitRowResponse | None
    draft: BlueprintUnitRowResponse | None
    warnings: list[str] = Field(default_factory=list)


class SetFormatStatusRequest(BaseModel):
    """``POST /blueprint/units/{unit}/formats/{name}/status``: the new status and the
    ``rev`` of the approved row it was read at (stale is a 409)."""

    model_config = ConfigDict(extra="forbid")

    status: FormatStatus
    rev: int = Field(ge=0)


class BranchResponse(BaseModel):
    id: str
    label: str
    purpose: str
    groups: list[str]
    leaves: list[str]
    plan_only: bool


class PlayResponse(BaseModel):
    id: str
    label: str
    branch: str
    leaf: str
    platform: str
    peso: str
    acts: bool


class PlayCatalogResponse(BaseModel):
    branches: list[BranchResponse]
    plays: list[PlayResponse]


class BlueprintUnitsResponse(BaseModel):
    """Every unit's approved and draft rows (units without rows are omitted) plus
    the play catalog."""

    profile_id: str
    units: list[BlueprintUnitStateResponse]
    catalog: PlayCatalogResponse


class BlueprintUnitDiffResponse(BaseModel):
    """Field-level: dotted body paths (``gtm.icp.target``) added, removed or
    changed between two versions of one unit. Empty values count as absent."""

    unit: str
    from_version: int
    to_version: int
    fields: BlueprintSectionDiff


class LiveValueResponse(BaseModel):
    """One measured number. ``value`` is null when there is no data or no
    ``metric_key``; a measured 0 is a number."""

    value: float | None = None
    as_of: datetime | None = None
    posts_counted: int = 0


class LiveFormatResponse(BaseModel):
    """One format's numbers within a play: measured posts, engagement per post, and a
    suggestion once it has enough posts to call."""

    name: str
    posts: int = 0
    avg: float | None = None
    suggestion: Literal["double_down", "drop"] | None = None
    reason: str | None = None


class LivePlayResponse(LiveValueResponse):
    """A play's KPI value plus its per-format numbers."""

    formats: list[LiveFormatResponse] = Field(default_factory=list)


class BlueprintLiveResponse(BaseModel):
    """``GET /blueprint/live``: per objective id and per ``play:<id>`` unit."""

    objectives: dict[str, LiveValueResponse] = Field(default_factory=dict)
    plays: dict[str, LivePlayResponse] = Field(default_factory=dict)
