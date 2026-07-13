# ee/pocketpaw_ee/cloud/mandates/foreman.py
# Created: 2026-06-11 (feat/belt-mandates, slice 3 — foreman).
#
# The FOREMAN — the LLM judgment seat of a mandate. Once per SHIFT it reads the
# charter, the sighting digest since the last shift, the last 3 shifts'
# outcomes, and (when a soul is bound) the soul recall, then makes EXACTLY ONE
# LLM call that returns a strict-JSON PlanProposal: a FEW tasks (≤ the
# charter's budget) or an explicit empty plan with a reason.
#
# Pluggable LLM layer (env ``POCKETPAW_MANDATE_LLM=claude|mock``):
#   * ``claude`` (default) — shells the ``claude`` CLI:
#       ``claude -p <prompt> --output-format json``
#     and reads the ``result`` field off the JSON envelope. DEMO-BAR: the CLI
#     shell-out is the LLM transport; a later PR can swap an SDK transport in
#     behind the same ``PlanLlm`` protocol. The prompt is passed as ONE argv
#     element — never interpolated into a shell string.
#   * ``mock`` — deterministic: plans one task per sighting (highest severity
#     first) up to the budget, or a no_action plan when the digest is empty.
#     Tests can override the scripted response via ``set_mock_plan()``.
#
# Validation discipline (proven in sim — encoded here, do not weaken):
#   * machine validation runs on ACTION fields (title, expected_outcome) and
#     structural fields (task count vs budget, evidence_refs non-empty) ONLY.
#   * the ``why`` narration is NEVER scanned — a well-behaved foreman names
#     forbidden things precisely when REFUSING them; scanning why would punish
#     the refusal.
#
# Prompt requirements (all sim-validated — keep them in ``build_prompt``):
#   charter verbatim with BOUNDARIES prominent; ≤ budget tasks; every task
#   cites sighting ids + names an expected KPI direction; an EMPTY plan with a
#   reason is correct when signals are quiet and KPIs healthy; boundaries
#   override KPI opportunities; never repeat a failed approach without stating
#   what changed; output strict JSON only.

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Subprocess timeout for the claude CLI call (seconds). A hung CLI must not
# wedge the shift trigger forever.
_CLI_TIMEOUT = 180.0


# ---------------------------------------------------------------------------
# The strict plan schema the LLM must return
# ---------------------------------------------------------------------------


class PlannedTask(BaseModel):
    """One task the foreman proposes for the shift."""

    title: str
    why: str
    evidence_refs: list[str] = Field(default_factory=list)
    expected_outcome: str
    est_cost_hours: float = 1.0


class PlanProposal(BaseModel):
    """The foreman's whole-shift output — strict JSON, nothing else."""

    shift_no: int
    no_action: bool = False
    no_action_reason: str | None = None
    tasks: list[PlannedTask] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Judgment context — everything the foreman sees
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ForemanContext:
    """The assembled judgment context for one shift."""

    shift_no: int
    charter: dict[str, Any]
    # Each digest entry: {id, patrol, severity, summary}
    sightings: list[dict[str, Any]] = field(default_factory=list)
    # Last 3 shifts, oldest-first: {no, state, outcome} — outcome is the
    # free-text result of the shift (what landed / failed / stood down).
    history: list[dict[str, Any]] = field(default_factory=list)
    # Soul recall lines (empty when no soul bound).
    soul_context: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pluggable LLM layer
# ---------------------------------------------------------------------------


class PlanLlm(Protocol):
    """One judgment call: prompt in, raw model text out.

    ``context`` rides along so a deterministic mock can answer without parsing
    prose; real transports ignore it and send only the prompt."""

    async def plan(self, *, prompt: str, context: ForemanContext) -> str: ...


