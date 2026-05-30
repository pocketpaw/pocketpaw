# ee/pocketpaw_ee/cloud/leads/router.py — capture ingest (public, origin-pinned,
# signed-key-gated; the edge Queue drains here) + authed tenant-scoped reads.
# The public capture endpoint deliberately has NO auth dependency: it is called
# by the deployed site's Queue consumer, authenticated by the per-site signed
# key + origin pin, not a user session.
#
# Created 2026-05-30 (feat/paw-sites-backend, RFC 12 Task 3.4): the Sites
# capture surface. Public POST /sites/{site_id}/capture (site-exists → origin
# pin → signed key → service hardening) and authed GET /sites/{site_id}/leads
# (plan-gated + RBAC + workspace-scoped read).
#
# Updated 2026-05-30 (security hardening): the public capture path now (C1)
# enforces MAX_PAYLOAD_BYTES on the JSON-encoded payload immediately after the
# signed-key check — an unauthenticated caller can no longer POST an unbounded
# body for a Mongo-write amplification DoS — and (H1) compares the signed key
# with secrets.compare_digest (constant time) instead of ``!=`` so the check is
# not vulnerable to a timing side channel.
#
# Updated 2026-05-30 (follow-up item 1): the per-IP rate-limit identity is now
# derived SERVER-SIDE from the connection — a sha256 hash of
# ``request.client.host`` (see ``_rate_key``) — and passed to the service as
# ``rate_key``. The caller-controlled ``body.submitter_ref`` is no longer the
# limiter key (it was randomizable to dodge the cap); it rides through only as an
# opaque, non-PII label on the stored Lead.

from __future__ import annotations

import hashlib
import json
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from pocketpaw.sites_capture.ingest import origin_allowed
from pocketpaw.sites_capture.models import MAX_PAYLOAD_BYTES
from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.deps import require_action_any_workspace, require_plan_feature
from pocketpaw_ee.cloud.leads import service as leads_service
from pocketpaw_ee.cloud.leads.dto import CaptureRequest, CaptureResponse, LeadOut, lead_to_dto
from pocketpaw_ee.cloud.models.site import Site as _SiteDoc

router = APIRouter(tags=["Sites"])


def _rate_key(request: Request) -> str:
    """Server-derived per-IP rate-limit identity: a sha256 hex digest of the
    client host. Derived from the CONNECTION, never from a request-body field,
    so a caller cannot mint a fresh per-IP bucket by varying the payload. The
    hash (not the raw IP) is stored, so the limiter never persists a bare IP.
    Falls back to an empty string when the host is unknown (e.g. a test transport
    with no client) — the service then collapses that to one shared bucket."""
    host = request.client.host if request.client else ""
    return hashlib.sha256(host.encode("utf-8")).hexdigest() if host else ""


@router.post("/sites/{site_id}/capture", response_model=CaptureResponse)
async def capture_lead(site_id: str, body: CaptureRequest, request: Request) -> CaptureResponse:
    """Public ingest. Order: site exists → origin pinned → signed key → payload
    size cap → delegate to the service (honeypot/rate/injection screen/mapping)."""
    # global-read: public ingest is keyed by site, not by a workspace session.
    site = await _SiteDoc.find_one({"script_name": site_id})
    if site is None:
        raise HTTPException(404, "Site not found")

    if not origin_allowed(site.allowed_origins, request.headers.get("origin")):
        raise HTTPException(403, "Origin not allowed for this site")

    # H1: constant-time compare so the key check can't be probed via timing.
    if not secrets.compare_digest(body.signed_key, site.signed_key):
        raise HTTPException(401, "Invalid signed key")

    # C1: reject oversized payloads before the service touches Mongo. This runs
    # after auth so an unauthenticated caller can't even reach the size check,
    # and caps the amplification a past-auth caller could otherwise drive.
    if len(json.dumps(body.payload, default=str).encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise HTTPException(413, "Payload exceeds size cap")

    lead = await leads_service.capture(
        site=site,
        form_type=body.form_type,
        payload=body.payload,
        submitter_ref=body.submitter_ref or "anon",
        rate_key=_rate_key(request),  # server-derived; the real per-IP limiter key
    )
    if lead is None:
        return CaptureResponse(ok=False, reason="dropped")
    return CaptureResponse(ok=True, lead_id=lead.id)


@router.get(
    "/sites/{site_id}/leads",
    response_model=list[LeadOut],
    dependencies=[
        Depends(require_plan_feature("fabric")),
        Depends(require_action_any_workspace("fabric.read")),
    ],
)
async def list_leads(
    site_id: str,
    limit: int = Query(100, ge=1, le=500),
    ctx: RequestContext = Depends(request_context),
) -> list[LeadOut]:
    leads = await leads_service.list_for_site(ctx.workspace_id, site_id, limit=limit)
    return [lead_to_dto(lead) for lead in leads]
