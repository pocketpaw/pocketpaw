# fx.py — in-process MCP server serving the paw-fx effects registry (drop-in
# visual effects: backgrounds, particles, 3d-hero, scroll, text, cursor,
# transition) to the claude_agent_sdk cloud chat backend.
#
# Created: 2026-09-06 (feat/fx-mcp-server, FX-2). Clones the icons.py shape:
# a single ``create_sdk_mcp_server`` with an SDK import-guard, ``SERVER_NAME`` /
# ``*_TOOL_ID`` allowlist constants, and the ``_error_response`` /
# ``_success_response`` helpers. PURE READ over a local directory: no identity,
# no persistence, no network. Tool ids namespace as ``mcp__pocketpaw_fx__<tool>``.
#
# Registry: a directory (``PAW_FX_REGISTRY_DIR``, default ``/opt/paw-fx/registry``)
# produced by the separate ``paw-fx`` repo, holding ``registry.json`` (the index)
# and ``items/<name>.json`` (full items with inlined files). Loaded lazily on
# first call, cached in-process, re-read when ``registry.json``'s mtime changes.
# Missing dir or malformed index logs ONE warning and behaves as an empty
# registry (fail-open, the surface never crashes). Item file paths must start
# with ``_fx/`` and contain no ``..`` (registry is trusted, the check is cheap).
#
# Engines: ``get_effect`` accepts html|svelte|react. Only html has real shells
# today; svelte/react return the same engine-neutral files with a note, and
# REFUSE items with non-empty ``needs`` (only dependency-free effects work on
# those engines in v1).
"""Agent-side MCP surface for the paw-fx effects registry."""

from __future__ import annotations

import difflib
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_fx"
# Claude Code namespaces in-process MCP tools as ``mcp__<server>__<tool>``.
SEARCH_EFFECTS_TOOL_ID = f"mcp__{SERVER_NAME}__search_effects"
GET_EFFECT_TOOL_ID = f"mcp__{SERVER_NAME}__get_effect"
LIST_EFFECT_CATEGORIES_TOOL_ID = f"mcp__{SERVER_NAME}__list_effect_categories"

FX_TOOL_IDS = (SEARCH_EFFECTS_TOOL_ID, GET_EFFECT_TOOL_ID, LIST_EFFECT_CATEGORIES_TOOL_ID)

DEFAULT_REGISTRY_DIR = "/opt/paw-fx/registry"
ENGINES = ("html", "svelte", "react")

# In-process cache: (registry_dir, registry.json mtime) -> items list.
_cache: tuple[tuple[str, float], list[dict[str, Any]]] | None = None
_warned = False


def _error_response(message: str) -> dict[str, Any]:
    """Build an MCP error response in the shape Claude's SDK expects."""
    return {
        "content": [{"type": "text", "text": f"Error: {message}"}],
        "is_error": True,
    }


def _success_response(body: Any) -> dict[str, Any]:
    """Build an MCP success response carrying ``body`` as JSON."""
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(body, separators=(",", ":"), default=str),
            }
        ]
    }


def _structured_error(body: dict[str, Any]) -> dict[str, Any]:
    """A structured ``{error: ...}`` body the agent can branch on (still JSON)."""
    out = _success_response(body)
    out["is_error"] = True
    return out


def _registry_dir() -> Path:
    return Path(os.environ.get("PAW_FX_REGISTRY_DIR") or DEFAULT_REGISTRY_DIR)


def _load_index() -> list[dict[str, Any]]:
    """The registry index items, cached on (dir, mtime). Empty on any failure."""
    global _cache, _warned
    index = _registry_dir() / "registry.json"
    try:
        key = (str(index), index.stat().st_mtime)
    except OSError:
        if not _warned:
            logger.warning("fx: registry index %s not found; serving an empty registry", index)
            _warned = True
        return []
    if _cache is not None and _cache[0] == key:
        return _cache[1]
    try:
        items = json.loads(index.read_text(encoding="utf-8"))["items"]
        if not isinstance(items, list):
            raise ValueError("items is not a list")
        items = [i for i in items if isinstance(i, dict) and isinstance(i.get("name"), str)]
    except (OSError, ValueError, KeyError, TypeError):
        if not _warned:
            logger.warning("fx: registry index %s is malformed; serving an empty registry", index)
            _warned = True
        items = []
    _cache = (key, items)
    return items


def _safe_path(path: Any) -> bool:
    return isinstance(path, str) and path.startswith("_fx/") and ".." not in path.split("/")


def _preview_url(name: str) -> str | None:
    base = os.environ.get("PAW_FX_GALLERY_URL")
    return f"{base}#{name}" if base else None


def _search(
    query: str, category: str | None, needs_js: bool | None, limit: int
) -> list[dict[str, Any]]:
    q = query.lower()
    ranked: list[tuple[int, dict[str, Any]]] = []
    for item in _load_index():
        needs = item.get("needs") or []
        if category and item.get("category") != category:
            continue
        if needs_js is False and needs:
            continue
        if needs_js is True and not needs:
            continue
        name = item["name"].lower()
        tags = [str(t).lower() for t in item.get("tags") or []]
        summary = str(item.get("summary") or "").lower()
        cat = str(item.get("category") or "").lower()
        if q == name:
            rank = 0
        elif q in name or any(q in t for t in tags):
            rank = 1
        elif q in summary or q in cat:
            rank = 2
        else:
            continue
        ranked.append((rank, item))
    ranked.sort(key=lambda r: (r[0], r[1]["name"]))
    return [
        {
            "name": i["name"],
            "category": i.get("category"),
            "tags": i.get("tags") or [],
            "summary": i.get("summary"),
            "needs": i.get("needs") or [],
            "license": i.get("license"),
            "preview_url": _preview_url(i["name"]),
        }
        for _, i in ranked[:limit]
    ]


