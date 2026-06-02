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
# submission at/above the drop threshold (see the follow-up item 5 note below for
# the MEDIUM -> HIGH threshold change).
#
# Updated 2026-05-30 (follow-up item 1): the per-IP rate-limit bucket is keyed on
# a SERVER-derived ``rate_key`` (the router hashes ``request.client.host``), not
# the caller-controlled ``submitter_ref``. ``submitter_ref`` was trivially
# randomizable to mint a fresh per-IP bucket on every request and so was never a
# real limiter. It is retained only as an opaque, non-PII provenance LABEL on the
# stored Lead; it is never the limiter key.
#
# Updated 2026-05-30 (follow-up item 2): every dropped submission (honeypot /
# rate-limit / injection screen) emits ONE low-severity audit event carrying the
# drop reason + counts via the canonical audit infra
# (``get_audit_logger().log(AuditEvent.create(...))``). The event NEVER carries
# the form payload — the payload is attacker-/user-supplied PII, and the whole
# point of the drop is to keep it out of the workspace, so it must not be
# resurfaced through the audit log either. "Low severity" maps to
# ``AuditSeverity.INFO``: the audit enum defines INFO < WARNING < CRITICAL <
# ALERT and has no dedicated LOW rung, and a routine ingest drop is informational
# (not a workspace-mutating or security-violation event).
#
# Updated 2026-05-30 (follow-up item 3): the rate limit is now ATOMIC. The old
# window read a persisted-lead count and THEN inserted (TOCTOU — a burst all read
# under-cap before any insert landed and all slipped past). It now ``$inc``-s a
# per-(scope, minute) ``SiteRateCounter`` doc via one ``find_one_and_update`` and
# tests the cap on the post-increment result, so the check and the increment are
# a single atomic step. See ``_within_rate_limit`` for the one known residual gap
# (increment-and-test over-counts a REJECTED request by one — strictly safer, not
# looser).
#
# Updated 2026-05-30 (follow-up item 5): the injection-screen drop threshold
# moved MEDIUM -> HIGH (``_INJECTION_DROP_THRESHOLD``). MEDIUM risked
# false-dropping legitimate lead text (e.g. "act as a guarantor" scans MEDIUM
# persona_hijack), and a lost lead is the worst failure here, so only HIGH-or-
# above verdicts now drop.

from __future__ import annotations

import logging
from datetime import UTC, datetime
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
from pocketpaw_ee.cloud.models.site_rate_counter import SiteRateCounter as _RateCounterDoc

logger = logging.getLogger(__name__)


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
        _emit_drop_audit(site=site, form_type=form_type, reason="honeypot")
        return None
    ok, window_count = await _within_rate_limit(site.workspace, site, effective_rate_key)
    if not ok:
        _emit_drop_audit(site=site, form_type=form_type, reason="rate_limit", count=window_count)
        return None
    if not _passes_injection_screen(payload):
        _emit_drop_audit(site=site, form_type=form_type, reason="injection")
        return None

    raw_mapping = site.event_mapping.get(form_type)
    if raw_mapping is None:
        _emit_drop_audit(site=site, form_type=form_type, reason="no_mapping")
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
