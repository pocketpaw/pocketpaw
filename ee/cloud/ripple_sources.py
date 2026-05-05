"""Concrete sources for the ripple $source resolver.

Importing this module registers every source via @register decorators.
``ripple_resolver`` itself stays free of cloud-domain imports — sources
live here, next to the entities they read.

Tenancy rule (from CLAUDE.md ee/cloud rule 7): every Mongo read MUST
scope by ctx.workspace_id.
"""

from __future__ import annotations

import logging
from typing import Any

from ee.cloud.models.pocket import Pocket as _PocketDoc
from ee.cloud.ripple_resolver import ResolveCtx, register

logger = logging.getLogger(__name__)


@register("workspace.pockets")
async def _workspace_pockets(ctx: ResolveCtx, args: dict[str, Any]) -> list[dict[str, Any]]:
    """Return id+metadata for every pocket in the workspace.
    Visibility filter mirrors pockets.service.list_pockets — owner,
    shared_with, or workspace-visible. The full rippleSpec is excluded
    (would be wasteful and recursive)."""
    docs = await _PocketDoc.find(
        {
            "workspace": ctx.workspace_id,
            "$or": [
                {"owner": ctx.user_id},
                {"shared_with": ctx.user_id},
                {"visibility": "workspace"},
            ],
        }
    ).to_list()
    return [
        {
            "id": str(d.id),
            "name": d.name,
            "type": d.type,
            "icon": d.icon,
            "color": d.color,
        }
        for d in docs
    ]


__all__ = ["_workspace_pockets"]
