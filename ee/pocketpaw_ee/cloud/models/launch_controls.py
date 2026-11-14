# ee/pocketpaw_ee/cloud/models/launch_controls.py: the singleton early-access
# launch-controls row (``launch_controls`` collection).
#
# One row, keyed ``key="global"`` under a unique index, holding the operator
# switches for the early-access launch: signup pause, free-usage kill switch and
# the daily free ceiling. A missing row means every switch is at its default.
# Only ``ee.cloud.launch_controls.service`` reads or writes it. An operator can
# also flip it straight in Mongo:
#   db.launch_controls.updateOne({key: "global"},
#       {$set: {free_usage_paused: true}}, {upsert: true})

from __future__ import annotations

from datetime import datetime

from beanie import Document
from pymongo import IndexModel

LAUNCH_CONTROLS_KEY = "global"


class LaunchControlsDoc(Document):
    """The singleton launch-controls row. Field defaults are the launch defaults."""

    key: str = LAUNCH_CONTROLS_KEY
    signups_paused: bool = False
    free_usage_paused: bool = False
    daily_free_ceiling_credits: int = 25_000
    updated_at: datetime | None = None
    updated_by: str | None = None

    class Settings:
        name = "launch_controls"
        indexes = [IndexModel([("key", 1)], unique=True, name="uq_launch_controls_key")]


__all__ = ["LAUNCH_CONTROLS_KEY", "LaunchControlsDoc"]
