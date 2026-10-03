# DiscoverListing Beanie document — one card in the public Discover index.
#
# Each row points at one item a SOURCE owns (``source`` + ``source_id``, unique
# together). The source owns ``title``, ``description``, ``kind``, ``audiences``,
# ``preview_image_url``, ``live_url``, ``media_kind``, ``media_url``,
# ``workspace`` and ``owner`` and overwrites them on every sync. Discover owns
# ``featured``, ``hidden``, ``reports`` (``{user, reason, at}``, one per user),
# ``dismissed_reporters`` (reporters staff dismissed on an unhide; their later
# reports are ignored), ``remix_count`` and ``slug``.
#
# ``slug`` is the URL handle, derived from the title at first sync, unique per
# source (``-2``, ``-3`` suffixes) and never re-derived on a title change. Rows
# written before slugs existed have none until the next reindex, so the
# ``(source, slug)`` unique index is partial (strings only) and they don't
# collide. mongomock ignores the partial filter: a test must not seed two
# slug-less rows in one source.
#
# ``workspace``, ``owner``, ``reports``, ``hidden`` and ``source_id`` never reach
# the public wire (``discover.dto.PublicListingResponse`` is an allow-list).
# Index ``(hidden, _id desc)`` serves the public list's filter + newest-first sort.
#
# Only ``ee.cloud.discover.service`` / ``service_admin`` import this doc
# (import-linter "Discover" contract).

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
    media_kind: str | None = None
    media_url: str | None = None
    slug: str | None = None
    featured: bool = False
    hidden: bool = False
    reports: list[dict[str, Any]] = Field(default_factory=list)
    dismissed_reporters: list[str] = Field(default_factory=list)
    remix_count: int = 0

    class Settings(TimestampedDocument.Settings):
        name = "discover_listings"
        indexes = [
            IndexModel([("source", 1), ("source_id", 1)], unique=True),
            IndexModel(
                [("source", 1), ("slug", 1)],
                unique=True,
                partialFilterExpression={"slug": {"$type": "string"}},
            ),
            IndexModel([("hidden", 1), ("featured", 1), ("createdAt", -1)]),
            IndexModel([("hidden", 1), ("_id", -1)]),
            IndexModel([("kind", 1)]),
            IndexModel([("audiences", 1)]),
        ]
