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
# Updated 2026-06-06 (feat/1345-draft-published): SiteResponse now carries the
# draft/published state the frontend reads to stop "Live" from lying —
# ``status`` ("draft" | "published") is the version state (draft == there are
# unpublished edits), and ``is_live`` is the REAL-deploy axis (True only after a
# successful publish/deploy). Added SiteStatusResponse (the standalone status read
# backing the badge, with the draft/published version pointers) and
# PreviewResponse (the current DRAFT content the builder preview renders, which
# fixes the dead-published-URL iframe).

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class PublishRequest(BaseModel):
    pocket_id: str


class SiteResponse(BaseModel):
    id: str
    pocket_id: str
    name: str
    script_name: str
    deployed: bool
    signed_key: str
    url: str = ""
    # Draft/published state machine (pocketpaw#1345). ``status`` is the version
    # state: "draft" (unpublished edits exist) | "published" (live candidate ==
    # latest). ``is_live`` is the deploy-confirmed axis: True ONLY after a
    # successful publish/deploy. A freshly created site is status="draft",
    # is_live=False — it is NOT live until an explicit publish succeeds.
    status: str = "draft"
    is_live: bool = False


class SiteStatusResponse(BaseModel):
    """The draft/published status read backing the Draft/Live badge."""

    pocket_id: str
    site_id: str | None = None
    status: str  # draft | published
    is_live: bool
    draft_version: int | None = None
    published_version: int | None = None
    url: str = ""


class PreviewResponse(BaseModel):
    """The current DRAFT content the builder preview renders (not the published
    URL). ``content`` is the rippleSpec for a ripple site, or the svelte source
    map for a svelte site. ``engine`` tells the renderer which it got."""

    pocket_id: str
    engine: str = "ripple"
    content: dict[str, Any] | None = None


class DomainRequest(BaseModel):
    hostname: str


class DomainStatusResponse(BaseModel):
    hostname: str
    cname_target: str
    status: str  # pending | verifying | live | error
