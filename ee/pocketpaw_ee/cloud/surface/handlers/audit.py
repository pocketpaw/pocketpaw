# audit.py — /audit surface preamble.
#
# Created: 2026-05-24 — Renders the last 10 audit entries (action,
# target, actor, timestamp) so the agent can quote them when the user
# asks "what happened today?". Tenancy enforced by
# ``audit_service.agent_list_audit``.
#
# Changes: 2026-08-02 (PA-2, feat/prompt-assembler-seam) — returns a
# ``SurfacePreamble`` keyed on a digest of what was rendered. An append-only log
# read as a LIST: no revision to point at, and the rendered rows already carry
# each entry's timestamp, so the digest moves the moment a new entry lands and
# holds still while nothing happens. The unavailable branch reads nothing and
# gets its own exact key.

from __future__ import annotations

import logging
from datetime import UTC, datetime

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta, SurfacePreamble
from pocketpaw_ee.cloud.surface.handlers._helpers import (
    content_key,
    meta_key,
    truncate_preamble,
)

logger = logging.getLogger(__name__)

LIST_LIMIT = 10


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> SurfacePreamble:
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
        return SurfacePreamble(
            text=(
                '<surface kind="audit" route="/audit" />'
                "<audit-snapshot>(unavailable)</audit-snapshot>"
            ),
            cache_key=meta_key("audit", "unavailable"),
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
    text = truncate_preamble("\n".join(parts))
    return SurfacePreamble(text=text, cache_key=content_key("audit", text))


__all__ = ["build_preamble"]
