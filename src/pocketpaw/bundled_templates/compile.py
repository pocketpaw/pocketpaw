# src/pocketpaw/bundled_templates/compile.py
# Created: 2026-05-25 (feat/rfc-03-v2-compile) — translates a validated
# RFC 03 v2 ``PocketTemplate`` into a runtime-shaped ``rippleSpec`` dict.
# This module is the OSS-side seam between design-time templates and
# runtime executors. The compile output is a plain dict — the EE-side
# runtime (``pocketpaw_ee.cloud.pockets.source_executor.SourceBinding``,
# etc.) consumes it via ``model_validate``, so the seam keeps the OSS→EE
# import boundary clean (import-linter enforced).
"""Translate an RFC 03 v2 ``PocketTemplate`` into a runtime rippleSpec.

The ``compile_template`` function is the OSS-core seam between the
design-time Pydantic schema (``PocketTemplate`` and its sub-models) and
the runtime executors (RFC 04 source executor, RFC 05 action executor,
RFC 06 A2UI patcher, …). The output is a plain dict so it can cross
the OSS↔EE boundary without dragging a Pydantic import into either
side beyond the schema each owns.

Scope of this PR (PR 2b — data-sources only)
============================================

The compile pass implemented here translates ONE field — ``data_sources[]`` —
into the runtime's ``sources`` block. Every other top-level field is
passed through verbatim so downstream PRs can read them off the same
seam without re-loading the template:

* ``actions``        — passthrough; CEL evaluation in PR 2c, Instinct in PR 2d
* ``agents``         — passthrough; agent-backend binding in a later PR
* ``triggers``       — passthrough; cron / temporal / source_change in PR 2f
* ``permissions``    — passthrough; RBAC runtime in PR 2g
* ``instinct_rules`` — passthrough; Instinct execution in PR 2d
* ``connectors``     — passthrough; the runtime resolves them at install
* ``outcomes``       — passthrough; the meter listens on these names

The output also carries ``state``, ``name``, ``version``, ``pattern``,
``shape``, ``display_name``, ``description``, ``vertical``, ``kb_scope``,
``skill_refs``, ``screenshots``, ``icon``, ``color``, and
``schema_version`` so install-time tooling has a single seam to read
off — no need to re-parse the YAML template.

Forbidden: this module MUST NOT import ``pocketpaw_ee.*``. The runtime
``SourceBinding`` lives in EE; the compile output here is shaped to be
consumable by it but never imports the Pydantic class. Cross-boundary
shape compatibility is verified by tests via lazy imports gated by
``pytest.importorskip("pocketpaw_ee")``.

``data_sources[]`` translation rules
====================================

The RFC's ``data_sources[]`` sub-schema and the runtime's
``SourceBinding`` declare overlapping but non-identical field sets:

* ``name`` (kebab-case) -> dict key in ``sources``; removed from value.
* ``method`` (GET only) -> ``method``; pass through.
* ``path`` (relative)   -> ``path``; pass through.
* ``bind``              -> ``bind``; pass through.
* ``refresh`` (str list with optional ``<keyword>:<arg>`` forms) ->
  ``refresh`` (Literal list) plus optional ``refresh_interval_seconds``:
    - ``pocket_open`` / ``manual`` / ``webhook`` pass through bare.
    - ``interval:<dur>`` splits into bare ``interval`` plus the parsed
      seconds (``1h`` -> 3600, ``30s`` -> 30, ``7d`` -> 604800, …).
    - ``signal:<event>`` is dropped — the runtime has no equivalent
      yet; tracked for a future RFC 04 PR.
* ``transform`` -> passthrough; RFC 04 M2 runtime feature. The runtime
  ``SourceBinding`` ignores unknown keys so this is safe today.

Duration parsing supports ``<n>s``, ``<n>m``, ``<n>h``, ``<n>d`` and
plain ``<n>`` (seconds). Anything else is treated as ``None`` — the
runtime then falls back to its configured minimum interval.

Purity contract
===============

``compile_template`` is a pure function. It performs no I/O, no
persistence, no side effects. The input is never mutated. Calling
twice with the same input produces identical output.
"""

from __future__ import annotations

import re
from typing import Any

from pocketpaw.bundled_templates.schema import (
    DataSourceDef,
    PocketTemplate,
)

# ---------------------------------------------------------------------------
# Refresh-keyword normalization
# ---------------------------------------------------------------------------

