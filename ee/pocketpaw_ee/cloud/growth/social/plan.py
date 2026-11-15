# ee/pocketpaw_ee/cloud/growth/social/plan.py — the weekly Plan: post slots per
# day for one brand, computed from its APPROVED Blueprint (never the draft) and
# its ideas. Nothing is stored; there is no slot table.
#
# Slot counts: a day starts from the Blueprint channels' ``posts_per_week``; the
# roadmap phase active that day (``active_phase``: the phase whose inclusive
# [start, end] covers it; when several overlap the earliest start wins, a tie
# goes to the first listed) overrides only the channels its cadence names. With
# no approved Blueprint there are no counts. Only ``POST_CHANNELS`` (x, reddit,
# linkedin) get slots; email / whatsapp sends stay in the Blueprint. A weekly
# count n is spread by ISO weekday i (Monday = 0): ``n // 7`` each, plus one on
# the first ``n % 7`` days, so 3 a week is Mon, Tue, Wed.
#
# Filling: an idea scheduled that week (unless skipped) sits in its channel's
# slots on its UTC date, in time order; one with no free slot is added as an
# extra filled slot, so a scheduled idea never moves or drops when the cadence
# changes. Only in the CURRENT week (UTC, ``blueprint._today``) do the empty
# slots on today and later, day by day, take approved ideas with no date and no
# post yet, oldest first, each at most once; past days and other weeks keep
# their empty slots. Waiting ideas are read per channel, capped at that
# channel's empty slots. An idea with no platform counts as ``x``.
#
# Tenancy: the route resolves the profile through ``social.service
# .require_profile`` (sibling 404); every idea read filters on workspace and
# profile.

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta

from pocketpaw_ee.cloud._core.context import RequestContext
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud._core.time import iso_utc
from pocketpaw_ee.cloud.growth.social import blueprint as social_blueprint
from pocketpaw_ee.cloud.growth.social import service as social_service
from pocketpaw_ee.cloud.growth.social.domain import POST_CHANNELS, GrowthBlueprint, RoadmapPhase
from pocketpaw_ee.cloud.growth.social.dto import PlanDayModel, PlanSlotModel, WeekPlanResponse
from pocketpaw_ee.cloud.growth.social.review import parse_week


def active_phase(blueprint: GrowthBlueprint, day: date) -> RoadmapPhase | None:
    """The phase covering ``day``; earliest start wins, ties go to the first listed."""
    covering = [p for p in blueprint.roadmap.phases if p.start <= day <= p.end]
    return min(covering, key=lambda p: p.start, default=None)


def _weekly(blueprint: GrowthBlueprint | None, day: date) -> dict[str, int]:
    if blueprint is None:
        return {}
    weekly = {c.channel: c.posts_per_week for c in blueprint.channels}
    phase = active_phase(blueprint, day)
    if phase is not None:
        weekly.update({c.channel: c.posts_per_week for c in phase.cadence})
    return {k: v for k, v in weekly.items() if k in POST_CHANNELS}


def _share(weekly: int, weekday: int) -> int:
    return weekly // 7 + (weekday < weekly % 7)


async def week_slots(workspace_id: str, profile_id: str, iso_week: str) -> WeekPlanResponse:
    today = social_blueprint._today()
    try:
        monday = parse_week(iso_week, today)
    except ValueError as exc:
        raise ValidationError(
            "social.plan_bad_week", "week must be an ISO week like 2026-W46"
        ) from exc
    days = [monday + timedelta(days=i) for i in range(7)]
    start = datetime.combine(monday, time(), tzinfo=UTC)
    blueprint = await social_blueprint.approved_for(workspace_id, profile_id)
    aware = social_service._aware

    scheduled: dict[tuple[date, str], list] = defaultdict(list)
    for idea in await social_service.scheduled_ideas(
        workspace_id, profile_id, start, start + timedelta(7)
    ):
        scheduled[(aware(idea.scheduled_at).date(), idea.platform or "x")].append(idea)

    # Slots per post channel for each day of the week.
    counts = []
    for i, day in enumerate(days):
        weekly = _weekly(blueprint, day)
        counts.append({c: _share(weekly.get(c, 0), i) for c in POST_CHANNELS})
    fill_from = today if monday <= today <= days[-1] else None
    waiting: dict[str, list] = {}
    for channel in POST_CHANNELS:
        free = 0
        if fill_from is not None:
            free = sum(
                max(counts[i][channel] - len(scheduled.get((day, channel), [])), 0)
                for i, day in enumerate(days)
                if day >= fill_from
            )
        waiting[channel] = await social_service.waiting_ideas(
            workspace_id, profile_id, channel, free
        )

    out = []
    for i, day in enumerate(days):
        slots: list[PlanSlotModel] = []
        others = sorted({c for d, c in scheduled if d == day} - set(POST_CHANNELS))
        for channel in [*POST_CHANNELS, *others]:
            placed = sorted(
                scheduled.get((day, channel), []), key=lambda x: (aware(x.scheduled_at), x.id)
            )
            row = [
                PlanSlotModel(channel=channel, idea_id=x.id, scheduled_at=iso_utc(x.scheduled_at))
                for x in placed
            ]
            fills = waiting.get(channel, []) if fill_from is not None and day >= fill_from else []
            for _ in range(counts[i].get(channel, 0) - len(row)):
                nxt = fills.pop(0) if fills else None
                row.append(PlanSlotModel(channel=channel, idea_id=nxt.id if nxt else None))
            slots.extend(row)
        out.append(PlanDayModel(date=day.isoformat(), slots=slots))
    return WeekPlanResponse(week=iso_week, days=out)


async def get_week(
    ctx: RequestContext, iso_week: str, profile_id: str | None = None
) -> WeekPlanResponse:
    """``GET /plan``: the caller's profile (sibling 404), then its week."""
    workspace_id, profile = await social_service.require_profile(ctx, profile_id)
    return await week_slots(workspace_id, profile.id, iso_week)


__all__ = ["active_phase", "get_week", "week_slots"]
