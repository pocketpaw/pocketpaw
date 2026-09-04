"""EE /files router — unified workspace files listing + v2 tree/browse.

2026-09-05 (files vault, feat/files-links): two reads guarded by ``kb.read``
(the same MEMBER action the knowledge search uses). ``GET /files/{id}/links``
returns a file's outgoing wikilink targets and backlinks;
``GET /files/graph?pocket_id=`` returns the library as nodes + edges with the
same pocket-membership rule as ``GET /files``. The links route takes
``{file_id:path}`` because editor notes store ``ws:path`` ids that can carry
slashes. Errors are ``CloudError``
(``file.not_found``, ``files.pocket_forbidden``), never ``HTTPException``.

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

2026-08-29 (T3 "Files content search"): added ``POST /files/search`` — the
same listing rows, selected by what is INSIDE them rather than by filename.
It lives here, on the surface that already speaks file rows, rather than as a
flag on ``POST /kb/search`` which answers with kb articles; the full argument
is in ``files/content_search.py``'s header. The pocket-read gate that
``list_files`` had inline is now ``_pocket_readable``, shared by both
endpoints so the two can't drift into enforcing different rules.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from pocketpaw_ee.cloud._core.errors import Forbidden
from pocketpaw_ee.cloud.files.abac_config import AbacRuleSet
from pocketpaw_ee.cloud.files.browse import browse_mount
from pocketpaw_ee.cloud.files.content_search import CONTENT_SEARCH_ROUTE, search_file_contents
from pocketpaw_ee.cloud.files.dto import (
    ContentSearchRequest,
    FileLinksResponse,
    FilesGraphResponse,
    RequestContext,
)
from pocketpaw_ee.cloud.files.errors import FilesError, MountNotFound
from pocketpaw_ee.cloud.files.registry import ProviderRegistry
from pocketpaw_ee.cloud.files.service import UnifiedFilesService
from pocketpaw_ee.cloud.files.tree import CachedTreeBuilder
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.shared.deps import (
    current_user_id,
    current_workspace_id,
    require_action_any_workspace,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/files",
    tags=["Files"],
    dependencies=[Depends(require_license)],
)

_SVC = UnifiedFilesService()


async def _pocket_readable(pocket_id: str, user_id: str) -> bool:
    """The pocket-read gate for this surface (Stage 3.E).

    Extracted so the listing and content search cannot drift apart — one
    endpoint enforcing a slightly different membership rule than the other is
    how a "private pocket" stops being private. A failing membership lookup
    denies: an ACL that can't be evaluated is not an ACL that passed.
    """
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    try:
        return await pockets_service.is_member(pocket_id=pocket_id, user_id=user_id)
    except Exception:
        return False


@router.get("")
async def list_files(
    workspace_id: str | None = Query(None),
    source: Literal["chat", "local", "drive"] | None = Query(None),
    pocket_id: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user_id: str = Depends(current_user_id),
    current_workspace: str = Depends(current_workspace_id),
) -> JSONResponse:
    """List files in the caller's current workspace.

    ``workspace_id`` is accepted for explicitness but must match the
    caller's current workspace — cross-workspace listing is rejected.

    Pagination is offset-based: ``limit`` bounds each page (default 100,
    max 500) and ``offset`` skips that many rows into the sorted, deduped
    listing. The response carries ``total`` (rows matching the query,
    independent of the page) and ``has_more`` so clients can render a
    "load more" affordance. Offset pages are sliced after the merge +
    dedupe + newest-first sort, so pages are stable while the set is
    unchanged. Fetching is capped at 500 rows per source, so ``offset``
    beyond ~500 cannot page further (the FE stops offering "load more"
    when a page comes back empty).

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

    if pocket_id and not await _pocket_readable(pocket_id, user_id):
        return JSONResponse(
            status_code=403,
            content={"detail": "files.pocket_forbidden"},
        )

    page = await _SVC.list_unified(
        current_workspace, source=source, limit=limit, offset=offset, pocket_id=pocket_id
    )

    return JSONResponse(
        content={
            "workspace_id": current_workspace,
            "pocket_id": pocket_id,
            "source": source or "all",
            # One serializer, on the dataclass. The inline dict this replaces
            # silently re-dropped summary/collections/tags/agent_id after the
            # service was fixed to carry them — same bug, one hop later.
            "files": [f.to_json() for f in page.files],
            "warnings": page.warnings,
            "total": page.total,
            "has_more": page.has_more,
            "offset": offset,
            "limit": limit,
        }
    )


