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
#
# Updated 2026-06-18 (feat/sites-dedupe-migration, PERF-2): added ``archived`` — a
# non-destructive tombstone flag for the duplicate Site docs the pre-PERF-1 minting
# left behind. PERF-1 made NEW publishes stable (one upserted doc per pocket), but
# EXISTING data still carries dupes (one pocket had 14 docs). The PERF-2 dedupe
# migration (``sites.dedupe``) keeps ONE canonical doc per (workspace, pocket_id)
# active and sets ``archived=True`` on the rest — it NEVER deletes, so the data is
# recoverable. The gallery read (``service.list_for_workspace`` / listSites) filters
# ``archived`` so each pocket shows exactly one card. Defaults False, so every
# existing doc and every fresh publish reads active until the migration archives it.
#
# Updated 2026-06-19 (P2b-backend — "Last Deployed"): added ``deployed_at`` — the UTC
# timestamp of the most recent SUCCESSFUL live deploy. ``service.publish`` stamps it
# (``datetime.now(UTC)``) ONLY when a non-preview deploy succeeds and ``deployed``
# flips True — NOT on a preview/edit/arm build and NOT on every ``updatedAt`` bump,
# so it is a true "last shipped" marker, not a "last touched" one. Defaults None;
# backfill is not required (pre-P2b rows read null, exposed as None on the DTOs).
#
# Updated 2026-06-20 (DS-2 — dynamic-site D1 bindings): added ``d1_database_id`` —
# the Cloudflare D1 database id a DYNAMIC site's deployed Worker is bound to.
# ``service.publish`` derives a STABLE per-(workspace, pocket) id for a
# ``pattern="dynamic"`` publish, persists it here on first publish, and REUSES the
# stored value on every re-publish so the binding target (and the data behind it)
# is stable across deploys. "" for static sites (no D1 binding). Defaults "", so
# pre-DS-2 rows and every static publish read empty — no migration.
#
# Updated 2026-06-24 (integration/billing-credits, BC-9 — per-site annual plan):
# added the per-site billing fields a published site carries on its OWN recurring
# annual plan (the Webflow model — each site has its own tier, not just the
# workspace plan). ``plan_tier`` is the site-plan catalog key (basic | pro |
# business — ``site_plans.SITE_PLAN_CATALOG``), ``subscription_id`` the Dodo
# subscription this site's annual sub maps to, ``annual_renewal_date`` the next
# renewal stamp the webhook updates, and ``subscription_status`` the lifecycle
# (none | active | cancelled). ``service.publish_pocket`` stamps ``plan_tier`` +
# ``subscription_id`` at publish; the per-site ``subscription.*`` webhook (routed
# by a ``site_id`` on its metadata) advances ``subscription_status`` /
# ``annual_renewal_date``. All default to backward-compatible values (None /
# "none") so every pre-BC-9 row and every workspace-plan-only site reads as having
# no per-site sub — no migration.
#
# Updated 2026-06-24 (feat/charge-first-sites — charge-first per-site publishing):
# a PAID-tier site is now created as PENDING and NOT deployed live until the
# ``subscription.active`` webhook confirms payment. Two additions support that:
#   * ``pending_deploy_inputs`` — the deploy inputs (rippleSpec / theme / engine /
#     svelte source / pattern / builder_origin / name) captured at publish time so
#     the webhook-time ``activate_site`` can run the deferred deploy WITHOUT
#     re-reading the pocket (the webhook only carries workspace_id + site_id, and
#     the pocket's draft may have moved on by activation time). Set ONLY for a
#     pending paid publish; "" / empty for a free/live publish, and cleared once
#     the site is activated. Default empty dict so every pre-charge-first row reads
#     as having no pending deploy.
#   * ``_checkout_url`` — a TRANSIENT pydantic PrivateAttr (NOT persisted to Mongo)
#     the publish path stashes the Dodo checkout link on so the router can surface
#     it on ``SiteResponse.checkout_url`` for a paid publish. None for a free
#     publish. Private so it never round-trips through the DB.
#
# Updated 2026-07-08 (DP0-1 — Dynamic Paw Sites Phase 0 provisioning state): added
# ``provision_status`` (none | provisioning | provisioned | failed) alongside
# ``d1_database_id``. It tracks where a dynamic site is in the durable D1 provision
# job. The contract the job upholds: it persists ``d1_database_id`` IMMEDIATELY
# after the D1 is created (status still ``provisioning``) so a retry reuses the same
# D1 instead of orphaning a second one; status advances to ``provisioned`` only
# after migrate + deploy succeed, and to ``failed`` on error. Defaults ``"none"`` so
# every static site and every pre-DP0 row reads "not provisioning" — no migration.

