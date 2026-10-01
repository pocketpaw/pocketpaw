# ee/pocketpaw_ee/cloud/leads/router.py — the leads routes.
#
# Public ingest (no auth dependency; the per-site signed key is the credential):
#   * POST /sites/{site_id}/capture — JSON capture (the edge Queue drains here).
#   * POST /capture/form — native-form capture for IMPORTED sites (urlencoded,
#     hidden paw_site_id / paw_key / paw_page / paw_redirect / paw_form_type),
#     answered with a 303 back to the site. Final URL {captureApiBase}/capture/form.
#   Both: site exists -> origin gate (only when ``Site.enforce_origin``) ->
#   constant-time key compare -> payload size cap -> ``leads_service.capture``.
#   The origin is otherwise RECORDED on the lead (``origin_unrecognized`` judged
#   against ``_effective_origins``: the allowlist plus the site's own url host and
#   custom domains). The pin guarded a key that is public in page source and only
#   bound browsers, so as a default gate it mostly 403'd real visitors.
#
# Owner routes ("sites" plan feature + ``fabric.read``, workspace-scoped; another
# workspace's lead is a 404). ``site_id`` is the site's script_name, as on the Lead:
#   * GET   /sites/{site_id}/leads
#   * PATCH /sites/{site_id}/leads/{lead_id}  {status?, read?}
#   * POST  /sites/{site_id}/leads/read-all   -> {updated}
#
# Invariants: the per-IP limiter key is derived from the connection
# (``_rate_key``), never the body; ``_redirect_base`` never emits a host the caller
# chose, and on a site with no url ``_safe_relative_redirect`` is the ONLY lock on
# the 303 Location, so its ``//`` and ``://`` rejections must stay.

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
from pocketpaw_ee.cloud.leads.dto import (
    CaptureRequest,
    CaptureResponse,
    LeadOut,
    LeadUpdate,
    ReadAllResponse,
    lead_to_dto,
)
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


def _origin_of(request: Request) -> str:
    """The submitting page's ``Origin`` header, trimmed ("" when absent)."""
    return (request.headers.get("origin") or "").strip()


def _effective_origins(site: _SiteDoc) -> list[str]:
    """The hosts a submission may legitimately come FROM, derived rather than only
    read off ``allowed_origins``.

    ``allowed_origins`` is STAMPED at publish (``_with_deployed_host``) and grown by
    ``add_domain``, which means it is only as good as the write paths that maintain
    it — and they have gaps. A draft/preview publish returns before the deploy stamp
    runs, an async (react) build inserts its Site row with ``url=""`` and fills the
    url in later from the worker, and any row created before the stamping landed
    still carries just the localhost seed. In every one of those the site's OWN
    deployed host is missing from its own allowlist, so its own visitors read as
    foreign.

    That was survivable only while nobody looked at the answer. Now that origin is
    recorded on each lead, a stale allowlist would mark a site's genuine traffic
    ``origin_unrecognized`` — a flag that fires on the normal case teaches an owner
    to ignore it, which is worse than not having it. And for a site that opts INTO
    enforcement it would be a live 403 on its own pages.

    So the site's canonical ``url`` host and every attached custom ``domains``
    hostname are folded in here. Both are values WE wrote from a deploy we
    performed, never caller input, so this widens the set only to hosts the site
    demonstrably owns.
    """
    hosts = list(site.allowed_origins)
    candidates = [site.url or ""]
    candidates.extend(d.hostname for d in (site.domains or []) if d.hostname)
    for candidate in candidates:
        host = candidate.strip().lower()
        if "://" in host:
            host = host.split("://", 1)[1]
        host = host.split("/", 1)[0].split(":", 1)[0]
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def _origin_gate(site: _SiteDoc, origin: str) -> None:
    """Enforce the origin pin ONLY for a site that opted into it.

    Formspree posture (see ``Site.enforce_origin`` for the full reasoning): by
    default a submission from an unrecognized host is ACCEPTED and attributed on
    the Lead rather than 403'd. The pin guards a credential that is already public
    in the page source on three of the four engines, and ``Origin`` constrains
    browsers only — so as a gate it mostly turned real submissions into a JSON 403
    the customer's prospect had to look at.

    A site that flips ``enforce_origin`` gets the old fail-closed behaviour back
    verbatim, including on an empty allowlist and a missing header.
    """
    if site.enforce_origin and not origin_allowed(_effective_origins(site), origin or None):
        raise HTTPException(403, "Origin not allowed for this site")


