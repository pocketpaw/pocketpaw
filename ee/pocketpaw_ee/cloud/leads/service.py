# ee/pocketpaw_ee/cloud/leads/service.py — the ONLY writer of Lead documents.
#
# Two ways in, one insert:
#   * ``capture`` serves the public site-form routes. Hardening order: honeypot ->
#     atomic per-(scope, minute) rate limit -> injection screen -> contact-form
#     normalize/validate -> event-mapping -> persist. Origin pinning, the signed
#     key and the payload size cap live in the router (they need the request).
#   * ``capture_internal`` serves leads the product writes itself (the concierge's
#     send_to_team, a handoff with a contact). It skips the public-form steps
#     (honeypot, event_mapping, signed key, per-IP limit; its callers rate-limit
#     on their own) and keeps the injection screen.
# Both go through ``_persist``: insert, then AWAIT ``lead.captured`` on the bus
# with identifiers only (workspace_id, lead_id, site_id, site_name, form_type,
# source_kind), never the visitor's values. The emit runs inline on the request;
# ``EventBus.emit`` swallows a failing handler, so a subscriber can never lose a
# persisted lead.
#
# The owner side: ``update_lead`` (status / read) and ``mark_all_read``.
# ``lead.updated`` is emitted on a status change only. Every read and write
# filters on workspace (and site), so another tenant's lead reads as absent.
#
# Invariants: the per-IP limiter keys on the server-derived ``rate_key``, never
# the caller's ``submitter_ref``; increment-and-test over-counts a rejected
# request by one (stricter, never looser). The injection screen drops HIGH and
# above only (MEDIUM false-drops real lead text, and a lost lead is the worst
# failure). A dropped submission emits one INFO audit event with the reason and
# never the payload.

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from pocketpaw.security.injection_scanner import (
    ThreatLevel,
    get_injection_scanner,
)
from pocketpaw.sites_capture import contact_form
from pocketpaw.sites_capture.ingest import (
    interpolate_mapping,
    is_honeypot_tripped,
    origin_allowed,
)
from pocketpaw.sites_capture.models import SiteEventMapping
from pocketpaw_ee.cloud.leads.domain import Lead
from pocketpaw_ee.cloud.models.lead import Lead as _LeadDoc
from pocketpaw_ee.cloud.models.lead import LeadSource as _LeadSourceDoc
from pocketpaw_ee.cloud.models.site import Site as _SiteDoc
from pocketpaw_ee.cloud.models.site_rate_counter import SiteRateCounter as _RateCounterDoc
from pocketpaw_ee.cloud.shared.events import event_bus

logger = logging.getLogger(__name__)


def _to_domain(doc: _LeadDoc) -> Lead:
    return Lead(
        id=str(doc.id),
        workspace_id=doc.workspace,
        site_id=doc.site_id,
        form_type=doc.form_type,
        properties=doc.properties,
        submitter_ref=doc.source.submitter_ref if doc.source else "",
        origin=doc.source.origin if doc.source else "",
        origin_unrecognized=bool(doc.source and doc.source.origin_unrecognized),
        source_kind=(doc.source.kind if doc.source else "") or "form",
        conversation_ref=(doc.source.conversation_ref if doc.source else "") or "",
        status=doc.status or "new",
        read_at=doc.read_at,
        created_at=getattr(doc, "createdAt", None),
    )


def _emit_drop_audit(
    *,
    site: _SiteDoc,
    form_type: str,
    reason: str,
    count: int | None = None,
) -> None:
    """Emit ONE low-severity audit event for a dropped submission.

    Carries the drop ``reason`` + an optional numeric ``count`` (e.g. the
    rate-limit window count) and NOTHING from the form payload — the payload is
    untrusted PII the drop exists to keep out of the workspace, so it must not
    leak into the audit log. Severity is INFO (the lowest rung the audit infra
    defines; a routine ingest drop is informational, not a security violation).
    Audit failures must never break ingest, so the whole call is wrapped."""
    try:
        from pocketpaw.security.audit import AuditEvent, AuditSeverity, get_audit_logger

        context: dict[str, Any] = {
            "reason": reason,
            "site_id": site.script_name,
            "form_type": form_type,
        }
        if count is not None:
            context["count"] = count
        get_audit_logger().log(
            AuditEvent.create(
                severity=AuditSeverity.INFO,
                actor="sites_capture",
                action="sites.capture.drop",
                target=site.script_name,
                status="dropped",
                category="sites_capture",
                workspace_id=site.workspace,
                **context,
            )
        )
    except Exception:  # noqa: BLE001 — audit must never break ingest
        logger.warning("sites capture drop audit-log write failed", exc_info=True)


def _bucket_minute(now: datetime) -> datetime:
    """Truncate a UTC timestamp to the minute — the rate-limit window key."""
    return now.replace(second=0, microsecond=0)


