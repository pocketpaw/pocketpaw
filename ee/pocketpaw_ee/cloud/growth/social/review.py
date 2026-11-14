# ee/pocketpaw_ee/cloud/growth/social/review.py — the pure math behind the
# Growth › Social weekly review (``GET /growth/social/review``). No database:
# the service does every workspace-filtered read and hands plain values in.
#
# Plain arithmetic only, no statistics engine. A post's numbers come from its
# 7-day snapshot (or its latest when it has none). ``engagement_rate`` is
# (likes + replies + reposts + quotes) / impressions, ``clicks_per_1k_views`` is
# tracked-link clicks per 1000 impressions; both are None without impressions
# and averages skip None. ``repo_uniques_week`` is the profile's repos' unique
# visitors over the 7 UTC days starting the day the post went live. An
# experiment's arm table counts its published posts per arm and averages the
# experiment's metric; ``min_posts_met`` is false until every arm has
# ``min_posts_per_arm`` posts. Tag averages leave out empty tags.

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from pocketpaw_ee.cloud.growth.social.domain import PostMetrics

REVIEW_BUCKET = 7
TAGS = ("format", "pillar", "hook_type")


def parse_week(value: str | None, today: date) -> date:
    """The Monday of ISO week ``YYYY-Www`` (``today``'s week when None). ValueError if bad."""
    if value is None:
        return today - timedelta(days=today.weekday())
    year, sep, week = value.partition("-W")
    if not sep or len(year) != 4 or len(week) != 2 or not (year + week).isdigit():
        raise ValueError(f"week must look like 2026-W07, not {value!r}")
    return date.fromisocalendar(int(year), int(week), 1)


def week_label(monday: date) -> str:
    year, week, _ = monday.isocalendar()
    return f"{year}-W{week:02d}"


def pick_snapshot(snaps: list[PostMetrics]) -> PostMetrics | None:
    """The 7-day snapshot, else the most recently captured one."""
    for s in snaps:
        if s.age_bucket == REVIEW_BUCKET:
            return s
    return max(snaps, key=lambda s: s.captured_at or datetime.min, default=None)


def engagement_rate(m: PostMetrics | None) -> float | None:
    if m is None or m.impressions <= 0:
        return None
    return (m.likes + m.replies + m.reposts + m.quotes) / m.impressions


def clicks_per_1k_views(clicks: int, m: PostMetrics | None) -> float | None:
    if m is None or m.impressions <= 0:
        return None
    return clicks * 1000 / m.impressions


def uniques_after(day_uniques: dict[str, int], start: date, days: int = 7) -> int:
    return sum(day_uniques.get((start + timedelta(days=i)).isoformat(), 0) for i in range(days))


def post_row(
    idea: dict[str, Any], snap: PostMetrics | None, clicks: int, repo_uniques: int
) -> dict[str, Any]:
    """One published post's review row. ``idea`` holds id, hook, the tags and published_at."""
    counts = {
        k: getattr(snap, k, 0) for k in ("impressions", "likes", "replies", "reposts", "quotes")
    }
    return {
        **idea,
        "age_bucket": snap.age_bucket if snap else None,
        **counts,
        "clicks": clicks,
        "clicks_per_1k_views": clicks_per_1k_views(clicks, snap),
        "engagement_rate": engagement_rate(snap),
        "repo_uniques_week": repo_uniques,
    }


def mean(values: list[float | None]) -> float | None:
    real = [v for v in values if v is not None]
    return sum(real) / len(real) if real else None


def tag_averages(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Per tag, per non-empty value: post count and average rates, most posts first."""
    out: dict[str, list[dict[str, Any]]] = {}
    for tag in TAGS:
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if row.get(tag):
                groups.setdefault(row[tag], []).append(row)
        out[tag] = [
            {
                "value": value,
                "posts": len(group),
                "clicks_per_1k_views": mean([r["clicks_per_1k_views"] for r in group]),
                "engagement_rate": mean([r["engagement_rate"] for r in group]),
            }
            for value, group in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        ]
    return out


def arm_table(
    rows: list[dict[str, Any]],
    *,
    variable: str,
    arm_a: str,
    arm_b: str,
    metric: str,
    min_posts_per_arm: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Posts and the metric's average per arm, and whether every arm has enough posts."""
    arms = []
    for arm, value in (("a", arm_a), ("b", arm_b)):
        group = [r for r in rows if r.get(variable) == value]
        arms.append(
            {
                "arm": arm,
                "value": value,
                "posts": len(group),
                "metric_average": mean([r[metric] for r in group]),
            }
        )
    min_posts_met = all(arm["posts"] >= min_posts_per_arm for arm in arms)
    return arms, min_posts_met


def repo_weeks(rows: list[tuple[str, str, int, int]], monday: date) -> list[dict[str, Any]]:
    """Per repo: stars at the end of the week and the week before, and uniques summed
    over each week. ``rows`` are (repo, day, stars, uniques)."""
    this = (monday, monday + timedelta(days=7))
    prev = (monday - timedelta(days=7), monday)
    out: dict[str, dict[str, Any]] = {}
    for repo, day, stars, uniques in sorted(rows):
        d = date.fromisoformat(day)
        entry = out.setdefault(
            repo,
            {
                "repo": repo,
                "stars": 0,
                "stars_week_before": 0,
                "uniques": 0,
                "uniques_week_before": 0,
            },
        )
        if this[0] <= d < this[1]:
            entry["stars"] = stars  # rows are sorted by day, so the last one wins
            entry["uniques"] += uniques
        elif prev[0] <= d < prev[1]:
            entry["stars_week_before"] = stars
            entry["uniques_week_before"] += uniques
    return list(out.values())
