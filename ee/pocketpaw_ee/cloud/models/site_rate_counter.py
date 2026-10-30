# ee/pocketpaw_ee/cloud/models/site_rate_counter.py — atomic per-minute rate
# counter for the public Paw Sites capture ingest. One document per
# (scope, scope_id, bucket-minute); the capture service ``$inc``-s it via a single
# ``find_one_and_update`` and tests the cap on the post-increment count, so a
# burst can't slip past the way the old count-then-insert (TOCTOU) check let it.
#
# Created 2026-05-30 (feat/paw-sites-backend, RFC 12 follow-up item 3): replaces
# the read-then-write rate-limit window with an atomic increment-and-test
# counter. ``scope`` is "site" (overall per-site cap) or "ip" (per server-derived
# rate_key cap); ``scope_id`` is the site_id, or ``"{site_id}:{rate_key}"`` for
# the per-IP scope. ``bucket`` is the window start truncated to the minute. A
# unique (scope, scope_id, bucket) index keeps it one doc per window; a short TTL
# on ``created_at`` self-purges stale buckets so the collection never grows.

from __future__ import annotations

from datetime import UTC, datetime

from beanie import Document
from pydantic import Field
from pymongo import IndexModel

# Window length is one minute; keep the doc a little past that so a counter is
# never GC'd mid-window. 120s = the 60s window + 60s of slack.
_COUNTER_TTL_SECONDS = 120


class SiteRateCounter(Document):
    """An atomic per-(scope, scope_id, minute) submission counter."""

    scope: str  # "site" (overall) | "ip" (per server-derived rate_key)
    scope_id: str  # site_id, or "{site_id}:{rate_key}" for the per-IP scope
    bucket: datetime  # window start, truncated to the minute (UTC)
    # ``hits`` (not ``count``) — ``count`` shadows Beanie's Document.count().
    hits: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "site_rate_counters"
        indexes = [
            # One doc per window — the atomic upsert keys on exactly this triple.
            IndexModel(
                [("scope", 1), ("scope_id", 1), ("bucket", 1)],
                unique=True,
                name="uq_scope_scope_id_bucket",
            ),
            # Stale buckets self-purge so the collection never accumulates.
            IndexModel([("created_at", 1)], expireAfterSeconds=_COUNTER_TTL_SECONDS),
        ]
