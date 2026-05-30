# ee/pocketpaw_ee/cloud/leads/service.py — sole owner of Lead writes. The
# public capture endpoint calls capture(); the Leads view calls list_for_site().
# Ingest hardening order mirrors paw-print: honeypot → rate limit → Guardian →
# event-mapping → persist. Origin pinning is enforced at the router (it needs
# the request's Origin header). Tenancy: every read filters on workspace.
#
# Created 2026-05-30 (feat/paw-sites-backend, RFC 12 Task 3.3): the cloud
# capture pipeline composing the OSS sites_capture primitive (honeypot +
# mapping interpolation) with a Mongo sliding-window rate limit, a best-effort
# Guardian screen, and the tenant-scoped Lead doc write + reads.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pocketpaw.sites_capture.ingest import interpolate_mapping, is_honeypot_tripped
from pocketpaw.sites_capture.models import SiteEventMapping
from pocketpaw_ee.cloud.leads.domain import Lead
from pocketpaw_ee.cloud.models.lead import Lead as _LeadDoc
from pocketpaw_ee.cloud.models.lead import LeadSource as _LeadSourceDoc
from pocketpaw_ee.cloud.models.site import Site as _SiteDoc


def _to_domain(doc: _LeadDoc) -> Lead:
    return Lead(
        id=str(doc.id),
        workspace_id=doc.workspace,
        site_id=doc.site_id,
        form_type=doc.form_type,
        properties=doc.properties,
        submitter_ref=doc.source.submitter_ref if doc.source else "",
        created_at=getattr(doc, "createdAt", None),
    )


async def _within_rate_limit(workspace_id: str, site: _SiteDoc, submitter_ref: str) -> bool:
    """Mongo sliding-window counter — generalization of paw-print's
    `within_rate_limit`. Counts leads in the last minute overall + per IP."""
    window_start = datetime.now(UTC) - timedelta(minutes=1)
    overall = await _LeadDoc.find(
        {"workspace": workspace_id, "site_id": site.script_name, "createdAt": {"$gte": window_start}}
    ).count()
    if overall >= site.rate_limit_per_min:
        return False
    per_ip = await _LeadDoc.find(
        {
            "workspace": workspace_id,
            "site_id": site.script_name,
            "source.submitter_ref": submitter_ref,
            "createdAt": {"$gte": window_start},
        }
    ).count()
    return per_ip < site.per_ip_limit_per_min


async def _pass_guardian(payload: dict[str, Any]) -> bool:
    """Best-effort Guardian screen — tolerant when the security stack is absent
    (verbatim posture from paw-print's `_pass_through_guardian`)."""
    import json

    try:
        from pocketpaw.security.guardian import get_guardian
    except Exception:
        return True
    try:
        guardian = get_guardian()
        check = getattr(guardian, "check_input", None)
        if check is None:
            return True
        verdict = await check(json.dumps(payload, default=str))
    except Exception:
        return True
    if isinstance(verdict, bool):
        return verdict
    return not getattr(verdict, "blocked", False)


async def capture(
    *,
    site: _SiteDoc,
    form_type: str,
    payload: dict[str, Any],
    submitter_ref: str,
) -> Lead | None:
    """Harden + persist one submission as a tenant-scoped Lead. Returns None
    when the submission is dropped (honeypot / rate-limited / Guardian / no
    mapping for this form_type)."""
    if is_honeypot_tripped(payload, honeypot_field=site.honeypot_field):
        return None
    if not await _within_rate_limit(site.workspace, site, submitter_ref):
        return None
    if not await _pass_guardian(payload):
        return None

    raw_mapping = site.event_mapping.get(form_type)
    if raw_mapping is None:
        return None
    mapping = SiteEventMapping.model_validate(raw_mapping)
    properties = interpolate_mapping(mapping, {"payload": payload, "submitter_ref": submitter_ref})

    doc = _LeadDoc(
        workspace=site.workspace,
        site_id=site.script_name,
        form_type=form_type,
        properties=properties,
        source=_LeadSourceDoc(form_type=form_type, site_id=site.script_name, submitter_ref=submitter_ref),
    )
    await doc.insert()
    # no-event: lead capture is a public ingest; the Leads view polls, no realtime subscriber yet
    return _to_domain(doc)


async def list_for_site(workspace_id: str, site_id: str, *, limit: int = 100) -> list[Lead]:
    cursor = (
        _LeadDoc.find({"workspace": workspace_id, "site_id": site_id})
        .sort(-_LeadDoc.createdAt)  # type: ignore[operator]
        .limit(limit)
    )
    return [_to_domain(doc) async for doc in cursor]


async def count_for_site(workspace_id: str, site_id: str) -> int:
    return await _LeadDoc.find({"workspace": workspace_id, "site_id": site_id}).count()


__all__ = ["Lead", "capture", "list_for_site", "count_for_site"]
