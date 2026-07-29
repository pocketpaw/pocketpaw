# Senses — service layer for the browsable connector catalog.
# Created: 2026-07-16 (SR-2 catalog listing API) — orchestrates the read behind
#   ``GET /api/v1/cloud/senses/catalog``: resolve the pocket's reachable
#   connector set from the EE connectors service (the ONLY owner of the
#   tenant-filtered WorkspaceConnector read — OSS-EE boundary §2 + tenant rule
#   §7), hand that set to the pure ``catalog.list_catalog`` for the BOUND overlay,
#   then map the returned domain groups to the wire DTOs. The registry catalog
#   itself is global/static (no tenant data); only the ``bound`` flag is
#   per-tenant, and it comes exclusively from the tenant-scoped store read here.
#
# Cloud rules followed (per workspace CLAUDE.md):
# §5  Module-level async functions, not a class.
# §7  The only per-tenant read (bound state) filters by workspace_id via the
#     connectors service; the catalog metadata is intentionally global (browse).

from __future__ import annotations

from pocketpaw_ee.cloud.connectors import service as connectors_service
from pocketpaw_ee.cloud.senses import catalog
from pocketpaw_ee.cloud.senses.dto import (
    CatalogActionResponse,
    CatalogCategoryResponse,
    CatalogConnectorResponse,
)


async def list_catalog(
    workspace_id: str,
    *,
    pocket_id: str | None = None,
) -> list[CatalogCategoryResponse]:
    """The whole browsable connector catalog, grouped by category, with per-tenant
    bound state overlaid for ``workspace_id`` / ``pocket_id``.

    The bound set is read from the EE connectors service (tenant-filtered on
    ``workspace``); an absent ``pocket_id`` resolves to workspace-scope (only
    workspace-scoped rows count), matching every other pocket-reach read. The
    catalog metadata (connectors, actions, trust, availability) is global registry
    data — no cross-tenant leak is possible because the only tenant-derived field
    is ``bound``.
    """
    bound = await connectors_service.list_bound_connector_names(workspace_id, pocket_id or "")
    groups = await catalog.list_catalog(bound_connectors=bound)
    return [
        CatalogCategoryResponse(
            category=group.category,
            connectors=[
                CatalogConnectorResponse(
                    connector=conn.connector,
                    display_name=conn.display_name,
                    type=conn.category,
                    senses=list(conn.senses),
                    bound=conn.bound,
                    actions=[
                        CatalogActionResponse(
                            action=action.action,
                            description=action.description,
                            trust_level=action.trust_level,
                            execution_mode=action.execution_mode,
                            available=action.available,
                            unavailable_reason=action.unavailable_reason,
                            cost_estimate=action.cost_estimate,
                        )
                        for action in conn.actions
                    ],
                )
                for conn in group.connectors
            ],
        )
        for group in groups
    ]
