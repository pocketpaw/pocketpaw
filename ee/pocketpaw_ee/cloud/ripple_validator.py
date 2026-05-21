"""Write-time validator for AI-generated rippleSpec expressions.

Companion to ``ripple_normalizer``. Where the normalizer **repairs** common
shape mistakes (alias `bind` → `items`, lift root nodes into `ui`, etc.),
this module **inspects** every ``{…}`` template expression in the spec and
flags those the renderer's expression resolver can't parse.

Out of scope: actually evaluating expressions, blocking writes. The
validator returns a list of warnings the caller can:

  * log (default — see ``validate_ripple_spec_logged``),
  * surface to telemetry / Sentry for an LLM-quality dashboard,
  * use in a future strict mode to retry the LLM with a corrective prompt.

Stays decoupled from the spec walker in ``ripple_normalizer`` so failures
in one path don't poison the other. They're called as separate steps from
``service.create`` / ``service.update`` / ``service.create_from_ripple_spec``
when wired in (see the integration call sites for "validate"+"normalize"
ordering).

Grammar must mirror ``ripple/src/lib/core/expression-resolver.ts``. Any
new token / method added to the resolver should be added here too — the
two files together are the contract.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


# Mirrors the whitelist in ``expression-resolver.ts:applyMethod``.
# Anything outside this set is a fluent-API hallucination and will return
# ``undefined`` at runtime.
_KNOWN_METHODS: frozenset[str] = frozenset(
    {
        # string
        "toLowerCase",
        "toUpperCase",
        "trim",
        "includes",
        "startsWith",
        "endsWith",
        # array
        "join",
        "sum",
        "count",
        "first",
        "last",
        "reverse",
        "where",
        "whereIn",
        "sortBy",
        "limit",
        # number
        "toFixed",
    }
)

# JS-isms that frequently leak from LLM training data but the resolver
# doesn't support. Each key is an anchored regex; matches surface as
# specific warnings so the prompt-tightening pass (P2.A) has actionable
# patterns to forbid.
_FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("arrow function", re.compile(r"=>")),
    ("function keyword", re.compile(r"\bfunction\b")),
    ("await keyword", re.compile(r"\bawait\b")),
    ("for/while loop", re.compile(r"\b(for|while)\s*\(")),
    ("class keyword", re.compile(r"\bclass\b")),
    ("new operator", re.compile(r"\bnew\s+[A-Z]")),
    ("template literal (backtick)", re.compile(r"`")),
    ("spread operator", re.compile(r"\.\.\.[a-zA-Z_$]")),
    ("typeof", re.compile(r"\btypeof\b")),
    ("instanceof", re.compile(r"\binstanceof\b")),
)


@dataclass(frozen=True)
class ExpressionWarning:
    """A single grammar issue found inside a `{...}` template."""

    path: str  # JSON-pointer-style path to the offending field
    expression: str  # the raw expression text (without `{}` wrappers)
    code: str  # short stable code for telemetry grouping
    detail: str  # human-readable detail


# ---------------------------------------------------------------------------
# Spec walking
# ---------------------------------------------------------------------------


def _walk_strings(node: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """Yield ``(jsonpath, string-value)`` for every string in the spec.

    Strings inside ``state`` initial values are skipped — they're plain
    seed data, not expression-language strings, and would generate
    false-positive warnings (e.g. a literal sentence containing `{}`).
    """
    if isinstance(node, dict):
        for key, value in node.items():
            child_path = f"{path}.{key}" if path else key
            if key == "state" and path == "rippleSpec":
                continue  # initial state values are not expressions
            yield from _walk_strings(value, child_path)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from _walk_strings(item, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node


_TEMPLATE_RE = re.compile(r"\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}")


def _expressions_in(value: str) -> list[str]:
    """Extract every ``{…}`` template body from a string. Supports one
    level of nested braces (object / array literals) so that
    ``{state.x.where('field', 'val')}`` and
    ``{cond ? a : [{value:'x'}]}`` both come out as a single match.
    """
    return [m.group(1) for m in _TEMPLATE_RE.finditer(value)]


# ---------------------------------------------------------------------------
# Per-expression checks
# ---------------------------------------------------------------------------


def _is_balanced(expr: str) -> bool:
    """Returns True if `()`, `[]`, `{}` brackets balance, ignoring
    contents of single- and double-quoted string literals.
    """
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[str] = []
    in_str: str | None = None
    prev = ""
    for ch in expr:
        if in_str:
            if ch == in_str and prev != "\\":
                in_str = None
            prev = ch
            continue
        if ch in ('"', "'"):
            in_str = ch
            prev = ch
            continue
        if ch in "([{":
            stack.append(ch)
        elif ch in ")]}":
            if not stack or stack.pop() != pairs[ch]:
                return False
        prev = ch
    return not stack and in_str is None


_METHOD_CALL_RE = re.compile(r"\.([a-zA-Z_$][\w$]*)\s*\(")


def _check_methods(expr: str) -> Iterator[tuple[str, str]]:
    """Yield ``(code, detail)`` for any method call outside the whitelist."""
    for match in _METHOD_CALL_RE.finditer(expr):
        name = match.group(1)
        if name in _KNOWN_METHODS:
            continue
        # Heuristic: skip member access patterns where the dot is preceded
        # by a closing bracket of an array index (e.g. `state.r[0].name(` —
        # which itself is a non-method call). Treat all unknown names as
        # warnings; the resolver returns ``undefined`` for them anyway.
        yield (
            "unknown_method",
            (
                f"`.{name}(...)` is not a whitelisted method — resolver returns "
                f"undefined. Allowed: {', '.join(sorted(_KNOWN_METHODS))}."
            ),
        )


def _check_forbidden(expr: str) -> Iterator[tuple[str, str]]:
    for label, pattern in _FORBIDDEN_PATTERNS:
        if pattern.search(expr):
            yield "forbidden_syntax", f"unsupported JS syntax: {label}"


def _validate_one(path: str, expression: str) -> list[ExpressionWarning]:
    """Run every grammar check against a single ``{…}`` body."""
    warnings: list[ExpressionWarning] = []

    if not _is_balanced(expression):
        warnings.append(
            ExpressionWarning(
                path=path,
                expression=expression,
                code="unbalanced_brackets",
                detail="`(`, `[`, or `{` without a matching close",
            )
        )
        # Skip downstream checks — the regexes can mis-fire on broken
        # syntax. Reporting one grammar error per expression is enough.
        return warnings

    for code, detail in _check_methods(expression):
        warnings.append(
            ExpressionWarning(path=path, expression=expression, code=code, detail=detail)
        )

    for code, detail in _check_forbidden(expression):
        warnings.append(
            ExpressionWarning(path=path, expression=expression, code=code, detail=detail)
        )

    return warnings


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_ripple_spec(spec: dict[str, Any] | None) -> list[ExpressionWarning]:
    """Walk the spec and return one warning per offending expression.

    Empty list means the spec's expression grammar is fully supported by
    the current resolver. Does not block; never raises.
    """
    if not isinstance(spec, dict):
        return []
    out: list[ExpressionWarning] = []
    # Anchor the walk at ``rippleSpec`` so the `state` skip rule fires.
    for path, value in _walk_strings({"rippleSpec": spec}, ""):
        for expr in _expressions_in(value):
            out.extend(_validate_one(path, expr))
    return out


def validate_ripple_spec_logged(
    spec: dict[str, Any] | None,
    *,
    pocket_id: str | None = None,
    workspace_id: str | None = None,
) -> list[ExpressionWarning]:
    """Validate + emit one ``log.warning`` per finding.

    Logs include ``pocket_id`` and ``workspace_id`` so a downstream log
    pipeline can group by tenant / pocket without parsing the message.
    Returns the same warnings list ``validate_ripple_spec`` would.
    """
    warnings = validate_ripple_spec(spec)
    for w in warnings:
        log.warning(
            "ripple_spec.invalid_expression",
            extra={
                "pocket_id": pocket_id,
                "workspace_id": workspace_id,
                "field_path": w.path,
                "expression": w.expression,
                "code": w.code,
                "detail": w.detail,
            },
        )
    return warnings


class RippleSpecGrammarError(Exception):
    """Raised by :func:`validate_ripple_spec_strict` when the spec
    contains expressions outside the supported grammar.

    Carries the full warnings list so callers (e.g. an LLM-retry loop)
    can build an actionable corrective prompt — file path, expression
    text, and reason are all on the warning, not just the message.
    """

    def __init__(self, warnings: list[ExpressionWarning]) -> None:
        self.warnings = warnings
        super().__init__(self._format(warnings))

    @staticmethod
    def _format(warnings: list[ExpressionWarning]) -> str:
        lines = [f"{len(warnings)} grammar violation(s) in rippleSpec:"]
        for w in warnings[:20]:  # cap the message length
            lines.append(f"  - [{w.code}] {w.path}: {w.detail}\n      expr: {w.expression!r}")
        if len(warnings) > 20:
            lines.append(f"  - …and {len(warnings) - 20} more")
        return "\n".join(lines)


def validate_ripple_spec_strict(
    spec: dict[str, Any] | None,
    *,
    pocket_id: str | None = None,
    workspace_id: str | None = None,
) -> None:
    """Validate the spec; raise :class:`RippleSpecGrammarError` if any
    expression is outside the resolver's supported grammar.

    Use this only on the LLM-generation path where the caller can
    handle a retry. Callers that only want to record the issue should
    use :func:`validate_ripple_spec_logged` instead — strict mode would
    block legitimate writes from older specs that still parse but use
    deprecated forms.
    """
    warnings = validate_ripple_spec_logged(spec, pocket_id=pocket_id, workspace_id=workspace_id)
    if warnings:
        raise RippleSpecGrammarError(warnings)


def format_warnings_for_agent(warnings: list[ExpressionWarning]) -> str:
    """Build a compact, agent-readable summary of the warnings.

    Suitable as the ``error`` field on an MCP tool result so the LLM
    sees specifically *why* its spec was rejected and can target its
    fix at the offending expression.
    """
    if not warnings:
        return ""
    lines = ["The rippleSpec was persisted but contains unsupported expressions:"]
    for w in warnings[:10]:
        lines.append(f"  • {w.path}: {w.detail}")
        lines.append(f"      expression: {w.expression}")
    if len(warnings) > 10:
        lines.append(f"  • …and {len(warnings) - 10} more")
    lines.append(
        "Fix each expression to use only the supported grammar — paths, "
        "ternaries, comparisons, the whitelisted method calls "
        "(.where / .sortBy / .limit / etc.), and inline literals. "
        "No arrow functions, .map / .filter / .find, or other JS syntax."
    )
    return "\n".join(lines)


__all__ = [
    "ExpressionWarning",
    "RippleSpecGrammarError",
    "format_warnings_for_agent",
    "validate_ripple_spec",
    "validate_ripple_spec_logged",
    "validate_ripple_spec_strict",
]
