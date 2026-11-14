# ee/pocketpaw_ee/cloud/models/social_experiment.py — one Growth › Social A/B
# experiment for a profile: a hypothesis, the variable it tests (format, pillar or
# hook_type), its two arm values, the metric and keep rule, and a verdict.
#
# Invariants: at most one experiment per (workspace, profile) has verdict
# "running" (checked by the service on create; there is no unique index, so two
# simultaneous creates could race). Everything except ``verdict`` and
# ``closed_at`` is fixed at create: no route updates it.
#
# Only ``ee.cloud.growth.social.service`` may import this doc class
# (import-linter "Growth" contract).

from __future__ import annotations

from datetime import datetime

from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class SocialExperiment(TimestampedDocument):
    """One founder-defined experiment on a social profile's daily drafts."""

    workspace: str
    profile: str
    hypothesis: str
    variable: str
    arm_a: str
    arm_b: str
    metric: str
    keep_rule: str = ""
    keep_threshold: float = 0.0
    min_posts_per_arm: int = 6
    start: datetime
    end: datetime
    verdict: str = "running"
    closed_at: datetime | None = None

    class Settings(TimestampedDocument.Settings):
        name = "growth_social_experiments"
        indexes = [IndexModel([("workspace", 1), ("profile", 1), ("verdict", 1)])]
