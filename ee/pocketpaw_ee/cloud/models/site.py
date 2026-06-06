# ee/pocketpaw_ee/cloud/models/site.py — a published Paw Site + its custom
# domains. workspace-scoped. The capture config (origin allowlist, signed key,
# rate limits, event mapping) lives here so the public capture endpoint can
# harden ingest without a second store. SiteDomain tracks the Cloudflare-for-
# SaaS hostname lifecycle the Domains panel polls.
#
# Created 2026-05-30 (feat/paw-sites-backend, RFC 12 Task 3.2): new Site +
# SiteDomain documents. Capture-hardening fields mirror
# ``pocketpaw.sites_capture.SiteFormConfig`` so the public endpoint reads one
# store. Compound (workspace, pocket_id) index serves the per-pocket lookup.
#
# Updated 2026-06-01 (Phase 3 — local fake-deploy): added ``url`` — the deployed
# site's canonical URL. In LOCAL deploy mode (no Cloudflare creds) publish()
# stores the localhost URL the per-site static server serves
# (http://127.0.0.1:<port>/<site_id>/) so the SiteResponse carries a real
# openable address for the cmux smoke. In the real CF path it is left "" in v1
# (the deployed Worker is reached via its custom domain, surfaced through the
# domains list) until a canonical workers.dev URL is wired.
#
# Updated 2026-06-06 (feat/1345-draft-published): added ``status`` ("draft" |
# "published"), the denormalized version state the sites service keeps in sync
# with the pocket_versions log. Paired with ``deployed`` (the real-deploy axis),
# it lets the SiteResponse tell the frontend draft vs published vs live without a
# version-log scan on every read — the fix for "a site is stamped deployed the
# moment it's created". A freshly created site is status="draft", deployed=False.

from __future__ import annotations

from typing import Any

from beanie import Indexed
from pydantic import BaseModel, Field

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class SiteDomain(BaseModel):
    """A custom hostname attached to a site (Cloudflare for SaaS)."""

    hostname: str
    cf_hostname_id: str = ""
    cname_target: str = ""
    status: str = "pending"  # pending | verifying | live | error


class Site(TimestampedDocument):
    """A published site generated from a pocket's rippleSpec."""

    workspace: Indexed(str)  # type: ignore[valid-type]
    pocket_id: str
    owner: str
    name: str = ""
    # Workers-for-Platforms script name (== site id) once deployed.
    script_name: str = ""
    # The REAL-deploy axis: True ONLY after a successful publish/deploy. A site
    # created as a draft is deployed=False until an explicit publish succeeds.
    deployed: bool = False
    # Draft/published version state (pocketpaw#1345). "draft" = there are
    # unpublished edits (the latest version is newer than the published one, or
    # nothing is published yet); "published" = the published version IS the
    # latest. Denormalized from the pocket_versions log by the sites service so
    # ``_to_response`` is a sync read. Combined with ``deployed``, the frontend
    # shows: draft (status=draft), published+live (published & deployed),
    # published-with-pending-edits (status=draft & deployed — refined since live).
    status: str = "draft"
    # Canonical deployed URL. LOCAL mode: the localhost URL the per-site static
    # server serves. CF mode: "" in v1 (reached via custom domain).
    url: str = ""
    # Capture hardening config (mirrors sites_capture.SiteFormConfig fields).
    allowed_origins: list[str] = Field(default_factory=list)
    signed_key: str = ""
    rate_limit_per_min: int = 60
    per_ip_limit_per_min: int = 10
    honeypot_field: str = "company_website"
    event_mapping: dict[str, Any] = Field(default_factory=dict)
    domains: list[SiteDomain] = Field(default_factory=list)

    class Settings:
        name = "sites"
        indexes = [
            [("workspace", 1), ("pocket_id", 1)],
        ]
