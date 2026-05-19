# service.py — Audit entity business logic.
# Created: 2026-05-17 — Read-only wrapper over pocketpaw.audit.store
#   AuditStore.search_entries with mandatory workspace_id tenancy filter
#   sourced from ctx.workspace_id. # no-event: read-only entity per Rule 9.
from __future__ import annotations

import logging

from ee.cloud._core.context import RequestContext
from ee.cloud._core.errors import CloudError, Internal
from ee.cloud.audit.dto import AuditEntryDTO, AuditListResponse, ListAuditRequest
from pocketpaw.audit.store import AuditStore, get_audit_store  # type: ignore[import-untyped]

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


__all__ = ["agent_list_audit"]
