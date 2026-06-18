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
# Updated 2026-06-17 (feat/sites-svelte-component-edit, SE-2b): added
# ``builder_origin`` — the builder origin the site was published with, or "" for
# a normal (non-editable) site. When set, the generated page carries the gated
# edit-bridge keyed on it. Persisted so a component-edit republish re-applies it
# and the site stays editable across edits.
#
# Updated 2026-06-18 (feat/sites-stable-identity, PERF-1): a published Paw Site now
# has a STABLE per-(workspace, pocket_id) identity — its ``_id`` is derived
# deterministically from the pair (``service._live_object_id``), so a re-publish
# UPSERTS the SAME Site doc (one canonical row per pocket) instead of inserting a
# fresh one each time. No schema change: the upsert keys on the primary ``_id``
# (already unique), so the existing compound (workspace, pocket_id) index — which
# still serves the per-pocket reads — is sufficient and no new unique key is added.

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
    deployed: bool = False
    # Canonical deployed URL. LOCAL mode: the localhost URL the per-site static
    # server serves. CF mode: "" in v1 (reached via custom domain).
    url: str = ""
    # SE-2b: the builder origin this site was published with, or "" when it was
    # published as a normal (non-editable) site. When set, the generated page
    # carries the gated edit-bridge keyed on this origin. Persisted so a
    # component-edit republish can re-apply it and the site stays editable.
    builder_origin: str = ""
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
