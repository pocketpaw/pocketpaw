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
#
# Updated 2026-07-22 (SI-4 — feat/sites-import-endpoint): added the NATIVE-FORM
# capture sibling, POST /capture/form (final URL: {captureApiBase}/capture/form,
# i.e. /api/v1/capture/form — captureApiBase already carries /api/v1, so the
# spec's "/v1/capture/form" would have doubled the segment; this is the
# router-consistent resolution and the cross-repo contract paw-sites' import
# rewiring must target). IMPORTED sites rewire their <form>s to a plain
# application/x-www-form-urlencoded POST here, with hidden fields ``paw_site_id``,
# ``paw_key`` (the per-site signed key), ``paw_page``, ``paw_redirect`` and
# optionally ``paw_form_type``. The endpoint runs the SAME hardening ladder as the
# JSON capture (site exists → origin pin → constant-time signed key → payload size
# cap → leads_service.capture), then 303-redirects to ``paw_redirect`` — which MUST
# be a relative path (open-redirect guard: absolute / protocol-relative /
# backslash / CR-LF all 400), resolved against the validated request Origin.

from __future__ import annotations

import hashlib
import json
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

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


def _safe_relative_redirect(path: str) -> bool:
    """Open-redirect guard for the native-form 303: the redirect target must be a
    RELATIVE path on the submitting site. Rejects absolute URLs (no leading "/"),
    protocol-relative ("//host"), backslash tricks, embedded schemes, and CR/LF
    (header injection). The accepted value is later prefixed with the VALIDATED
    request Origin, so the browser can only ever land back on the pinned site."""
    if not path.startswith("/") or path.startswith("//"):
        return False
    if "\\" in path or "\r" in path or "\n" in path or "://" in path:
        return False
    return len(path) <= 2048


@router.post("/capture/form")
async def capture_form(request: Request) -> Response:
    """Native-form ingest for IMPORTED sites (SI-4). The imported page's <form>s
    are rewired to POST application/x-www-form-urlencoded here with hidden fields:
    ``paw_site_id`` (the site), ``paw_key`` (the per-site signed key), ``paw_page``
    (provenance label), ``paw_redirect`` (relative post-submit path), and optional
    ``paw_form_type`` (event-mapping key; defaults to "lead" — the mapping every
    site doc seeds). CROSS-REPO CONTRACT: the final URL is
    {captureApiBase}/capture/form (/api/v1/capture/form) — see the module comment.

    Hardening mirrors the JSON capture exactly: site exists → origin pin →
    constant-time key compare → payload size cap → the SAME
    ``leads_service.capture`` pipeline (honeypot / rate limit / injection screen /
    mapping). The open-redirect guard rejects any non-relative ``paw_redirect``
    BEFORE anything is recorded. The response is a 303 See Other back to the
    validated Origin + redirect path — a DROPPED submission still 303s (the
    visitor is never shown an error that leaks the drop heuristics)."""
    form = await request.form()
    site_id = str(form.get("paw_site_id") or "")
    key = str(form.get("paw_key") or "")
    page = str(form.get("paw_page") or "")
    redirect = str(form.get("paw_redirect") or "") or "/"
    form_type = str(form.get("paw_form_type") or "lead")

    # global-read: public ingest is keyed by site, not by a workspace session.
    site = await _SiteDoc.find_one({"script_name": site_id}) if site_id else None
    if site is None:
        raise HTTPException(404, "Site not found")

    if not origin_allowed(site.allowed_origins, request.headers.get("origin")):
        raise HTTPException(403, "Origin not allowed for this site")

    # H1 (same as the JSON path): constant-time compare — no timing side channel.
    if not secrets.compare_digest(key, site.signed_key):
        raise HTTPException(401, "Invalid signed key")

    # Open-redirect guard BEFORE any write: a bad redirect fails the whole submit.
    if not _safe_relative_redirect(redirect):
        raise HTTPException(400, "paw_redirect must be a relative path on the site")

    # The lead payload is every NON-``paw_*`` text field. UploadFile parts (a file
    # input on an imported form) are skipped — the capture pipeline stores JSON
    # properties, never file bodies.
    payload = {
        k: v for k, v in form.multi_items() if isinstance(v, str) and not k.startswith("paw_")
    }

    # C1 (same as the JSON path): cap the payload size before the service runs.
    if len(json.dumps(payload, default=str).encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise HTTPException(413, "Payload exceeds size cap")

    await leads_service.capture(
        site=site,
        form_type=form_type,
        payload=payload,
        # Opaque provenance label (mirrors submitter_ref on the JSON path — never
        # a limiter key). Truncated: paw_page is caller-controlled text.
        submitter_ref=f"form:{page}"[:256] if page else "form",
        rate_key=_rate_key(request),  # server-derived; the real per-IP limiter key
    )

    # 303 back to the site. The Location is the VALIDATED Origin (already pinned
    # against site.allowed_origins above) + the RELATIVE redirect path, so the
    # browser can only land back on the submitting site. origin_allowed fails
    # closed on a missing Origin, so it is always present here.
    origin = (request.headers.get("origin") or "").rstrip("/")
    return Response(status_code=303, headers={"Location": f"{origin}{redirect}"})


# Leads is a Sites surface, so its plan gate is the "sites" feature (go+) — the
# same flag the sites router uses, decoupled from the enterprise-only Fabric
# ontology (2026-06-25 decouple-sites-from-fabric). The RBAC action stays
# "fabric.read" (an action-role, not a plan feature — unchanged here).
@router.get(
    "/sites/{site_id}/leads",
    response_model=list[LeadOut],
    dependencies=[
        Depends(require_plan_feature("sites")),
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
