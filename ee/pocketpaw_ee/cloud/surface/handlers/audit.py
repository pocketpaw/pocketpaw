# audit.py — /audit surface preamble.
#
# Created: 2026-05-24 — Renders the last 10 audit entries (action,
# target, actor, timestamp) so the agent can quote them when the user
# asks "what happened today?". Tenancy enforced by
# ``audit_service.agent_list_audit``.

from __future__ import annotations

import logging
from datetime import UTC, datetime

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta
from pocketpaw_ee.cloud.surface.handlers._helpers import truncate_preamble

logger = logging.getLogger(__name__)

LIST_LIMIT = 10


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> str:
    """Render the audit surface preamble."""
    try:
        from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
        from pocketpaw_ee.cloud.audit import service as audit_service

        ctx = RequestContext(
            user_id=user_id,
            workspace_id=workspace_id,
            request_id="surface-audit",
            scope=ScopeKind.WORKSPACE,
            started_at=datetime.now(UTC),
        )
        resp = await audit_service.agent_list_audit(ctx, {"limit": LIST_LIMIT})
    except Exception:
        logger.debug("audit_handler: list failed", exc_info=True)
        return (
            '<surface kind="audit" route="/audit" /><audit-snapshot>(unavailable)</audit-snapshot>'
        )

    entries = list(getattr(resp, "entries", []) or [])
    parts = [
        '<surface kind="audit" route="/audit" />',
        f'<audit-snapshot count="{len(entries)}" />',
    ]
    if not entries:
        parts.append("<audit-list>(no entries)</audit-list>")
    else:
        rows = [
            f"- {e.timestamp}: {e.actor} {e.action} -> {e.description[:60]}"
            for e in entries[:LIST_LIMIT]
        ]
        parts.append("<audit-list>\n" + "\n".join(rows) + "\n</audit-list>")
    return truncate_preamble("\n".join(parts))


__all__ = ["build_preamble"]
