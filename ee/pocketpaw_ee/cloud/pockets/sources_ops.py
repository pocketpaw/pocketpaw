# sources_ops.py — Pure mutate helpers for ``rippleSpec.sources``.
# Created: 2026-05-22 (RFC 04 alpha follow-up) — closes the edit-path gap:
#   RFC 04 lets a pocket carry a top-level ``rippleSpec.sources`` block of
#   read-only GET data bindings. It worked on CREATE but the pocket EDIT
#   specialist had no op that could write the ``sources`` key on an
#   EXISTING pocket. This module is the pure-helper sibling of
#   ``state_ops.py`` (which mutates ``rippleSpec.state``) and
#   ``spec_ops.py`` (which mutates ``rippleSpec.ui``) — it owns the
#   third top-level key.
#
# These helpers are side-effect-free: they take and return plain ``dict``
# structures, never touch Beanie or the SSE bus, and never log. The
# service layer (``service.py``) wraps them with binding validation,
# persistence, and event emission. Keeping this module Beanie-free is an
# import-linter contract (``ee/pyproject.toml``).
#
# Unlike ``state`` (dotted-path addressable) a source entry is keyed by a
# single flat name — ``sources["prs"]`` — so these helpers take a bare
# ``key`` rather than a path. The binding *value* is validated upstream by
# the ``SourceBinding`` Pydantic model in ``source_executor.py``; this
# module stores whatever dict it is handed.

from __future__ import annotations

from typing import Any


def set_source(spec: dict[str, Any], key: str, binding: dict[str, Any]) -> dict[str, Any]:
    """Write ``binding`` at ``spec["sources"][key]``, creating the
    ``sources`` dict if it is absent. Overwrites an existing entry.

    Returns ``spec`` (mutated in place) so callers can chain.

    Raises ``ValueError`` if ``key`` is empty or if ``spec["sources"]``
    exists but is not a dict (a malformed spec the agent should not be
    appending to).
    """
    if not key:
        raise ValueError("source key is required")
    sources = spec.get("sources")
    if sources is None:
        sources = {}
        spec["sources"] = sources
    if not isinstance(sources, dict):
        raise ValueError(f"rippleSpec.sources is a {type(sources).__name__}, expected an object")
    sources[key] = binding
    return spec


def remove_source(spec: dict[str, Any], key: str) -> dict[str, Any]:
    """Remove ``spec["sources"][key]``. No-op when the ``sources`` block
    or the key is absent — removal is idempotent so the agent can call it
    without first checking existence.

    Returns ``spec`` (mutated in place).
    """
    sources = spec.get("sources")
    if isinstance(sources, dict):
        sources.pop(key, None)
    return spec


__all__ = ["remove_source", "set_source"]
