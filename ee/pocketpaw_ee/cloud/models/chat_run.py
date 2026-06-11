"""Beanie document for one assistant chat turn.

Changes:
- 2026-06-10 (sov/w3a-igw — per-run token metering) — added the ``usage`` field
  so each run records the actual prompt / completion / cached token counts (and
  cost / model / backend) the backend reports, instead of the counts being
  dropped. ``run_core`` captures the backend's ``token_usage`` event and persists
  the assembled dict here via ``mark_completed`` / ``mark_terminal``. ``{}`` when
  the backend reported no usage (legacy / empty-text / pre-metering runs), so the
  field is a precondition for outcome-based (token-metered) pricing without
  changing any existing run lifecycle.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from beanie import Document
from pydantic import Field
from pymongo import IndexModel

RunStatus = Literal["queued", "running", "completed", "interrupted", "failed", "cancelled"]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ChatRunDoc(Document):
    run_id: str
    workspace: str
    context_type: str  # "dm" | "group" | "pocket" | "session"
    scope_id: str
    session_key: str
    group: str | None = None
    user_id: str
    agent_id: str
    client_message_id: str
    user_message_id: str
    assistant_message_id: str | None = None
    status: RunStatus = "queued"
    partial_text: str = ""
    error: str | None = None
    # Per-run token usage assembled from the backend's ``token_usage`` event
    # (input / output / cached_input token counts + total_cost_usd + model +
    # backend). ``{}`` when the backend reported nothing — keeps legacy /
    # empty-text runs unchanged. This is the durable metering sink that
    # outcome-based pricing reads off.
    usage: dict[str, Any] = Field(default_factory=dict)
    createdAt: datetime = Field(default_factory=_utcnow)
    started_at: datetime | None = None
    ended_at: datetime | None = None

    class Settings:
        name = "chat_runs"
        # Uniques close the create_run find-then-insert race.
        indexes = [
            IndexModel([("run_id", 1)], unique=True),
            IndexModel([("workspace", 1), ("client_message_id", 1)], unique=True),
            [("workspace", 1), ("context_type", 1), ("scope_id", 1), ("createdAt", -1)],
        ]
