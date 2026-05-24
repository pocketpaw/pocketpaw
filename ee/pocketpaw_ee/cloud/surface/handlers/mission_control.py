# mission_control.py — /mission-control surface preamble.
#
# Created: 2026-05-24 — Reads the unified work-items feed
# (``mission_control.service.agent_list_work_items``) and reports
# section counts so the agent knows what's pending vs done without
# probing again. Tenancy carried via the ``RequestContext`` per
# canonical pattern.

from __future__ import annotations

import logging
from collections import Counter

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta
from pocketpaw_ee.cloud.surface.handlers._helpers import truncate_preamble

logger = logging.getLogger(__name__)


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> str:
    """Render the mission control surface preamble."""
    try:
        from datetime import UTC, datetime

        from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
        from pocketpaw_ee.cloud.mission_control import service as mc_service

        ctx = RequestContext(
            user_id=user_id,
            workspace_id=workspace_id,
            request_id="surface-mc",
            scope=ScopeKind.WORKSPACE,
            started_at=datetime.now(UTC),
        )
        items = await mc_service.agent_list_work_items(ctx, {"limit": 200})
    except Exception:
        logger.debug("mission_control_handler: work_items failed", exc_info=True)
        return (
            '<surface kind="mission_control" route="/mission-control" />'
            "<mc-snapshot>(unavailable)</mc-snapshot>"
        )

    sections: Counter = Counter()
    for it in items or []:
        section = getattr(it, "section", None) or "unknown"
        sections[str(section)] += 1
    summary = " · ".join(f"{name}={count}" for name, count in sorted(sections.items())) or "empty"

    parts = [
        '<surface kind="mission_control" route="/mission-control" />',
        f"<mc-snapshot>total={len(items or [])} · {summary}</mc-snapshot>",
    ]
    return truncate_preamble("\n".join(parts))


__all__ = ["build_preamble"]
