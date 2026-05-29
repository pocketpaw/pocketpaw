# service.py — Audit entity business logic.
# Created: 2026-05-17 — Read-only wrapper over pocketpaw.audit.store
#   AuditStore.search_entries with mandatory workspace_id tenancy filter
#   sourced from ctx.workspace_id. # no-event: read-only entity per Rule 9.
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from beanie import PydanticObjectId

from pocketpaw.audit.store import AuditStore, get_audit_store  # type: ignore[import-untyped]
from pocketpaw_ee.cloud._core.context import RequestContext
from pocketpaw_ee.cloud._core.errors import CloudError, Internal, ValidationError
from pocketpaw_ee.cloud.audit.domain import AuditEventDomain, AuditPage
from pocketpaw_ee.cloud.audit.dto import (
    AuditEntryDTO,
    AuditEventOut,
    AuditListResponse,
    AuditPageResponse,
    AuditQueryRequest,
    ListAuditRequest,
)
from pocketpaw_ee.cloud.models.audit_event import AuditEvent as _AuditEventDoc

logger = logging.getLogger(__name__)


async def agent_list_audit(
    ctx: RequestContext,
    body: ListAuditRequest | dict | None = None,
    *,
    store: AuditStore | None = None,  # DI seam for tests
) -> AuditListResponse:
    body = ListAuditRequest.model_validate(body or {})
    if not ctx.workspace_id:
        return AuditListResponse(entries=[], total=0)
    store = store or get_audit_store()
    try:
        entries = await store.search_entries(
            workspace_id=ctx.workspace_id,
            pocket_id=body.pocket_id,
            category=body.category,
            actor=body.actor,
            q=body.q,
            limit=body.limit,
        )
    except CloudError:
        raise
    except Exception as exc:
        logger.error("audit.list failed", exc_info=True)
        raise Internal("audit.list_failed", "audit query failed") from exc
    dtos = [AuditEntryDTO.model_validate(e, from_attributes=True) for e in entries]
    return AuditListResponse(entries=dtos, total=len(dtos))


# ---------------------------------------------------------------------------
# Workspace-mutation audit log (Wave 2 Task 10).
#
# ``record`` is called from workspace/service.py on every mutating op. It is
# fire-and-forget: a write failure is logged and swallowed so an audit
# outage cannot block a legitimate mutation. ``list_events`` is the read
# path behind GET /workspaces/{id}/audit, paginated by a composite
# ``(at, _id)`` cursor for stable ordering.
#
# IP + user-agent are stubbed to None for now; threading them through
# RequestContext is deferred to Wave 3.
# ---------------------------------------------------------------------------


def _doc_to_domain(doc: _AuditEventDoc) -> AuditEventDomain:
    return AuditEventDomain(
        id=str(doc.id),
        workspace_id=doc.workspace,
        actor_id=doc.actor_id,
        action=doc.action,
        target_type=doc.target_type,
        target_id=doc.target_id,
        metadata=dict(doc.metadata or {}),
        ip=doc.ip,
        user_agent=doc.user_agent,
        at=doc.at,
    )


def _domain_to_wire(d: AuditEventDomain) -> AuditEventOut:
    return AuditEventOut(
        id=d.id,
        workspaceId=d.workspace_id,
        actorId=d.actor_id,
        action=d.action,
        targetType=d.target_type,
        targetId=d.target_id,
        metadata=d.metadata,
        ip=d.ip,
        userAgent=d.user_agent,
        at=d.at.isoformat(),
    )


def _encode_cursor(at: datetime, oid: PydanticObjectId) -> str:
    return f"{at.isoformat()}|{oid!s}"


def _decode_cursor(cursor: str) -> tuple[datetime, PydanticObjectId]:
    try:
        at_iso, oid_str = cursor.split("|", 1)
        return datetime.fromisoformat(at_iso), PydanticObjectId(oid_str)
    except (ValueError, TypeError) as exc:
        raise ValidationError("audit.bad_cursor", "Invalid pagination cursor") from exc


