# ee/pocketpaw_ee/cloud/models/social_post_metric.py — one snapshot of a
# published Growth › Social post's metrics at a fixed age (1, 3, 7 or 28 days
# after it went live), scoped to a workspace, profile and idea. ``platform`` is
# the network it went out on: "x" rows come from X's API and carry
# ``x_post_id``; other rows come from Postiz post stats and carry
# ``postiz_post_id``. Rows written before ``platform`` existed are X rows and
# read as "x" (the stored doc has no key; queries for X match a missing key too).
#
# Append-only: the unique (workspace, idea, platform, postiz_post_id, age_bucket)
# index means each post's bucket is written once and never updated or deleted; a
# second write is a DuplicateKeyError the metrics sweep skips. It replaced the
# (workspace, idea, age_bucket) index, which ``service.run_post_metrics`` drops
# before it writes a non-X row, since Beanie never drops an index itself.
#
# Only ``ee.cloud.growth.social.service`` may import this doc class
# (import-linter "Growth" contract).

from __future__ import annotations

from datetime import datetime

from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class SocialPostMetric(TimestampedDocument):
    """One published post's metrics at one age bucket."""

    workspace: str
    profile: str = ""
    idea: str
    platform: str = "x"
    x_post_id: str = ""
    postiz_post_id: str | None = None
    age_bucket: int
    impressions: int = 0
    likes: int = 0
    replies: int = 0
    reposts: int = 0
    quotes: int = 0
    bookmarks: int = 0
    captured_at: datetime

    class Settings(TimestampedDocument.Settings):
        name = "growth_social_post_metrics"
        indexes = [
            IndexModel(
                [
                    ("workspace", 1),
                    ("idea", 1),
                    ("platform", 1),
                    ("postiz_post_id", 1),
                    ("age_bucket", 1),
                ],
                unique=True,
            ),
        ]
