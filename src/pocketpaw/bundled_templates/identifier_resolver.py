# src/pocketpaw/bundled_templates/identifier_resolver.py
# Created: 2026-05-28 (feat/rfc-03-v2-cel-eval) — Protocol the CEL
# runtime evaluator (``cel_runtime.evaluate_cel``) uses to look up
# top-level free identifiers from an expression. The reference
# implementation (``TemplateIdentifierResolver``) gates dotted paths
# against the template's declared ``state.joined_entities[]`` so
# undeclared joins fail loudly instead of silently passing.
"""Identifier resolution for CEL evaluation.

The RFC 03 v2 expression grammar treats free identifiers in CEL as
references to:

* **Flat names** — properties of the primary entity (also surfaced as
  ``state.columns[].field`` entries with no dot).
* **Dot-paths** — properties of a joined entity reached through a
  declared ``state.joined_entities[].via_link`` FabricLink.

The runtime evaluator never reaches into either source directly. It
asks a resolver that implements :class:`IdentifierResolver`. This keeps
the evaluator pure and lets future PRs swap in richer implementations
(e.g. a ``FabricResolver`` in PR 2g that actually walks a registered
FabricLink when the row context doesn't carry the joined entity inline).

This PR ships the Protocol + a single reference implementation that
resolves against the in-memory row context only.

Fabric live-link traversal is intentionally out of scope here. The
reference resolver's docstring repeats that boundary.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pocketpaw.bundled_templates.schema import PocketTemplate, StateBinding


@runtime_checkable
class IdentifierResolver(Protocol):
    """Look up a top-level identifier from a CEL expression.

    Implementations decide what counts as a legitimate identifier; the
    reference impl gates dotted roots against the template's declared
    joined entities, but a future Fabric-backed implementation may
    accept any registered link and lazy-load the joined row.

    The contract is intentionally minimal:

    * ``path`` is the *leftmost segment* of the identifier the
      evaluator pulled from the parsed CEL AST. For a free identifier
      ``days_remaining`` that is the whole name. For a dotted path
      ``tenant.late_payment_count_12mo`` that is just ``"tenant"``;
      CEL's evaluator walks the rest of the path against whatever
      value the resolver returns.
    * ``context`` is the per-row dict the caller passed to
      :func:`pocketpaw.bundled_templates.cel_runtime.evaluate_cel`.
    * On success: return the looked-up value. The evaluator wraps it
      in a celpy type via ``celpy.json_to_cel``.
    * On failure: raise :class:`KeyError`. The evaluator catches
      ``KeyError`` and surfaces it as a typed
      :class:`pocketpaw.bundled_templates.cel_runtime.CelEvaluationError`.
    """

    def resolve(self, path: str, context: dict[str, Any]) -> Any:
        """Resolve ``path`` against ``context``; raise ``KeyError`` if it
        cannot be resolved."""
        ...


class TemplateIdentifierResolver:
    """Reference :class:`IdentifierResolver` keyed off a
    :class:`PocketTemplate`.

    Resolution rules:

    * **Flat names** (no leading segment / no dot): look up ``path`` in
      ``context``. Raise ``KeyError`` if absent.
    * **Dotted roots** (the leftmost segment of a dot-path, passed in
      as ``path`` by the evaluator): the segment must match a
      ``state.joined_entities[].name`` declared on the template *and*
      the row context must carry the joined value inline. Both checks
      raise ``KeyError`` on miss.

    What this resolver does **not** do (out of scope for PR 2c):

    * It does not call into Fabric to fetch a joined row when the
      context lacks it inline. PR 2g adds a ``FabricResolver`` that
      walks ``via_link`` against a live :class:`FabricLinkRegistry`.
    * It does not enforce that the primary entity is
      ``tier: registered``. PR 2g adds the linter that pairs with
      that policy.

    The constructor accepts either a full :class:`PocketTemplate` or
    just its inner :class:`StateBinding` — handy when the caller has
    only the inner block (the runtime composer in PR 2d, for example).
    """

    __slots__ = ("_declared_join_names",)

    def __init__(self, template_or_state: PocketTemplate | StateBinding) -> None:
        if isinstance(template_or_state, PocketTemplate):
            state = template_or_state.state
        else:
            state = template_or_state
        self._declared_join_names: frozenset[str] = frozenset(j.name for j in state.joined_entities)

    def resolve(self, path: str, context: dict[str, Any]) -> Any:
        """Resolve a leftmost-segment identifier against the per-row
        ``context``. See class docstring for rules."""
        # The evaluator hands us only the *leftmost* segment by
        # construction — so ``path`` itself never carries a dot.
        if "." in path:
            # Defensive: a misbehaving evaluator might pass a full
            # path. Reject so the bug surfaces immediately rather
            # than silently doing the wrong thing.
            raise KeyError(
                f"TemplateIdentifierResolver expects leftmost segments, "
                f"not full dot-paths; got {path!r}"
            )

        if path in self._declared_join_names:
            # Dotted-root case. Drill into the row context. The
            # evaluator handles the rest of the path natively via CEL
            # member-access semantics.
            if path not in context:
                raise KeyError(
                    f"identifier {path!r} is a declared joined entity but "
                    f"is missing from the row context"
                )
            return context[path]

        # Flat case. Pure context lookup. We deliberately don't accept
        # an undeclared dotted root even if it happens to be in the
        # context — see the rationale in the class docstring.
        if path not in context:
            raise KeyError(
                f"identifier {path!r} is not declared on the template "
                f"and is not present in the row context"
            )
        return context[path]


__all__ = [
    "IdentifierResolver",
    "TemplateIdentifierResolver",
]
