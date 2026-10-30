# ee/pocketpaw_ee/cloud/launch_controls/domain.py: the launch-controls value the
# guards read. Platform-wide, so it carries no tenancy fields.

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class LaunchControls(BaseModel):
    """The early-access launch switches as one immutable snapshot.

    ``read_failed`` is True when the snapshot is the defaults because the read
    failed. The pause flags fail open on it; the daily ceiling fails closed.
    """

    model_config = ConfigDict(frozen=True)

    signups_paused: bool = False
    free_usage_paused: bool = False
    daily_free_ceiling_credits: int = 25_000
    read_failed: bool = False


__all__ = ["LaunchControls"]
