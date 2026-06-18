# ee/pocketpaw_ee/sites/dto.py — request/response DTOs for the Sites control
# plane. Distinct request and response shapes per the cloud 4-file rules.
# Created: 2026-05-30 (feat/paw-sites-backend, RFC 12 Task 3.5).
#
# Updated 2026-06-01 (Phase 3 — local fake-deploy): SiteResponse carries ``url``,
# the deployed site's openable address. LOCAL mode returns the localhost URL the
# per-site static server serves so the caller (and the cmux smoke) can open the
# published site directly. Empty in the CF path in v1 (reached via custom
# domain). The frontend Site type mirrors this in Phase 4.
#
# Updated 2026-06-17 (pocketpaw#1345 backend half — by-pocket preview + status):
# added SitePreviewResponse and SiteStatusResponse, the two by-pocket read DTOs
# the #432 frontend already calls (getSitePreviewByPocket / getSiteStatusByPocket
# in core/sites/api.ts). Field names/types mirror the frontend types.ts EXACTLY:
# preview is {pocket_id, engine, content} (content optional — absent when nothing
# drafted; a rippleSpec for engine="ripple", a {path: contents} source map for
# engine="svelte"); status is {pocket_id, status, is_live} plus the optional
# site_id the frontend type also declares. Without these, every Preview-tab fetch
# 404'd and the builder showed "Nothing to preview yet".
# Updated 2026-06-17 (feat/sites-svelte-component-edit, SE-2b): added
# MakeEditableRequest (the body for POST /sites/by-pocket/{pocket_id}/editable;
# builder_origin optional) and SiteResponse.builder_origin so the UI can tell
# whether a published site carries the edit-bridge (non-empty = editable).
# Updated 2026-06-18 (feat/branch-primitive-sites-draft, BP-2 / pocketpaw#1345):
# SiteStatusResponse gains ``has_unpublished_changes`` — the Branch primitive now
# derives draft/published from the version pointers (versions.get_draft /
# get_published), so a pocket can carry a draft NEWER than its published version.
# This flag (default False, backward-compatible) lets the builder badge "has
# unpublished edits" without inferring it from the Site doc. ``status``/``is_live``
# semantics are unchanged in shape; only their derivation moves onto versions.
# Updated 2026-06-18 (feat/branch-primitive-revert-history, BP-4): added two DTOs
# for the Branch-primitive surfaces — SiteVersionResponse + VersionHistoryResponse
# (the ordered version timeline GET /sites/by-pocket/{pocket_id}/versions returns)
# and RequestPublishResponse (the Action created by POST
# /sites/by-pocket/{pocket_id}/request-publish, the clean entry to the merge gate
# so the client never hand-builds the Instinct proposal).

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class PublishRequest(BaseModel):
    pocket_id: str


class MakeEditableRequest(BaseModel):
    """Body for POST /sites/by-pocket/{pocket_id}/editable (SE-2b). The builder
    origin is optional — the service falls back to the configured dashboard
    origin (PAW_SITES_BUILDER_ORIGIN) when it is omitted, so the body may be
    empty."""

    builder_origin: str = ""


class SiteResponse(BaseModel):
    id: str
    pocket_id: str
    name: str
    script_name: str
    deployed: bool
    signed_key: str
    url: str = ""
    # SE-2b: the builder origin the site was published with, or "" for a normal
    # (non-editable) site. Non-empty means the page carries the edit-bridge.
    builder_origin: str = ""


class SitePreviewResponse(BaseModel):
    """Draft content to render in the in-app builder Preview tab. ``engine``
    selects the shape of ``content``: "ripple" → a rippleSpec object; "svelte" →
    a {path: contents} source map. ``content`` is None when there is nothing
    drafted yet. Mirrors the frontend SitePreviewResponse (core/sites/types.ts)."""

    pocket_id: str
    engine: str
    content: dict[str, Any] | None = None


class SiteStatusResponse(BaseModel):
    """Authoritative draft/published + is_live state for a pocket, so the builder
    labels accurately even before the site appears in the gallery list. ``status``
    is "draft" (no published version) or "published"; ``is_live`` is the ONLY
    signal that earns a "Live" badge — it requires a published version AND a real
    successful deploy (the Site doc's ``deployed``), never an optimistic stamp.
    ``has_unpublished_changes`` is True when a draft version is newer than the
    published one (edits the publish would ship). ``site_id`` carries the deployed
    Site's id when one exists. Mirrors the frontend SiteStatusResponse
    (core/sites/types.ts); ``has_unpublished_changes`` defaults False so the field
    is backward-compatible for callers that do not yet read it."""

    pocket_id: str
    status: str  # draft | published
    is_live: bool
    has_unpublished_changes: bool = False
    site_id: str | None = None


class SiteVersionResponse(BaseModel):
    """One row of a pocket's version timeline (BP-4). Mirrors the durable
    ArtifactVersion row's reading-relevant fields: ``version_no`` (the monotonic
    ordinal), ``status`` (draft|published|merged|reverted), ``label`` (e.g.
    "Revert to v2"), ``author`` (who wrote it), ``created_at`` (ISO). ``id`` is
    the version id a later revert / request-publish targets."""

    id: str
    version_no: int
    branch: str
    status: str
    label: str | None = None
    author: str | None = None
    created_at: str


class VersionHistoryResponse(BaseModel):
    """The ordered version timeline for a pocket (oldest → newest), returned by
    GET /sites/by-pocket/{pocket_id}/versions. Tenant-scoped."""

    pocket_id: str
    versions: list[SiteVersionResponse]


class RequestPublishResponse(BaseModel):
    """The review Action created by POST /sites/by-pocket/{pocket_id}/request-
    publish (BP-4 Part C). Carries the created Action's id + status so the client
    can show "submitted for review" without re-fetching. ``status`` is "pending"
    on creation (the gate item awaits operator approval)."""

    action_id: str
    status: str
    pocket_id: str
    to_version_id: str
    from_version_id: str | None = None


class DomainRequest(BaseModel):
    hostname: str


class DomainStatusResponse(BaseModel):
    hostname: str
    cname_target: str
    status: str  # pending | verifying | live | error
