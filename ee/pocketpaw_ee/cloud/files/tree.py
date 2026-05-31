"""Parallel fan-out tree builder.

Queries every registered provider's list_mounts in parallel, applies the ABAC
ruleset at the mount level (mounts may be tagged via their provider but today
none are — untagged mounts always pass), then merges all resolved mounts into
a single FolderNode tree by splitting each path on '/'.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Literal, overload

from pocketpaw_ee.cloud.files.abac_config import AbacRuleSet
from pocketpaw_ee.cloud.files.dto import FolderNode, RequestContext, ResolvedMount
from pocketpaw_ee.cloud.files.registry import FolderProvider, ProviderRegistry


def _insert(root: FolderNode, mount: ResolvedMount) -> None:
    parts = [p for p in mount.path.split("/") if p]
    cursor = root
    accumulated = ""
    for i, part in enumerate(parts):
        accumulated += "/" + part
        child = next((c for c in cursor.children if c.name == part), None)
        is_leaf = i == len(parts) - 1
        if child is None:
            caps = ["read"]
            if is_leaf and mount.writable:
                caps.append("upload")
            child = FolderNode(
                path=accumulated,
                name=part,
                provider_id=mount.provider_id if is_leaf else "",
                children=[],
                capabilities=caps,
            )
            cursor.children.append(child)
        else:
            if is_leaf:
                child.provider_id = mount.provider_id
                if mount.writable and "upload" not in child.capabilities:
                    child.capabilities.append("upload")
        cursor = child


@overload
async def build_tree(
    *,
    ctx: RequestContext,
    registry: ProviderRegistry,
    rules: AbacRuleSet,
    collect_warnings: Literal[False] = False,
) -> FolderNode: ...
@overload
async def build_tree(
    *,
    ctx: RequestContext,
    registry: ProviderRegistry,
    rules: AbacRuleSet,
    collect_warnings: Literal[True],
) -> tuple[FolderNode, list[dict[str, str]]]: ...
async def build_tree(
    *,
    ctx: RequestContext,
    registry: ProviderRegistry,
    rules: AbacRuleSet,
    collect_warnings: bool = False,
):
    providers: list[FolderProvider] = registry.all()
    results = await asyncio.gather(*(p.list_mounts(ctx) for p in providers), return_exceptions=True)

    warnings: list[dict[str, str]] = []
    mounts: list[ResolvedMount] = []
    for provider, res in zip(providers, results, strict=True):
        if isinstance(res, BaseException):
            warnings.append({"provider_id": provider.provider_id, "code": "files.provider_error"})
            continue
        mounts.extend(res)

    mounts.sort(key=lambda m: m.order)
    root = FolderNode(path="/", name="", provider_id="", children=[], capabilities=["read"])
    for m in mounts:
        _insert(root, m)

    if collect_warnings:
        return root, warnings
    return root


# In-process TTL cache for /tree.
# Key is (user_id, workspace_id). Value is (expires_at, tree, warnings).
_TREE_CACHE: dict[tuple[str, str | None], tuple[float, FolderNode, list[dict[str, str]]]] = {}
_TREE_TTL_SECONDS = 30.0


def invalidate_tree_cache(*, user_id: str | None = None, workspace_id: str | None = None) -> None:
    """Evict cached tree entries.

    If both user_id and workspace_id are None, clears the entire cache.
    Otherwise evicts any key that matches the supplied fields (None acts as wildcard).
    """
    if user_id is None and workspace_id is None:
        _TREE_CACHE.clear()
        return
    to_delete = [
        key
        for key in _TREE_CACHE
        if (user_id is None or key[0] == user_id)
        and (workspace_id is None or key[1] == workspace_id)
    ]
    for key in to_delete:
        _TREE_CACHE.pop(key, None)


class CachedTreeBuilder:
    """Wraps build_tree with a per-(user, workspace) TTL cache.

    The clock source is injectable for tests; defaults to time.monotonic.
    """

    def __init__(
        self,
        *,
        registry: ProviderRegistry,
        rules: AbacRuleSet,
        ttl_seconds: float = _TREE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._registry = registry
        self._rules = rules
        self._ttl = ttl_seconds
        self._clock = clock

    async def build(
        self, *, ctx: RequestContext, collect_warnings: bool = True
    ) -> tuple[FolderNode, list[dict[str, str]]]:
        key = (ctx.user_id, ctx.workspace_id)
        now = self._clock()
        cached = _TREE_CACHE.get(key)
        if cached is not None and cached[0] > now:
            _, tree, warnings = cached
            return tree, list(warnings)
        tree, warnings = await build_tree(
            ctx=ctx,
            registry=self._registry,
            rules=self._rules,
            collect_warnings=True,
        )
        _TREE_CACHE[key] = (now + self._ttl, tree, warnings)
        return tree, list(warnings)