async def _bump(scope: str, scope_id: str, bucket: datetime, now: datetime) -> int:
    """Atomically increment the (scope, scope_id, bucket) counter and return the
    POST-increment hit count. A single ``find_one_and_update`` with ``$inc`` +
    upsert is the whole check-and-increment, so two racing requests can never
    both read an under-cap value and both get in (the TOCTOU the old
    count-then-insert window had). ``return_document=True`` == AFTER (mongomock +
    pymongo accept the bool)."""
    coll = _RateCounterDoc.get_pymongo_collection()
    doc = await coll.find_one_and_update(
        {"scope": scope, "scope_id": scope_id, "bucket": bucket},
        {"$inc": {"hits": 1}, "$setOnInsert": {"created_at": now}},
        upsert=True,
        return_document=True,
    )
    return int(doc["hits"])


async def _within_rate_limit(workspace_id: str, site: _SiteDoc, rate_key: str) -> tuple[bool, int]:
    """Atomic per-minute rate limit. Increments a counter doc per window and
    tests the cap on the post-increment count, so a burst can't slip past the way
    the old read-then-write window let it.

    Returns ``(ok, count)`` where ``count`` is the post-increment hit count that
    drove the verdict (per-IP if that cap tripped, else overall) — the drop audit
    carries it.

    The per-IP window is keyed on ``rate_key`` (the server-derived host hash),
    NOT ``submitter_ref`` — see the module header. The per-IP cap is checked and
    incremented FIRST so a single flooding host that trips its own cap never eats
    the site-wide budget on its rejected requests.

    KNOWN GAP (noted in the PR): a request rejected at one cap has already
    incremented the counter it reached (and a request rejected at the OVERALL cap
    has already consumed a per-IP slot). Increment-and-test over-counts rejected
    requests by one because there is no compensating decrement / multi-key
    transaction. The effect is a slightly stricter limiter under sustained abuse,
    never a looser one — acceptable, and the safe direction to err. The counter
    is keyed and tenant-scoped per (site, minute); the window is the same minute
    bucket the old logic used. A fully race-free multi-key check would need a
    Mongo transaction across the two counter docs (deferred)."""
    now = datetime.now(UTC)
    bucket = _bucket_minute(now)
    site_scope_id = f"{workspace_id}:{site.script_name}"

    per_ip = await _bump("ip", f"{site_scope_id}:{rate_key}", bucket, now)
    if per_ip > site.per_ip_limit_per_min:
        return False, per_ip
    overall = await _bump("site", site_scope_id, bucket, now)
    if overall > site.rate_limit_per_min:
        return False, overall
    return True, per_ip


# Form input is untrusted, attacker-controlled text. A HIGH-or-higher verdict
# from the injection scanner means a known instruction-override / persona-hijack
# / delimiter / exfil / jailbreak / tool-abuse pattern was matched, so the
# submission is dropped rather than persisted and surfaced into the workspace.
# Threshold is HIGH, not MEDIUM: MEDIUM risked false-dropping legitimate lead
# text (e.g. "act as a guarantor" scans MEDIUM persona_hijack), and a lost lead
# is the worst failure here.
_INJECTION_DROP_THRESHOLD = ThreatLevel.HIGH
_THREAT_RANK = {ThreatLevel.NONE: 0, ThreatLevel.LOW: 1, ThreatLevel.MEDIUM: 2, ThreatLevel.HIGH: 3}


