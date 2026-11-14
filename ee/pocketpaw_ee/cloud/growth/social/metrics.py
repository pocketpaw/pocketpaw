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
    "X_BATCH_SIZE",
    "XMetricsFn",
    "resolve_metrics_fn",
    "set_production_metrics_fn",
    "x_post_metrics",
]