class ClaudeCliLlm:
    """Default transport — shells the ``claude`` CLI (demo bar).

    ``claude -p <prompt> --output-format json`` prints a JSON envelope whose
    ``result`` field carries the model's text. The prompt is a single argv
    element (the CLI does its own auth); nothing is shell-interpolated."""

    async def plan(self, *, prompt: str, context: ForemanContext) -> str:
        proc = await asyncio.create_subprocess_exec(
            "claude",
            "-p",
            prompt,
            "--output-format",
            "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=_CLI_TIMEOUT)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"claude CLI timed out after {_CLI_TIMEOUT}s") from None
        out = out_b.decode("utf-8", "replace")
        if proc.returncode != 0:
            err = err_b.decode("utf-8", "replace")
            raise RuntimeError(f"claude CLI failed (exit {proc.returncode}): {err.strip()[:300]}")
        # The envelope is JSON with a ``result`` field; tolerate a bare-text
        # response (older CLI / plain output) by falling back to stdout.
        try:
            envelope = json.loads(out)
        except json.JSONDecodeError:
            return out
        if isinstance(envelope, dict) and isinstance(envelope.get("result"), str):
            return envelope["result"]
        return out


# Test hook — when set, MockLlm returns this verbatim (a dict is dumped to
# JSON). Reset with ``set_mock_plan(None)``.
_MOCK_PLAN: dict[str, Any] | str | None = None


def set_mock_plan(plan: dict[str, Any] | str | None) -> None:
    """Override the MockLlm's next responses (tests). ``None`` restores the
    deterministic default."""
    global _MOCK_PLAN
    _MOCK_PLAN = plan


class MockLlm:
    """Deterministic foreman for tests + offline demos.

    Default behavior: one task per sighting (highest severity first) capped at
    the charter budget; an explicit no_action plan when the digest is empty.
    ``set_mock_plan`` overrides the response entirely."""

    async def plan(self, *, prompt: str, context: ForemanContext) -> str:
        if _MOCK_PLAN is not None:
            return _MOCK_PLAN if isinstance(_MOCK_PLAN, str) else json.dumps(_MOCK_PLAN)

        budget = int((context.charter.get("budget") or {}).get("max_tasks_per_shift") or 3)
        kpis = context.charter.get("kpis") or []
        kpi_hint = f"{kpis[0]['name']} {kpis[0]['direction']}" if kpis else "surface health up"
        ranked = sorted(context.sightings, key=lambda s: int(s.get("severity") or 0), reverse=True)
        if not ranked:
            return json.dumps(
                {
                    "shift_no": context.shift_no,
                    "no_action": True,
                    "no_action_reason": "Signals are quiet and KPIs are healthy — standing down.",
                    "tasks": [],
                }
            )
        tasks = [
            {
                "title": f"Address: {s.get('summary', 'sighting')[:80]}",
                "why": f"Sighting {s.get('id')} (severity {s.get('severity')}) from the "
                f"{s.get('patrol')} patrol warrants action this shift.",
                "evidence_refs": [str(s.get("id"))],
                "expected_outcome": f"KPI {kpi_hint}; sighting resolved.",
                "est_cost_hours": 1.0,
            }
            for s in ranked[:budget]
        ]
        return json.dumps(
            {
                "shift_no": context.shift_no,
                "no_action": False,
                "no_action_reason": None,
                "tasks": tasks,
            }
        )


def resolve_llm() -> PlanLlm:
    """Pick the transport from ``POCKETPAW_MANDATE_LLM`` (``claude`` default)."""
    choice = (os.environ.get("POCKETPAW_MANDATE_LLM") or "claude").strip().lower()
    if choice == "mock":
        return MockLlm()
    return ClaudeCliLlm()


# ---------------------------------------------------------------------------
# Prompt — every sim-validated rule lives here
# ---------------------------------------------------------------------------


def build_prompt(context: ForemanContext) -> str:
    """Assemble the single judgment prompt. Charter rides VERBATIM (as JSON)
    with the BOUNDARIES block pulled out and stated first — boundaries override
    every KPI opportunity."""
    charter = context.charter
    boundaries = list(charter.get("boundaries") or [])
    says_no = list(charter.get("says_no") or [])
    budget = (charter.get("budget") or {}).get("max_tasks_per_shift", 3)

    sighting_lines = (
        "\n".join(
            f"- id={s['id']} patrol={s.get('patrol')} severity={s.get('severity')}: "
            f"{s.get('summary')}"
            for s in context.sightings
        )
        or "(none — the surface has been quiet since the last shift)"
    )
    history_lines = (
        "\n".join(
            f"- shift {h.get('no')}: state={h.get('state')} outcome={h.get('outcome') or 'n/a'}"
            for h in context.history
        )
        or "(no prior shifts)"
    )
    soul_lines = "\n".join(f"- {line}" for line in context.soul_context) or "(none)"

    return f"""You are the FOREMAN of a standing engineering mandate. Once per shift you decide \
what FEW tasks (if any) the crew should run. You are judged on judgment, not output volume.

== BOUNDARIES (ABSOLUTE — these override every KPI opportunity) ==
{json.dumps(boundaries, indent=2)}
The mandate also SAYS NO to:
{json.dumps(says_no, indent=2)}
If a tempting task would cross a boundary, you refuse it. When refusing, you may name the \
forbidden thing in your reasoning — that is correct behavior.

== CHARTER (verbatim) ==
{json.dumps(charter, indent=2)}

== SIGHTINGS since the last shift ==
{sighting_lines}

== LAST SHIFTS' OUTCOMES (oldest first) ==
{history_lines}
Never repeat an approach that already failed above without explicitly stating in the task's \
"why" what is different this time.

== SOUL CONTEXT (long-lived memory of this mandate) ==
{soul_lines}

== YOUR RULES ==
1. Plan AT MOST {budget} task(s) this shift. Fewer is better. Pick only what moves a KPI.
2. Every task MUST cite at least one sighting id in "evidence_refs" and MUST name an \
expected KPI and its direction in "expected_outcome" (e.g. "open_cves down").
3. An EMPTY plan is a correct, respected outcome: if the signals are quiet and the KPIs are \
healthy, set "no_action": true with a short "no_action_reason" and an empty "tasks" list. \
Do not invent work.
4. Boundaries override KPI opportunities — a boundary-crossing task is never worth it.
5. This is shift number {context.shift_no}; set "shift_no" to exactly {context.shift_no}.

== OUTPUT (STRICT) ==
Reply with STRICT JSON only — no prose, no markdown fences, no commentary:
{{"shift_no": {context.shift_no}, "no_action": false, "no_action_reason": null, "tasks": \
[{{"title": "...", "why": "...", "evidence_refs": ["<sighting id>"], "expected_outcome": \
"<kpi> <up|down>; ...", "est_cost_hours": 1.0}}]}}"""


# ---------------------------------------------------------------------------
# Output parsing + machine validation
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def parse_plan(raw: str) -> PlanProposal:
    """Parse the model's text into a PlanProposal — tolerating a fenced JSON
    block or stray text around a single top-level JSON object."""
    text = raw.strip()
    m = _FENCE.match(text)
    if m:
        text = m.group(1).strip()
    try:
        return PlanProposal.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValueError):
        # Last resort: find the outermost {...} span.
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return PlanProposal.model_validate(json.loads(text[start : end + 1]))
        raise


