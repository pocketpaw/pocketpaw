# ee/pocketpaw_ee/cloud/leads/service.py — sole owner of Lead writes. The
# public capture endpoint calls capture(); the Leads view calls list_for_site().
# Ingest hardening order: honeypot → rate limit → injection screen →
# event-mapping → persist. Origin pinning + the payload size cap are enforced at
# the router (they need the request). Tenancy: every read filters on workspace.
#
# Created 2026-05-30 (feat/paw-sites-backend, RFC 12 Task 3.3): the cloud
# capture pipeline composing the OSS sites_capture primitive (honeypot +
# mapping interpolation) with a Mongo sliding-window rate limit, an input
# screen, and the tenant-scoped Lead doc write + reads.
#
# Updated 2026-05-30 (security hardening, H2): replaced the dead Guardian
# `check_input` call — GuardianAgent only screens shell commands (check_command),
# so `getattr(guardian, "check_input", None)` was always None and the screen
# always accepted, letting untrusted form input reach mapping + DB unchecked.
# Now screens the stringified payload through the real InjectionScanner (the
# command-injection / prompt-injection heuristic scanner) and drops any
# submission with a MEDIUM-or-higher threat verdict.
#
# Updated 2026-05-30 (follow-up item 1): the per-IP rate-limit bucket is keyed on
# a SERVER-derived ``rate_key`` (the router hashes ``request.client.host``), not
# the caller-controlled ``submitter_ref``. ``submitter_ref`` was trivially
# randomizable to mint a fresh per-IP bucket on every request and so was never a
# real limiter. It is retained only as an opaque, non-PII provenance LABEL on the
# stored Lead; it is never the limiter key.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pocketpaw.security.injection_scanner import (
    ThreatLevel,
    get_injection_scanner,
)
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


async def _within_rate_limit(workspace_id: str, site: _SiteDoc, rate_key: str) -> bool:
    """Mongo sliding-window counter — generalization of paw-print's
    `within_rate_limit`. Counts leads in the last minute overall + per IP.

    The per-IP window is keyed on ``rate_key`` (the server-derived hash of the
    client host stored on ``source.rate_key``), NOT ``submitter_ref`` — see the
    module header. ``rate_key`` is server-controlled, so a caller cannot mint a
    fresh per-IP bucket by varying a request body field."""
    window_start = datetime.now(UTC) - timedelta(minutes=1)
    overall = await _LeadDoc.find(
        {
            "workspace": workspace_id,
            "site_id": site.script_name,
            "createdAt": {"$gte": window_start},
        }
    ).count()
    if overall >= site.rate_limit_per_min:
        return False
    per_ip = await _LeadDoc.find(
        {
            "workspace": workspace_id,
            "site_id": site.script_name,
            "source.rate_key": rate_key,
            "createdAt": {"$gte": window_start},
        }
    ).count()
    return per_ip < site.per_ip_limit_per_min


# Form input is untrusted, attacker-controlled text. A MEDIUM-or-higher verdict
# from the injection scanner means a known instruction-override / persona-hijack
# / delimiter / exfil / jailbreak / tool-abuse pattern was matched, so the
# submission is dropped rather than persisted and surfaced into the workspace.
_INJECTION_DROP_THRESHOLD = ThreatLevel.MEDIUM
_THREAT_RANK = {ThreatLevel.NONE: 0, ThreatLevel.LOW: 1, ThreatLevel.MEDIUM: 2, ThreatLevel.HIGH: 3}


def _passes_injection_screen(payload: dict[str, Any]) -> bool:
    """Screen the stringified form payload through the real InjectionScanner.

    Returns False (drop the submission) when the scanner reports a MEDIUM-or-
    higher threat. The scanner's heuristic ``scan`` is synchronous and needs no
    LLM/API key, so it always runs. Replaces the prior dead Guardian call, which
    referenced a ``check_input`` method GuardianAgent never had and so always
    accepted."""
    import json

    content = json.dumps(payload, default=str)
    result = get_injection_scanner().scan(content, source="sites_capture")
    return _THREAT_RANK[result.threat_level] < _THREAT_RANK[_INJECTION_DROP_THRESHOLD]


async def capture(
    *,
    site: _SiteDoc,
    form_type: str,
    payload: dict[str, Any],
    submitter_ref: str,
    rate_key: str = "",
) -> Lead | None:
    """Harden + persist one submission as a tenant-scoped Lead. Returns None
    when the submission is dropped (honeypot / rate-limited / injection screen /
    no mapping for this form_type).

    ``rate_key`` is the server-derived per-IP limiter identity (the router hashes
    the client host). ``submitter_ref`` is only an opaque label. An empty
    ``rate_key`` falls back to a fixed sentinel — never to ``submitter_ref`` —
    so a missing client host collapses to one shared bucket rather than handing
    the caller a fresh bucket per request."""
    effective_rate_key = rate_key or "unknown"
    if is_honeypot_tripped(payload, honeypot_field=site.honeypot_field):
        return None
    if not await _within_rate_limit(site.workspace, site, effective_rate_key):
        return None
    if not _passes_injection_screen(payload):
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
        source=_LeadSourceDoc(
            form_type=form_type,
            site_id=site.script_name,
            submitter_ref=submitter_ref,
            rate_key=effective_rate_key,
        ),
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
