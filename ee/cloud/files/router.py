"""Files API routes. Legacy /api/v1/files kept intact; /tree + /browse added."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ee.cloud.files.abac_config import AbacRuleSet
from ee.cloud.files.browse import browse_mount
from ee.cloud.files.errors import FilesError, MountNotFound
from ee.cloud.files.mongo_store import MongoFileStore
from ee.cloud.files.registry import ProviderRegistry
from ee.cloud.files.schemas import RequestContext
from ee.cloud.files.service import UnifiedFilesService
from ee.cloud.files.tree import CachedTreeBuilder
from ee.cloud.shared.deps import current_workspace_id
from ee.cloud.uploads.mongo_store import MongoFileStore as UploadsStore

router = APIRouter(prefix="/api/v1/files", tags=["files"])

# Module-level singleton — mirrors ee/cloud/uploads/router.py pattern.
_UPLOADS_STORE = UploadsStore()


def _service() -> UnifiedFilesService:
    return UnifiedFilesService(MongoFileStore(_UPLOADS_STORE))


@router.get("")
async def list_files(
    workspace_id: str = Query(...),
    source: str = Query("all"),
    current_ws: str = Depends(current_workspace_id),
    svc: UnifiedFilesService = Depends(_service),
) -> dict:
    if workspace_id != current_ws:
        raise HTTPException(status_code=403, detail="files.workspace_mismatch")
    return await svc.list(workspace_id, source=source)


def build_router(
    *,
    registry: ProviderRegistry,
    rules: AbacRuleSet,
    ctx_factory: Callable[[Request], RequestContext],
    tree_builder: CachedTreeBuilder | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/files", tags=["files"])
    cached = tree_builder or CachedTreeBuilder(registry=registry, rules=rules)

    @router.get("/tree")
    async def get_tree(
        request: Request,
        workspace_id: str | None = Query(None),
    ) -> dict[str, Any]:
        ctx = ctx_factory(request)
        if workspace_id is not None and workspace_id != ctx.workspace_id:
            raise HTTPException(status_code=403, detail="files.workspace_mismatch")
        tree, warnings = await cached.build(ctx=ctx, collect_warnings=True)
        return {**tree.model_dump(), "warnings": warnings}

    @router.get("/browse")
    async def get_browse(
        request: Request,
        mount: str = Query(...),
        cursor: str | None = Query(None),
        limit: int = Query(50, ge=1, le=500),
        workspace_id: str | None = Query(None),
    ) -> dict[str, Any]:
        ctx = ctx_factory(request)
        if workspace_id is not None and workspace_id != ctx.workspace_id:
            raise HTTPException(status_code=403, detail="files.workspace_mismatch")
        variables = {"workspace_id": ctx.workspace_id or ""}
        try:
            page = await browse_mount(
                ctx=ctx,
                registry=registry,
                rules=rules,
                mount_path=mount,
                variables=variables,
                cursor=cursor,
                limit=limit,
                filters={},
            )
        except MountNotFound:
            raise HTTPException(status_code=404, detail="files.mount_not_found") from None
        except FilesError as e:
            raise HTTPException(status_code=e.http_status, detail=e.code) from e
        return page.model_dump()

    return router