# Runtime ``RefreshTrigger`` literal (kept in sync with
# ``ee/pocketpaw_ee/cloud/pockets/source_executor.RefreshTrigger``).
# Listed here for the OSS side so the compile pass can drop entries the
# runtime would reject. Keep in sync; when the runtime grows a new
# trigger (e.g. ``signal``), drop it from ``_UNSUPPORTED_REFRESH_PREFIXES``.
_KNOWN_REFRESH_KEYWORDS: frozenset[str] = frozenset(
    {"pocket_open", "manual", "interval", "webhook"}
)

# Colon-prefixed refresh entries the RFC permits but the runtime
# currently has no equivalent for. They are silently dropped from the
# compile output; downstream PRs that wire the corresponding runtime
# support remove the prefix from this set.
_UNSUPPORTED_REFRESH_PREFIXES: frozenset[str] = frozenset({"signal"})

# Compile-time duration parser. Matches ``<integer><unit>`` with
# ``unit`` in ``s | m | h | d``. A bare integer is interpreted as
# seconds for ergonomic agent-authored templates. Unknown forms return
# ``None`` and the runtime falls back to the configured floor.
_DURATION_RE = re.compile(r"^(\d+)([smhd]?)$")
_DURATION_MULTIPLIERS: dict[str, int] = {
    "": 1,
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
}


def _parse_duration_to_seconds(value: str) -> int | None:
    """Parse ``"1h"`` / ``"30s"`` / ``"7d"`` / ``"3600"`` -> seconds.

    Returns ``None`` for any unrecognized form so the runtime's interval
    floor takes over instead of honouring a bogus value.
    """
    m = _DURATION_RE.match(value.strip())
    if not m:
        return None
    qty = int(m.group(1))
    unit = m.group(2)
    multiplier = _DURATION_MULTIPLIERS.get(unit)
    if multiplier is None:
        return None
    return qty * multiplier


def _compile_refresh(refresh: list[str]) -> tuple[list[str], int | None]:
    """Translate the RFC-form ``refresh`` list into the runtime form.

    Returns ``(refresh_list, refresh_interval_seconds | None)``.

    Rules:
      * Bare ``pocket_open`` / ``manual`` / ``interval`` / ``webhook``
        pass through.
      * ``interval:<dur>`` splits into bare ``interval`` plus the parsed
        seconds. The interval value is the LAST one wins if multiple
        ``interval:`` entries exist (callers should pass at most one).
      * ``signal:<event>`` is silently dropped (no runtime equivalent
        yet — tracked for a future RFC 04 PR).
      * Anything else is dropped to keep the output shape valid.

    The output ``refresh_list`` preserves declaration order and is
    deduped (each runtime keyword appears at most once).
    """
    out: list[str] = []
    interval_seconds: int | None = None

    for raw in refresh:
        if not isinstance(raw, str):
            continue
        # Strip optional ``<keyword>:<arg>`` suffix to get the bare form.
        if ":" in raw:
            keyword, _, arg = raw.partition(":")
            keyword = keyword.strip()
            arg = arg.strip()
        else:
            keyword = raw.strip()
            arg = ""

        if keyword in _UNSUPPORTED_REFRESH_PREFIXES:
            # Deferred runtime feature (e.g. signal:gmail.inbox.update).
            continue

        if keyword not in _KNOWN_REFRESH_KEYWORDS:
            # Drop unknown keywords rather than emit garbage the runtime
            # would reject.
            continue

        if keyword == "interval" and arg:
            parsed = _parse_duration_to_seconds(arg)
            if parsed is not None:
                interval_seconds = parsed

        if keyword not in out:
            out.append(keyword)

    return out, interval_seconds


# ---------------------------------------------------------------------------
# data_sources[] -> sources{}
# ---------------------------------------------------------------------------


def _compile_data_source(src: DataSourceDef) -> dict[str, Any]:
    """Translate one ``DataSourceDef`` into the runtime per-source dict.

    The ``name`` field is NOT included in the returned dict — it
    becomes the dict key in the parent ``sources`` block (matching the
    runtime ``rippleSpec.sources: {name: SourceBinding}`` convention).

    The optional ``transform`` field passes through verbatim. The
    runtime ``SourceBinding`` ignores unknown keys, so this is safe;
    when RFC 04 M2 ships the transform registry, the field is already
    on the seam.
    """
    refresh_list, interval_seconds = _compile_refresh(src.refresh)
    entry: dict[str, Any] = {
        "method": src.method,
        "path": src.path,
        "bind": src.bind,
        "refresh": refresh_list,
    }
    if interval_seconds is not None:
        entry["refresh_interval_seconds"] = interval_seconds
    if src.transform is not None:
        entry["transform"] = src.transform
    return entry


