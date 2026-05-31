# src/pocketpaw/bundled_templates/instinct_composer.py
# Created: 2026-05-28 (feat/rfc-03-v2-instinct-exec) — pure-library
# implementation of the RFC 03 v2 5-step Instinct resolution order.
# Consumes the PR 2c CEL evaluator + identifier resolver. Produces an
# immutable ``InstinctDecision`` value object describing what the EE
# runtime should do next (BLOCK / ESCALATE_APPROVAL / EXECUTE /
# NOTIFY_AND_EXECUTE). The runtime side — approval queue, action
# invocation, outcome emission — lives in ``ee/cloud/pockets/`` and is
# wired in a follow-up PR. Bulk fan-out (PR 2e), temporal trigger
# sweeper (PR 2f), and Fabric ``tier: registered`` enforcement (PR 2g)
# all consume this composer; their per-row decision shape is the
# same.
"""5-step Instinct resolution composer for RFC 03 v2 templates.

Public surface
--------------

* :class:`InstinctDecision` — frozen Pydantic v2 model describing the
  composed verdict, the rules that participated, the reason code, and
  any notify rules to fire as side effects.
* :func:`resolve_instinct` — the pure function that walks the 5-step
  order from the RFC §"Instinct resolution order" and returns an
  ``InstinctDecision``.
* :class:`InstinctResolutionError` — raised for action lookup misses
  and for rule evaluation failures. CEL eval failures wrap the
  underlying ``CelEvaluationError`` on ``.__cause__`` so callers can
  reach the original diagnostic.

Resolution order (verbatim from the RFC)
----------------------------------------

1. **block rules** — first match → ``BLOCK``, short-circuit.
2. **approval rules** — any match → ``ESCALATE_APPROVAL`` with reason
   ``operator_overlay_escalated``.
3. **per-action policy** — ``auto`` falls through; ``require_approval``
   escalates with reason ``author_floor``; ``notify_only`` falls
   through and flips the final verdict to ``NOTIFY_AND_EXECUTE``.
4. **execute** — this PR returns the verdict; the EE runtime dispatches.
5. **side effects** — notify rules whose ``when`` matches are returned
   on the decision so the EE runtime can fan them out in parallel
   with the action invocation.

Two RFC invariants are pinned in tests and enforced by the early-return
structure of :func:`resolve_instinct`:

* ``block always wins`` — step 1's first match returns immediately. No
  step 2-5 work is done. Tests pin both the simple case and the
  block+approval-simultaneous case.
* ``the operator overlay can only escalate, never demote`` — step 2
  can only return ``ESCALATE_APPROVAL``; it never returns ``EXECUTE``.
  Step 3's ``require_approval`` per-action floor is reached only if
  step 2 returned no match, so the floor is never lowered.

Scope (locked by the PR brief)
------------------------------

* Library / pure function. No I/O, no Beanie, no ``pocketpaw_ee``
  imports. The OSS import-linter contract enforces this.
* ``InstinctDecision`` is **immutable** (``ConfigDict(frozen=True)``).
* Side effects are *described* (returned in ``notify_rules``) but not
  *fired* — the EE runtime owns dispatch.
* Bulk fan-out is the consumer's job (PR 2e).

Workspace + row context merging
-------------------------------

The composer merges ``workspace_context`` and ``row_context`` into a
single activation for CEL evaluation. **Row context wins on
collision** — per-row data should never be silently shadowed by
workspace defaults. The merge happens once and is reused for every
rule the composer evaluates.

Resolver injection
------------------

If the caller passes no ``resolver``, the composer constructs a
default :class:`TemplateIdentifierResolver` keyed off
``template.state``. Tests and the future ``FabricResolver`` (PR 2g)
can swap in a richer implementation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from pocketpaw.bundled_templates.cel_runtime import (
    CelEvaluationError,
    evaluate_cel,
)
from pocketpaw.bundled_templates.identifier_resolver import (
    IdentifierResolver,
    TemplateIdentifierResolver,
)
from pocketpaw.bundled_templates.schema import (
    ActionDef,
    InstinctRule,
    PocketTemplate,
)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


InstinctVerdict = Literal["BLOCK", "ESCALATE_APPROVAL", "EXECUTE", "NOTIFY_AND_EXECUTE"]
"""Set of terminal verdicts the composer can produce.

* ``BLOCK`` — step 1 matched. The runtime must abort the action and
  emit a ``blocked_by_rule`` audit-log entry.
* ``ESCALATE_APPROVAL`` — step 2 matched or step 3's
  ``require_approval`` floor fired. The runtime must enqueue the
  action on the Instinct approval queue.
* ``EXECUTE`` — step 3 picked ``auto`` and no overlay escalated. The
  runtime invokes the action directly.
