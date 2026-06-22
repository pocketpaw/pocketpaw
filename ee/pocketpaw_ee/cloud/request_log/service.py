"""Service layer for the RequestLog entity.

``record()`` is called from the middleware after every HTTP request.
``list_events()`` and ``list_events_response()`` power the read path.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from beanie import PydanticObjectId

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.models.request_log import RequestLog as _RequestLogDoc
from pocketpaw_ee.cloud.request_log.dto import (
    RequestLogOut,
    RequestLogPageResponse,
    RequestLogQuery,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Domain helpers
# ---------------------------------------------------------------------------


def _doc_to_wire(doc: _RequestLogDoc) -> RequestLogOut:
    return RequestLogOut(
        id=str(doc.id),
        workspaceId=doc.workspace,
        actorId=doc.actor_id,
        method=doc.method,
        path=doc.path,
        statusCode=doc.status_code,
        durationMs=doc.duration_ms,
        isError=doc.is_error,
        ip=doc.ip,
        userAgent=doc.user_agent,
        at=doc.at.isoformat(),
    )


def _encode_cursor(at: datetime, oid: PydanticObjectId) -> str:
    return f"{at.isoformat()}|{oid!s}"


def _decode_cursor(cursor: str) -> tuple[datetime, PydanticObjectId]:
    try:
        at_iso, oid_str = cursor.split("|", 1)
        return datetime.fromisoformat(at_iso), PydanticObjectId(oid_str)
    except (ValueError, TypeError) as exc:
        raise ValidationError("request_log.bad_cursor", "Invalid pagination cursor") from exc


def _parse_iso(value: str, field_name: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(
            "request_log.bad_timestamp",
            f"Invalid ISO-8601 in {field_name!r}",
        ) from exc


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


async def record(
    workspace_id: str,
    actor_id: str,
    method: str,
    path: str,
    status_code: int,
    duration_ms: float,
    is_error: bool,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Persist one HTTP request log entry.

    Fire-and-forget: a write failure is logged and swallowed so a request-log
    outage cannot block the application.
    """
    try:
        doc = _RequestLogDoc(
            workspace=workspace_id,
            actor_id=actor_id,
            method=method,
            path=path,
            status_code=status_code,
            duration_ms=duration_ms,
            is_error=is_error,
            ip=ip,
            user_agent=user_agent,
        )
        await doc.insert()
    except Exception:
        logger.warning("request_log.record failed for %s %s", method, path, exc_info=True)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


async def list_events(
    workspace_id: str,  # kept for auth gate; NOT used as a filter — returns ALL logs
    query: RequestLogQuery | dict,
) -> tuple[list[RequestLogOut], str | None]:
    """Cursor-paginated request-log list, newest first.

    Returns entries from ALL workspaces (and non-workspace global requests).
    The ``workspace_id`` parameter is only used for the auth gate on the
    router — the read path does NOT filter by workspace.
    """
    body = RequestLogQuery.model_validate(query or {})

    clauses: list[dict[str, Any]] = []
    if body.method:
        clauses.append({"method": body.method.upper()})
    if body.actor:
        clauses.append({"actor_id": body.actor})
    if body.isError is not None:
        clauses.append({"is_error": body.isError})

    status_filter: dict[str, int] = {}
    if body.minStatus is not None:
        status_filter["$gte"] = body.minStatus
    if body.maxStatus is not None:
        status_filter["$lte"] = body.maxStatus
    if status_filter:
        clauses.append({"status_code": status_filter})

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

    mongo_filter: dict[str, Any] = (
        clauses[0] if len(clauses) == 1 else {"$and": clauses} if clauses else {}
    )

    docs = (
        await _RequestLogDoc.find(mongo_filter)
        .sort([("at", -1), ("_id", -1)])
        .limit(body.limit + 1)
        .to_list()
    )

    has_more = len(docs) > body.limit
    rows = docs[: body.limit]
    next_cursor = _encode_cursor(rows[-1].at, rows[-1].id) if has_more and rows else None
    items = [_doc_to_wire(d) for d in rows]
    return items, next_cursor


async def list_events_response(
    workspace_id: str,
    query: RequestLogQuery | dict,
) -> RequestLogPageResponse:
    """Service-level helper returning the wire shape."""
    items, next_cursor = await list_events(workspace_id, query)
    return RequestLogPageResponse(items=items, nextCursor=next_cursor)


__all__ = [
    "list_events",
    "list_events_response",
    "record",
]