def _parse_iso(value: str, field_name: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError("audit.bad_timestamp", f"Invalid ISO-8601 in {field_name!r}") from exc


async def record(
    workspace_id: str,
    actor_id: str,
    action: str,
    *,
    target_type: str,
    target_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Persist one workspace audit event.

    Never raises — an audit-write failure is logged so the mutating
    operation that triggered it can complete. # no-event: audit rows ARE
    the event sink; emitting a realtime event here would be self-referential.
    """
    try:
        doc = _AuditEventDoc(
            workspace=workspace_id,
            actor_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            metadata=dict(metadata or {}),
            ip=ip,
            user_agent=user_agent,
        )
        await doc.insert()
    except Exception:
        logger.warning("audit.record failed for %s/%s", workspace_id, action, exc_info=True)
        return

    # Fire-and-forget SIEM delivery — never blocks the audit write.
    from pocketpaw_ee.cloud.audit import webhooks as _webhooks

    _webhooks.schedule_delivery(doc)


async def list_events(workspace_id: str, query: AuditQueryRequest | dict) -> AuditPage:
    """Cursor-paginated audit-event list, newest first."""
    body = AuditQueryRequest.model_validate(query or {})

    # Why uniform $and: piling fields into a flat dict works today but
    # future patches that add another ``at`` clause (e.g. a tail-window
    # filter alongside the cursor) would silently clobber the earlier
    # one. ``$and`` of leaf clauses composes safely.
    clauses: list[dict[str, Any]] = [{"workspace": workspace_id}]
    if body.action:
        clauses.append({"action": body.action})
    if body.actor:
        clauses.append({"actor_id": body.actor})

    time_filter: dict[str, datetime] = {}
    if body.since:
        time_filter["$gte"] = _parse_iso(body.since, "since")
    if body.until:
        time_filter["$lte"] = _parse_iso(body.until, "until")
    if time_filter:
        clauses.append({"at": time_filter})

    if body.cursor:
        c_at, c_oid = _decode_cursor(body.cursor)
        clauses.append(
            {
                "$or": [
                    {"at": {"$lt": c_at}},
                    {"at": c_at, "_id": {"$lt": c_oid}},
                ],
            }
        )

    mongo_filter: dict[str, Any] = clauses[0] if len(clauses) == 1 else {"$and": clauses}

    # Per reference_mongomock_quirks: no .aggregate() — straight find+sort
    # with a composite sort key for stable ordering across pages.
    docs = (
        await _AuditEventDoc.find(mongo_filter)
        .sort([("at", -1), ("_id", -1)])
        .limit(body.limit + 1)
        .to_list()
    )

    has_more = len(docs) > body.limit
    rows = docs[: body.limit]
    next_cursor = _encode_cursor(rows[-1].at, rows[-1].id) if has_more and rows else None
    return AuditPage(items=[_doc_to_domain(d) for d in rows], next_cursor=next_cursor)


async def stream_export_csv(
    workspace_id: str,
    *,
    since: str | None = None,
    until: str | None = None,
):
    """Async generator yielding CSV rows (header + one row per event).

    Iterates the motor cursor with ``async for`` so memory stays flat
    regardless of result count — ``.to_list()`` would buffer the entire
    workspace's audit history before any row reached the client.
    """
    import csv
    import io

    mongo_filter: dict[str, Any] = {"workspace": workspace_id}
    time_filter: dict[str, datetime] = {}
    if since:
        time_filter["$gte"] = _parse_iso(since, "since")
    if until:
        time_filter["$lte"] = _parse_iso(until, "until")
    if time_filter:
        mongo_filter["at"] = time_filter

    buf = io.StringIO()
    writer = csv.writer(buf)

    def _flush() -> str:
        out = buf.getvalue()
        buf.seek(0)
        buf.truncate(0)
        return out

    writer.writerow(
        ["at", "actor_id", "action", "target_type", "target_id", "ip", "user_agent", "metadata"]
    )
    yield _flush()

    cursor = _AuditEventDoc.find(mongo_filter).sort([("at", -1), ("_id", -1)])
    async for d in cursor:
        writer.writerow(
            [
                d.at.isoformat(),
                d.actor_id,
                d.action,
                d.target_type,
                d.target_id or "",
                d.ip or "",
                d.user_agent or "",
                json.dumps(d.metadata or {}, default=str),
            ]
        )
        yield _flush()


async def list_events_response(
    workspace_id: str,
    query: AuditQueryRequest | dict,
) -> AuditPageResponse:
    """Service-level helper that wraps ``list_events`` in the wire shape.

    Kept inline (cloud Rule 8: mapping helpers live next to the service)
    so the router stays a thin adapter.
    """
    page = await list_events(workspace_id, query)
    return AuditPageResponse(
        items=[_domain_to_wire(d) for d in page.items],
        nextCursor=page.next_cursor,
    )


__all__ = [
    "agent_list_audit",
    "list_events",
    "list_events_response",
    "record",
    "stream_export_csv",
]
