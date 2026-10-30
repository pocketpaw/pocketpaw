"""Service layer for the DeepWorkLog entity.

``record()`` is called from business-logic services (cycles, planner,
mission_control) after every deep work operation.
``list_events()`` and ``list_events_response()`` power the read path.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from beanie import PydanticObjectId

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.deep_work_log.dto import (
    DeepWorkLogOut,
    DeepWorkLogPageResponse,
    DeepWorkLogQuery,
)
from pocketpaw_ee.cloud.models.deep_work_log import DeepWorkLog as _DeepWorkLogDoc

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Domain helpers
# ---------------------------------------------------------------------------


def _doc_to_wire(doc: _DeepWorkLogDoc) -> DeepWorkLogOut:
    return DeepWorkLogOut(
        id=str(doc.id),
        workspaceId=doc.workspace,
        actorId=doc.actor_id,
        action=doc.action,
        targetType=doc.target_type,
        targetId=doc.target_id,
        metadata=doc.metadata,
        at=doc.at.isoformat(),
    )


def _encode_cursor(at: datetime, oid: PydanticObjectId) -> str:
    return f"{at.isoformat()}|{oid!s}"


def _decode_cursor(cursor: str) -> tuple[datetime, PydanticObjectId]:
    try:
        at_iso, oid_str = cursor.split("|", 1)
        return datetime.fromisoformat(at_iso), PydanticObjectId(oid_str)
    except (ValueError, TypeError) as exc:
        raise ValidationError("deep_work_log.bad_cursor", "Invalid pagination cursor") from exc


def _parse_iso(value: str, field_name: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(
            "deep_work_log.bad_timestamp",
            f"Invalid ISO-8601 in {field_name!r}",
        ) from exc


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


async def record(
    workspace_id: str,
    actor_id: str,
    action: str,
    target_type: str,
    target_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Persist one deep work log entry.

    Fire-and-forget: a write failure is logged and swallowed so a logging
    outage cannot block the operation.
    """
    try:
        doc = _DeepWorkLogDoc(
            workspace=workspace_id,
            actor_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            metadata=metadata or {},
        )
        await doc.insert()
    except Exception:
        logger.warning("deep_work_log.record failed for action %s", action, exc_info=True)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


async def list_events(
    workspace_id: str,
    query: DeepWorkLogQuery | dict,
) -> tuple[list[DeepWorkLogOut], str | None]:
    """Cursor-paginated deep work log list, newest first."""
    body = DeepWorkLogQuery.model_validate(query or {})

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

    mongo_filter: dict[str, Any] = (
        clauses[0] if len(clauses) == 1 else {"$and": clauses} if clauses else {}
    )

    docs = (
        await _DeepWorkLogDoc.find(mongo_filter)
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
    query: DeepWorkLogQuery | dict,
) -> DeepWorkLogPageResponse:
    """Service-level helper returning the wire shape."""
    items, next_cursor = await list_events(workspace_id, query)
    return DeepWorkLogPageResponse(items=items, nextCursor=next_cursor)


__all__ = [
    "list_events",
    "list_events_response",
    "record",
]