@router.post(
    CONTENT_SEARCH_ROUTE,
    dependencies=[Depends(require_action_any_workspace("kb.read"))],
)
async def search_files_by_content(
    body: ContentSearchRequest,
    user_id: str = Depends(current_user_id),
    current_workspace: str = Depends(current_workspace_id),
) -> JSONResponse:
    """Search INSIDE the caller's files; return listing rows, not kb articles.

    Rows carry the same shape ``GET /files`` returns plus a ``match`` block
    (article id, scope, kb title, snippet, whether that article is verbatim),
    so the panel can render a hit with the components it already has.

    Guarded by ``kb.read``: the ``match`` block carries kb titles and
    summaries, which is kb content — without the guard this would be a second,
    ungated door to what ``POST /kb/search`` protects. ``pocket_id`` goes
    through the same membership gate as the listing.

    ``degraded`` is the honesty field. ``"kb_unavailable"`` means the search
    could not run (the UI must say so — an empty list would read as "nothing
    matched"); ``"verbatim"`` means at least one hit is an uncompiled article,
    so matching against it was literal. ``null`` means a normal search.
    """
    if body.pocket_id and not await _pocket_readable(body.pocket_id, user_id):
        return JSONResponse(
            status_code=403,
            content={"detail": "files.pocket_forbidden"},
        )

    result = await search_file_contents(
        workspace_id=current_workspace,
        user_id=user_id,
        query=body.query,
        limit=body.limit,
        pocket_id=body.pocket_id,
    )

    return JSONResponse(
        content={
            "workspace_id": current_workspace,
            "pocket_id": body.pocket_id,
            "query": body.query,
            "files": [m.to_json() for m in result.matches],
            "scopes": result.scopes,
            "degraded": result.degraded,
            "limit": body.limit,
        }
    )


@router.get(
    "/graph",
    response_model=FilesGraphResponse,
    dependencies=[Depends(require_action_any_workspace("kb.read"))],
)
async def files_graph(
    pocket_id: str | None = Query(None),
    user_id: str = Depends(current_user_id),
    current_workspace: str = Depends(current_workspace_id),
) -> FilesGraphResponse:
    """The library as a link graph. Same pocket rule as ``GET /files``: with
    ``pocket_id`` the caller must be a member; without it, workspace-only rows."""
    if pocket_id:
        from pocketpaw_ee.cloud.pockets import service as pockets_service

        try:
            allowed = await pockets_service.is_member(pocket_id=pocket_id, user_id=user_id)
        except Exception:
            allowed = False
        if not allowed:
            raise Forbidden("files.pocket_forbidden")
    return await _SVC.files_graph(current_workspace, pocket_id=pocket_id)


@router.get(
    # ``:path`` so an editor-written id (``ws:Daily/2026-09-05.md``, slash
    # included) reaches the handler; Starlette backtracks past ``/links``.
    "/{file_id:path}/links",
    response_model=FileLinksResponse,
    dependencies=[Depends(require_action_any_workspace("kb.read"))],
)
async def file_links(
    file_id: str,
    current_workspace: str = Depends(current_workspace_id),
) -> FileLinksResponse:
    """What this file links to, and what links to it. 404 ``file.not_found``
    for a missing or cross-workspace id."""
    return await _SVC.file_links(current_workspace, file_id)


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
