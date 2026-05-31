# src/pocketpaw/bundled_templates/expressions.py
# Created: 2026-05-25 (feat/rfc-03-v2-schema-chokepoint) — Pydantic v2
# field type for CEL expressions. Parses with ``celpy.CELParser`` at
# validation time; never evaluates. A malformed expression raises a
# ValueError which Pydantic wraps into ValidationError.
"""CEL (Common Expression Language) typed field for Pydantic models.

The RFC 03 v2 schema declares that ``saved_views[].filter``,
``columns[].filter``, ``triggers[].filter`` / ``.when``, and
``instinct_rules.rules[].when`` are CEL expressions. The chokepoint
treats them as **parse-at-validation, no evaluation** — the schema
loader confirms each expression is syntactically valid CEL but never
runs it. Evaluation happens later, in the runtime, against the row /
event context.

Why no evaluation here:

* The schema model has no row / workspace context to bind identifiers
  to. Evaluation would force a fake context that drifts.
* Parse-failure surfaces author errors at lint time (CLI ``template
  lint`` -> RFC 03 v2 section "Style and tooling notes").
* Custom functions (``within(field, duration)``) and runtime helpers
  (``now()``) are not registered in this layer; CEL accepts unknown
  identifiers as free variables at parse time, so the syntax check
  still works.

Usage from a Pydantic v2 model::

    from typing import Annotated
    from pocketpaw.bundled_templates.expressions import CelExpression

    class SavedView(BaseModel):
        filter: CelExpression  # parses on validation; raises ValueError
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BeforeValidator

# Local lazy import so the module imports even if celpy is absent at
# import time. The validator function imports celpy on first call. This
# matches the loader.py pattern of keeping optional / heavy imports off
# the hot module-load path.
_PARSER = None


def _get_parser() -> Any:
    """Return a shared ``celpy.CELParser`` instance, importing celpy
    lazily on first call."""
    global _PARSER
    if _PARSER is None:
        import celpy  # noqa: PLC0415 — lazy import by design

        _PARSER = celpy.CELParser()
    return _PARSER


def _validate_cel(value: Any) -> str:
    """Pydantic BeforeValidator: ensure the value is a string and parses
    as CEL. Returns the original string so the model carries the raw
    expression text (the runtime evaluator parses it again with a real
    context)."""
    if not isinstance(value, str):
        raise ValueError(f"CEL expression must be a string, got {type(value).__name__}")

    # Reject the empty string explicitly — celpy parses "" as Tree([]),
    # which would be a silent author mistake.
    if not value.strip():
        raise ValueError("CEL expression must not be empty")

    parser = _get_parser()
    try:
        parser.parse(value)
    except Exception as exc:  # noqa: BLE001 — celpy raises a wide error hierarchy
        # Surface as ValueError so Pydantic wraps it into ValidationError.
        raise ValueError(f"invalid CEL expression: {exc}") from exc
    return value


# Pydantic v2 typed alias. Annotated[str, BeforeValidator(...)] means
# Pydantic runs the validator on the raw input before any type coercion
# and the model stores the original string. Re-exportable as a Pydantic
# field annotation.
CelExpression = Annotated[str, BeforeValidator(_validate_cel)]


__all__ = ["CelExpression"]
