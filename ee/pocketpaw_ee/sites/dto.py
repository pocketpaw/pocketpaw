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
# Updated 2026-06-18 (feat/sites-stable-identity, PERF-1): SiteStatusResponse gains
# ``url`` — the canonical live address of the pocket's deployed site. With stable
# per-pocket identity (one Site doc per pocket) pocket_status reads the ONE
# canonical doc and surfaces its non-null url, so the builder/gallery link to the
# address the latest build actually serves at instead of a stale dupe's url=None.
# Optional (default None, backward-compatible) — None when no deployed site exists.
# Updated 2026-06-18 (feat/branch-primitive-revert-history, BP-4): added two DTOs
# for the Branch-primitive surfaces — SiteVersionResponse + VersionHistoryResponse
# (the ordered version timeline GET /sites/by-pocket/{pocket_id}/versions returns)
# and RequestPublishResponse (the Action created by POST
# /sites/by-pocket/{pocket_id}/request-publish, the clean entry to the merge gate
# so the client never hand-builds the Instinct proposal).
# Updated 2026-06-18 (feat/branch-primitive-audit, BP-7): added AuditFinding +
# AuditResponse — the response shape for POST /sites/by-pocket/{pocket_id}/audit
# (the first non-editor PRODUCER). Each finding carries a ``fix_prompt`` the UI
# feeds to the EXISTING edit path so the fix lands as a reviewable draft; there is
# NO new apply endpoint (BP-7 reuses edit_svelte_component / refine).
# Updated 2026-06-19 (P2b-backend — "Last Deployed"): SiteResponse and
# SiteStatusResponse gain ``deployed_at`` — the ISO-8601 string of the pocket's most
# recent successful live deploy (the Site doc's ``deployed_at``), or None before the
# first deploy. The builder/gallery surface a "Last deployed <time>" label without a
# second fetch. Optional (default None) so the field is backward-compatible.
# Updated 2026-06-20 (DS-3 — read a dynamic site's D1 data): added the read-only
# DTOs the operator data-view (DS-4 FE) consumes:
#   * SiteDataTableInfo — one declared table {name, fields, primary_key} from the
#     dynamic pocket's spec ``objects``. Always available (it comes from the spec),
#     even when the live D1 data is not reachable.
#   * SiteDataTablesResponse — the response of
#     GET /sites/by-pocket/{pocket_id}/data: {pocket_id, available, reason, tables}.
#     ``available`` is False with ``reason="live_on_cloudflare_only"`` in local/dev
#     mode (no live D1 to read), but ``tables`` is still listed from the spec so the
#     UI degrades cleanly instead of erroring.
#   * SiteDataRowsResponse — the response of
#     GET /sites/by-pocket/{pocket_id}/data/{table}: {pocket_id, table, available,
#     reason, columns, rows}. ``rows`` is the D1 rows (capped); ``columns`` is the
#     table's declared field names. In local mode ``available`` is False and ``rows``
#     is empty, but ``columns`` is still listed from the spec.
# These are READ-ONLY views (no request DTO — the inputs are path params); the
# data view never writes through this surface.

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
    # P2b: ISO-8601 timestamp of the most recent successful live deploy, or None
    # before the first deploy (a preview-only / never-deployed pocket reads None).
    deployed_at: str | None = None


class SitePreviewResponse(BaseModel):
    """Draft content to render in the in-app builder Preview tab. ``engine``
    selects the shape of ``content``: "ripple" → a rippleSpec object; "svelte" →
    a {path: contents} source map. ``content`` is None when there is nothing
    drafted yet. Mirrors the frontend SitePreviewResponse (core/sites/types.ts)."""

    pocket_id: str
    engine: str
    content: dict[str, Any] | None = None


class DevPreviewResponse(BaseModel):
    """The live Vite dev-server URL for a pocket's EDITING preview (Phase 2 / P2a).
    ``url`` is a localhost address (http://127.0.0.1:<port>/) the builder iframe
    frames so edits hot-reload over Vite HMR in ~ms, instead of a full per-edit
    rebuild. This is the editing preview only — publish still does the full prod
    build + workerd smoke."""

    pocket_id: str
    url: str


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
    # PERF-1: the canonical live url of the pocket's deployed site (the address the
    # latest build serves at). None when no deployed site exists.
    url: str | None = None
    # P2b: ISO-8601 timestamp of the pocket's most recent successful live deploy,
    # read from the canonical Site doc's ``deployed_at``. None when the pocket has
    # never been deployed (no Site doc, or a pre-P2b row that predates the field).
    deployed_at: str | None = None


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


class AuditFinding(BaseModel):
    """One issue surfaced by the site audit (BP-7). ``check`` is the short check id
    (e.g. "a11y.img_alt"); ``tier`` is "deterministic" for the rule-based core (a
    later "judgment" tier is deferred); ``severity`` is "error" | "warning".
    ``location`` is a {file, hint} pointer (the file + a source snippet). The
    UI feeds ``fix_prompt`` to the EXISTING edit path (edit_svelte_component /
    refine), which lands the fix as a reviewable draft in the Tray — there is no
    separate apply endpoint."""

    id: str
    check: str
    tier: str
    severity: str  # error | warning
    message: str
    fix_prompt: str
    location: dict[str, str] = {}


class AuditResponse(BaseModel):
    """The result of POST /sites/by-pocket/{pocket_id}/audit — the findings for a
    pocket's published-site source. A clean site returns an empty ``findings``
    list. Tenant-scoped; engine selects how the source was read (svelte source map
    vs rippleSpec)."""

    pocket_id: str
    engine: str
    findings: list[AuditFinding] = []


class SiteDataTableInfo(BaseModel):
    """One declared table of a dynamic site, read from the pocket spec's
    ``objects`` (DS-3). ``name`` is the table/object name; ``fields`` is the
    column→type map (the spec's field types); ``primary_key`` is the PRIMARY KEY
    column (empty when the spec did not declare one). This is spec-derived, so it
    is available even when the live D1 data is not reachable (local/dev mode)."""

    name: str
    fields: dict[str, str] = {}
    primary_key: str = ""


class SiteDataTablesResponse(BaseModel):
    """The tables of a dynamic site's data store, for the operator data-view
    (DS-3; backs GET /sites/by-pocket/{pocket_id}/data). The table LIST always
    comes from the pocket spec's ``objects``, so it is populated even when the
    live D1 is not reachable. ``available`` is True only when the rows behind
    those tables can actually be read (a live Cloudflare deploy); it is False in
    local/dev mode with ``reason="live_on_cloudflare_only"`` so the UI degrades
    cleanly — it can show the schema but explain why no rows load."""

    pocket_id: str
    available: bool
    reason: str = ""
    tables: list[SiteDataTableInfo] = []


class SiteDataRowsResponse(BaseModel):
    """The rows of ONE table of a dynamic site's data store (DS-3; backs
    GET /sites/by-pocket/{pocket_id}/data/{table}). ``columns`` is the table's
    declared field names (spec-derived, always present); ``rows`` is the live D1
    rows, CAPPED (the service applies a LIMIT). ``available`` is False in local/dev
    mode (``reason="live_on_cloudflare_only"``) and ``rows`` is then empty, but
    ``columns`` is still listed so the UI can render the table header + an
    explanatory empty state instead of erroring."""

    pocket_id: str
    table: str
    available: bool
    reason: str = ""
    columns: list[str] = []
    rows: list[dict[str, Any]] = []


class DomainRequest(BaseModel):
    hostname: str


class DomainStatusResponse(BaseModel):
    hostname: str
    cname_target: str
    status: str  # pending | verifying | live | error
