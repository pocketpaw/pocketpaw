# ee/pocketpaw_ee/cloud/growth/social/live.py — live values for the approved
# Blueprint's objectives and play KPIs (``GET /blueprint/live``). Nothing stored.
#
# The Blueprint comes only from ``blueprint.approved_for`` (none: empty maps); the
# numbers from ``service.live_posts`` / ``service.latest_repo_stars``, scoped to
# the workspace and brand. Each objective or play KPI may name a ``metric_key``:
# - ``impressions_per_week``: posts published in the last 7 days, each at its
#   largest age bucket, summed.
# - ``engagement_per_post``: likes + replies + reposts at the 7-day snapshot,
#   averaged over the posts that have one (younger posts are left out; no window).
# - ``link_clicks_per_week``: tracked-link clicks over the last 7 UTC days, today
#   included, on published posts that have a link.
# - ``github_stars``: each brand repo's newest stored stars, summed.
# No key, or no data, is ``value: null``, never 0; a measured 0 is a number.
# ``as_of`` is the newest data point counted (now for clicks, which count live).
# Scope: an objective with a ``channel`` counts only ideas on that platform, one
# without counts the whole brand. A play counts only ideas on its platform, and an
# idea with no ``play_id`` counts toward a play only when that play is its
# platform's one active acting post play (two or more: the play's post values are
# null). Stars are brand-wide.

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from pocketpaw_ee.cloud._core.context import RequestContext
from pocketpaw_ee.cloud.growth.social import blueprint as social_blueprint
from pocketpaw_ee.cloud.growth.social import play_catalog
from pocketpaw_ee.cloud.growth.social import service as social_service
from pocketpaw_ee.cloud.growth.social.blueprint_units_dto import (
    BlueprintLiveResponse,
    LiveValueResponse,
)
from pocketpaw_ee.cloud.growth.social.domain import GrowthBlueprint, LivePost, PostMetrics

WEEK_DAYS = 7
ENGAGEMENT_BUCKET = 7
_ACTIVE = ("core", "testing")


def _newest(snaps: list[PostMetrics]) -> datetime | None:
    return max((s.captured_at for s in snaps if s.captured_at), default=None)


def impressions_per_week(posts: list[LivePost], now: datetime) -> LiveValueResponse:
    since = now - timedelta(days=WEEK_DAYS)
    latest = [
        max(p.snapshots, key=lambda s: s.age_bucket)
        for p in posts
        if p.snapshots and p.published_at >= since
    ]
    if not latest:
        return LiveValueResponse()
    return LiveValueResponse(
        value=float(sum(s.impressions for s in latest)),
        as_of=_newest(latest),
        posts_counted=len(latest),
    )


def engagement_per_post(posts: list[LivePost], now: datetime) -> LiveValueResponse:
    """Only posts with a 7-day snapshot count; a younger post is left out."""
    week = [s for p in posts for s in p.snapshots if s.age_bucket == ENGAGEMENT_BUCKET]
    if not week:
        return LiveValueResponse()
    total = sum(s.likes + s.replies + s.reposts for s in week)
    return LiveValueResponse(value=total / len(week), as_of=_newest(week), posts_counted=len(week))


def link_clicks_per_week(posts: list[LivePost], now: datetime) -> LiveValueResponse:
    linked = [p for p in posts if p.link_clicks]
    if not linked:
        return LiveValueResponse()
    today = now.astimezone(UTC).date()
    days = {(today - timedelta(days=i)).isoformat() for i in range(WEEK_DAYS)}
    clicks = sum(n for p in linked for c in p.link_clicks for day, n in c.items() if day in days)
    return LiveValueResponse(value=float(clicks), as_of=now, posts_counted=len(linked))


def github_stars(stars: list[tuple[int, datetime]]) -> LiveValueResponse:
    if not stars:
        return LiveValueResponse()
    return LiveValueResponse(
        value=float(sum(n for n, _ in stars)), as_of=max(at for _, at in stars)
    )


_POST_METRICS: dict[str, Callable[[list[LivePost], datetime], LiveValueResponse]] = {
    "impressions_per_week": impressions_per_week,
    "engagement_per_post": engagement_per_post,
    "link_clicks_per_week": link_clicks_per_week,
}


def _play_posts(
    play_id: str, acting: dict[str, list[str]], posts: list[LivePost]
) -> list[LivePost] | None:
    """The posts counted toward a play; None when they cannot be told apart."""
    q = play_catalog.play(play_id)
    if q is None or q.channel is None:
        return None
    # Old idea with no play_id: it counts toward a play only when that play is its
    # platform's single active acting post play.
    if acting.get(q.channel) != [play_id]:
        return None
    return [p for p in posts if p.platform == q.channel]


def compute(
    bp: GrowthBlueprint,
    posts: list[LivePost],
    stars: list[tuple[int, datetime]],
    now: datetime,
) -> BlueprintLiveResponse:
    def measure(key: Any, scoped: list[LivePost] | None) -> LiveValueResponse:
        if key == "github_stars":
            return github_stars(stars)
        fn = _POST_METRICS.get(key)
        return LiveValueResponse() if fn is None or scoped is None else fn(scoped, now)

    objectives = {}
    for o in (bp.strategy.get("goals") or {}).get("objectives") or []:
        if isinstance(o, dict) and o.get("id"):
            channel = o.get("channel")
            scoped = [p for p in posts if p.platform == channel] if channel else posts
            objectives[o["id"]] = measure(o.get("metric_key"), scoped)

    acting: dict[str, list[str]] = {}
    for play_id, body in bp.plays.items():
        q = play_catalog.play(play_id)
        if q and q.acts and q.cadence == "posts" and q.channel and body.get("status") in _ACTIVE:
            acting.setdefault(q.channel, []).append(play_id)
    plays = {
        f"play:{play_id}": measure(
            (body.get("kpi") or {}).get("metric_key"), _play_posts(play_id, acting, posts)
        )
        for play_id, body in bp.plays.items()
    }
    return BlueprintLiveResponse(objectives=objectives, plays=plays)


async def get(
    ctx: RequestContext, profile_id: str | None = None, now: datetime | None = None
) -> BlueprintLiveResponse:
    """The approved Blueprint's live values; empty maps when there is none."""
    workspace_id, profile = await social_service.require_profile(ctx, profile_id)
    bp = await social_blueprint.approved_for(workspace_id, profile.id)
    if bp is None:
        return BlueprintLiveResponse()
    posts = await social_service.live_posts(workspace_id, profile.id)
    stars = await social_service.latest_repo_stars(workspace_id, profile.github_repos)
    return compute(bp, posts, stars, now or datetime.now(UTC))
