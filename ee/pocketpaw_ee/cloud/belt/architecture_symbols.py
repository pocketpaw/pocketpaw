# ee/pocketpaw_ee/cloud/belt/architecture_symbols.py — a repo's symbols per file
# from its loom world-model, for the Code tab's file view
# (``architecture_code.read_file``).
#
# The model is ``worldmodel-<C4 scope>.json`` (the repo's own ``docs/c4`` scope,
# a plain slug only), else ``orient.world_model_for(repo)``'s
# ``worldmodel-<repo dir name>.json``, both in ``POCKETPAW_FACTORY_LOOM_DIR`` or
# the nearest ``.loom/`` above the repo.
# Only ``symbol`` entities are read, grouped by their own ``path`` (so the
# direction of ``composes`` never matters); ``callers`` is the count of distinct
# symbols with a ``calls`` edge to it (component-level ``calls`` never count). A
# model over ``MAX_WORLD_MODEL_BYTES`` is never parsed (checked by size before the
# load). The index is built off the event loop and cached per (file, mtime,
# size); only the compact index is kept.
# Loom reads Python and Go only, so a Swift repo's model has no symbols.

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from .orient import _loom_dir, world_model_for

logger = logging.getLogger(__name__)
MAX_WORLD_MODEL_BYTES = 25_000_000
_CACHE_MAX = 8
_SCOPE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
# ponytail: drop-all when full, like architecture_code's cache
_CACHE: dict[tuple[str, int, int], dict[str, list[dict[str, Any]]]] = {}


def _index(data: Any) -> dict[str, list[dict[str, Any]]]:
    """``{path: [{name, kind, line, callers}]}``, callers desc then line asc."""
    data = data if isinstance(data, dict) else {}
    ents = data.get("entities") if isinstance(data.get("entities"), list) else []
    syms = {
        e.get("id"): e
        for e in ents
        if isinstance(e, dict)
        and e.get("kind") == "symbol"
        and isinstance(e.get("path"), str)
        and isinstance(e.get("id"), str)
    }
    callers: dict[str, set] = defaultdict(set)
    for edge in data.get("edges") if isinstance(data.get("edges"), list) else []:
        if not isinstance(edge, dict) or edge.get("type") != "calls":
            continue
        if edge.get("to") in syms and edge.get("from") in syms:
            callers[edge["to"]].add(edge.get("from"))
    by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sid, e in syms.items():
        attrs = e.get("attrs") if isinstance(e.get("attrs"), dict) else {}
        line = str(attrs.get("line", ""))
        by_path[e["path"]].append({
            "name": str(e.get("symbol") or e.get("name") or sid),
            "kind": attrs.get("kind"),
            "line": int(line) if line.isdigit() else None,
            "callers": len(callers.get(sid, ())),
        })  # fmt: skip
    for rows in by_path.values():
        rows.sort(key=lambda s: (-s["callers"], s["line"] or 0, s["name"]))
    return dict(by_path)


def _load(path: Path) -> dict[str, list[dict[str, Any]]]:
    with path.open() as fh:
        return _index(json.load(fh))


def _model_path(repo: Path, scope: Any) -> Path | None:
    loom = _loom_dir(repo)
    if loom and isinstance(scope, str) and _SCOPE.fullmatch(scope):
        if (by_scope := loom / f"worldmodel-{scope}.json").is_file():
            return by_scope
    return world_model_for(repo)


async def symbols_for(
    repo: Path, file: str, scope: Any = None
) -> tuple[list[dict[str, Any]], str | None]:
    """``(symbols, skipped)`` for one repo file; ``skipped`` is ``"no model"``,
    ``"too large"`` or ``"unreadable"`` when no symbols could be read."""
    model = None
    try:
        if (model := _model_path(repo, scope)) is None:
            return [], "no model"
        st = model.stat()
        if st.st_size > MAX_WORLD_MODEL_BYTES:
            return [], "too large"
        key = (str(model), st.st_mtime_ns, st.st_size)
        if (index := _CACHE.get(key)) is None:
            index = await asyncio.to_thread(_load, model)
            if len(_CACHE) >= _CACHE_MAX:
                _CACHE.clear()
            _CACHE[key] = index
    except (OSError, ValueError):
        logger.warning("World-model read of %s failed", model, exc_info=True)
        return [], "unreadable"
    return [dict(s) for s in index.get(file, [])], None
