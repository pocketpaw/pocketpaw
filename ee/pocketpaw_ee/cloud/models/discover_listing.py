# DiscoverListing Beanie document — one card in the public Discover index.
#
# Created 2026-10-01 (feat/discover-index): the thin, cross-product catalogue.
# Each row points at one item a SOURCE owns (``source`` + ``source_id``, unique
# together); site templates are the first source. The source owns ``title``,
# ``description``, ``kind``, ``audiences``, ``preview_image_url``, ``live_url``,
# ``workspace`` and ``owner`` and overwrites them on every sync. Discover owns
# ``featured``, ``hidden``, ``reports`` (``{user, reason, at}``, one per user)
# and ``remix_count``; a source sync never touches those.
#
# ``workspace``, ``owner``, ``reports``, ``hidden`` and ``source_id`` never reach
# the public wire (``discover.dto.PublicListingResponse`` is an allow-list).
#
# Only ``ee.cloud.discover.service`` / ``service_admin`` import this doc
# (import-linter "Discover" contract).
#
# Updated 2026-10-02 (feat/discover-index, hardening): ``dismissed_reporters``,
# the user ids whose reports staff dismissed on an unhide; their later reports
# on this listing are ignored. Discover-owned, like ``reports``. Index
# ``(hidden, _id desc)`` serves the public list's filter + newest-first sort.

from __future__ import annotations

from typing import Any

from pydantic import Field
from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class DiscoverListing(TimestampedDocument):
    """One public Discover card, synced from a source's own record."""

    source: str
    source_id: str
    workspace: str
    owner: str
    kind: str
    title: str
    description: str = ""
    audiences: list[str] = Field(default_factory=list)
    preview_image_url: str | None = None
    live_url: str | None = None
    featured: bool = False
    hidden: bool = False
    reports: list[dict[str, Any]] = Field(default_factory=list)
    dismissed_reporters: list[str] = Field(default_factory=list)
    remix_count: int = 0

    class Settings(TimestampedDocument.Settings):
        name = "discover_listings"
        indexes = [
            IndexModel([("source", 1), ("source_id", 1)], unique=True),
            IndexModel([("hidden", 1), ("featured", 1), ("createdAt", -1)]),
            IndexModel([("hidden", 1), ("_id", -1)]),
            IndexModel([("kind", 1)]),
            IndexModel([("audiences", 1)]),
        ]
