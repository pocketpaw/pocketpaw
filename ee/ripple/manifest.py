# backend/ee/ripple/manifest.py
"""Fetch and cache the Ripple widget manifest from a CDN.

The manifest is generated at ripple build time and published as
`@ripple-ui/svelte/dist/manifest.json`. This module fetches it,
caches the parse result in-process for a configurable TTL, and
formats it as a markdown block suitable for system-prompt injection.

On any failure (network, timeout, parse, schema), get_manifest()
returns None — the caller is expected to fall back to a different
source (today: kb scope search).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT_SECONDS = 5.0
_SCHEMA = "ripple.manifest/v1"

# Module-global cache: url -> (expires_at_monotonic, parsed_manifest)
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


async def get_manifest(url: str, ttl_seconds: int) -> dict[str, Any] | None:
    """Fetch the manifest from `url`, with in-process TTL caching.

    Returns the parsed manifest dict on success, or None on any failure.
    """
    now = time.monotonic()
    cached = _cache.get(url)
    if cached is not None and cached[0] > now:
        return cached[1]

    parsed = await _fetch_and_validate(url)
    if parsed is None:
        return None

    _cache[url] = (now + ttl_seconds, parsed)
    return parsed


async def _fetch_and_validate(url: str) -> dict[str, Any] | None:
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=_FETCH_TIMEOUT_SECONDS)
    except httpx.TimeoutException:
        logger.warning("ripple manifest fetch timed out: %s", url)
        return None
    except httpx.HTTPError as exc:
        logger.warning("ripple manifest fetch failed: %s (%s)", url, exc)
        return None

    if response.status_code >= 400:
        logger.warning("ripple manifest fetch returned %s: %s", response.status_code, url)
        return None

    try:
        parsed = response.json()
    except (json.JSONDecodeError, ValueError):
        logger.error("ripple manifest is not valid JSON: %s", url)
        return None

    if not isinstance(parsed, dict):
        logger.error("ripple manifest is not a JSON object: %s", url)
        return None
    if parsed.get("schema") != _SCHEMA:
        logger.error("ripple manifest schema mismatch (got %r): %s", parsed.get("schema"), url)
        return None
    if not isinstance(parsed.get("widgets"), list):
        logger.error("ripple manifest missing widgets array: %s", url)
        return None

    # spec envelope is optional for backward compatibility with older
    # manifests, but log if missing — agents fed a manifest without it
    # have to fall back to the system prompt for the envelope contract.
    if not isinstance(parsed.get("spec"), dict):
        logger.info("ripple manifest has no spec envelope (older publish): %s", url)

    return parsed


# Per-widget inner-item alias table for KNOWN drift in widget item arrays.
# The manifest declares item shapes as TypeScript strings (e.g.
# ``Array<{ text: string; ... }>``) which are not machine-introspectable,
# so a small hand-curated alias table covers the widgets whose item
# arrays have empirically drifted in production. Add an entry here the
# first time a new drift is observed — the comment trail below doubles
# as a record of past LLM failures.
#
# Format: ``{ widget_type: { items_prop_key: { wrong_inner_key: right_inner_key } } }``
_KNOWN_ITEM_ALIASES: dict[str, dict[str, dict[str, str]]] = {
    # `timeline` events: agents emit `description` (universal name)
    # instead of the manifest's `detail`. Clean rename.
    "timeline": {"events": {"description": "detail"}},
}


def validate_against_manifest(
    spec: dict[str, Any] | None,
    manifest: dict[str, Any],
    *,
    apply_aliases: bool = False,
) -> list[dict[str, Any]]:
    """Walk a rippleSpec tree and flag nodes whose ``props`` keys are not
    declared in the manifest for that ``type``.

    Also covers a small set of widgets whose inner item arrays have
    drifted in production (see ``_KNOWN_ITEM_ALIASES``). When
    ``apply_aliases=True``, the spec is mutated in place to rewrite the
    known wrong keys to their canonical form.

    Returns one issue dict per offending node; ``[]`` when ``spec`` is not
    a dict or no issues are found. Issue shape::

        {
          "path": "ui.children[2].children[1]",
          "type": "timeline",
          "unknown_props": ["maxItem"],          # top-level prop drift
          "allowed_props": ["events", "maxItems"],
          "item_issues": [                       # inner-item drift
            {
              "path": "...props.events[0]",
              "from": "description",
              "to": "detail",
              "applied": True,
            },
          ],
        }
    """
    if not isinstance(spec, dict):
        return []
    widgets = manifest.get("widgets") or []
    by_type: dict[str, set[str]] = {}
    for w in widgets:
        t = w.get("type")
        if isinstance(t, str):
            by_type[t] = set((w.get("props") or {}).keys())

    root = spec.get("ui") if isinstance(spec.get("ui"), dict) else spec
    issues: list[dict[str, Any]] = []
    _walk_validate(root, "ui", by_type, apply_aliases, issues)
    return issues


def _walk_validate(
    node: Any,
    path: str,
    by_type: dict[str, set[str]],
    apply_aliases: bool,
    issues: list[dict[str, Any]],
) -> None:
    if not isinstance(node, dict):
        return

    wtype = node.get("type")
    props = node.get("props")
    if isinstance(wtype, str) and wtype in by_type and isinstance(props, dict):
        allowed = by_type[wtype]
        unknown = sorted(k for k in props.keys() if k not in allowed)
        item_issues: list[dict[str, Any]] = []

        for items_key, alias_map in _KNOWN_ITEM_ALIASES.get(wtype, {}).items():
            arr = props.get(items_key)
            if not isinstance(arr, list):
                continue
            for i, item in enumerate(arr):
                if not isinstance(item, dict):
                    continue
                for wrong_k, right_k in alias_map.items():
                    if wrong_k not in item or right_k in item:
                        continue
                    if apply_aliases:
                        item[right_k] = item.pop(wrong_k)
                    item_issues.append(
                        {
                            "path": f"{path}.props.{items_key}[{i}]",
                            "from": wrong_k,
                            "to": right_k,
                            "applied": apply_aliases,
                        }
                    )

        if unknown or item_issues:
            issues.append(
                {
                    "path": path,
                    "type": wtype,
                    "unknown_props": unknown,
                    "allowed_props": sorted(allowed),
                    "item_issues": item_issues,
                }
            )

    children = node.get("children")
    if isinstance(children, list):
        for i, child in enumerate(children):
            _walk_validate(child, f"{path}.children[{i}]", by_type, apply_aliases, issues)


def format_for_prompt(manifest: dict[str, Any]) -> str:
    """Render the manifest as a markdown block for system-prompt injection.

    Returns "" if the manifest has no widgets — caller treats this the same
    as a fetch failure (i.e. falls back to kb search).
    """
    widgets = manifest.get("widgets") or []
    if not widgets:
        return ""

    lines: list[str] = []
    lines.append("\n\n<ripple-widget-reference>")

    # The envelope contract goes FIRST. The agent's most expensive failure
    # mode is inventing the wrong top-level field name (`root`, `tree`,
    # `view`) for the renderable tree — anchoring `ui` here, before the
    # widget catalog, prevents that. Per-widget examples below show
    # node-shape only; this section shows how nodes wrap into a spec.
    spec_envelope = manifest.get("spec")
    if isinstance(spec_envelope, dict):
        ui_field = spec_envelope.get("uiField") or "ui"
        state_field = spec_envelope.get("stateField") or "state"
        envelope_version = spec_envelope.get("version") or "1.0"
        aliases = spec_envelope.get("aliasesNotAllowed") or []
        description = spec_envelope.get("description") or ""
        example = spec_envelope.get("example")

        lines.append("## Spec envelope")
        lines.append("")
        if description:
            lines.append(description)
            lines.append("")
        lines.append(
            f'Top-level shape: `{{ "version": "{envelope_version}", '
            f'"{state_field}": {{...}}, "{ui_field}": {{...}} }}`.'
        )
        if aliases:
            alias_list = ", ".join(f"`{a}`" for a in aliases)
            lines.append(
                f"Do NOT name the renderable tree {alias_list}. The field is `{ui_field}` exactly."
            )
        if example:
            lines.append("")
            lines.append("**Envelope example:**")
            lines.append("```json")
            lines.append(json.dumps(example, indent=2))
            lines.append("```")
        lines.append("")
        lines.append("## Widget catalog")
        lines.append("")

    lines.append(
        "The following Ripple widgets are available. Use these props, types, "
        "and example specs when building the UI."
    )
    lines.append("")

    for w in widgets:
        wtype = w.get("type", "?")
        category = w.get("category", "?")
        desc = w.get("description", "")
        lines.append(f"### `{wtype}` ({category})")
        lines.append(desc)
        props = w.get("props") or {}
        if props:
            lines.append("")
            lines.append("**Props:**")
            for name, spec in props.items():
                req = " *(required)*" if spec.get("required") else ""
                lines.append(
                    f"- `{name}`: `{spec.get('type', '?')}`{req} — {spec.get('description', '')}"
                )
        example = w.get("example")
        if example:
            lines.append("")
            lines.append("**Example:**")
            lines.append("```json")
            lines.append(json.dumps(example, indent=2))
            lines.append("```")
        lines.append("")

    lines.append("</ripple-widget-reference>")
    return "\n".join(lines)