def _redirect_base(site: _SiteDoc, origin: str) -> str:
    """Absolute prefix for the native-form 303, chosen so it can NEVER be an
    attacker-controlled host.

    This used to be the request ``Origin`` unconditionally, which was safe only
    because the origin had just been pinned. With the pin now opt-in, that would be
    an open redirect: anyone could POST with ``Origin: https://evil.test`` and be
    sent there. So the base is resolved in a strict order:

      1. the request Origin, but ONLY when it is on the site's allowlist — the
         common case, and the one that keeps a visitor on the custom domain they
         are actually browsing rather than bouncing them to a workers.dev URL;
      2. the site's own canonical ``url`` — correct whenever the origin is absent,
         unrecognized, or forged;
      3. "" — a relative Location, when the site has no url yet (a draft, or an
         async build whose worker has not filled it in).

    Every branch is a value WE control or have already validated, so an unvalidated
    origin cannot reach the ``Location`` header under any input.
    """
    if origin and origin_allowed(_effective_origins(site), origin):
        return origin.rstrip("/")
    return (site.url or "").rstrip("/")


@router.post("/sites/{site_id}/capture", response_model=CaptureResponse)
async def capture_lead(site_id: str, body: CaptureRequest, request: Request) -> CaptureResponse:
    """Public ingest. Order: site exists → origin pinned → signed key → payload
    size cap → delegate to the service (honeypot/rate/injection screen/mapping)."""
    # global-read: public ingest is keyed by site, not by a workspace session.
    site = await _SiteDoc.find_one({"script_name": site_id})
    if site is None:
        raise HTTPException(404, "Site not found")

    origin = _origin_of(request)
    _origin_gate(site, origin)

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
        origin=origin,
        known_origins=_effective_origins(site),
    )
    if lead is None:
        return CaptureResponse(ok=False, reason="dropped")
    return CaptureResponse(ok=True, lead_id=lead.id)


def _safe_relative_redirect(path: str) -> bool:
    """Open-redirect guard for the native-form 303: the redirect target must be a
    RELATIVE path on the submitting site. Rejects absolute URLs (no leading "/"),
    protocol-relative ("//host"), backslash tricks, embedded schemes, and CR/LF
    (header injection).

    This carries MORE weight since the origin pin became opt-in (2026-08-13). It
    used to be the second of two locks — the accepted path was prefixed with an
    Origin that had already been pinned, so a bad path still could not leave the
    site. Now ``_redirect_base`` resolves to "" for a site with no canonical url
    yet (a draft, or an async build whose worker has not reported back), and on
    that branch THIS FUNCTION IS THE ONLY LOCK: the accepted value becomes the
    whole ``Location``. The ``//host`` and ``://`` rejections are what stop a
    protocol-relative or absolute target there, so neither may be relaxed on the
    grounds that something downstream re-checks the host. Nothing does."""
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

    origin = _origin_of(request)
    _origin_gate(site, origin)

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
        origin=origin,
        known_origins=_effective_origins(site),
    )

    # 303 back to the site. The base is resolved by ``_redirect_base`` — an
    # allowlisted Origin, else the site's own canonical url, else relative — so the
    # Location is never a host the caller chose. The redirect path itself already
    # passed ``_safe_relative_redirect`` above.
    location = f"{_redirect_base(site, origin)}{redirect}"
    return Response(status_code=303, headers={"Location": location})


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


_OWNER_DEPS = [
    Depends(require_plan_feature("sites")),
    Depends(require_action_any_workspace("fabric.read")),
]


@router.patch(
    "/sites/{site_id}/leads/{lead_id}",
    response_model=LeadOut,
    dependencies=_OWNER_DEPS,
)
async def update_lead(
    site_id: str,
    lead_id: str,
    body: LeadUpdate,
    ctx: RequestContext = Depends(request_context),
) -> LeadOut:
    """Set a lead's status and/or read state. ``lead.updated`` fires on a status
    change. Another workspace's (or site's) lead is a 404."""
    lead = await leads_service.update_lead(
        ctx.workspace_id, site_id, lead_id, status=body.status, read=body.read
    )
    if lead is None:
        raise HTTPException(404, "Lead not found")
    return lead_to_dto(lead)


@router.post(
    "/sites/{site_id}/leads/read-all",
    response_model=ReadAllResponse,
    dependencies=_OWNER_DEPS,
)
async def read_all_leads(
    site_id: str,
    ctx: RequestContext = Depends(request_context),
) -> ReadAllResponse:
    """Mark every unread lead on this site read."""
    return ReadAllResponse(updated=await leads_service.mark_all_read(ctx.workspace_id, site_id))
