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

Changes:
  - 2026-05-30 (issue #1301): added ``find_unreferenced_state_keys`` —
    a pure ui-walk collector (mirrors ``find_unwired_live_buttons``) that
    returns top-level ``state`` keys no ui node references. Closes the
    silent-orphan-state gap where an add-widget intent landing as a
    state-only merge patch persisted with nothing rendering it. Used by
    ``pockets/service.merge_spec`` to emit a NON-BLOCKING warning.
  - 2026-05-22 (Increment 5): added the catalog-as-allowlist gate —
    ``CatalogViolationError`` plus ``validate_against_catalog_strict``
    (agent-generation path, RAISES) and ``..._logged`` (human/import
    path, structured warn, does not block). Mirrors the
    ``validate_ripple_spec_strict`` / ``..._logged`` split. The catalog
    walk + the embed URL/host policy live in OSS ``pocketpaw.ripple.manifest``;
    this module supplies the EE-side strict/logged wiring + the
    agent-readable error formatting.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from pocketpaw.ripple.manifest import (
    check_embed_nodes_in_spec,
    find_unwired_live_buttons,
    validate_action_verbs,
    validate_against_catalog,
)

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


# ---------------------------------------------------------------------------
# Catalog-as-allowlist gate (Increment 5)
#
# A node whose ``type`` is not in the widget manifest renders as a red
# "Unknown widget type" box. A node whose ``embed`` URL violates the host
# policy is an SSRF / clickjacking boundary. Both checks are run together
# here: strict (raises) on the agent-generation path, logged (warns) on
# the human / import path. The pure walks live in OSS
# ``pocketpaw.ripple.manifest``; this layer adds the EE wiring.
# ---------------------------------------------------------------------------


class CatalogViolationError(Exception):
    """Raised by :func:`validate_against_catalog_strict` when a spec
    contains a node outside the widget catalog allow-list, or an
    ``embed`` node whose URL violates the host policy.

    Carries the full violations list so a caller (e.g. an LLM-retry
    loop) can build an actionable corrective prompt. Mirrors
    :class:`RippleSpecGrammarError`'s shape — ``.violations`` plus a
    ``format_violations_for_agent`` helper.
    """

    def __init__(self, violations: list[dict[str, Any]]) -> None:
        self.violations = violations
        super().__init__(self._format(violations))

    @staticmethod
    def _format(violations: list[dict[str, Any]]) -> str:
        lines = [f"{len(violations)} catalog violation(s) in rippleSpec:"]
        for v in violations[:20]:  # cap the message length
            if "reason" in v:
                lines.append(f"  - [embed] {v['path']}: {v['reason']}\n      url: {v.get('url')!r}")
            else:
                hint = f" — did you mean '{v['suggestion']}'?" if v.get("suggestion") else ""
                lines.append(f"  - [unknown_type] {v['path']}: type '{v['type']}'{hint}")
        if len(violations) > 20:
            lines.append(f"  - …and {len(violations) - 20} more")
        return "\n".join(lines)


def _collect_catalog_violations(
    spec: dict[str, Any] | None,
    allowed_types: list[str] | set[str],
    embed_allowed_hosts: list[str] | set[str],
) -> list[dict[str, Any]]:
    """Run both the catalog-type walk and the embed URL/host walk, return
    the combined violations list (unknown-type issues first, then embed
    policy issues)."""
    violations: list[dict[str, Any]] = []
    violations.extend(validate_against_catalog(spec, allowed_types))
    violations.extend(check_embed_nodes_in_spec(spec, embed_allowed_hosts))
    return violations


def format_violations_for_agent(violations: list[dict[str, Any]]) -> str:
    """Build a compact, agent-readable summary of catalog violations.

    Suitable as the ``error`` field on an MCP tool result so the LLM
    sees specifically *which* node type / embed URL was rejected and can
    target its fix.
    """
    if not violations:
        return ""
    lines = ["The rippleSpec was rejected — it contains nodes outside the widget catalog:"]
    for v in violations[:10]:
        if "reason" in v:
            lines.append(f"  • {v['path']}: embed url rejected — {v['reason']}")
        else:
            hint = f" Use '{v['suggestion']}' instead." if v.get("suggestion") else ""
            lines.append(f"  • {v['path']}: type '{v['type']}' is not a catalog widget.{hint}")
    if len(violations) > 10:
        lines.append(f"  • …and {len(violations) - 10} more")
    lines.append(
        "Every node `type` must appear verbatim in the widget catalog. For content "
        "the catalog can't express, use the sanctioned `embed` widget — its `url` "
        "must be https and point at an allow-listed host."
    )
    return "\n".join(lines)


def validate_against_catalog_logged(
    spec: dict[str, Any] | None,
    allowed_types: list[str] | set[str],
    *,
    embed_allowed_hosts: list[str] | set[str],
    pocket_id: str | None = None,
    workspace_id: str | None = None,
) -> list[dict[str, Any]]:
    """Validate the catalog allow-list + embed policy, emit one
    ``log.warning`` per violation, and return the violations list.

    Use this on the human / import path — a violation is recorded for
    triage but does NOT block the write (an older imported spec may use
    a widget that has since left the catalog).
    """
    violations = _collect_catalog_violations(spec, allowed_types, embed_allowed_hosts)
    for v in violations:
        if "reason" in v:
            log.warning(
                "ripple_spec.embed_policy_violation",
                extra={
                    "pocket_id": pocket_id,
                    "workspace_id": workspace_id,
                    "field_path": v["path"],
                    "embed_url": v.get("url"),
                    "detail": v["reason"],
                },
            )
        else:
            log.warning(
                "ripple_spec.unknown_widget_type",
                extra={
                    "pocket_id": pocket_id,
                    "workspace_id": workspace_id,
                    "field_path": v["path"],
                    "widget_type": v["type"],
                    "suggestion": v.get("suggestion"),
                },
            )
    return violations


def validate_against_catalog_strict(
    spec: dict[str, Any] | None,
    allowed_types: list[str] | set[str],
    *,
    embed_allowed_hosts: list[str] | set[str],
    pocket_id: str | None = None,
    workspace_id: str | None = None,
) -> None:
    """Validate the catalog allow-list + embed policy; raise
    :class:`CatalogViolationError` if any node is outside the catalog or
    any ``embed`` URL violates the host policy.

    Use this only on the agent-generation path where the caller can
    handle a retry. Human / import callers should use
    :func:`validate_against_catalog_logged` — strict mode would block a
    legitimate import of an older spec.
    """
    violations = validate_against_catalog_logged(
        spec,
        allowed_types,
        embed_allowed_hosts=embed_allowed_hosts,
        pocket_id=pocket_id,
        workspace_id=workspace_id,
    )
    if violations:
        raise CatalogViolationError(violations)


# ---------------------------------------------------------------------------
# Action-verb + unwired-live-button gate (2026-05-23).
#
# Closes a class of LLM "loophole" failures the prompt-side rule in
# PR #1194 couldn't catch: the specialist authoring buttons with
# fictitious action verbs (``action: "fetch"``) or live-labelled
# Refresh buttons whose on_click is empty / inert. The pure walkers
# live in OSS ``pocketpaw.ripple.manifest``; this layer adds the
# strict / logged wiring + the agent-readable error formatting.
# ---------------------------------------------------------------------------


class ActionWiringViolationError(Exception):
    """Raised by :func:`validate_action_wiring_strict` when a spec
    carries an event handler with an unknown action verb or a
    live-labelled button whose on_click is empty / inert.

    Same shape as :class:`CatalogViolationError`: ``.violations`` plus
    a class-level formatter so the chat agent's retry loop can surface
    a focused corrective hint.
    """

    def __init__(self, violations: list[dict[str, Any]]) -> None:
        self.violations = violations
        super().__init__(self._format(violations))

    @staticmethod
    def _format(violations: list[dict[str, Any]]) -> str:
        lines = [f"{len(violations)} action-wiring violation(s) in rippleSpec:"]
        for v in violations[:20]:
            if "action" in v:
                hint = f" — did you mean '{v['suggestion']}'?" if v.get("suggestion") else ""
                lines.append(
                    f"  - [unknown_verb] {v['path']}: action '{v['action']}' "
                    f"is not a recognized verb{hint}"
                )
            else:
                lines.append(
                    f"  - [unwired_live_button] {v['path']} (label={v.get('label')!r}): "
                    f"{v['reason']}"
                )
        if len(violations) > 20:
            lines.append(f"  - …and {len(violations) - 20} more")
        return "\n".join(lines)


def _collect_action_wiring_violations(spec: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Run both action-wiring walks, return the combined list.

    Unknown-verb issues first (cheaper to fix, also catch the inert
    on_click failure mode at the same site), then unwired-button
    issues. Deduplication by ``path`` so a verb violation on the same
    handler that also fails the live-button check isn't reported
    twice.
    """
    violations: list[dict[str, Any]] = []
    violations.extend(validate_action_verbs(spec))
    seen_paths = {v["path"] for v in violations}
    for v in find_unwired_live_buttons(spec):
        if v["path"] not in seen_paths:
            violations.append(v)
    return violations


def format_action_violations_for_agent(violations: list[dict[str, Any]]) -> str:
    """Build a compact, agent-readable summary of action-wiring violations.

    Suitable as the ``error`` field on an MCP tool result so the
    specialist sees specifically which handler / button failed and
    can target its fix.
    """
    if not violations:
        return ""
    lines = ["The rippleSpec was rejected — event-handler wiring is broken:"]
    for v in violations[:10]:
        if "action" in v:
            hint = (
                f" Use '{v['suggestion']}' instead."
                if v.get("suggestion")
                else " Use one of: set, push, run_source, api, navigate, toast, emit."
            )
            lines.append(f"  • {v['path']}: action '{v['action']}' is not a recognized verb.{hint}")
        else:
            lines.append(f"  • {v['path']} (label={v.get('label')!r}): {v['reason']}")
    if len(violations) > 10:
        lines.append(f"  • …and {len(violations) - 10} more")
    lines.append(
        "Action verbs are a closed set — see ripple/event-dispatcher.ts. A button "
        "labelled 'Refresh' / 'Sync' / 'Fetch' must call `run_source` with a "
        "declared `sources` key OR `api` with a real `url`. An invented verb "
        '(e.g. `action: "fetch"`) silently no-ops at runtime, which looks '
        "like a working button but does nothing."
    )
    return "\n".join(lines)


def validate_action_wiring_logged(
    spec: dict[str, Any] | None,
    *,
    pocket_id: str | None = None,
    workspace_id: str | None = None,
) -> list[dict[str, Any]]:
    """Validate action-handler wiring, emit one ``log.warning`` per
    violation, and return the violations list.

    Use this on the human / import path — a violation is recorded for
    triage but does NOT block the write (an older imported spec may
    have wiring that's since become inert).
    """
    violations = _collect_action_wiring_violations(spec)
    for v in violations:
        if "action" in v:
            log.warning(
                "ripple_spec.unknown_action_verb",
                extra={
                    "pocket_id": pocket_id,
                    "workspace_id": workspace_id,
                    "field_path": v["path"],
                    "action": v["action"],
                    "suggestion": v.get("suggestion"),
                },
            )
        else:
            log.warning(
                "ripple_spec.unwired_live_button",
                extra={
                    "pocket_id": pocket_id,
                    "workspace_id": workspace_id,
                    "field_path": v["path"],
                    "label": v.get("label"),
                    "reason": v.get("reason"),
                },
            )
    return violations


def validate_action_wiring_strict(
    spec: dict[str, Any] | None,
    *,
    pocket_id: str | None = None,
    workspace_id: str | None = None,
) -> None:
    """Validate action-handler wiring; raise
    :class:`ActionWiringViolationError` if any handler uses an
    unknown verb or any live-labelled button is unwired.

    Use this only on the agent-generation path where the caller can
    handle a retry. Human / import callers should use
    :func:`validate_action_wiring_logged` — strict mode would block a
    legitimate import of an older spec.
    """
    violations = validate_action_wiring_logged(
        spec,
        pocket_id=pocket_id,
        workspace_id=workspace_id,
    )
    if violations:
        raise ActionWiringViolationError(violations)


# ---------------------------------------------------------------------------
# Orphan-state gate (issue #1301, 2026-05-30).
#
# An add-widget intent that lands as a STATE-ONLY merge patch is
# structurally valid at every layer, and nothing cross-references state
# against ui — so a new state key with no ui referent persists silently
# and renders nothing. This pure collector closes that gap: it walks the
# ui tree, gathers EVERY state reference (template expressions + node-level
# ``bind`` values, dotted and bare), unions in the legitimate ``sources``
# bind targets (write-targets no widget may read yet), and returns the
# state keys with no referent.
#
# Mirrors the structure of ``find_unwired_live_buttons`` in OSS
# ``pocketpaw.ripple.manifest`` (a pure ui-walk collector). The caller
# (``pockets/service.merge_spec``) surfaces the result as a NON-BLOCKING
# warning — a state-only seed of a ``sources`` bind target is legitimate.
# ---------------------------------------------------------------------------


# Top-level state key inside a ``{state.<key>...}`` expression body. The
# resolver addresses state by dotted path; only the FIRST segment is the
# top-level key the merge shallow-merges against.
_STATE_REF_RE = re.compile(r"\bstate\.([A-Za-z_$][\w$]*)")

# Built-in loop / event context vars the renderer injects inside ``each``
# bodies. A bare ``bind`` to one of these is loop-scoped, NOT a top-level
# state key, so it must not count as a state reference.
_LOOP_CONTEXT_VARS: frozenset[str] = frozenset({"item", "card", "index", "event", "i"})


def _state_keys_in_expression(value: str) -> Iterator[str]:
    """Yield every top-level state key referenced inside any ``{…}``
    template in ``value`` (e.g. ``{state.tickets}`` → ``tickets``,
    ``{state.x + state.y}`` → ``x``, ``y``)."""
    for body in _expressions_in(value):
        for m in _STATE_REF_RE.finditer(body):
            yield m.group(1)


def _normalize_bind_target(bind: str) -> str:
    """Strip a leading ``state.`` from a dotted bind path and return the
    top-level key. ``"state.prs"`` → ``"prs"``; ``"draft_lane"`` →
    ``"draft_lane"``; ``"state.form.x"`` → ``"form"`` (only the top-level
    key the merge shallow-merges against matters)."""
    stripped = bind[len("state.") :] if bind.startswith("state.") else bind
    # Only the first dotted segment is the top-level state key.
    return stripped.split(".", 1)[0].split("[", 1)[0]


def _collect_referenced_state(
    node: Any,
    referenced: set[str],
    loop_vars: frozenset[str],
) -> None:
    """Recursive ui walk that records every state key a node reads.

    Two reference forms are collected:

    * ``{state.<key>…}`` template expressions in ANY string value, and
    * node-level ``bind`` values — dotted (``state.x``) or bare (``x``).
      A bare ``bind`` that names a loop / event context var in scope is
      loop-scoped, not a top-level state key, and is skipped.

    Generous on purpose: over-collecting a reference only suppresses a
    warning (safe); under-collecting would emit a false positive. The
    ``state`` seed block is never walked here — initial values are plain
    data, not references.
    """
    if isinstance(node, dict):
        # Extend loop-var scope for ``each`` bodies — the conventional
        # loop-context names plus any ``item_as`` / ``index_as`` aliases.
        # Reserved ONLY inside ``each``; a top-level bare bind is real state.
        child_loop_vars = loop_vars
        if node.get("type") == "each":
            extra = set(loop_vars)
            for alias_key in ("item_as", "index_as"):
                alias = node.get(alias_key)
                if isinstance(alias, str) and alias:
                    extra.add(alias)
            child_loop_vars = frozenset(extra | _LOOP_CONTEXT_VARS)

        for key, value in node.items():
            if key == "bind" and isinstance(value, str) and value:
                if value.startswith("state."):
                    referenced.add(_normalize_bind_target(value))
                else:
                    head = _normalize_bind_target(value)
                    if head not in child_loop_vars:
                        referenced.add(head)
                continue
            if isinstance(value, str):
                referenced.update(_state_keys_in_expression(value))
            else:
                _collect_referenced_state(value, referenced, child_loop_vars)
    elif isinstance(node, list):
        for child in node:
            _collect_referenced_state(child, referenced, loop_vars)
    elif isinstance(node, str):
        referenced.update(_state_keys_in_expression(node))


def find_unreferenced_state_keys(spec: dict[str, Any] | None) -> list[str]:
    """Return top-level ``state`` keys that no ui node references.

    Walks ``spec["ui"]`` collecting every state reference (template
    expressions in all forms, node-level ``bind`` values dotted and bare,
    excluding loop-scoped item vars), unions in every ``sources`` bind
    target (legitimate write-targets a widget may not read yet), then
    returns the ``state`` keys minus that referenced set, sorted.

    Pure / side-effect-free — mirrors ``find_unwired_live_buttons``. The
    caller decides what to do with the result; this never blocks.

    Returns ``[]`` when ``spec`` is not a dict or has no ``state`` dict.
    """
    if not isinstance(spec, dict):
        return []
    state = spec.get("state")
    if not isinstance(state, dict) or not state:
        return []

    referenced: set[str] = set()

    # 1. References from the ui tree (templates + node-level binds).
    ui = spec.get("ui")
    if isinstance(ui, (dict, list)):
        # Top-level scope reserves NO loop vars — a state key literally named
        # ``item``/``index``/etc. read by a top-level bare bind must be rescued
        # (loop-context names are reserved only inside ``each`` bodies).
        _collect_referenced_state(ui, referenced, frozenset())

    # 2. Legitimate ``sources`` write-targets — a source binding seeds a
    #    state key that no widget may read yet (the unconditional seed
    #    rule). Union them in so they're never flagged as orphans.
    sources = spec.get("sources")
    if isinstance(sources, dict):
        for binding in sources.values():
            if isinstance(binding, dict):
                bind = binding.get("bind")
                if isinstance(bind, str) and bind:
                    referenced.add(_normalize_bind_target(bind))

    return sorted(k for k in state if k not in referenced)


__all__ = [
    "ActionWiringViolationError",
    "CatalogViolationError",
    "ExpressionWarning",
    "RippleSpecGrammarError",
    "find_unreferenced_state_keys",
    "format_action_violations_for_agent",
    "format_violations_for_agent",
    "format_warnings_for_agent",
    "validate_action_wiring_logged",
    "validate_action_wiring_strict",
    "validate_against_catalog_logged",
    "validate_against_catalog_strict",
    "validate_ripple_spec",
    "validate_ripple_spec_logged",
    "validate_ripple_spec_strict",
]
