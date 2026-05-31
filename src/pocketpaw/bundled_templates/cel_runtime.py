# src/pocketpaw/bundled_templates/cel_runtime.py
# Created: 2026-05-28 (feat/rfc-03-v2-cel-eval) — runtime CEL evaluator
# that pairs with the parse-only ``expressions.CelExpression`` field
# already on the chokepoint. Foundation for PR 2d (Instinct 5-step
# composer), PR 2f (temporal trigger sweeper), and PR 2g (Fabric
# ``tier: registered`` enforcement). Pure library function — OSS-side,
# no ``pocketpaw_ee`` imports, no I/O, deterministic given a fixed
# ``now``.
"""Runtime evaluator for the RFC 03 v2 CEL expression grammar.

The schema chokepoint (``expressions.CelExpression``) parses every
``filter`` / ``when`` field on a template at validation time. It does
*not* evaluate — there is no row context at validation time. This
module is the runtime side: given a parsed expression, an
:class:`IdentifierResolver`, and a row context, it returns the
evaluated value.

Design notes
------------

* Pure / referentially transparent given the same ``now``. No I/O,
  no global mutable state.
* The custom ``within(field, duration)`` function is the only CEL
  function the runtime registers on top of the celpy built-ins. Per
  RFC 03 v2 ("Expression grammar"), no other custom functions ship.
* ``now`` is injected per-call so tests can be deterministic; the
  default uses ``datetime.now(UTC)``. Production callers can override
  this once a workspace-wide clock abstraction lands.
* Identifier resolution flows through the
  :class:`~pocketpaw.bundled_templates.identifier_resolver.IdentifierResolver`
  Protocol. We pre-walk the parsed AST for top-level free identifiers
  and resolve each one *before* handing the activation to celpy — that
  way an undeclared joined-entity root (a Fabric ``tier: registered``
  miss) surfaces as a typed :class:`CelEvaluationError` instead of an
  opaque ``CELEvalError`` from inside celpy.
* All failure modes raise :class:`CelEvaluationError`. The chokepoint
  loader has its own typed error
  (:class:`pocketpaw.bundled_templates.errors.TemplateValidationError`)
  for *parse*-time issues; this class covers *eval*-time issues. They
  are deliberately separate — a caller catching parse errors should
  not silently swallow eval errors.

Out of scope for PR 2c
----------------------

* Fabric ``via_link`` enforcement (PR 2g).
* Instinct 5-step execution (PR 2d) — that consumes this evaluator but
  is not built here.
* Temporal trigger sweeper rising-edge state (PR 2f).
* Any CLI surface change (e.g. extending ``template lint`` to evaluate
  against a synthetic context).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pocketpaw.bundled_templates.identifier_resolver import IdentifierResolver


class CelEvaluationError(Exception):
    """Raised when :func:`evaluate_cel` cannot produce a value.

    Wraps both parse-time and eval-time failures with a single typed
    exception so callers (Instinct composer, temporal sweeper, future
    linters) can ``except CelEvaluationError`` without reaching into
    celpy's internal hierarchy.

    Attributes
    ----------
    expression:
        The original expression text.
    cause:
        The underlying exception, if any.
    """

    def __init__(
        self,
        message: str,
        *,
        expression: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.expression = expression
        self.cause = cause


def evaluate_cel(
    expression: str,
    context: dict[str, Any],
    resolver: IdentifierResolver,
    *,
    now: datetime | None = None,
) -> Any:
    """Evaluate a CEL ``expression`` against a row ``context``.

    Parameters
    ----------
    expression:
        Raw CEL text. Re-parsed here on every call — the chokepoint's
        parse-side cache is keyed off the Pydantic model so we cannot
        rely on it. Cheap (lark is fast) and keeps this function
        stateless.
    context:
        Per-row dict — e.g. ``{"days_remaining": 25, "tenant":
        {"late_payment_count_12mo": 4}}``. Keys are CEL identifier
        names; values are plain Python (the evaluator converts to
        celpy types via :func:`celpy.json_to_cel`).
    resolver:
        Implements :class:`IdentifierResolver`. The evaluator pulls
        the leftmost segment of every free identifier from the
        parsed AST and asks the resolver to validate + supply it.
        ``KeyError`` from the resolver becomes
        :class:`CelEvaluationError` carrying the identifier name.
    now:
        Injected wall-clock time. Used by the custom ``within(field,
        duration)`` function. Defaults to :func:`datetime.now` in UTC.
        Tests should pass a fixed value to stay deterministic.

    Returns
    -------
    The Python-typed result: ``bool`` for boolean expressions, ``int``
    for integers, ``float`` for doubles, ``str`` for strings, ``None``
    for null. CEL list / map results are returned as plain Python
    ``list`` / ``dict``.

    Raises
    ------
    CelEvaluationError:
        Any failure mode. The original celpy exception is exposed on
        the ``.cause`` attribute for callers that want richer
        diagnostics.
    """
    # Lazy import: matches the chokepoint pattern (``expressions.py``)
    # and keeps celpy off the module-load hot path. The runtime only
    # imports it once per process — Python caches the import — so the
    # per-call cost is negligible.
    import celpy  # noqa: PLC0415 — lazy import by design
    from celpy import celtypes  # noqa: PLC0415
    from celpy.evaluation import CELEvalError  # noqa: PLC0415

    if now is None:
        now = datetime.now(UTC)

    env = celpy.Environment()

    # Parse → AST. Surface parse errors as CelEvaluationError so the
    # caller never sees raw ``CELParseError`` types leaking from
    # celpy's internals.
    try:
        ast = env.compile(expression)
    except Exception as exc:  # noqa: BLE001 — celpy raises a wide hierarchy
        raise CelEvaluationError(
            f"failed to parse CEL expression: {exc}",
            expression=expression,
            cause=exc,
        ) from exc

    # Walk the AST for top-level free identifiers, gate each one
    # through the resolver, and build the celpy activation. We resolve
    # via the protocol so dotted roots that don't match a declared
    # joined entity raise immediately (PR 2g will plug a richer
    # resolver in here without changing this code path).
    free_idents = _collect_free_identifiers(ast)
    activation: dict[str, Any] = {}
    for ident in free_idents:
        try:
            value = resolver.resolve(ident, context)
        except KeyError as exc:
            raise CelEvaluationError(
                f"identifier {ident!r} could not be resolved: {exc}",
                expression=expression,
                cause=exc,
            ) from exc
        activation[ident] = celpy.json_to_cel(value)

    # Register the one custom function the v2 schema exposes. ``now``
    # is captured in the closure so callers control determinism.
    def within(field: Any, duration: Any) -> Any:
        now_ts = celtypes.TimestampType(now)
        return celtypes.BoolType((now_ts - duration) <= field <= (now_ts + duration))

    try:
        program = env.program(ast, functions={"within": within})
    except Exception as exc:  # noqa: BLE001
        raise CelEvaluationError(
            f"failed to compile CEL program: {exc}",
            expression=expression,
            cause=exc,
        ) from exc

    try:
        result = program.evaluate(activation)
    except CELEvalError as exc:
        # celpy stuffs a useful structured payload into the args
        # tuple; surface it inline so authors can debug without
        # decoding ``exc.args``.
        raise CelEvaluationError(
            f"CEL evaluation failed: {exc}",
            expression=expression,
            cause=exc,
        ) from exc
    except Exception as exc:  # noqa: BLE001 — defensive belt-and-braces
        raise CelEvaluationError(
            f"unexpected error during CEL evaluation: {exc}",
            expression=expression,
            cause=exc,
        ) from exc

    return _to_python(result)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _collect_free_identifiers(ast: Any) -> list[str]:
    """Walk a celpy / lark AST and return the leftmost-segment
    identifiers in source order, deduplicated.

    We pick up identifiers from ``Tree('ident')`` nodes — those are
    the standalone variable references the CEL grammar emits. Member
    access (``foo.bar.baz``) shows up as a tree of ``member_dot`` /
    ``member`` nodes whose leftmost child eventually hits a ``Tree
    ('ident')``, so the same walk catches dotted-path roots without
    duplicating them.
    """
    seen: set[str] = set()
    found: list[str] = []

    def walk(node: Any) -> None:
        # Tokens are leaves; trees have ``data`` + ``children``.
        if hasattr(node, "data"):
            if node.data == "ident":
                # ``Tree('ident', [Token('IDENT', 'name')])``.
                for child in node.children:
                    name = getattr(child, "value", None) or str(child)
                    if name not in seen:
                        seen.add(name)
                        found.append(name)
                # No need to recurse further inside an ``ident`` node.
                return
            for child in node.children:
                walk(child)

    walk(ast)
    return found


def _to_python(value: Any) -> Any:
    """Convert a celpy result back to a plain Python value.

    The runtime contract promises Python-typed results so downstream
    code (the Instinct composer, the temporal sweeper, the policy
    linter) doesn't have to import celpy types just to read a boolean.
    """
    # Lazy import — same rationale as in ``evaluate_cel``.
    from celpy import celtypes  # noqa: PLC0415

    if value is None:
        return None
    if isinstance(value, celtypes.BoolType):
        return bool(value)
    if isinstance(value, celtypes.IntType | celtypes.UintType):
        return int(value)
    if isinstance(value, celtypes.DoubleType):
        return float(value)
    if isinstance(value, celtypes.StringType):
        return str(value)
    if isinstance(value, celtypes.ListType):
        return [_to_python(v) for v in value]
    if isinstance(value, celtypes.MapType):
        return {_to_python(k): _to_python(v) for k, v in value.items()}
    # TimestampType, DurationType, BytesType: pass through. Callers
    # that need them will recognise the celpy type.
    return value


def collect_free_identifiers(expression: str) -> list[str]:
    """Parse ``expression`` as CEL and return its leftmost-segment
    free identifiers in source order, deduplicated.

    Public helper for lint-side consumers (PR 2g's
    :func:`fabric_validator.validate_template_with_registry`) that need
    the same identifier set the evaluator walks at runtime — without
    having to import any celpy types or duplicate the AST walker.

    Returns an empty list for expressions that fail to parse; the
    schema chokepoint (:class:`pocketpaw.bundled_templates.expressions.CelExpression`)
    already rejects malformed CEL at validation time, so a parse error
    here means a caller has fed in an unvalidated string and shouldn't
    block validation on that case.
    """
    # Lazy import — same rationale as in ``evaluate_cel``.
    import celpy  # noqa: PLC0415

    env = celpy.Environment()
    try:
        ast = env.compile(expression)
    except Exception:  # noqa: BLE001 — celpy raises a wide hierarchy
        return []
    return _collect_free_identifiers(ast)


__all__ = [
    "CelEvaluationError",
    "collect_free_identifiers",
    "evaluate_cel",
]
