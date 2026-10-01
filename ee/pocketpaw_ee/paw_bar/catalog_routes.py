# ee/pocketpaw_ee/paw_bar/catalog_routes.py — owner route for importing a
# concierge's product catalog from the store's own website.
#
#   POST /paw-bar/admin/site/{site_id}/catalog/import/preview   → CatalogImportPreview
#
# Preview only: it reads the site (``catalog_import.preview_catalog_import``) and
# writes nothing. The owner reviews the products in the dashboard and saves the
# merged catalog through PATCH /paw-bar/admin/site/{site_id}/widget/spec, the one
# write path into the spec. Gated like the other concierge admin mutations:
# ``paw_bar.manage`` (ADMIN), the session's active workspace, and the site loaded
# workspace-scoped, so a foreign or malformed id is a 404 before any fetch. A
# failed import is a 200 with ``status: "failed"`` and a ``reason``, never a 5xx;
# a hosted (non-connected) site is ``not_connected_site`` with nothing fetched.
#
# A separate module so router.py does not grow. Mounted beside ``paw_bar.router``
# in ``pocketpaw_ee.cloud``.

from __future__ import annotations

from fastapi import APIRouter, Depends

from pocketpaw_ee.cloud._core.deps import current_workspace_id
from pocketpaw_ee.paw_bar import catalog_import
from pocketpaw_ee.paw_bar.catalog_import import CatalogImportPreview
from pocketpaw_ee.paw_bar.router import _load_site_scoped, _require_paw_bar_manage

router = APIRouter(tags=["PawBar"])


@router.post(
    "/paw-bar/admin/site/{site_id}/catalog/import/preview",
    response_model=CatalogImportPreview,
    dependencies=[Depends(_require_paw_bar_manage)],
)
async def preview_site_catalog_import(
    site_id: str,
    workspace_id: str = Depends(current_workspace_id),
) -> CatalogImportPreview:
    """The products the site publishes, for the owner to pick from. Writes nothing.

    Awaited inline (bounded by ``catalog_import.IMPORT_WALL_CLOCK_SEC``), like the
    knowledge re-sync: the owner pressed a button and is waiting for the list.
    """
    site = await _load_site_scoped(site_id, workspace_id)
    return await catalog_import.preview_catalog_import(site)
