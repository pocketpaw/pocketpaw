"""EE /files router — unified workspace files listing + v2 tree/browse.

The module-level ``router`` keeps the Cluster E sub-PR 4 contract
intact: ``GET /files`` returns a single list the paw-enterprise
FilesPanel renders without caring which origin each row came from.

Files Tab v2 (this PR) layers tree/browse endpoints on top via
``build_router`` — a factory that composes a ProviderRegistry + ABAC
rule set + request-context factory. ``build_files_router`` in
``bootstrap.py`` wires the concrete providers.

2026-05-03 (Stage 3.E "Files as Knowledge"): ``GET /files`` accepts
``pocket_id``. Members see the pocket's files; non-members get a 403.
Without ``pocket_id`` the listing returns workspace-scoped rows only —
pocket files don't bleed into the workspace Files panel.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from ee.cloud.files.abac_config import AbacRuleSet
from ee.cloud.files.browse import browse_mount
from ee.cloud.files.dto import RequestContext
from ee.cloud.files.errors import FilesError, MountNotFound
from ee.cloud.files.registry import ProviderRegistry
from ee.cloud.files.service import UnifiedFilesService
from ee.cloud.files.tree import CachedTreeBuilder
from ee.cloud.license import require_license
from ee.cloud.shared.deps import current_user_id, current_workspace_id

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/files",
    tags=["Files"],
    dependencies=[Depends(require_license)],
)

_SVC = UnifiedFilesService()


@router.get("")
async def list_files(
    workspace_id: str | None = Query(None),
    source: Literal["chat", "local", "drive"] | None = Query(None),
    pocket_id: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    user_id: str = Depends(current_user_id),
    current_workspace: str = Depends(current_workspace_id),
) -> JSONResponse:
    """List files in the caller's current workspace.

    ``workspace_id`` is accepted for explicitness but must match the
    caller's current workspace — cross-workspace listing is rejected.

    Stage 3.E: when ``pocket_id`` is set, the caller must be a pocket
    member (owner / team / shared / workspace-visible). Non-members get
    a 403 ``files.pocket_forbidden``. Without ``pocket_id`` the listing
    is workspace-scoped — pocket files never bleed into the workspace
    Files panel.
    """
    if workspace_id and workspace_id != current_workspace:
        return JSONResponse(
            status_code=403,
            content={
                "detail": "workspace.mismatch",
                "message": "Cannot list files outside your current workspace.",
            },
        )

    if pocket_id:
        from ee.cloud.pockets import service as pockets_service

        try:
            allowed = await pockets_service.is_member(
                pocket_id=pocket_id, user_id=user_id
            )
        except Exception:
            allowed = False
        if not allowed:
            return JSONResponse(
                status_code=403,
                content={"detail": "files.pocket_forbidden"},
            )

    files, warnings = await _SVC.list_unified(
        current_workspace, source=source, limit=limit, pocket_id=pocket_id
    )

    return JSONResponse(
        content={
            "workspace_id": current_workspace,
            "pocket_id": pocket_id,
            "source": source or "all",
            "files": [
                {
                    "id": f.id,
                    "source": f.source,
                    "filename": f.filename,
                    "mime": f.mime,
                    "size": f.size,
                    "url": f.url,
                    "created": f.created.isoformat() if f.created else None,
                    "chat_id": f.chat_id,
                }
                for f in files
            ],
            "warnings": warnings,
        }
    )


def build_router(
    *,
    registry: ProviderRegistry,
    rules: AbacRuleSet,
    ctx_factory: Callable[[Request], RequestContext | Any],
    tree_builder: CachedTreeBuilder | None = None,
) -> APIRouter:
    """Files Tab v2 tree/browse endpoints.

    Separate from the module-level ``router`` because tree/browse need a
    composed provider registry and ABAC rule set — see
    ``bootstrap.build_files_router`` for the concrete wiring.

    ``ctx_factory`` may be sync OR async — if it returns an awaitable,
    the handler will await it. This lets real wiring resolve the
    authenticated user from the request session without forcing every
    test harness to declare an async lambda.
    """
    import inspect

    v2 = APIRouter(prefix="/files", tags=["Files"])
    cached = tree_builder or CachedTreeBuilder(registry=registry, rules=rules)

    async def _resolve_ctx(request: Request) -> RequestContext:
        result = ctx_factory(request)
        if inspect.isawaitable(result):
            return await result
        return result

    @v2.get("/tree")
    async def get_tree(
        request: Request,
        workspace_id: str | None = Query(None),
    ) -> dict[str, Any]:
        ctx = await _resolve_ctx(request)
        if workspace_id is not None and workspace_id != ctx.workspace_id:
            raise HTTPException(status_code=403, detail="files.workspace_mismatch")
        tree, warnings = await cached.build(ctx=ctx, collect_warnings=True)
        return {**tree.model_dump(), "warnings": warnings}

    @v2.get("/browse")
    async def get_browse(
        request: Request,
        mount: str = Query(...),
        cursor: str | None = Query(None),
        limit: int = Query(50, ge=1, le=500),
        workspace_id: str | None = Query(None),
    ) -> dict[str, Any]:
        ctx = await _resolve_ctx(request)
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

    return v2
