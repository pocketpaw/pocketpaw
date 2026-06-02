# src/pocketpaw/sites_capture/models.py — generalized from paw_print/models.py.
# A "site form" is the generalization of a paw-print widget: an origin-pinned,
# rate-limited, signed-key-gated public ingest surface. SiteEventMapping is the
# verbatim generalization of PawPrintEventMapping (event type → store object).

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

_MAX_PAYLOAD_BYTES = 8 * 1024  # forms are richer than widget events; 8KB cap


class SiteEventMapping(BaseModel):
    """How an inbound form submission becomes a tenant-store Lead record.

    `creates` is the lead's logical type; `fields` values use `{{ placeholder }}`
    interpolation over the submission payload + metadata (mirrors paw-print)."""

    creates: str
    fields: dict[str, str] = Field(default_factory=dict)


class SiteFormConfig(BaseModel):
    """Per-site capture configuration. The control plane creates one when a
    site publishes; the capture endpoint reads it to harden ingest."""

    site_id: str
    allowed_origins: list[str] = Field(default_factory=list)
    signed_key: str
    rate_limit_per_min: int = 60
    per_ip_limit_per_min: int = 10
    honeypot_field: str = "company_website"
    event_mapping: dict[str, SiteEventMapping] = Field(default_factory=dict)


class SiteFormSubmission(BaseModel):
    """One inbound form submission, before it becomes a Lead."""

    site_id: str
    form_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    submitter_ref: str  # IP hash or client token, for per-IP rate limiting
    submitted_at: datetime = Field(default_factory=datetime.now)


MAX_PAYLOAD_BYTES = _MAX_PAYLOAD_BYTES
