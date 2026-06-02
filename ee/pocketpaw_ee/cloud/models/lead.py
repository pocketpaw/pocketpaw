# ee/pocketpaw_ee/cloud/models/lead.py — captured form submission, the tenant
# cloud store sink for Paw Sites (NOT local SQLite Fabric). workspace + site
# scoped and indexed by (workspace, site_id, createdAt desc) so the Leads view
# pages efficiently per site/time. Storage is negligible (100k leads ≈ 200MB).
#
# Created 2026-05-30 (feat/paw-sites-backend, RFC 12 Task 3.2): new Lead + LeadSource
# documents. NOTE: the compound time index uses ``createdAt`` (camelCase) — the
# actual timestamp column TimestampedDocument defines — not the plan's literal
# ``created_at``, which names no field on the base doc and would index nothing.
# This matches the canonical tenant/time-indexed docs (foresight_run, chat_run,
# instinct_approval, message, task) so the per-site/time paging query is cheap.
#
# Updated 2026-05-30 (follow-up item 1): LeadSource gains ``rate_key`` — the
# SERVER-derived hash of the client host the per-IP rate limiter buckets on.
# ``submitter_ref`` stays as an opaque caller LABEL only (never the limiter key),
# because a caller can randomize it to dodge the per-IP cap.

from __future__ import annotations

from typing import Any

from beanie import Indexed
from pydantic import BaseModel, Field

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class LeadSource(BaseModel):
    """Provenance of a captured lead."""

    form_type: str  # e.g. "AppointmentRequest"
    site_id: str
    submitter_ref: str = ""  # opaque, caller-supplied LABEL (not PII, not the limiter key)
    rate_key: str = ""  # server-derived host hash; the per-IP limiter buckets on this


class Lead(TimestampedDocument):
    """One captured form submission for a published site."""

    workspace: Indexed(str)  # type: ignore[valid-type]
    site_id: Indexed(str)  # type: ignore[valid-type]
    form_type: str
    # Resolved record properties (post event-mapping interpolation).
    properties: dict[str, Any] = Field(default_factory=dict)
    source: LeadSource

    class Settings:
        name = "leads"
        indexes = [
            # ``createdAt`` desc is the per-site Leads list cursor (newest
            # first); the compound index keeps that query cheap once a site
            # accumulates thousands of submissions.
            [("workspace", 1), ("site_id", 1), ("createdAt", -1)],
        ]