def validate_plan(plan: PlanProposal, charter: dict[str, Any]) -> list[str]:
    """Machine validation of a PlanProposal. Returns a list of violation
    strings (empty = valid).

    CRITICAL (sim-proven): checks run on ACTION fields — ``title`` and
    ``expected_outcome`` — plus structural fields (task count vs budget,
    evidence_refs non-empty). The ``why`` narration is NEVER scanned: a
    well-behaved foreman names forbidden things when REFUSING them."""
    violations: list[str] = []
    budget = int((charter.get("budget") or {}).get("max_tasks_per_shift") or 3)

    if plan.no_action:
        if plan.tasks:
            violations.append("no_action plan must carry zero tasks")
        if not (plan.no_action_reason or "").strip():
            violations.append("no_action plan must carry a no_action_reason")
        return violations

    if len(plan.tasks) > budget:
        violations.append(
            f"plan proposes {len(plan.tasks)} tasks but the charter budget caps a shift at {budget}"
        )

    forbidden = [
        p.strip().lower()
        for p in [*(charter.get("says_no") or []), *(charter.get("boundaries") or [])]
        if isinstance(p, str) and p.strip()
    ]
    for i, task in enumerate(plan.tasks):
        if not task.evidence_refs:
            violations.append(f"task {i + 1} ({task.title[:40]!r}) cites no sighting ids")
        action_text = f"{task.title} {task.expected_outcome}".lower()
        for phrase in forbidden:
            if phrase in action_text:
                violations.append(
                    f"task {i + 1} ({task.title[:40]!r}) crosses a charter boundary: "
                    f"{phrase!r} appears in its action fields"
                )
    return violations


# ---------------------------------------------------------------------------
# The one judgment call
# ---------------------------------------------------------------------------


async def plan_shift(context: ForemanContext, llm: PlanLlm | None = None) -> PlanProposal:
    """Build the prompt, make ONE LLM call, parse the strict-JSON plan.

    Raises on transport failure or unparseable output — the caller (service)
    decides how a failed judgment surfaces. Validation is the caller's step
    (``validate_plan``) so the service can map violations to CloudErrors."""
    llm = llm or resolve_llm()
    prompt = build_prompt(context)
    raw = await llm.plan(prompt=prompt, context=context)
    plan = parse_plan(raw)
    # The model is told to echo the shift number; normalize drift rather than
    # failing the shift on an off-by-one echo.
    if plan.shift_no != context.shift_no:
        logger.warning(
            "foreman echoed shift_no=%s for shift %s — normalizing",
            plan.shift_no,
            context.shift_no,
        )
        plan = plan.model_copy(update={"shift_no": context.shift_no})
    return plan


__all__ = [
    "ClaudeCliLlm",
    "ForemanContext",
    "MockLlm",
    "PlanLlm",
    "PlanProposal",
    "PlannedTask",
    "build_prompt",
    "parse_plan",
    "plan_shift",
    "resolve_llm",
    "set_mock_plan",
    "validate_plan",
]
