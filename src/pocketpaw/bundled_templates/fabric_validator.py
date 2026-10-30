# src/pocketpaw/bundled_templates/fabric_validator.py
# Created: 2026-05-28 (feat/rfc-03-v2-fabric) — lint-time
# ``tier: registered`` enforcement for RFC 03 v2 templates. Public entry
# point ``validate_template_with_registry`` collects every miss as a
# typed :class:`FabricValidationError` and returns them to the caller
# (CLI ``template lint`` in a follow-up PR) instead of raising — the
# caller renders all problems at once. Pure / referentially transparent
# function — no I/O, no module state. The runtime-side complement is
# :class:`fabric_resolver.FabricResolver`.
"""Lint-time enforcement of RFC 03 v2 ``tier: registered`` policies.

The validator answers two questions about a :class:`PocketTemplate`:

1. Does the registry know every entity type and every via_link the
   template references?
2. Does every dotted identifier in the template's CEL expressions and
   column fields trace back to a declared ``state.joined_entities[]``
   entry?

Templates that demand none of the above — no dot-paths in columns, no
joined entities, no dotted CEL roots — are treated as synthetic-tier
and pass through cleanly even against an empty / null registry.

Why return a list, not raise
----------------------------

A real lint surface (CLI, IDE, CI) wants to render every problem at
once. Raising on the first miss makes authors play whack-a-mole. The
:func:`validate_template_with_registry` function returns
``list[FabricValidationError]`` — empty on success — so the caller can
sort / group / link to docs and render the whole batch.

Severity surface
----------------

Every error currently ships ``severity='error'``. The
:class:`FabricValidationError` model carries the field so a future
extension (e.g. warning when a registered entity exists but a property
is unrecognised) can land without rewriting the contract.

Public surface
--------------

* :class:`FabricValidationError` — frozen Pydantic v2 model with
  ``message``, ``path``, ``severity``, and ``data``.
* :func:`validate_template_with_registry` — lint entry point.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from pocketpaw.bundled_templates.cel_runtime import collect_free_identifiers
from pocketpaw.bundled_templates.fabric_registry import FabricRegistry
from pocketpaw.bundled_templates.schema import PocketTemplate

Severity = Literal["error", "warning"]


class FabricValidationError(BaseModel):
    """One Fabric-tier lint finding.

    Frozen so callers can put findings in sets / sort keys without
    accidentally mutating them mid-render. ``data`` is a typed-loose
    dict to keep the schema additive — new payload fields (column
    index, expression source span, registry suggestion) can land
    without bumping the contract.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    message: str = Field(..., description="Human-readable lint message.")
    path: str = Field(
        ...,
        description="Dotted JSON-path into the template where the miss applies.",
    )
    severity: Severity = Field(default="error")
    data: dict[str, Any] = Field(default_factory=dict)


