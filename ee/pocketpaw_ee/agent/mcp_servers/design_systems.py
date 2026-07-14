# design_systems.py — in-process MCP server exposing the bundled library of
# portable DESIGN.md design systems to the claude_agent_sdk cloud chat backend.
#
# Created: 2026-07-06 (feat/sites-crew-design-systems, SC-7b). The Svelte-track
# site-authoring skill (pocketpaw-create-svelte-site) runs on the
# claude_agent_sdk backend, which only sees in-process MCP servers — a plain
# BaseTool is invisible to it (same reason media.py / stock_images.py / icons.py
# / palette.py exist). The load-bearing insight from our 2026-07-06 design-system
# research is that agents produce far better sites when they PICK and adapt a
# coherent, professional visual identity (full color scales + type scale +
# component states + rationale) instead of inventing a look cold. This server is
# the retriever over that bundled library.
#
# What this file does: clones the icons.py / palette.py shape — a single
# ``create_sdk_mcp_server`` with an SDK import-guard, ``SERVER_NAME`` /
# ``*_TOOL_ID`` allowlist constants, and the ``_error_response`` /
# ``_success_response`` helpers. Unlike its network-backed siblings this is a
# PURE LOCAL READ: it reads files straight from the OSS-core bundled package via
# ``pocketpaw.bundled_design_systems.bundled_design_systems_dir()`` — NO kb-go,
# NO network — so it needs NO workspace/user identity, persists nothing, and
# binds no session.
#
# TWO tools:
#   * ``list_design_systems`` — returns every bundled system's manifest
#     (slug / name / description / aesthetic / industries / page_types) so the
#     agent can CHOOSE one.
#   * ``get_design_system`` — takes a ``slug`` and returns that system's full
#     ``DESIGN.md`` text + ``tokens.css`` text + manifest, so the agent can read
#     the tokens + rationale and theme the build from them.
#
# Tool ids namespace as ``mcp__pocketpaw_design_systems__<tool>`` so the Claude
# Code allowlist machinery matches them.
#
# EE→OSS boundary: this module imports the bundled-dir helper from src/pocketpaw
# (allowed — EE depends on OSS core). Fail-soft everywhere: an unknown slug
# returns an error listing the valid slugs; an unreadable file returns a soft
# error; a missing/empty bundle returns an empty list. Nothing raises into the
# agent.
"""Agent-side MCP surface for the bundled DESIGN.md design-system library."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_design_systems"
# Claude Code namespaces in-process MCP tools as ``mcp__<server>__<tool>``.
LIST_DESIGN_SYSTEMS_TOOL_ID = f"mcp__{SERVER_NAME}__list_design_systems"
GET_DESIGN_SYSTEM_TOOL_ID = f"mcp__{SERVER_NAME}__get_design_system"

DESIGN_SYSTEM_TOOL_IDS = (LIST_DESIGN_SYSTEMS_TOOL_ID, GET_DESIGN_SYSTEM_TOOL_ID)

# The three files every bundled ``<slug>/`` directory carries.
_DESIGN_MD = "DESIGN.md"
_TOKENS_CSS = "tokens.css"
_MANIFEST_JSON = "manifest.json"


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


def _systems_dir() -> Path:
    """Resolve the bundled design-systems directory from OSS core.

    Imported lazily so the EE→OSS import stays inside the function (mirrors how
    stock_images.py reaches its OSS helper) and a missing OSS install surfaces as
    a soft error, not an import-time crash.
    """
    from pocketpaw.bundled_design_systems import bundled_design_systems_dir

    return bundled_design_systems_dir()


def _iter_system_dirs() -> list[Path]:
    """Return the child ``<slug>/`` directories that carry a manifest, sorted by
    slug. A directory without a ``manifest.json`` is skipped (not a system)."""
    root = _systems_dir()
    if not root.is_dir():
        return []
    out = [d for d in root.iterdir() if d.is_dir() and (d / _MANIFEST_JSON).is_file()]
    out.sort(key=lambda d: d.name)
    return out


def _load_manifest(system_dir: Path) -> dict[str, Any] | None:
    """Read + parse one system's ``manifest.json``. Returns ``None`` on any read
    or parse error so a single corrupt manifest can't sink the whole listing."""
    try:
        raw = (system_dir / _MANIFEST_JSON).read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:  # noqa: BLE001 — one bad manifest must not break list
        logger.warning("design_systems: unreadable manifest in %s", system_dir, exc_info=True)
        return None
    if not isinstance(data, dict):
        return None
    # Trust the on-disk slug but default it to the directory name so the manifest
    # always carries a usable slug for the follow-up get call.
    data.setdefault("slug", system_dir.name)
    return data


def _list_manifests() -> list[dict[str, Any]]:
    """Load every bundled system's manifest, skipping any that fail to parse."""
    manifests: list[dict[str, Any]] = []
    for system_dir in _iter_system_dirs():
        manifest = _load_manifest(system_dir)
        if manifest is not None:
            manifests.append(manifest)
    return manifests


