"""Ripple $source resolver — replaces {"$source": "<name>", ...args} markers
in pocket rippleSpecs with live workspace data on read.

Reads only. Persistence stores markers verbatim; resolution happens in
pockets.service.get before wire-dict conversion. Unknown sources log
and return None — they MUST NOT raise, so a stale spec can't brick the
canvas.

Sources are registered via @register("name"). Each source is an async
function (ResolveCtx, args) -> Any. Tenancy is the source's
responsibility — every Mongo read MUST scope by ctx.workspace_id.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

SOURCE_KEY = "$source"


@dataclass(frozen=True)
class ResolveCtx:
    workspace_id: str
    user_id: str
    pocket_id: str


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
    return await _walk(spec, ctx)


async def _walk(node: Any, ctx: ResolveCtx) -> Any:
    if isinstance(node, dict):
        if SOURCE_KEY in node:
            return await _resolve_marker(node, ctx)
        return {k: await _walk(v, ctx) for k, v in node.items()}
    if isinstance(node, list):
        return [await _walk(item, ctx) for item in node]
    return node


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
