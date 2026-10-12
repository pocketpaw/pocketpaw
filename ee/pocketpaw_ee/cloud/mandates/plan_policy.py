# ee/pocketpaw_ee/cloud/mandates/plan_policy.py — the plan gate's AUTO policy.
#
# With ``POCKETPAW_BELT_PLAN_GATE=auto`` the shift trigger awaits Clef's plan
# score and asks ``decide`` whether the plan can pass the gate without a human.
# Pure: no I/O, no clock. The caller (``service.trigger_shift``) does the
# resolve through the same path the console's ``plan/resolve`` route uses.
#
# All or nothing. The plan is APPROVED only when every task cites an open
# sighting of this mandate, the plan fits the charter budget, and Clef is
# confident it is in scope and low risk. Anything else ESCALATES the whole plan
# (it stays pending for a human, as with the gate off): Clef unavailable or
# truncated, tasks not independent enough, a task too big, or a per-task rule
# failing. The policy never rejects; a rejection would file a teaching sighting
# that a human did not write.
#
# Thresholds default to values calibrated on the supervisor's gate log of the
# first unattended pilot (``tests/cloud/test_belt_plan_policy.py`` holds those
# rows) and are overridable per env (``thresholds_from_env``).

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

FLAG = "POCKETPAW_BELT_PLAN_GATE"
APPROVER = "system:belt-policy"


@dataclass(frozen=True)
class Thresholds:
    min_in_scope: float = 0.80
    min_low_risk: float = 0.75
    min_independent: float = 0.40
    max_too_big: float = 0.65


@dataclass(frozen=True)
class Decision:
    approve: bool
    rule: str
    reason: str


_ENV = {
    "min_in_scope": "POCKETPAW_BELT_POLICY_MIN_IN_SCOPE",
    "min_low_risk": "POCKETPAW_BELT_POLICY_MIN_LOW_RISK",
    "min_independent": "POCKETPAW_BELT_POLICY_MIN_INDEPENDENT",
    "max_too_big": "POCKETPAW_BELT_POLICY_MAX_TOO_BIG",
}


def enabled() -> bool:
    """True when the plan gate runs the auto policy (default: manual)."""
    return os.environ.get(FLAG, "").strip().lower() == "auto"


def thresholds_from_env() -> Thresholds:
    """The defaults, each overridable by its env var; a bad value keeps the default."""
    values: dict[str, float] = {}
    for name, var in _ENV.items():
        raw = os.environ.get(var, "").strip()
        if not raw:
            continue
        try:
            values[name] = float(raw)
        except ValueError:
            logger.warning("%s=%r is not a number; using the default", var, raw)
    return Thresholds(**values)


def _numbers(v: Any) -> str:
    return (
        f"in_scope {v.in_scope:.2f}, low_risk {v.low_risk:.2f}, "
        f"tasks_independent {v.tasks_independent:.2f}, too_big {v.too_big:.2f}"
    )


def decide(
    tasks: list[dict[str, Any]],
    open_sighting_ids: set[str],
    budget_max: int,
    verdict: Any,
    thresholds: Thresholds | None = None,
) -> Decision:
    """Approve the whole plan or escalate it. ``verdict`` is Clef's
    ``PlanVerdict`` (or ``None`` when Clef was off, failed or timed out); a
    negative score means Clef did not answer that question."""
    t = thresholds or Thresholds()

    def esc(rule: str, reason: str) -> Decision:
        return Decision(False, rule, reason)

    if verdict is None:
        return esc("clef_unavailable", "Clef gave no plan score (off, failed or timed out)")
    scores = (verdict.in_scope, verdict.low_risk, verdict.tasks_independent, verdict.too_big)
    if any(s is None or s < 0 for s in scores):
        return esc("clef_unavailable", f"Clef left a question unanswered ({_numbers(verdict)})")
    if verdict.truncated:
        return esc("clef_truncated", f"Clef saw a truncated plan ({_numbers(verdict)})")
    if verdict.tasks_independent < t.min_independent:
        return esc(
            "tasks_not_independent",
            f"tasks_independent {verdict.tasks_independent:.2f} < {t.min_independent:.2f} "
            f"({_numbers(verdict)})",
        )
    if verdict.too_big > t.max_too_big:
        return esc(
            "task_too_big",
            f"too_big {verdict.too_big:.2f} > {t.max_too_big:.2f} ({_numbers(verdict)})",
        )
    if not tasks:
        return esc("empty_plan", "the plan has no tasks")
    if len(tasks) > budget_max:
        return esc("over_budget", f"{len(tasks)} tasks > the charter budget of {budget_max}")
    for i, task in enumerate(tasks):
        refs = {str(r) for r in task.get("evidence_refs") or []}
        if not refs & open_sighting_ids:
            return esc(
                "no_open_evidence",
                f"task {i + 1} ({str(task.get('title') or '')[:60]!r}) cites no open sighting",
            )
    if verdict.in_scope < t.min_in_scope:
        return esc(
            "out_of_scope",
            f"in_scope {verdict.in_scope:.2f} < {t.min_in_scope:.2f} ({_numbers(verdict)})",
        )
    if verdict.low_risk < t.min_low_risk:
        return esc(
            "risky",
            f"low_risk {verdict.low_risk:.2f} < {t.min_low_risk:.2f} ({_numbers(verdict)})",
        )
    return Decision(
        True,
        "auto_approve",
        f"belt-policy auto-approve: every task cites an open sighting, "
        f"{len(tasks)}/{budget_max} tasks within budget, Clef {_numbers(verdict)} "
        f"(needs in_scope >= {t.min_in_scope:.2f}, low_risk >= {t.min_low_risk:.2f}, "
        f"tasks_independent >= {t.min_independent:.2f}, too_big <= {t.max_too_big:.2f})",
    )


__all__ = ["APPROVER", "FLAG", "Decision", "Thresholds", "decide", "enabled", "thresholds_from_env"]
