# ee/pocketpaw_ee/cloud/growth/social/live.py — live values for the approved
# Blueprint's objectives and play KPIs (``GET /blueprint/live``). Nothing stored.
#
# The Blueprint comes only from ``blueprint.approved_for`` (none: empty maps); the
# numbers from ``service.live_posts`` / ``service.latest_repo_stars``, scoped to
# the workspace and brand. Each objective or play KPI may name a ``metric_key``:
# - ``impressions_per_week``: posts published in the last 7 days, each at its
#   largest age bucket, summed (a snapshot with no impressions is left out).
# - ``engagement_per_post``: likes + replies + reposts at the 7-day snapshot (the
#   ones reported), averaged over the posts that have one with any of the three
#   (younger posts are left out; no window).
# - ``link_clicks_per_week``: tracked-link clicks over the last 7 UTC days, today
#   included, on published posts that have a link.
# - ``github_stars``: each brand repo's newest stored stars, summed.
# No key, or no data, is ``value: null``, never 0; a measured 0 is a number.
# ``as_of`` is the newest data point counted (now for clicks, which count live).
# Scope: an objective with a ``channel`` counts only ideas on that platform, one
# without counts the whole brand. A play counts only ideas on its platform: an idea
# with a ``play_id`` counts toward exactly that play, and an old one with none only
# when the play is its platform's one active acting post play (two or more: none of
# them gets it). A Postiz entry carries its idea's ``play_id``; on a platform that
# play is not on (a cross-post) it counts as unstamped. Stars are brand-wide.
# Each play also lists ``formats``: per format seen on its counted ideas, the posts
# with a 7-day snapshot and their engagement per post, and a suggestion against the
# play's own average once a format has ``FORMAT_MIN_POSTS`` (the ratio, rounded to
# 2 places, at or over ``DOUBLE_DOWN_RATIO`` is double_down, at or under
# ``DROP_RATIO`` is drop).

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
    LiveFormatResponse,
    LivePlayResponse,
    LiveValueResponse,
)
from pocketpaw_ee.cloud.growth.social.domain import GrowthBlueprint, LivePost, PostMetrics

WEEK_DAYS = 7
ENGAGEMENT_BUCKET = 7
FORMAT_MIN_POSTS = 5
DOUBLE_DOWN_RATIO = 1.3
DROP_RATIO = 0.7


def _newest(snaps: list[PostMetrics]) -> datetime | None:
    return max((s.captured_at for s in snaps if s.captured_at), default=None)


def impressions_per_week(posts: list[LivePost], now: datetime) -> LiveValueResponse:
    since = now - timedelta(days=WEEK_DAYS)
    latest = [
        max(p.snapshots, key=lambda s: s.age_bucket)
        for p in posts
        if p.snapshots and p.published_at >= since
    ]
    latest = [s for s in latest if s.impressions is not None]
    if not latest:
        return LiveValueResponse()
    return LiveValueResponse(
        value=float(sum(s.impressions or 0 for s in latest)),
        as_of=_newest(latest),
        posts_counted=len(latest),
    )


def engagement_per_post(posts: list[LivePost], now: datetime) -> LiveValueResponse:
    """Only posts with a 7-day snapshot count; a younger post is left out. A count the
    platform did not report (None) adds nothing, and a snapshot with none of likes,
    replies and reposts is left out (its engagement is unknown, not 0)."""
    week = [
        s
        for p in posts
        for s in p.snapshots
        if s.age_bucket == ENGAGEMENT_BUCKET
        and not (s.likes is None and s.replies is None and s.reposts is None)
    ]
    if not week:
        return LiveValueResponse()
    total = sum((s.likes or 0) + (s.replies or 0) + (s.reposts or 0) for s in week)
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
    """The posts counted toward a play; None for a play with no platform."""
    q = play_catalog.play(play_id)
    if q is None or q.channel is None:
        return None
    unit = f"play:{play_id}"
    # Old idea with no play_id: it counts toward a play only when that play is its
    # platform's single active acting post play.
    single = acting.get(q.channel) == [play_id]

    def counts(p: LivePost) -> bool:
        own = _own_play(p)
        return own == unit if own else single

    return [p for p in posts if p.platform == q.channel and counts(p)]


def _own_play(p: LivePost) -> str | None:
    """The post's ``play_id`` when that play is on the post's platform. A cross-post
    (a Postiz LinkedIn Page copy of an X idea) reads as unstamped on its platform."""
    q = play_catalog.play((p.play_id or "").removeprefix("play:"))
    return p.play_id if q is not None and q.channel == p.platform else None


def _n_posts(n: int) -> str:
    return f"{n} post" if n == 1 else f"{n} posts"


def suggest(posts: int, avg: float | None, play_avg: float | None) -> tuple[str | None, str | None]:
    """A format's suggestion and its plain-text reason, against the play average."""
    if posts < FORMAT_MIN_POSTS:
        return None, f"{_n_posts(posts)}, needs {FORMAT_MIN_POSTS} for a call"
    if avg is None or not play_avg:
        return None, None
    # Rounded once: the call and the printed reason always use the same number.
    ratio = round(avg / play_avg, 2)
    reason = f"{_n_posts(posts)}, {ratio:g}x the play average"
    if ratio >= DOUBLE_DOWN_RATIO:
        return "double_down", reason
    if ratio <= DROP_RATIO:
        return "drop", reason
    return None, reason


def formats(posts: list[LivePost], now: datetime) -> list[LiveFormatResponse]:
    """Per format on ``posts``, most measured posts first. The average is engagement
    per post: the only per-post KPI key, and the fallback for every other key."""
    play_avg = engagement_per_post(posts, now).value
    groups: dict[str, list[LivePost]] = {}
    for p in posts:
        if p.format:
            groups.setdefault(p.format, []).append(p)
    out = []
    for name, group in groups.items():
        m = engagement_per_post(group, now)
        suggestion, reason = suggest(m.posts_counted, m.value, play_avg)
        out.append(
            LiveFormatResponse(
                name=name,
                posts=m.posts_counted,
                avg=m.value,
                suggestion=suggestion,
                reason=reason,
            )
        )
    return sorted(out, key=lambda f: (-f.posts, f.name))


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

    acting = social_blueprint.acting_post_plays(bp)
    plays = {}
    for play_id, body in bp.plays.items():
        scoped = _play_posts(play_id, acting, posts)
        value = measure((body.get("kpi") or {}).get("metric_key"), scoped)
        plays[f"play:{play_id}"] = LivePlayResponse(
            **value.model_dump(), formats=formats(scoped, now) if scoped else []
        )
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