def _valid_slugs() -> list[str]:
    """The slugs (directory names) the retriever can serve, sorted."""
    return [d.name for d in _iter_system_dirs()]


async def _list_handler(args: dict) -> dict:
    """MCP handler for ``design_systems__list_design_systems``.

    Pure local read: returns every bundled system's manifest so the agent can
    choose one. An empty ``design_systems`` means the bundle is missing/empty —
    the caller should proceed and design a look itself, not treat it as an error.
    """
    try:
        manifests = _list_manifests()
    except Exception as exc:  # noqa: BLE001 — fail soft, never raise into the agent
        logger.warning("design_systems: list failed", exc_info=True)
        return _error_response(f"listing design systems failed: {exc}")

    return _success_response({"ok": True, "count": len(manifests), "design_systems": manifests})


async def _get_handler(args: dict) -> dict:
    """MCP handler for ``design_systems__get_design_system``.

    Pure local read: takes a ``slug`` and returns that system's full
    ``DESIGN.md`` text, ``tokens.css`` text, and parsed manifest. An unknown slug
    returns an error that lists the valid slugs; an unreadable file returns a soft
    error. Never raises into the agent.
    """
    slug = args.get("slug")
    if not isinstance(slug, str) or not slug.strip():
        return _error_response("get_design_system requires a non-empty `slug`.")
    slug = slug.strip()

    valid = _valid_slugs()
    if slug not in valid:
        # Path-traversal-safe: we only ever serve a slug that is an actual child
        # directory of the bundle, so a crafted "../" slug simply isn't in the set.
        valid_str = ", ".join(valid) if valid else "(none available)"
        return _error_response(f"unknown design system slug {slug!r}. Valid slugs: {valid_str}.")

    system_dir = _systems_dir() / slug
    try:
        design_md = (system_dir / _DESIGN_MD).read_text(encoding="utf-8")
        tokens_css = (system_dir / _TOKENS_CSS).read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 — unreadable file → soft error
        logger.warning("design_systems: read failed for %r", slug, exc_info=True)
        return _error_response(f"could not read design system {slug!r}: {exc}")

    manifest = _load_manifest(system_dir) or {"slug": slug}
    return _success_response(
        {
            "ok": True,
            "slug": slug,
            "name": manifest.get("name", slug),
            "design_md": design_md,
            "tokens_css": tokens_css,
            "manifest": manifest,
        }
    )


def build_design_systems_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server for the design-system library, or
    return ``None`` if the Claude Agent SDK isn't installed. Matches the ``(name,
    server)`` / ``None`` shape of ``build_icons_server`` so the backend's MCP
    registration loop treats it identically."""
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_design_systems MCP disabled")
        return None

    @tool(
        "list_design_systems",
        (
            "List the bundled, ready-to-use design systems — coherent, "
            "professional visual identities (full color scales, type scale, "
            "spacing, components) you can pick from and adapt instead of "
            "inventing a look cold. Use this FIRST when building a site or page: "
            "review the systems, match one to the brand's industry + desired "
            "aesthetic, then call `get_design_system` for its full tokens. Takes "
            "no arguments. Returns {ok, count, design_systems:[{slug, name, "
            "description, aesthetic, industries, page_types}]}. Pick by matching "
            "`aesthetic`/`industries` to the brief. An empty list means no bundle "
            "is installed — design a coherent look yourself."
        ),
        {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    )
    async def list_design_systems_tool(args):  # type: ignore[no-untyped-def]
        return await _list_handler(args)

    @tool(
        "get_design_system",
        (
            "Fetch one bundled design system's full spec by slug. Use after "
            "`list_design_systems` to load the system you picked. Args: `slug` "
            "(required — a slug from the list, e.g. 'clean-saas'). Returns {ok, "
            "slug, name, design_md, tokens_css, manifest}. `design_md` is the "
            "DESIGN.md (YAML token front-matter + prose rationale, do's/don'ts, "
            "anti-patterns) — READ IT to design on-brand. `tokens_css` is the same "
            "tokens as CSS custom properties (--color-primary-500, --font-heading, "
            "…): drop it into the page and build with the variables so the result "
            "is coherent. An unknown slug returns an error listing the valid slugs "
            "— call `list_design_systems` to see them."
        ),
        {
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The design system slug to fetch (from list_design_systems).",
                },
            },
            "required": ["slug"],
            "additionalProperties": False,
        },
    )
    async def get_design_system_tool(args):  # type: ignore[no-untyped-def]
        return await _get_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[list_design_systems_tool, get_design_system_tool],
    )
    return SERVER_NAME, server


__all__ = [
    "LIST_DESIGN_SYSTEMS_TOOL_ID",
    "GET_DESIGN_SYSTEM_TOOL_ID",
    "DESIGN_SYSTEM_TOOL_IDS",
    "SERVER_NAME",
    "build_design_systems_server",
]
