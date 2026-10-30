# actions_ops.py — Pure mutate helpers for ``rippleSpec.actions``.
# Created: 2026-05-22 (RFC 05 M2a) — the write-action sibling of
#   ``sources_ops.py``. RFC 05 adds a top-level ``rippleSpec.actions`` block
#   of write bindings (POST/PUT/PATCH/DELETE) alongside the RFC 04
#   ``rippleSpec.sources`` block of read bindings. This module owns the
#   ``actions`` key the way ``sources_ops.py`` owns ``sources``,
#   ``state_ops.py`` owns ``state``, and ``spec_ops.py`` owns ``ui``.
#
# These helpers are side-effect-free: they take and return plain ``dict``
# structures, never touch Beanie or the SSE bus, and never log. The
# service layer (``service.py``) wraps them with binding validation,
# persistence, and event emission. Keeping this module Beanie-free is an
# import-linter contract (``ee/pyproject.toml``).
#
# An action entry is keyed by a single flat name — ``actions["mark_done"]``
# — so these helpers take a bare ``key`` rather than a path. The binding
# *value* is validated upstream by the ``ActionBinding`` Pydantic model in
# ``action_executor.py``; this module stores whatever dict it is handed.

from __future__ import annotations

from typing import Any


def set_action(spec: dict[str, Any], key: str, binding: dict[str, Any]) -> dict[str, Any]:
    """Write ``binding`` at ``spec["actions"][key]``, creating the
    ``actions`` dict if it is absent. Overwrites an existing entry.

    Returns ``spec`` (mutated in place) so callers can chain.

    Raises ``ValueError`` if ``key`` is empty or if ``spec["actions"]``
    exists but is not a dict (a malformed spec the agent should not be
    appending to).
    """
    if not key:
        raise ValueError("action key is required")
    actions = spec.get("actions")
    if actions is None:
        actions = {}
        spec["actions"] = actions
    if not isinstance(actions, dict):
        raise ValueError(f"rippleSpec.actions is a {type(actions).__name__}, expected an object")
    actions[key] = binding
    return spec


def remove_action(spec: dict[str, Any], key: str) -> dict[str, Any]:
    """Remove ``spec["actions"][key]``. No-op when the ``actions`` block
    or the key is absent — removal is idempotent so the agent can call it
    without first checking existence.

    Returns ``spec`` (mutated in place).
    """
    actions = spec.get("actions")
    if isinstance(actions, dict):
        actions.pop(key, None)
    return spec


__all__ = ["remove_action", "set_action"]
