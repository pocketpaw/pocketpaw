# src/pocketpaw/bundled_templates/fabric_resolver.py
# Created: 2026-05-28 (feat/rfc-03-v2-fabric) — strict
# :class:`IdentifierResolver` for RFC 03 v2's ``tier: registered``
# enforcement. Drop-in replacement for the PR 2c reference resolver
# (:class:`TemplateIdentifierResolver`) that additionally validates
# every declared join against a :class:`FabricRegistry`. The CEL
# evaluator (``cel_runtime.evaluate_cel``) consumes both resolvers
# through the shared Protocol — no evaluator change needed.
"""Strict :class:`IdentifierResolver` keyed off a
:class:`FabricRegistry`.

Compared to PR 2c's :class:`TemplateIdentifierResolver`, this resolver
layers two extra gates:

* A leading segment is recognised as a *dotted root* either because
  it appears in ``state.joined_entities[].name`` OR because it shows
  up as the leading-segment of any ``state.columns[].field`` dot-path.
  Dotted roots that aren't declared in ``joined_entities`` raise
  ``KeyError`` naming the undeclared join — context fallback is
  rejected for them.
* Declared joins that ARE in ``joined_entities`` are still gated by
  the registry: the resolver calls ``registry.link_exists(state.entity_type,
  joined.entity_type, joined.via_link)`` and raises if the registry
  says no.

Flat identifiers (those that aren't recognised as dotted roots by
either signal) fall through to plain context lookup — same loose
behaviour as the PR 2c reference resolver. This keeps free-variable
CEL helpers (e.g. ``within(field, duration)`` whose first argument is
a property name not declared as a column) working without false
positives.

This is the runtime-side enforcement. The lint-side complement is
:func:`validate_template_with_registry` in :mod:`fabric_validator` —
same Protocol, different call site, broader coverage (entity types,
column fields, every CEL expression on the template).
"""

from __future__ import annotations

from typing import Any

from pocketpaw.bundled_templates.fabric_registry import FabricRegistry
from pocketpaw.bundled_templates.schema import PocketTemplate, StateBinding


def _collect_column_dot_roots(state: StateBinding) -> frozenset[str]:
    """Leading-segment set extracted from ``state.columns[].field``
    entries that contain a dot. Used to recognise dotted-root usage
    even when the join isn't declared — that mismatch is the canonical
    ``tier: registered`` lint failure surfacing at runtime."""
    roots: set[str] = set()
    for col in state.columns:
        if "." in col.field:
            roots.add(col.field.split(".", 1)[0])
    return frozenset(roots)


class FabricResolver:
    """Strict :class:`IdentifierResolver` gated by a
    :class:`FabricRegistry`.

    Constructor accepts either a full :class:`PocketTemplate` or just
    its inner :class:`StateBinding` (the latter is convenient for the
    runtime composer that only carries the state block).

    Raises
    ------
    KeyError
        For every failure mode — undeclared join, unregistered link,
        missing context. The CEL evaluator catches ``KeyError`` and
        wraps it in a typed
        :class:`pocketpaw.bundled_templates.cel_runtime.CelEvaluationError`,
        so callers see consistent diagnostics regardless of resolver.

    Notes
    -----
    The registry is consulted on **every** dotted-root lookup. For
    long-running runtimes (the Instinct sweeper, the temporal trigger
    loop), implementations of :class:`FabricRegistry` should cache
    internally — this class deliberately does not cache because the
    registry may swap out under it (a workspace re-mount, an EE
    feature flag toggle).
    """

    __slots__ = ("_column_dot_roots", "_entity_type", "_joins_by_name", "_registry")

    def __init__(
        self,
        template_or_state: PocketTemplate | StateBinding,
        registry: FabricRegistry,
    ) -> None:
        state = (
            template_or_state.state
            if isinstance(template_or_state, PocketTemplate)
            else template_or_state
        )
        self._entity_type: str = state.entity_type
        # Index by name once — the resolver fires on every expression
        # evaluation, so the O(1) lookup matters under the temporal
        # sweeper / instinct loops.
        self._joins_by_name = {j.name: j for j in state.joined_entities}
        self._column_dot_roots = _collect_column_dot_roots(state)
        self._registry: FabricRegistry = registry

    def resolve(self, path: str, context: dict[str, Any]) -> Any:
        """Resolve a leftmost-segment identifier.

        Contract matches the
        :class:`~pocketpaw.bundled_templates.identifier_resolver.IdentifierResolver`
        Protocol so this class drops into ``evaluate_cel`` wherever
        :class:`TemplateIdentifierResolver` does.
        """
        # Defensive guard mirroring the reference resolver — a
        # misbehaving evaluator might pass a full dot-path; surface
        # the bug rather than silently doing the wrong thing.
        if "." in path:
            raise KeyError(
                f"FabricResolver expects leftmost segments, not full dot-paths; got {path!r}"
            )

        join = self._joins_by_name.get(path)
        if join is not None:
            # Declared-join case. Validate via the registry FIRST so
            # an unregistered link surfaces even when the row context
            # happens to carry the join inline (lint-time strictness
            # at runtime).
            if not self._registry.link_exists(self._entity_type, join.entity_type, join.via_link):
                raise KeyError(
                    f"via_link not registered: {join.via_link!r} on "
                    f"{self._entity_type}->{join.entity_type}"
                )
            if path not in context:
                raise KeyError(
                    f"declared joined entity {path!r} (via {join.via_link!r}) "
                    f"is missing from the row context"
                )
            return context[path]

        if path in self._column_dot_roots:
            # The template uses ``path`` as a dotted root somewhere
            # (a column field like ``vendor.name``), but never
            # declared a matching ``joined_entities`` entry. That's
            # the canonical ``tier: registered`` runtime miss.
            raise KeyError(
                f"undeclared join: {path!r} is used as a dotted root in "
                f"state.columns[] but is not declared in state.joined_entities[]"
            )

        # Flat case. Fall through to plain context lookup — same loose
        # behaviour as PR 2c's reference resolver. Keeps free-variable
        # CEL helpers working without false positives.
        if path not in context:
            raise KeyError(
                f"identifier {path!r} is not declared on the template and "
                f"is absent from the row context"
            )
        return context[path]


__all__ = [
    "FabricResolver",
]