async def _search_handler(args: dict) -> dict:
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return _error_response("search_effects requires a non-empty `query`.")
    limit = args.get("limit")
    if not isinstance(limit, int) or limit < 1:
        limit = 20
    category = args.get("category") or None
    needs_js = args.get("needs_js")
    if not isinstance(needs_js, bool):
        needs_js = None
    return _success_response({"items": _search(query.strip(), category, needs_js, min(limit, 100))})


async def _get_handler(args: dict) -> dict:
    name = args.get("name")
    if not isinstance(name, str) or not name.strip():
        return _error_response("get_effect requires a non-empty `name`.")
    name = name.strip()
    engine = args.get("engine") or "html"
    if engine not in ENGINES:
        return _error_response(f"engine must be one of {', '.join(ENGINES)}.")

    names = [i["name"] for i in _load_index()]
    item_path = _registry_dir() / "items" / f"{name}.json"
    if name not in names or "/" in name or not item_path.is_file():
        return _structured_error(
            {
                "error": "unknown_effect",
                "name": name,
                "suggestions": difflib.get_close_matches(name, names, n=3, cutoff=0),
            }
        )
    try:
        item = json.loads(item_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("fx: item %s is unreadable", item_path)
        return _structured_error({"error": "unknown_effect", "name": name, "suggestions": []})

    bad = [f.get("path") for f in item.get("files") or [] if not _safe_path(f.get("path"))]
    if bad:
        logger.warning("fx: item %s has unsafe file paths %r", name, bad)
        return _structured_error({"error": "unsafe_item_paths", "name": name, "paths": bad})

    needs = item.get("needs") or []
    if engine != "html" and needs:
        return _structured_error(
            {"error": "needs_unsupported_on_engine", "needs": needs, "engine": engine}
        )
    item["engine"] = engine
    if engine != "html":
        item["note"] = "svelte/react shells not yet available; files are engine-neutral"
    return _success_response(item)


async def _categories_handler(args: dict) -> dict:
    counts: dict[str, int] = {}
    for item in _load_index():
        cat = str(item.get("category") or "")
        counts[cat] = counts.get(cat, 0) + 1
    return _success_response(
        {"categories": [{"category": c, "count": n} for c, n in sorted(counts.items())]}
    )


def build_fx_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server for the effects registry, or ``None``
    if the Claude Agent SDK isn't installed (same shape as ``build_icons_server``)."""
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_fx MCP disabled")
        return None

    @tool(
        "search_effects",
        (
            "Search the paw-fx registry of drop-in visual effects (animated "
            "backgrounds, particles, 3D heroes, scroll/text/cursor effects, page "
            "transitions) for a generated site. Args: `query` (required), optional "
            "`category` (backgrounds|particles|3d-hero|scroll|text|cursor|transition), "
            "optional `needs_js` (false = only dependency-free effects), optional "
            "`limit` (default 20). Returns {items:[{name, category, tags, summary, "
            "needs, license, preview_url}]}. Call get_effect with a `name` to fetch "
            "its files and snippet. Empty items means nothing matched; do not invent "
            "an effect."
        ),
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "category": {"type": "string"},
                "needs_js": {"type": "boolean"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    )
    async def search_effects_tool(args):  # type: ignore[no-untyped-def]
        return await _search_handler(args)

    @tool(
        "get_effect",
        (
            "Fetch one paw-fx effect by `name`: its files (write each `path` "
            "verbatim into the site, all live under `_fx/`), the HTML `snippet` "
            "to place, `usage` notes and `options`. Optional `engine` "
            "(html|svelte|react, default html); svelte/react only accept "
            "dependency-free effects (empty `needs`) for now and return "
            "engine-neutral files with a `note`. Errors are structured: "
            "{error:'unknown_effect', suggestions} or "
            "{error:'needs_unsupported_on_engine', needs, engine}."
        ),
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "engine": {"type": "string", "enum": list(ENGINES)},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    )
    async def get_effect_tool(args):  # type: ignore[no-untyped-def]
        return await _get_handler(args)

    @tool(
        "list_effect_categories",
        "List paw-fx effect categories with item counts: {categories:[{category, count}]}.",
        {"type": "object", "properties": {}, "additionalProperties": False},
    )
    async def list_effect_categories_tool(args):  # type: ignore[no-untyped-def]
        return await _categories_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[search_effects_tool, get_effect_tool, list_effect_categories_tool],
    )
    return SERVER_NAME, server


__all__ = [
    "DEFAULT_REGISTRY_DIR",
    "FX_TOOL_IDS",
    "GET_EFFECT_TOOL_ID",
    "LIST_EFFECT_CATEGORIES_TOOL_ID",
    "SEARCH_EFFECTS_TOOL_ID",
    "SERVER_NAME",
    "build_fx_server",
]