from __future__ import annotations

from datetime import datetime
from typing import Any

from beanie import Indexed
from pydantic import BaseModel, Field, PrivateAttr

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
    # P2b: UTC timestamp of the most recent SUCCESSFUL live deploy. Stamped by
    # service.publish ONLY when a non-preview deploy succeeds (when ``deployed``
    # flips True) — never on a preview/edit build, never on a plain updatedAt bump.
    # None until the pocket has been deployed at least once (old rows read null).
    deployed_at: datetime | None = None
    # Canonical deployed URL. LOCAL mode: the localhost URL the per-site static
    # server serves. CF mode: "" in v1 (reached via custom domain).
    url: str = ""
    # DS-2: the Cloudflare D1 database id this site's deployed Worker is bound to.
    # Set ONLY for a dynamic site (pattern == "dynamic"); "" for a static site.
    # Stable across re-publishes (publish reuses the stored value) so the D1
    # binding target — and the data behind it — never moves under a live site.
    d1_database_id: str = ""
    # DP0-1: where a dynamic site sits in the durable D1 provision job
    # (none | provisioning | provisioned | failed). Contract: the job persists
    # ``d1_database_id`` IMMEDIATELY after the D1 is created (status still
    # ``provisioning``) so a retry REUSES the same D1 instead of orphaning a second
    # one; status becomes ``provisioned`` only after migrate + deploy succeed, and
    # ``failed`` on error. Defaults "none" for static sites and pre-DP0 rows.
    provision_status: str = "none"
    # BC-9: per-site annual plan (the Webflow model — each published site has its
    # OWN recurring annual plan on a tier, distinct from the workspace plan).
    # ``plan_tier`` is the site-plan catalog key (basic | pro | business — see
    # ``billing.site_plans``); None until a publish stamps one. ``subscription_id``
    # is the Dodo subscription id for this site's annual sub (None when Dodo is
    # unconfigured — the tier is recorded without a live charge in v1).
    # ``annual_renewal_date`` is the next renewal stamp the per-site webhook
    # updates; ``subscription_status`` tracks the lifecycle (none | active |
    # cancelled). Defaults keep pre-BC-9 / workspace-plan-only sites at "no sub".
    plan_tier: str | None = None
    subscription_id: str | None = None
    annual_renewal_date: datetime | None = None
    subscription_status: str = "none"
    # charge-first: the deploy inputs captured at publish time for a PENDING paid
    # site, so the ``subscription.active`` webhook can run the deferred deploy
    # without re-reading the pocket (the webhook carries only workspace_id +
    # site_id, and the pocket's draft may have advanced by activation time). Keys:
    # ripple_spec, theme, engine, source, pattern, builder_origin, name. Empty for
    # a free/live publish; cleared once the site is activated.
    pending_deploy_inputs: dict[str, Any] = Field(default_factory=dict)
    # charge-first: TRANSIENT (NOT persisted) Dodo checkout link for a paid
    # publish. The publish path stashes it here so the router can surface it on
    # ``SiteResponse.checkout_url``; a PrivateAttr so it never serializes to Mongo.
    _checkout_url: str | None = PrivateAttr(default=None)
    # PERF-2: a non-destructive tombstone for duplicate Site docs the pre-PERF-1
    # per-publish ObjectId minting left behind. The dedupe migration keeps ONE
    # canonical doc per (workspace, pocket_id) active and sets this True on the
    # rest (never deletes). The gallery read filters it so each pocket shows once.
    archived: bool = False
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