def _passes_injection_screen(payload: dict[str, Any]) -> bool:
    """Screen the stringified form payload through the real InjectionScanner.

    Returns False (drop the submission) when the scanner reports a HIGH-or-higher
    threat (see ``_INJECTION_DROP_THRESHOLD``). The scanner's heuristic ``scan``
    is synchronous and needs no LLM/API key, so it always runs. Replaces the
    prior dead Guardian call, which referenced a ``check_input`` method
    GuardianAgent never had and so always accepted."""
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
    origin: str = "",
    known_origins: list[str] | None = None,
) -> Lead | None:
    """Harden + persist one submission as a tenant-scoped Lead. Returns None
    when the submission is dropped (honeypot / rate-limited / injection screen /
    no mapping for this form_type).

    ``rate_key`` is the server-derived per-IP limiter identity (the router hashes
    the client host). ``submitter_ref`` is only an opaque label. An empty
    ``rate_key`` falls back to a fixed sentinel — never to ``submitter_ref`` —
    so a missing client host collapses to one shared bucket rather than handing
    the caller a fresh bucket per request.

    ``origin`` is the submitting page's ``Origin`` header, recorded on the lead for
    attribution. It is NOT a gate here: the router owns the (opt-in,
    ``Site.enforce_origin``) enforcement decision, and by default a submission from
    an unrecognized host is accepted and flagged rather than dropped. Passing it is
    what lets an owner see that leads are arriving from somewhere they did not
    expect — the visibility that replaces the old silent 403.

    ``known_origins`` is the set the flag is judged against, and the router passes
    its DERIVED set (``allowed_origins`` plus the site's own url host and attached
    custom domains) rather than letting this re-read the stored field. The two must
    agree: if the gate accepts a host the flag then marks unrecognized, the flag
    fires on a site's own normal traffic and stops meaning anything. Defaults to the
    stored field for a caller with nothing better."""
    effective_rate_key = rate_key or "unknown"
    if is_honeypot_tripped(payload, honeypot_field=site.honeypot_field):
        _emit_drop_audit(site=site, form_type=form_type, reason="honeypot")
        return None
    ok, window_count = await _within_rate_limit(site.workspace, site, effective_rate_key)
    if not ok:
        _emit_drop_audit(site=site, form_type=form_type, reason="rate_limit", count=window_count)
        return None
    if not _passes_injection_screen(payload):
        _emit_drop_audit(site=site, form_type=form_type, reason="injection")
        return None

    # CONTACT FORM ONLY — alias normalization + schema validation. Other form
    # types have no declared schema, so they keep the previous behaviour exactly.
    if form_type == contact_form.CONTACT_FORM_TYPE:
        # Non-destructive: adds canonical keys, drops nothing. This is what makes
        # a lead from an ALREADY-PUBLISHED site land — those Workers POST
        # ``name=...`` and will until someone republishes them, which nobody does
        # because the site looks fine. Also what lets an IMPORTED form's
        # ``your-email`` / ``Phone Number`` reach the mapping at all.
        payload = contact_form.normalize(payload)
        reason = contact_form.validate(payload)
        if reason is not None:
            _emit_drop_audit(site=site, form_type=form_type, reason=reason)
            return None

    raw_mapping = site.event_mapping.get(form_type)
    if raw_mapping is None:
        _emit_drop_audit(site=site, form_type=form_type, reason="no_mapping")
        return None
    mapping = SiteEventMapping.model_validate(raw_mapping)
    properties = interpolate_mapping(mapping, {"payload": payload, "submitter_ref": submitter_ref})

    return await _persist(
        site,
        form_type,
        properties,
        _LeadSourceDoc(
            form_type=form_type,
            site_id=site.script_name,
            submitter_ref=submitter_ref,
            rate_key=effective_rate_key,
            origin=origin,
            # Evaluated NOW, against the allowlist as it stands at capture time, so
            # the flag keeps meaning "unrecognized when it arrived" even after the
            # owner later connects the domain it came from.
            origin_unrecognized=bool(origin)
            and not origin_allowed(
                site.allowed_origins if known_origins is None else known_origins, origin
            ),
        ),
    )


async def _persist(
    site: _SiteDoc,
    form_type: str,
    properties: dict[str, Any],
    source: _LeadSourceDoc,
    *,
    status: str = "new",
) -> Lead:
    """Insert one Lead and ring the workspace. The one insert both entries share."""
    doc = _LeadDoc(
        workspace=site.workspace,
        site_id=site.script_name,
        form_type=form_type,
        properties=properties,
        source=source,
        status=status,
    )
    await doc.insert()
    # Identifiers only, never the visitor's values (untrusted PII); a subscriber
    # that needs them reads the Lead. ``source_kind`` lets the bridge skip a
    # handoff lead, whose handoff already notified the owner.
    await event_bus.emit(
        "lead.captured",
        {
            "workspace_id": site.workspace,
            "lead_id": str(doc.id),
            "site_id": site.script_name,
            # The site's DISPLAY name (site_id is a hex script name); "" when unnamed.
            "site_name": site.name,
            "form_type": form_type,
            "source_kind": source.kind,
        },
    )
    return _to_domain(doc)


async def capture_internal(
    *,
    site: _SiteDoc,
    form_type: str,
    kind: str,
    properties: dict[str, Any],
    conversation_ref: str = "",
    submitter_ref: str = "",
    status: str = "new",
) -> Lead | None:
    """Persist a lead the product writes itself (``kind`` concierge / handoff /
    booking). No honeypot, event_mapping, signed key or per-IP limit: the caller
    already authenticated the visitor and enforces its own rate limit. The HIGH
    injection screen still runs; a drop returns None and writes nothing."""
    if not _passes_injection_screen(properties):
        _emit_drop_audit(site=site, form_type=form_type, reason="injection")
        return None
    return await _persist(
        site,
        form_type,
        dict(properties),
        _LeadSourceDoc(
            form_type=form_type,
            site_id=site.script_name,
            submitter_ref=submitter_ref,
            kind=kind,
            conversation_ref=conversation_ref,
        ),
        status=status,
    )