def _compile_sources(template: PocketTemplate) -> dict[str, dict[str, Any]]:
    """Translate ``template.data_sources[]`` into a ``{name: entry}``
    dict matching the runtime's ``rippleSpec.sources`` shape."""
    return {src.name: _compile_data_source(src) for src in template.data_sources}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def compile_template(template: PocketTemplate) -> dict[str, Any]:
    """Compile a validated ``PocketTemplate`` into a runtime rippleSpec dict.

    This is the OSS-core seam between RFC 03 v2 (the design-time schema)
    and the runtime executors (RFC 04 source executor, etc.). The output
    is a plain dict so it can cross the OSS↔EE boundary without dragging
    EE's Pydantic models into OSS code (forbidden by the import-linter
    contract) or vice versa.

    In this PR (PR 2b) the compile pass implements ONE translation —
    ``data_sources[]`` -> ``sources``. Every other top-level field is
    passed through verbatim so the seam is incrementally useful as PRs
    2c (CEL action evaluator), 2d (Instinct execution), 2f (triggers),
    and 2g (permissions) land.

    Args:
        template: A validated ``PocketTemplate`` instance — typically
            obtained via ``load_template(slug, strict=True)`` or
            ``PocketTemplate.model_validate(yaml.safe_load(...))``.

    Returns:
        A dict of shape::

            {
                # Translated fields
                "sources": {
                    "<name>": {
                        "method": "GET",
                        "path": "/items",
                        "bind": "state.items",
                        "refresh": ["pocket_open", "manual", "interval"],
                        "refresh_interval_seconds": 3600,  # optional
                        "transform": "flatten_leases",     # optional
                    },
                    ...
                },
                # Passthrough fields — read off the same seam
                "schema_version": "2",
                "name": "...",
                "version": "...",
                "pattern": "...",
                "shape": "...",
                "state": {...},
                "actions": [...],
                "agents": [...],
                "triggers": [...],
                "outcomes": [...],
                "connectors": [...],
                "permissions": {...} | None,
                "instinct_rules": {...} | None,
                "kb_scope": "...",
                "skill_refs": [...],
                "display_name": "..." | None,
                "description": "...",
                "vertical": "...",
                "screenshots": [...],
                "icon": "..." | None,
                "color": "..." | None,
            }

    The function is pure: no I/O, no input mutation, deterministic
    output. Calling twice with the same input yields identical dicts.
    """
    # Translated field — the only field this PR semantically transforms.
    sources = _compile_sources(template)

    # Passthrough fields — JSON-mode dump preserves nested Pydantic
    # models as plain dicts, lists, strings, ints. The fields list mirrors
    # ``PocketTemplate``'s top-level shape; new top-level RFC fields
    # need a corresponding entry here.
    template_dict = template.model_dump(mode="json")

    out: dict[str, Any] = {
        "schema_version": template_dict["schema_version"],
        "name": template_dict["name"],
        "version": template_dict["version"],
        "pattern": template_dict["pattern"],
        "vertical": template_dict["vertical"],
        "shape": template_dict["shape"],
        "description": template_dict["description"],
        "state": template_dict["state"],
        "actions": template_dict.get("actions", []),
        "connectors": template_dict.get("connectors", []),
        "agents": template_dict.get("agents", []),
        "triggers": template_dict.get("triggers", []),
        "outcomes": template_dict.get("outcomes", []),
        "kb_scope": template_dict.get("kb_scope", "pocket"),
        "skill_refs": template_dict.get("skill_refs", []),
        "screenshots": template_dict.get("screenshots", []),
        "sources": sources,
    }
    # Optional fields — emit only if populated so the compile output
    # stays compact for the agent-authored cases.
    if template_dict.get("display_name") is not None:
        out["display_name"] = template_dict["display_name"]
    if template_dict.get("icon") is not None:
        out["icon"] = template_dict["icon"]
    if template_dict.get("color") is not None:
        out["color"] = template_dict["color"]
    if template_dict.get("permissions") is not None:
        out["permissions"] = template_dict["permissions"]
    if template_dict.get("instinct_rules") is not None:
        out["instinct_rules"] = template_dict["instinct_rules"]

    return out


__all__ = ["compile_template"]
