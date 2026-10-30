"""Ripple $source resolver — replaces {"$source": "<name>", ...args} markers
in pocket rippleSpecs with live workspace data on read.

Reads only. Persistence stores markers verbatim; resolution happens in
pockets.service.get before wire-dict conversion. Unknown sources log
and return None — they MUST NOT raise, so a stale spec can't brick the
canvas.

Sources are registered via @register("name"). Each source is an async
function (ResolveCtx, args) -> Any. Tenancy is the source's
responsibility — every Mongo read MUST scope by ctx.workspace_id.

Resolution is two-pass: collect every marker, resolve the DISTINCT ones
concurrently, then rebuild the tree. ``ResolveCtx.memo`` dedupes identical
markers (same workspace, viewer, source name and args). A caller serializing
many pockets for one viewer (``pockets.service.list_pockets``) shares one memo
across the whole page, so a ``workspace.pockets`` marker repeated in N specs
costs one read instead of N. That sharing assumes a source's result does not
depend on ``ctx.pocket_id`` (true today: it only appears in log lines). A new
source that reads it must not be memoized across pockets. The memo lives for
one request; never hold one at module level.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

SOURCE_KEY = "$source"


@dataclass(frozen=True)
class ResolveCtx:
    workspace_id: str
    user_id: str
    pocket_id: str
    # Per-request memo: marker key -> the task resolving it. Tasks, not results,
    # so two pockets resolving the same marker concurrently share one read.
    memo: dict[str, asyncio.Future[Any]] | None = field(
        default=None, compare=False, hash=False, repr=False
    )


SourceFn = Callable[[ResolveCtx, dict[str, Any]], Awaitable[Any]]
_REGISTRY: dict[str, SourceFn] = {}


def register(name: str) -> Callable[[SourceFn], SourceFn]:
    def deco(fn: SourceFn) -> SourceFn:
        _REGISTRY[name] = fn
        return fn

    return deco


async def resolve_ripple_spec(spec: dict[str, Any], ctx: ResolveCtx) -> dict[str, Any]:
    """Walk spec, replace {"$source": ...} dicts with resolved values.
    Returns a new structure; input is not mutated."""
    if ctx.memo is None:
        ctx = dataclasses.replace(ctx, memo={})
    markers: list[dict[str, Any]] = []
    _collect(spec, markers)
    results = await asyncio.gather(*(_memoized(m, ctx) for m in markers))
    # Deep-copied per marker: a memoized result is shared across pockets, and
    # each resolved spec must own its data.
    resolved = {id(m): copy.deepcopy(r) for m, r in zip(markers, results, strict=True)}
    return _rebuild(spec, resolved)


def _collect(node: Any, out: list[dict[str, Any]]) -> None:
    if isinstance(node, dict):
        if SOURCE_KEY in node:
            out.append(node)
            return
        for v in node.values():
            _collect(v, out)
    elif isinstance(node, list):
        for item in node:
            _collect(item, out)


def _rebuild(node: Any, resolved: dict[int, Any]) -> Any:
    if isinstance(node, dict):
        if SOURCE_KEY in node:
            return resolved[id(node)]
        return {k: _rebuild(v, resolved) for k, v in node.items()}
    if isinstance(node, list):
        return [_rebuild(item, resolved) for item in node]
    return node


def _memo_key(marker: dict[str, Any], ctx: ResolveCtx) -> str | None:
    try:
        return json.dumps(
            [ctx.workspace_id, ctx.user_id, marker], sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError):
        return None  # not JSON-shaped; resolve it uncached


def _memoized(marker: dict[str, Any], ctx: ResolveCtx) -> Awaitable[Any]:
    key = _memo_key(marker, ctx)
    if key is None or ctx.memo is None:
        return _resolve_marker(marker, ctx)
    task = ctx.memo.get(key)
    if task is None:
        task = asyncio.ensure_future(_resolve_marker(marker, ctx))
        ctx.memo[key] = task
    return task


async def _resolve_marker(marker: dict[str, Any], ctx: ResolveCtx) -> Any:
    name = marker.get(SOURCE_KEY)
    if not isinstance(name, str):
        logger.warning(
            "ripple_resolver: $source value is not a string: %r (workspace=%s pocket=%s)",
            name,
            ctx.workspace_id,
            ctx.pocket_id,
        )
        return None
    fn = _REGISTRY.get(name)
    if fn is None:
        logger.warning(
            "ripple_resolver: unknown $source %r (workspace=%s pocket=%s)",
            name,
            ctx.workspace_id,
            ctx.pocket_id,
        )
        return None
    args = {k: v for k, v in marker.items() if k != SOURCE_KEY}
    try:
        return await fn(ctx, args)
    except Exception:
        logger.exception(
            "ripple_resolver: source %r failed (workspace=%s pocket=%s)",
            name,
            ctx.workspace_id,
            ctx.pocket_id,
        )
        return None


__all__ = ["ResolveCtx", "SOURCE_KEY", "register", "resolve_ripple_spec"]