def validate_template_with_registry(
    template: PocketTemplate,
    registry: FabricRegistry,
) -> list[FabricValidationError]:
    """Return every Fabric-tier lint finding for ``template`` against
    ``registry``.

    Empty list = clean. The function never raises on a lint miss —
    only on programmer errors (e.g. passing in something that isn't a
    :class:`PocketTemplate`, which Pydantic will catch at the boundary
    of any caller that respects the type hint).
    """
    state = template.state
    errors: list[FabricValidationError] = []

    # ------------------------------------------------------------------
    # Step 1 — collect every CEL expression on the template AND identify
    # the leading segments each one uses. Walk once so the synthetic-tier
    # detection and the per-expression lint share the same data.
    # ------------------------------------------------------------------
    cel_sites = _collect_cel_sites(template)

    # Combine: any expression that uses a dotted root? Plus any column
    # that uses a dotted field. Plus any declared joined entity. Any of
    # these signals registered-tier intent.
    column_dot_roots: set[str] = {c.field.split(".", 1)[0] for c in state.columns if "." in c.field}
    declared_joins: set[str] = {j.name for j in state.joined_entities}
    cel_dot_roots: set[str] = set()
    for site in cel_sites:
        for ident in site.identifiers:
            # CEL identifiers are returned as leftmost segments only —
            # we can't tell from the identifier alone whether the
            # expression used it as a dot-root or as a flat name. But
            # combined with the structural signals (column dot-paths,
            # joined entities), we have enough to tag "registered-tier
            # intent."
            if ident in column_dot_roots or ident in declared_joins:
                cel_dot_roots.add(ident)

    uses_dot_paths = bool(column_dot_roots) or bool(declared_joins) or bool(cel_dot_roots)

    # ------------------------------------------------------------------
    # Step 2 — primary state.entity_type check.
    # Only fires for registered-tier templates so synthetic ones (no
    # dots, no joins) don't trip on a Null / empty registry.
    # ------------------------------------------------------------------
    if uses_dot_paths and not registry.entity_type_exists(state.entity_type):
        errors.append(
            FabricValidationError(
                message=(
                    f"unknown entity_type: {state.entity_type!r} is not "
                    f"registered in the Fabric registry"
                ),
                path="state.entity_type",
                severity="error",
                data={"entity_type": state.entity_type},
            )
        )

    # ------------------------------------------------------------------
    # Step 3 — joined_entities[]: entity_type known + via_link registered.
    # ------------------------------------------------------------------
    for i, je in enumerate(state.joined_entities):
        path_prefix = f"state.joined_entities[{i}]"
        if not registry.entity_type_exists(je.entity_type):
            errors.append(
                FabricValidationError(
                    message=(
                        f"unknown joined entity_type: {je.entity_type!r} on "
                        f"joined_entities[{i}] (name={je.name!r})"
                    ),
                    path=f"{path_prefix}.entity_type",
                    severity="error",
                    data={
                        "name": je.name,
                        "entity_type": je.entity_type,
                        "index": i,
                    },
                )
            )
        # via_link check fires independently — even if the entity_type
        # is known, the link between primary and join may not be.
        if not registry.link_exists(state.entity_type, je.entity_type, je.via_link):
            errors.append(
                FabricValidationError(
                    message=(
                        f"via_link not registered: {je.via_link!r} on "
                        f"{state.entity_type}->{je.entity_type} "
                        f"(joined_entities[{i}].name={je.name!r})"
                    ),
                    path=f"{path_prefix}.via_link",
                    severity="error",
                    data={
                        "name": je.name,
                        "via_link": je.via_link,
                        "from_type": state.entity_type,
                        "to_type": je.entity_type,
                        "index": i,
                    },
                )
            )

    # ------------------------------------------------------------------
    # Step 4 — column ``field`` dot-paths: leading segment must be in
    # ``joined_entities``.
    # ------------------------------------------------------------------
    for i, col in enumerate(state.columns):
        if "." not in col.field:
            continue
        leading = col.field.split(".", 1)[0]
        if leading not in declared_joins:
            errors.append(
                FabricValidationError(
                    message=(
                        f"undeclared dotted root {leading!r} in "
                        f"state.columns[{i}].field={col.field!r}; declare it "
                        f"in state.joined_entities[]"
                    ),
                    path=f"state.columns[{i}].field",
                    severity="error",
                    data={
                        "leading_segment": leading,
                        "field": col.field,
                        "index": i,
                    },
                )
            )

    # ------------------------------------------------------------------
    # Step 5 — every CEL expression on the template: each free identifier
    # that is NOT a declared join AND not present in the column-field
    # set could be either a flat field or an undeclared dotted root.
    # We use a conservative rule: emit a lint error only when the
    # identifier is *also* mentioned as a dotted root somewhere on the
    # template (i.e. either a column with ``ident.X`` OR a CEL expression
    # uses it as a dotted root by virtue of appearing in another column).
    # That cross-signal keeps false positives low for plain flat CEL
    # references like ``days_remaining``.
    #
    # In practice the validator surfaces *most* undeclared-root cases
    # via the column-field path (Step 4). The CEL pass is the safety
    # net for expressions like ``ghost.attribute`` where the dotted
    # usage lives only inside the expression.
    # ------------------------------------------------------------------
    flat_column_names = {c.field for c in state.columns if "." not in c.field}
    for site in cel_sites:
        for ident in site.identifiers:
            if ident in declared_joins:
                continue
            if ident in flat_column_names:
                continue
            # Heuristic for "ident is used as a dotted root in this
            # expression": parse the raw expression text for ``ident.``
            # — the CEL grammar guarantees the dot must directly follow
            # the identifier with no whitespace in member access.
            if f"{ident}." in site.expression:
                errors.append(
                    FabricValidationError(
                        message=(
                            f"undeclared dotted root {ident!r} used in "
                            f"{site.path}={site.expression!r}; declare it in "
                            f"state.joined_entities[]"
                        ),
                        path=site.path,
                        severity="error",
                        data={
                            "leading_segment": ident,
                            "expression": site.expression,
                        },
                    )
                )

    return errors


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


class _CelSite(BaseModel):
    """One CEL expression collected from the template, with its parsed
    identifier set and the dotted JSON-path back to its source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    expression: str
    path: str
    identifiers: tuple[str, ...]


def _collect_cel_sites(template: PocketTemplate) -> list[_CelSite]:
    """Walk every CEL-bearing field on the template and return one
    :class:`_CelSite` per expression. The order matches the JSON-path
    order so error rendering is deterministic."""
    sites: list[_CelSite] = []

    # state.columns[].filter
    for i, col in enumerate(template.state.columns):
        if col.filter is not None:
            sites.append(
                _CelSite(
                    expression=col.filter,
                    path=f"state.columns[{i}].filter",
                    identifiers=tuple(collect_free_identifiers(col.filter)),
                )
            )

    # state.saved_views[].filter
    for i, sv in enumerate(template.state.saved_views):
        if sv.filter is not None:
            sites.append(
                _CelSite(
                    expression=sv.filter,
                    path=f"state.saved_views[{i}].filter",
                    identifiers=tuple(collect_free_identifiers(sv.filter)),
                )
            )

    # triggers[].when, triggers[].filter
    for i, trig in enumerate(template.triggers):
        if trig.when is not None:
            sites.append(
                _CelSite(
                    expression=trig.when,
                    path=f"triggers[{i}].when",
                    identifiers=tuple(collect_free_identifiers(trig.when)),
                )
            )
        if trig.filter is not None:
            sites.append(
                _CelSite(
                    expression=trig.filter,
                    path=f"triggers[{i}].filter",
                    identifiers=tuple(collect_free_identifiers(trig.filter)),
                )
            )

    # instinct_rules.rules[].when
    if template.instinct_rules is not None:
        for i, rule in enumerate(template.instinct_rules.rules):
            sites.append(
                _CelSite(
                    expression=rule.when,
                    path=f"instinct_rules.rules[{i}].when",
                    identifiers=tuple(collect_free_identifiers(rule.when)),
                )
            )

    return sites


__all__ = [
    "FabricValidationError",
    "validate_template_with_registry",
]
