"""Beanie document for agent long-term and daily memory entries."""

from __future__ import annotations

from typing import Any, Literal

from beanie import Indexed
from pydantic import Field

from pocketpaw_ee.cloud.models.base import TimestampedDocument

FactType = Literal["long_term", "daily"]


class MemoryFactDoc(TimestampedDocument):
    """A single LONG_TERM or DAILY memory entry.

    Distinct from the `messages` collection — chat messages and agent facts
    serve different product surfaces. ``user_id`` is the ownership handle
    (mirrors how FileMemoryStore partitions long-term memory per user).
    ``workspace_id`` is stamped on every row so multi-tenant ee deployments
    can scope reads at the adapter layer; rows without a workspace_id
    represent OSS / single-tenant usage and are never returned when a
    workspace filter is active.
    """

    type: Indexed(str)  # type: ignore[valid-type]  # "long_term" or "daily"
    content: str = ""
    tags: list[str] = Field(default_factory=list)
    user_id: str | None = None
    workspace_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    class Settings:
        name = "memory_facts"
        indexes = [
            [("type", 1), ("workspace_id", 1), ("user_id", 1), ("createdAt", -1)],
            [("workspace_id", 1), ("createdAt", -1)],
            "tags",
        ]
