# src/pocketpaw/sites_capture/ingest.py — pure, dependency-free ingest hardening
# generalized from ee/paw_bar/router.py. Origin pinning, honeypot, and mapping
# interpolation live here so the cloud entity and any other caller share one
# implementation. Changes (2026-10-01, CN-7): ``interpolate`` is public and
# paw-bar's legacy ingest imports it instead of carrying its own copy. The two
# origin policies stay separate on purpose (capture fails closed, paw-bar's demo
# allowlist fails open). Rate-limit COUNTING stays in the store (it needs persistence);
# this module only holds the stateless predicates + interpolation.

from __future__ import annotations

import re
from typing import Any

from pocketpaw.sites_capture.models import SiteEventMapping

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


def origin_allowed(allowed_origins: list[str], origin: str | None) -> bool:
    """Host-only origin match (port + path ignored), generalized from
    paw-bar's `_origin_allowed`. Difference: the capture path is a public
    internet endpoint, so an EMPTY allowlist or a MISSING origin fails closed."""
    if not allowed_origins:
        return False
    if not origin:
        return False
    host = origin.strip().lower()
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0]
    host = host.split(":", 1)[0]
    return host in allowed_origins


def is_honeypot_tripped(payload: dict[str, Any], *, honeypot_field: str) -> bool:
    """A hidden field a human never fills; if a bot filled it, drop the submit."""
    value = payload.get(honeypot_field)
    return bool(value)


def interpolate_mapping(mapping: SiteEventMapping, context: dict[str, Any]) -> dict[str, Any]:
    """Resolve `{{ a.b }}` placeholders in every mapping field — verbatim
    generalization of paw-bar's old `_interpolate` / `_lookup`."""
    return {key: interpolate(template, context) for key, template in mapping.fields.items()}


def interpolate(template: str, context: dict[str, Any]) -> Any:
    """Resolve `{{ a.b }}` placeholders against ``context``. A template that is
    one whole placeholder returns the raw value (types preserved); a mixed
    string substitutes stringified values, missing paths as ""."""
    full = re.fullmatch(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}", template)
    if full:
        return _lookup(full.group(1), context)

    def _replace(m: re.Match[str]) -> str:
        val = _lookup(m.group(1), context)
        return "" if val is None else str(val)

    return _PLACEHOLDER_RE.sub(_replace, template)


def _lookup(path: str, context: dict[str, Any]) -> Any:
    cur: Any = context
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur
