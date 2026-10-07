# ee/pocketpaw_ee/cloud/models/belt_run_feed.py — one Belt run stage's step feed.
#
# What the develop station's claude seat did on a run (tool calls with their
# args and results, the developer's prose), folded by ``belt/feed.py`` into the
# chat ``StepRecorder`` step shape. One row per (workspace, run, stage); a
# re-develop of the same run replaces its stage's row. Kept OUT of the Instinct
# ``code_change`` blob on purpose: ``GET /belt/runs`` reads every blob, and a
# feed can reach ~2 MB. Steps are scrubbed and capped before they get here
# (``feed.FEED_MAX_STEPS`` / ``FEED_MAX_BYTES``); this doc stores them as given.
#
# Only ``ee.cloud.belt.service`` imports this module (the 4-file entity rule).

from __future__ import annotations

from typing import Any

from pydantic import Field
from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class BeltRunFeed(TimestampedDocument):
    """The ordered steps one stage of a Belt run recorded.

    ``workspace`` is the tenancy key; ``action_id`` the run (its ``code_change``
    Instinct Action id); ``stage`` the station step (``develop`` today).
    ``steps`` are ``StepRecorder`` dicts; ``steps_omitted`` counts what the caps
    dropped."""

    workspace: str
    action_id: str
    stage: str
    steps: list[dict[str, Any]] = Field(default_factory=list)
    steps_omitted: int = 0

    class Settings:
        name = "belt_run_feeds"
        indexes = [
            IndexModel(
                [("workspace", 1), ("action_id", 1), ("stage", 1)],
                unique=True,
                name="uq_workspace_run_stage",
            ),
        ]
