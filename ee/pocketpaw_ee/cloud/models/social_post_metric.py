# ee/pocketpaw_ee/cloud/models/social_post_metric.py — one snapshot of a
# published Growth › Social X post's public metrics at a fixed age (1, 3, 7 or
# 28 days after it went live), scoped to a workspace, profile and idea.
#
# Append-only: the unique (workspace, idea, age_bucket) index means a bucket is
# written once and never updated or deleted; a second write for the same bucket
# is a DuplicateKeyError the metrics sweep skips.
#
# Only ``ee.cloud.growth.social.service`` may import this doc class
# (import-linter "Growth" contract).

from __future__ import annotations

from datetime import datetime

from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class SocialPostMetric(TimestampedDocument):
    """An X post's public metrics at one age bucket."""

    workspace: str
    profile: str = ""
    idea: str
    x_post_id: str
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
            IndexModel([("workspace", 1), ("idea", 1), ("age_bucket", 1)], unique=True),
        ]