async def list_for_site(workspace_id: str, site_id: str, *, limit: int = 100) -> list[Lead]:
    cursor = (
        _LeadDoc.find({"workspace": workspace_id, "site_id": site_id})
        .sort(-_LeadDoc.createdAt)  # type: ignore[operator]
        .limit(limit)
    )
    return [_to_domain(doc) async for doc in cursor]


async def has_conversation_lead(
    workspace_id: str, site_id: str, kind: str, conversation_ref: str
) -> bool:
    """Whether this site already holds a ``kind`` lead for the conversation."""
    if not conversation_ref:
        return False
    found = await _LeadDoc.find_one(
        {
            "workspace": workspace_id,
            "site_id": site_id,
            "source.kind": kind,
            "source.conversation_ref": conversation_ref,
        }
    )
    return found is not None


async def count_for_site(workspace_id: str, site_id: str) -> int:
    return await _LeadDoc.find({"workspace": workspace_id, "site_id": site_id}).count()


def _oid(lead_id: str) -> Any:
    from beanie import PydanticObjectId

    try:
        return PydanticObjectId(lead_id)
    except Exception:  # noqa: BLE001 — a malformed id is simply not found
        return None


async def update_lead(
    workspace_id: str,
    site_id: str,
    lead_id: str,
    *,
    status: str | None = None,
    read: bool | None = None,
) -> Lead | None:
    """Set a lead's status and/or read state; None when it isn't this
    workspace's lead on this site. ``read`` true keeps the first read time.
    Emits ``lead.updated`` when the status actually changed."""
    oid = _oid(lead_id)
    if oid is None:
        return None
    query = {"_id": oid, "workspace": workspace_id, "site_id": site_id}
    doc = await _LeadDoc.find_one(query)
    if doc is None:
        return None
    previous = doc.status or "new"
    update: dict[str, Any] = {}
    if status is not None and status != previous:
        update["status"] = status
    if read is True and doc.read_at is None:
        update["read_at"] = datetime.now(UTC)
    elif read is False and doc.read_at is not None:
        update["read_at"] = None
    if update:
        # A targeted $set on the tenant-scoped filter, never a whole-doc save.
        await _LeadDoc.get_pymongo_collection().update_one(query, {"$set": update})
        doc = await _LeadDoc.find_one(query)
        if doc is None:
            return None
    if "status" in update:
        await event_bus.emit(
            "lead.updated",
            {
                "workspace_id": workspace_id,
                "lead_id": lead_id,
                "site_id": site_id,
                "status": update["status"],
                "previous_status": previous,
            },
        )
    return _to_domain(doc)


async def mark_all_read(workspace_id: str, site_id: str) -> int:
    """Mark every unread lead on this workspace's site read; returns how many."""
    result = await _LeadDoc.get_pymongo_collection().update_many(
        {"workspace": workspace_id, "site_id": site_id, "read_at": None},
        {"$set": {"read_at": datetime.now(UTC)}},
    )
    return int(result.modified_count)


async def lead_payload(workspace_id: str, lead_id: str) -> dict[str, Any] | None:
    """The full lead as the owner-notification sinks see it (email body, webhook
    ``data``), or None when it doesn't exist in this workspace. Loaded at send
    time by the notification outbox so the queue never stores visitor data."""
    oid = _oid(lead_id)
    if oid is None:
        return None
    doc = await _LeadDoc.find_one({"_id": oid, "workspace": workspace_id})
    if doc is None:
        return None
    site = await _SiteDoc.find_one({"workspace": workspace_id, "script_name": doc.site_id})
    contact = contact_form.normalize(dict(doc.properties or {}))
    src = doc.source
    created = getattr(doc, "createdAt", None)
    return {
        "id": str(doc.id),
        "site_id": str(site.id) if site is not None else doc.site_id,
        "site_name": (site.name if site is not None else "") or "",
        "form_type": doc.form_type,
        "status": doc.status or "new",
        "name": str(contact.get(contact_form.FULL_NAME) or ""),
        "email": str(contact.get(contact_form.EMAIL) or ""),
        "phone": str(contact.get(contact_form.PHONE) or ""),
        "message": str(contact.get(contact_form.MESSAGE) or ""),
        "properties": dict(doc.properties or {}),
        "source": {
            "kind": str(getattr(src, "kind", "") or "form"),
            "form_type": getattr(src, "form_type", doc.form_type),
            "origin": getattr(src, "origin", ""),
            "origin_unrecognized": bool(getattr(src, "origin_unrecognized", False)),
            "conversation_ref": str(getattr(src, "conversation_ref", "") or ""),
        },
        "created_at": created.isoformat() if created is not None else None,
    }


__all__ = [
    "Lead",
    "capture",
    "capture_internal",
    "count_for_site",
    "has_conversation_lead",
    "lead_payload",
    "list_for_site",
    "mark_all_read",
    "update_lead",
]
