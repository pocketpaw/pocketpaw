# ee/fabric/policy.py — Scope-based visibility decisions for Fabric.
# Created: 2026-04-16 (feat/fabric-journal-projection) — Wave 3 / Org Architecture RFC,
# Phase 3. Ported verbatim from #938's ee/policy/engine.py, which was the only part of
# that PR worth keeping. The decision logic is correct and carries its own tests; only
# the surrounding write/read plumbing is being replaced with a journal projection.
#
# Why local copies of `_granted` + `_match` instead of calling soul-protocol's
# `scopes_overlap`? Two reasons, both mirroring #938's original rationale:
#   1. No hard dep on soul-protocol for the policy engine itself — importable in
#      minimal runtime slices (tests, tools) that don't want the full engine.
#   2. Keeps the audit trail (`decide()`) returning the exact caller scope that
#      granted access, which `scopes_overlap` abstracts away as a bool.
#
# Bidirectional containment (wildcard on either side grants access) matches
# soul-protocol's `scopes_overlap` semantics so a FabricObject tagged
# `org:sales:leads` is visible both to an explicit `org:sales:leads` caller and
# to a wildcard `org:sales:*` caller — the same rule Fabric will hit again when
# paw-runtime's retrieval router runs.

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Default: unscoped entities (scope == []) are visible to everyone. Set to False
# at process start if your tenant requires explicit scope on every entity. Per-call
# overrides flow through filter_visible(allow_unscoped=...).
DEFAULT_ALLOW_UNSCOPED = True


@dataclass
class PolicyDecision:
    """Result of applying the policy to a single entity."""

    allowed: bool
    entity_id: str
    entity_scopes: list[str]
    matched_scope: str | None = None
    reason: str = ""


def visible(
    entity: Any,
    user_scopes: list[str] | None,
    *,
    allow_unscoped: bool = DEFAULT_ALLOW_UNSCOPED,
) -> bool:
    """Return True when the caller is allowed to see this entity.

    ``entity`` is anything with a ``scope`` attribute or a ``scope`` key —
    FabricObject, dict, duck-typed stand-in. ``user_scopes`` is the caller's
    scope list; empty/None passes through (caller sees everything they
    otherwise would).
    """

    entity_scopes = _entity_scopes(entity)

    if not user_scopes:
        return True
    if not entity_scopes:
        return allow_unscoped

    return _match(entity_scopes, user_scopes)


def filter_visible(
    entities: list[Any],
    user_scopes: list[str] | None,
    *,
    allow_unscoped: bool = DEFAULT_ALLOW_UNSCOPED,
) -> tuple[list[Any], int]:
    """Return ``(visible_entities, hidden_count)`` for the caller.

    The hidden count is what the projection writes into the retrieval log so
    operators can see how many entries were filtered per call. Callers that
    only want the kept list should discard the count.
    """

    if not user_scopes:
        return list(entities), 0

    kept: list[Any] = []
    hidden = 0
    for entity in entities:
        if visible(entity, user_scopes, allow_unscoped=allow_unscoped):
            kept.append(entity)
        else:
            hidden += 1
    return kept, hidden


def decide(
    entity: Any,
    user_scopes: list[str] | None,
    *,
    allow_unscoped: bool = DEFAULT_ALLOW_UNSCOPED,
) -> PolicyDecision:
    """Return a PolicyDecision explaining why the entity was allowed/denied.

    Used by the audit path so operators can answer "why was X filtered?"
    without re-running the policy engine.
    """

    entity_scopes = _entity_scopes(entity)
    entity_id = _entity_id(entity)

    if not user_scopes:
        return PolicyDecision(
            allowed=True,
            entity_id=entity_id,
            entity_scopes=entity_scopes,
            reason="caller has no scope filter — pass-through",
        )

    if not entity_scopes:
        return PolicyDecision(
            allowed=allow_unscoped,
            entity_id=entity_id,
            entity_scopes=entity_scopes,
            reason=(
                "entity is unscoped — allowed by default"
                if allow_unscoped
                else "entity is unscoped — denied because allow_unscoped=False"
            ),
        )

    matched = _first_match(entity_scopes, user_scopes)
    if matched is not None:
        return PolicyDecision(
            allowed=True,
            entity_id=entity_id,
            entity_scopes=entity_scopes,
            matched_scope=matched,
            reason=f"caller has '{matched}' which grants entity scope",
        )

    return PolicyDecision(
        allowed=False,
        entity_id=entity_id,
        entity_scopes=entity_scopes,
        reason="no caller scope grants any entity scope",
    )


# ---------------------------------------------------------------------------
# Internals — verbatim from #938 so the decision semantics don't drift.
# ---------------------------------------------------------------------------


def _entity_scopes(entity: Any) -> list[str]:
    if entity is None:
        return []
    raw = getattr(entity, "scope", None)
    if raw is None and isinstance(entity, dict):
        raw = entity.get("scope")
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    return [s for s in raw if isinstance(s, str)]


def _entity_id(entity: Any) -> str:
    if entity is None:
        return ""
    raw = getattr(entity, "id", None)
    if raw is None and isinstance(entity, dict):
        raw = entity.get("id")
    return str(raw) if raw else ""


def _match(entity_scopes: list[str], user_scopes: list[str]) -> bool:
    """Boolean OR over the cartesian product. Matches soul-protocol's
    `scopes_overlap` for the specific-entity + wildcard-caller combo; we
    keep the local impl so this module doesn't drag soul-protocol in.
    """

    return any(_granted(e, a) for e in entity_scopes for a in user_scopes)


def _first_match(entity_scopes: list[str], user_scopes: list[str]) -> str | None:
    for a in user_scopes:
        for e in entity_scopes:
            if _granted(e, a):
                return a
    return None


def _granted(entity_scope: str, allowed_scope: str) -> bool:
    if allowed_scope == "*":
        return True
    if allowed_scope == entity_scope:
        return True
    if allowed_scope.endswith(":*"):
        prefix = allowed_scope[:-2]
        return entity_scope == prefix or entity_scope.startswith(prefix + ":")
    # Inverse: caller presents a specific scope, entity is tagged with a
    # wildcard subtree. Mirrors soul-protocol's scopes_overlap bidirectional
    # containment so a retrieval router operating on a concrete scope can
    # still see entities that were bulk-tagged with a wildcard.
    if entity_scope.endswith(":*"):
        prefix = entity_scope[:-2]
        return allowed_scope == prefix or allowed_scope.startswith(prefix + ":")
    return False