* ``NOTIFY_AND_EXECUTE`` — step 3 picked ``notify_only``. The runtime
  invokes the action AND pings the escalation target.
"""


class InstinctDecision(BaseModel):
    """Immutable result of composing a per-action ``instinct_policy``
    against the top-level ``instinct_rules.rules[]``.

    Frozen so callers can pass the decision through audit /
    serialization layers without worrying about downstream mutation.
    """

    model_config = ConfigDict(frozen=True)

    verdict: InstinctVerdict
    action_name: str
    matched_rules: list[InstinctRule] = Field(default_factory=list)
    """Rules that *participated in the decision* — the block rule that
    fired (BLOCK), or the approval rules that escalated
    (ESCALATE_APPROVAL via overlay). Empty when the verdict came from
    the per-action floor / fallback."""

    notify_rules: list[InstinctRule] = Field(default_factory=list)
    """Top-level ``notify`` rules whose ``when`` matched. Fire in
    parallel with the action invocation per RFC §"Instinct resolution
    order" step 5. Empty on BLOCK (step 1 short-circuits notify
    gathering)."""

    reason: str
    """Short code explaining the verdict: ``blocked_by_rule``,
    ``operator_overlay_escalated``, ``author_floor``, ``auto``, or
    ``notify_only``."""

    audit_data: dict[str, Any] = Field(default_factory=dict)
    """Extra diagnostic data for the audit log — currently carries the
    action name and the rule ``when`` text(s) that participated.
    Callers should treat this as opaque metadata."""


class InstinctResolutionError(Exception):
    """Raised by :func:`resolve_instinct`.

    Two failure modes:

    1. **Unknown action** — ``action_name`` is not present on
       ``template.actions[].name``. The runtime cannot dispatch
       something that doesn't exist; this is a programming error in
       the caller (template / action-name mismatch) and surfaces
       loudly.
    2. **CEL evaluation failure inside a rule** — the underlying
       :class:`CelEvaluationError` is preserved on ``__cause__`` via
       ``raise ... from exc``. The wrapping exception carries the
       rule and action names so the audit log has enough context to
       point at the offending rule.
    """


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------


def resolve_instinct(
    template: PocketTemplate,
    action_name: str,
    row_context: dict[str, Any],
    workspace_context: dict[str, Any] | None = None,
    *,
    resolver: IdentifierResolver | None = None,
    now: datetime | None = None,
) -> InstinctDecision:
    """Walk the RFC 03 v2 5-step Instinct resolution order for one row.

    Parameters
    ----------
    template:
        The fully-validated :class:`PocketTemplate` carrying
        ``actions[]`` and (optionally) ``instinct_rules``.
    action_name:
        Stable slug from ``ActionDef.name``. Must exist on the
        template; otherwise :class:`InstinctResolutionError` is raised.
    row_context:
        Per-row dict — every identifier referenced by every rule that
        needs evaluating must be present (the resolver enforces
        ``KeyError`` → :class:`CelEvaluationError` →
        :class:`InstinctResolutionError`).
    workspace_context:
        Optional workspace-scoped defaults — typically things like
        the current user's role, the workspace's risk-tolerance
        setting, etc. Merged with ``row_context``; **row wins on
        collision**.
    resolver:
        Optional :class:`IdentifierResolver`. Defaults to
        ``TemplateIdentifierResolver(template.state)``.
    now:
        Optional wall-clock for the CEL ``within(...)`` function.
        Defaults to ``datetime.now(UTC)``. Tests should pass a fixed
        value.

    Returns
    -------
    :class:`InstinctDecision`
        Immutable. See class docstring for field semantics.

    Raises
    ------
    InstinctResolutionError
        On unknown action or rule-eval failure.
    """
    if now is None:
        now = datetime.now(UTC)

    # Resolve the action up-front so a typo on ``action_name`` surfaces
    # before we evaluate any rules.
    action = _find_action(template, action_name)

    # Default resolver — same lookup the CEL primitive uses everywhere
    # else in the runtime. PR 2g will swap in a Fabric-backed resolver.
    if resolver is None:
        resolver = TemplateIdentifierResolver(template.state)

    # Merge once. ``workspace_context`` first so ``row_context`` keys
    # overwrite on collision (row wins). This matches the brief and
    # keeps per-row data from being shadowed by workspace defaults.
    merged_context: dict[str, Any] = {}
    if workspace_context:
        merged_context.update(workspace_context)
    merged_context.update(row_context)

    rules = list(template.instinct_rules.rules) if template.instinct_rules else []

    # -------------------------------------------------------------------
    # STEP 1 — block rules. First match wins; short-circuit immediately.
    # The ``block always wins`` invariant lives here: returning early
    # means steps 2-5 never run, even when an approval rule would also
    # match the same row.
    # -------------------------------------------------------------------
    for rule in rules:
        if rule.action != "block":
            continue
        if _eval_rule(rule, merged_context, resolver, now, action_name):
            return InstinctDecision(
                verdict="BLOCK",
                action_name=action_name,
                matched_rules=[rule],
                notify_rules=[],
                reason="blocked_by_rule",
                audit_data={"action_name": action_name, "rule_when": rule.when},
            )

    # -------------------------------------------------------------------
    # STEP 2 — approval rules. ANY match escalates. The overlay can
    # only escalate; the step 3 ``require_approval`` floor is reached
    # only when nothing matches here, so the floor is never demoted.
    # -------------------------------------------------------------------
    matched_approval: list[InstinctRule] = [
        rule
        for rule in rules
        if rule.action == "require_approval"
        and _eval_rule(rule, merged_context, resolver, now, action_name)
    ]
    # -------------------------------------------------------------------
    # STEP 5 (gathered now, returned later) — notify rules. We collect
    # them up-front so the same merged context + resolver is reused for
    # every rule evaluation in this composer call. They are returned
    # alongside the verdict (not fired here — the EE runtime owns
    # dispatch).
    # -------------------------------------------------------------------
    notify_rules: list[InstinctRule] = [
        rule
        for rule in rules
        if rule.action == "notify" and _eval_rule(rule, merged_context, resolver, now, action_name)
    ]

    if matched_approval:
        return InstinctDecision(
            verdict="ESCALATE_APPROVAL",
            action_name=action_name,
            matched_rules=matched_approval,
            notify_rules=notify_rules,
            reason="operator_overlay_escalated",
            audit_data={
                "action_name": action_name,
                "rule_whens": [r.when for r in matched_approval],
            },
        )

    # -------------------------------------------------------------------
    # STEP 3 — per-action policy floor. ``auto`` falls through to step
    # 4; ``require_approval`` escalates with reason ``author_floor``;
    # ``notify_only`` falls through and tags the verdict for the
    # ``NOTIFY_AND_EXECUTE`` path.
    # -------------------------------------------------------------------
    if action.instinct_policy == "require_approval":
        return InstinctDecision(
            verdict="ESCALATE_APPROVAL",
            action_name=action_name,
            matched_rules=[],
            notify_rules=notify_rules,
            reason="author_floor",
            audit_data={"action_name": action_name},
        )

    # -------------------------------------------------------------------
    # STEP 4 — execute. The composer does not invoke the action; the
    # caller dispatches off the verdict.
    # -------------------------------------------------------------------
    if action.instinct_policy == "notify_only":
        return InstinctDecision(
            verdict="NOTIFY_AND_EXECUTE",
            action_name=action_name,
            matched_rules=[],
            notify_rules=notify_rules,
            reason="notify_only",
            audit_data={"action_name": action_name},
        )

    # ``auto`` — plain execute. Notify rules still flow through (a
    # top-level notify rule can fire alongside an auto execute).
    return InstinctDecision(
        verdict="EXECUTE",
        action_name=action_name,
        matched_rules=[],
        notify_rules=notify_rules,
        reason="auto",
        audit_data={"action_name": action_name},
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _find_action(template: PocketTemplate, action_name: str) -> ActionDef:
    """Look up an action by name. Raises :class:`InstinctResolutionError`
    if not found — the caller passed a bad name and we want that to
    surface loudly, not silently no-op."""
    for action in template.actions:
        if action.name == action_name:
            return action
    raise InstinctResolutionError(
        f"action {action_name!r} is not declared on template "
        f"{template.name!r} (available: "
        f"{sorted(a.name for a in template.actions)})"
    )


def _eval_rule(
    rule: InstinctRule,
    context: dict[str, Any],
    resolver: IdentifierResolver,
    now: datetime,
    action_name: str,
) -> bool:
    """Evaluate a single rule's ``when`` against ``context``.

    Wraps :class:`CelEvaluationError` into
    :class:`InstinctResolutionError` so callers have a single typed
    exception to handle. Original cause is preserved on ``__cause__``
    via the ``from exc`` chain.

    A non-boolean result is coerced to ``bool`` — CEL ``when``
    expressions are documented as predicates, so a truthy non-bool is
    almost certainly an author mistake. We still accept it (the RFC
    says nothing stricter) but downstream linters can flag it.
    """
    try:
        result = evaluate_cel(rule.when, context, resolver, now=now)
    except CelEvaluationError as exc:
        raise InstinctResolutionError(
            f"rule {rule.when!r} failed to evaluate while resolving action {action_name!r}: {exc}"
        ) from exc
    return bool(result)


__all__ = [
    "InstinctDecision",
    "InstinctResolutionError",
    "InstinctVerdict",
    "resolve_instinct",
]
