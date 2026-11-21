# ee/pocketpaw_ee/cloud/growth/social/metrics.py — reads the public metrics of
# published X posts, behind the ``XMetricsFn`` seam the post-metrics sweep
# (``service.run_post_metrics``) calls.
#
# ``x_post_metrics`` is the production fn: it asks X's ``/2/tweets`` for
# ``public_metrics`` in batches of at most ``X_BATCH_SIZE`` ids, through the
# capped reader in ``x_api`` (one metered read per batch), and maps X's counts
# onto ``METRIC_FIELDS``. A batch the reader refuses (cap, no token, error)
# contributes nothing: the caller must not store zeros for a post it could not
# read, because snapshots are append-only. Read-only; nothing here writes to X.
# ``resolve_metrics_fn`` is None until the worker or a test installs one.
#
# Other platforms are read through Postiz (``PostizClient.post_analytics``):
# ``POSTIZ_STATS_PLATFORMS`` are the providers whose Postiz v2.25.0 integration
# reports per-post stats (X stays on its direct API; personal LinkedIn reports
# none), and ``postiz_counts`` maps Postiz labels onto ``METRIC_FIELDS``
# (exact, case-insensitive; unknown labels dropped). ``postiz_channel`` names
# the Blueprint channel a provider's posts count toward (a LinkedIn Page is
# ``linkedin``; any other provider keeps its own id).

from __future__ import annotations

from collections.abc import Awaitable, Callable

from pocketpaw_ee.cloud.growth.social.x_api import resolve_x_read_fn

X_BATCH_SIZE = 100

# Our name -> X's ``public_metrics`` key.
METRIC_FIELDS: dict[str, str] = {
    "impressions": "impression_count",
    "likes": "like_count",
    "replies": "reply_count",
    "reposts": "retweet_count",
    "quotes": "quote_count",
    "bookmarks": "bookmark_count",
}

# Postiz provider ids with per-post stats, minus "x" (read from X directly).
POSTIZ_STATS_PLATFORMS = frozenset(
    {
        "linkedin-page",
        "facebook",
        "instagram",
        "instagram-standalone",
        "threads",
        "youtube",
        "tiktok",
        "tiktok-business",
        "pinterest",
        "dribbble",
        "gmb",
    }
)

# Postiz series label (lowercased) -> our name. Several platforms name one metric
# differently (Views / Impressions, Comments / Replies, Shares / Reposts).
POSTIZ_LABELS: dict[str, str] = {
    "impressions": "impressions",
    "views": "impressions",
    "likes": "likes",
    "reactions": "likes",
    "replies": "replies",
    "comments": "replies",
    "reposts": "reposts",
    "retweets": "reposts",
    "shares": "reposts",
    "quotes": "quotes",
    "bookmarks": "bookmarks",
    "saves": "bookmarks",
}


# Postiz provider id -> Blueprint channel, where the two differ.
POSTIZ_CHANNELS: dict[str, str] = {"linkedin-page": "linkedin"}


def postiz_channel(provider: str) -> str:
    return POSTIZ_CHANNELS.get(provider, provider)


def postiz_counts(totals: dict[str, int]) -> dict[str, int]:
    """Our metric counts from Postiz ``{label: total}``; ``{}`` when no label is known.
    When two labels land on one name, the first one Postiz sent wins."""
    out: dict[str, int] = {}
    for label, total in totals.items():
        ours = POSTIZ_LABELS.get(label.strip().lower())
        if ours is not None and ours not in out:
            out[ours] = _count(total)
    return out


XMetricsFn = Callable[[str, list[str]], Awaitable[dict[str, dict[str, int]]]]


def _count(value: object) -> int:
    return value if isinstance(value, int) and value > 0 else 0


async def x_post_metrics(workspace_id: str, x_post_ids: list[str]) -> dict[str, dict[str, int]]:
    """``{x_post_id: {impressions, likes, replies, reposts, quotes, bookmarks}}`` for
    every id X answered. Ids X did not return, or whose batch was refused, are absent."""
    read = resolve_x_read_fn()
    out: dict[str, dict[str, int]] = {}
    for start in range(0, len(x_post_ids), X_BATCH_SIZE):
        batch = x_post_ids[start : start + X_BATCH_SIZE]
        data = await read(
            workspace_id, "/2/tweets", {"ids": ",".join(batch), "tweet.fields": "public_metrics"}
        )
        for tweet in (data or {}).get("data") or []:
            if not isinstance(tweet, dict) or not tweet.get("id"):
                continue
            pm = tweet.get("public_metrics")
            pm = pm if isinstance(pm, dict) else {}
            out[str(tweet["id"])] = {ours: _count(pm.get(x)) for ours, x in METRIC_FIELDS.items()}
    return out


_METRICS_FN: XMetricsFn | None = None


def set_production_metrics_fn(fn: XMetricsFn | None) -> None:
    global _METRICS_FN
    _METRICS_FN = fn


def resolve_metrics_fn() -> XMetricsFn | None:
    return _METRICS_FN


__all__ = [
    "METRIC_FIELDS",
    "POSTIZ_CHANNELS",
    "POSTIZ_LABELS",
    "POSTIZ_STATS_PLATFORMS",
    "X_BATCH_SIZE",
    "XMetricsFn",
    "postiz_channel",
    "postiz_counts",
    "resolve_metrics_fn",
    "set_production_metrics_fn",
    "x_post_metrics",
]
