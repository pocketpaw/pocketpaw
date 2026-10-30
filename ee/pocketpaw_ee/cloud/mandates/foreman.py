# ee/pocketpaw_ee/cloud/mandates/foreman.py — the FOREMAN, a mandate's LLM seat.
#
# Once per SHIFT it reads the charter; the OPEN sightings no task is working
# (capped; each new or carried over, with the tasks citing it and its full
# ``Depends on:`` line plus the named issues' states); the IN FLIGHT ones apart
# (a task or an open PR closing the issue works them: never planned again) with
# the line's runs in flight against its cap; the last 3 shifts' outcomes (each
# task's run result and what a human said at the gate); the mandate's LINE as
# git has it (built, awaiting the captain's merge); the bound repo's C4
# components; and the soul recall. Then it makes EXACTLY ONE LLM call that
# returns a strict-JSON PlanProposal: the shift filled up to ``shift_budget``
# (the line's headroom under its in-flight cap, never over the charter's
# ``max_tasks_per_shift``) with the highest-severity independent sightings,
# fewer only with a ``shortfall_reason``, or an explicit empty plan with a
# reason. A task may name a charter ``recipe`` (a deterministic command)
# instead of develop work.
#
# LLM transport (env ``POCKETPAW_MANDATE_LLM=claude|mock``): ``claude`` (default)
# runs the SYSTEM Claude Code CLI (``claude -p --tools "" --output-format json``,
# prompt on stdin, in an empty temp dir with the factory's scrubbed env, timing
# out after ``POCKETPAW_MANDATE_FOREMAN_TIMEOUT`` s, default 300); ``mock`` is
# deterministic (one task per open sighting not in flight, highest severity
# first, up to the shift budget; ``set_mock_plan()`` overrides it in tests).
# ``claude_cli_argv`` / ``claude_result_text`` are the ONE place the factory
# resolves the binary (``POCKETPAW_FACTORY_CLAUDE_BIN``, else ``which claude``)
# and model (a crew worker's own, else ``POCKETPAW_FACTORY_CLAUDE_MODEL``), and
# they pin every seat to no settings files, MCP, hooks or persisted session
# (only the develop station's owner setup opts out of the first three).
# ``run_claude_no_tools`` is the sandboxed tool-less call the foreman and the
# autopilot personas share. Prompts ride stdin, never argv or a shell.
#
# Validation discipline (sim-proven — do not weaken): ``validate_plan`` checks
# ACTION fields (title, expected_outcome) and structure (task count vs the
# charter's max, evidence_refs non-empty, recipe names declared) ONLY; the
# ``why`` narration is NEVER scanned, since a good foreman names forbidden
# things when REFUSING them. ``drop_in_flight`` then drops any task citing an
# in-flight sighting (dropping, not refusing: a refused plan holds the line).
#
# Prompt rules (sim-validated — keep them in ``build_prompt``): charter verbatim
# with BOUNDARIES first; fill the shift budget by severity, respecting
# ``Depends on:`` (fewer only with a shortfall_reason); every task cites sighting ids
# and names an expected KPI direction; an EMPTY plan with a reason is correct
# when signals are quiet or all in flight; boundaries override KPI
# opportunities; never repeat a failed approach without saying what changed;
# tasks in one shift are independent (each develops from the same line tip);
# work on the line and in-flight work are never planned again; a task extends
# the repo's existing C4 components, never duplicates one; strict JSON only.

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Subprocess timeout for the claude CLI call (seconds), env
# ``POCKETPAW_MANDATE_FOREMAN_TIMEOUT``. A hung CLI must not wedge the shift
# trigger forever; a long backlog prompt needs more than a short chat turn.
_CLI_TIMEOUT = 300.0


def _cli_timeout() -> float:
    try:
        value = float(os.environ.get("POCKETPAW_MANDATE_FOREMAN_TIMEOUT") or _CLI_TIMEOUT)
    except ValueError:
        return _CLI_TIMEOUT
    return value if value > 0 else _CLI_TIMEOUT


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
    # A charter recipe name: the develop station runs that command instead of
    # an LLM develop (checks still gate it). ``None`` = ordinary develop work.
    recipe: str | None = None


class PlanProposal(BaseModel):
    """The foreman's whole-shift output — strict JSON, nothing else."""

    shift_no: int
    no_action: bool = False
    no_action_reason: str | None = None
    # Why a plan leaves budget unused (blocked dependencies, work that is not
    # independent of this shift's tasks, boundaries); None when it fills it.
    shortfall_reason: str | None = None
    tasks: list[PlannedTask] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Judgment context — everything the foreman sees
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ForemanContext:
    """The assembled judgment context for one shift."""

    shift_no: int
    charter: dict[str, Any]
    # The open backlog, capped: {id, patrol, severity, summary, new, in_flight,
    # open_pr, tasks: [{shift_no, title, status}], depends_on} — ``new`` = filed
    # since the last shift; ``open_pr`` = an open PR closing its issue (in flight);
    # ``tasks`` are the planned tasks citing the sighting;
    # ``depends_on`` is its ``Depends on:`` line (None when it has none).
    sightings: list[dict[str, Any]] = field(default_factory=list)
    # ``#n`` -> ``open`` / ``closed`` on the bound repo's tracker, for the issues
    # a ``depends_on`` line names (empty when unknown).
    issue_states: dict[int, str] = field(default_factory=dict)
    # How many sightings were open before the cap (0 = same as ``sightings``).
    open_total: int = 0
    # The open sightings already being worked (same shape, ``in_flight`` true):
    # a task citing one is in flight, or an open PR closes its issue. Shown so
    # the foreman never plans them again; ``drop_in_flight`` enforces it.
    in_flight: list[dict[str, Any]] = field(default_factory=list)
    # The line's capacity (``service._headroom``): ``in_flight`` runs, ``cap``
    # and this shift's task ``budget``. Empty = the charter's max_tasks_per_shift.
    capacity: dict[str, int] = field(default_factory=dict)
    # Last 3 shifts, oldest-first: {no, state, outcome, tasks, gate} — outcome
    # is the free-text result of the shift; tasks are its planned tasks with
    # their run results ({title, evidence_refs, status, in_flight, error});
    # gate is what a human said when rejecting or editing its plan.
    history: list[dict[str, Any]] = field(default_factory=list)
    # The mandate's line as git has it (``belt.executor.line_status``):
    # {branch, base, exists, ahead, merged, pr_url, subjects}; ``subjects`` are
    # the commits not in the base yet, newest first. Built either way.
    line: dict[str, Any] = field(default_factory=dict)
    # Soul recall lines (empty when no soul bound).
    soul_context: list[str] = field(default_factory=list)
    # The bound repo's C4 containers/components, one line each, capped
    # (``belt.orient.c4_lines``); empty when the repo has no C4 model.
    architecture: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pluggable LLM layer
# ---------------------------------------------------------------------------


class PlanLlm(Protocol):
    """One judgment call: prompt in, raw model text out.

    ``context`` rides along so a deterministic mock can answer without parsing
    prose; real transports ignore it and send only the prompt."""

    async def plan(self, *, prompt: str, context: ForemanContext) -> str: ...


# Every factory seat runs isolated from whatever config sits on disk: no
# user/project/local settings (so a worktree's ``.claude/settings.json`` can't
# grant permissions or add hooks), no MCP servers (``.mcp.json`` ignored), no
# hooks. Auth is the CLI's keychain/OAuth login, which is not a setting source.
# Never ``--bare``: it forces API-key auth.
_ISOLATION_FLAGS = (
    "--setting-sources",
    "",
    "--strict-mcp-config",
    "--settings",
    '{"disableAllHooks":true}',
)


def claude_cli_argv(
    *args: str, isolated: bool = True, stream: bool = False, model: str = ""
) -> list[str]:
    """argv for one headless call to the SYSTEM Claude Code CLI.

    Every factory LLM seat (foreman, develop, fix, review) builds its command
    here: ``<bin> -p <args...> <isolation flags> --output-format json
    --no-session-persistence [--model M]``. No seat persists its session: the
    transcript would keep the whole prompt (task, crew instructions) under
    ``~/.claude/projects`` on the host. The binary is ``POCKETPAW_FACTORY_CLAUDE_BIN``, else
    ``claude`` on PATH — never the SDK's bundled copy, which goes stale.
    Resolved per call so env changes apply. ``isolated=False`` drops the
    isolation flags: only the develop station's owner setup passes it, and only
    after restoring the worktree's agent config to the base commit.
    ``stream=True`` asks for ``stream-json`` (one JSON event per line; ``-p``
    needs ``--verbose`` for it) so the develop seat's tool calls can be read
    back as a feed; ``claude_result_text`` reads both formats.

    ``model`` is a crew worker's Agent model; it wins over
    ``POCKETPAW_FACTORY_CLAUDE_MODEL`` when ``cli_model`` accepts it."""
    binary = os.environ.get("POCKETPAW_FACTORY_CLAUDE_BIN") or shutil.which("claude") or "claude"
    isolation = _ISOLATION_FLAGS if isolated else ()
    output = ("stream-json", "--verbose") if stream else ("json",)
    argv = [binary, "-p", *args, *isolation, "--output-format", *output]
    argv.append("--no-session-persistence")
    model = cli_model(model) or (os.environ.get("POCKETPAW_FACTORY_CLAUDE_MODEL") or "").strip()
    if model:
        argv += ["--model", model]
    return argv


# A Claude model the CLI runs: a ``claude-*`` id (optionally region/provider
# dotted, e.g. ``us.anthropic.claude-…``) or a CLI alias, with an optional
# ``[1m]`` context suffix. Lowercase only, and every dotted part starts with a
# letter or digit, so nothing that could read as a flag.
_CLI_MODEL = re.compile(
    r"^(?:(?:[a-z0-9][a-z0-9-]*\.)*claude-[a-z0-9.:-]+|default|sonnet|opus|haiku|opusplan|fable)"
    r"(?:\[1m\])?$"
)


def cli_model(model: str) -> str:
    """An Agent's model id as the claude CLI takes it, or ``""`` (use the env).

    Agent models are catalog ids (``anthropic/claude-sonnet-4-5``), so the
    ``anthropic/`` prefix is dropped and the id lowercased. Only a Claude model
    passes (``claude-*`` or a CLI alias); another provider's model (``gpt-4o``,
    ``openai/gpt-4o``) or a value that could read as a flag is ``""``."""
    model = (model or "").strip().lower().removeprefix("anthropic/")
    return model if _CLI_MODEL.match(model) else ""


def claude_result_envelope(stdout: str) -> dict[str, Any] | None:
    """The CLI's final ``{"type": "result", ...}`` envelope: the whole stdout
    for ``--output-format json``, else the LAST ``type == "result"`` line of a
    ``stream-json`` run (the CLI may print more events after it). ``None`` when
    there is none (bare text, a killed run)."""
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        envelope = None
        for line in reversed(stdout.splitlines()):
            if '"result"' not in line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("type") == "result":
                return event
    # A one-line stream cut after ``system/init`` parses whole too; only a result
    # envelope (or an untyped legacy one) counts.
    if isinstance(envelope, dict) and envelope.get("type", "result") == "result":
        return envelope
    return None


def claude_result_text(stdout: str) -> str:
    """The model text from the result envelope (its ``result`` field), for
    either output format; a stdout with no envelope (older CLI, bare text) is
    returned as-is."""
    envelope = claude_result_envelope(stdout)
    if envelope is not None and isinstance(envelope.get("result"), str):
        return envelope["result"]
    return stdout


async def run_claude_no_tools(
    prompt: str, *, timeout: float, run: Any = None, prefix: str = "belt-foreman-"
) -> str:
    """One tool-less ``claude -p`` call for a seat that reads third-party text.

    No tools (``--tools ""``), a fresh empty temp dir as cwd (nothing on disk
    to read even if a tool slipped through), the prompt on stdin, and the
    develop station's runner, which passes only the scrubbed env. ``run`` is
    injectable for tests. Returns the model text; a non-zero exit raises
    ``RuntimeError`` with the output redacted."""
    from pocketpaw.security.redact import redact_output
    from pocketpaw_ee.cloud.belt.develop_station import run_subprocess

    with tempfile.TemporaryDirectory(prefix=prefix) as cwd:
        code, out, err = await (run or run_subprocess)(
            claude_cli_argv("--tools", ""),
            cwd=Path(cwd),
            timeout=timeout,
            stdin=prompt,
        )
    if code != 0:
        detail = redact_output((err or out).strip())[:300]
        raise RuntimeError(f"claude CLI failed (exit {code}): {detail}")
    return claude_result_text(out)


@dataclass
class ClaudeCliLlm:
    """Default transport — the foreman reads third-party text (sightings), so
    it runs through ``run_claude_no_tools``. ``run`` is the develop station's
    subprocess runner (injectable for tests)."""

    run: Any = None

    async def plan(self, *, prompt: str, context: ForemanContext) -> str:
        return await run_claude_no_tools(prompt, timeout=_cli_timeout(), run=self.run)


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

    Default behavior: one task per open sighting that is not already in flight
    (highest severity first) capped at the charter budget; an explicit no_action
    plan when nothing is left to plan.
    ``set_mock_plan`` overrides the response entirely."""

    async def plan(self, *, prompt: str, context: ForemanContext) -> str:
        if _MOCK_PLAN is not None:
            return _MOCK_PLAN if isinstance(_MOCK_PLAN, str) else json.dumps(_MOCK_PLAN)

        budget = shift_budget(context)
        kpis = context.charter.get("kpis") or []
        kpi_hint = f"{kpis[0]['name']} {kpis[0]['direction']}" if kpis else "surface health up"
        ranked = sorted(
            (s for s in context.sightings if not s.get("in_flight")),
            key=lambda s: int(s.get("severity") or 0),
            reverse=True,
        )
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


def shift_budget(context: ForemanContext) -> int:
    """This shift's task budget: the line's headroom when the caller measured
    it, else the charter's ``max_tasks_per_shift``."""
    charter_max = int((context.charter.get("budget") or {}).get("max_tasks_per_shift") or 3)
    return int(context.capacity.get("budget", charter_max))


def resolve_llm() -> PlanLlm:
    """Pick the transport from ``POCKETPAW_MANDATE_LLM`` (``claude`` default)."""
    choice = (os.environ.get("POCKETPAW_MANDATE_LLM") or "claude").strip().lower()
    if choice == "mock":
        return MockLlm()
    return ClaudeCliLlm()


# ---------------------------------------------------------------------------
# Prompt — every sim-validated rule lives here
# ---------------------------------------------------------------------------


def _sighting_line(s: dict[str, Any]) -> str:
    """One open sighting: new or carried over, plus every task that cited it
    (``IN FLIGHT`` when one is still being worked)."""
    tags = ["new" if s.get("new") else "carried over"]
    tasks = s.get("tasks") or []
    if tasks:
        tags.append(
            "tasks: "
            + "; ".join(f'shift {t["shift_no"]} "{t["title"]}" {t["status"]}' for t in tasks)
        )
    if s.get("in_flight"):
        tags.append("IN FLIGHT")
    if s.get("open_pr"):
        tags.append(f"open PR #{s['open_pr']}")
    return (
        f"- id={s['id']} patrol={s.get('patrol')} severity={s.get('severity')} "
        f"[{' | '.join(tags)}]: {s.get('summary')}"
    )


# A ``Depends on:`` note names at most this many issue states, and the notes in
# one prompt stop at this many characters (the line itself is capped upstream).
_DEPENDS_REFS_CAP = 10
_DEPENDS_PROMPT_CAP = 3000


def _depends_note(s: dict[str, Any], states: dict[int, str]) -> str:
    """The sighting's ``Depends on:`` line in full, with the state of each
    issue it names when known (``#4 closed, #5 open``); "" when it has none."""
    line = s.get("depends_on")
    if not line:
        return ""
    refs = [int(n) for n in re.findall(r"#(\d+)", line)][:_DEPENDS_REFS_CAP]
    known = ", ".join(f"#{n} {states[n]}" for n in refs if n in states)
    return f"\n    {line}" + (f" ({known})" if known else "")


def _history_lines(h: dict[str, Any]) -> str:
    """One past shift: its outcome, each planned task's run result (with the
    failure reason), and any gate note."""
    lines = [f"- shift {h.get('no')}: state={h.get('state')} outcome={h.get('outcome') or 'n/a'}"]
    for t in h.get("tasks") or []:
        refs = ", ".join(t.get("evidence_refs") or []) or "none"
        line = f'    - task "{t.get("title")}" (cites {refs}): {t.get("status")}'
        if t.get("line"):
            line += f" ({t['line']})"
        if t.get("error"):
            line += f" — reason: {str(t['error'])[:300]}"
        if t.get("in_flight"):
            line += " (IN FLIGHT)"
        lines.append(line)
    lines += [f"    - at the gate: {note}" for note in h.get("gate") or []]
    return "\n".join(lines)


def build_prompt(context: ForemanContext) -> str:
    """Assemble the single judgment prompt. Charter rides VERBATIM (as JSON)
    with the BOUNDARIES block pulled out and stated first — boundaries override
    every KPI opportunity."""
    charter = context.charter
    boundaries = list(charter.get("boundaries") or [])
    says_no = list(charter.get("says_no") or [])
    budget = shift_budget(context)

    rows, room = [], _DEPENDS_PROMPT_CAP
    for s in context.sightings:
        note = _depends_note(s, context.issue_states)
        note = note if len(note) <= room else ""
        room -= len(note)
        rows.append(_sighting_line(s) + note)
    sighting_lines = (
        "\n".join(rows)
        or "(none — every sighting is resolved by a landed task or IN FLIGHT below, "
        "or the surface is quiet)"
    )
    in_flight_lines = "\n".join(_sighting_line(s) for s in context.in_flight) or "(none)"
    cap = context.capacity
    if cap:
        in_flight_lines += (
            "\nTasks in flight on this line (runs, and plans waiting at the plan gate): "
            f"{cap.get('in_flight', 0)} of a cap of "
            f"{cap.get('cap', budget)}, so this shift may start at most {budget} more."
        )
    if context.open_total > len(context.sightings):
        sighting_lines += (
            f"\n(showing {len(context.sightings)} of {context.open_total} open sightings: "
            "highest severity first, then oldest)"
        )
    history_lines = "\n".join(_history_lines(h) for h in context.history) or "(no prior shifts)"
    line = context.line
    if not line.get("exists"):
        line_lines = "(nothing has landed on the line yet)"
    elif line.get("merged"):
        line_lines = f"(everything on {line['branch']} is merged into {line['base']})"
    else:
        line_lines = "\n".join(
            [
                f"{line['branch']}: {line['ahead']} commit(s) not in {line['base']} yet, "
                "awaiting the captain's merge:",
                *(f"- {s}" for s in line.get("subjects") or []),
            ]
        )
    soul_lines = "\n".join(f"- {line}" for line in context.soul_context) or "(none)"
    recipe_names = sorted((charter.get("recipes") or {}).keys())
    recipe_lines = "\n".join(f"- {name}" for name in recipe_names) or "(none)"
    architecture_lines = "\n".join(context.architecture) or "(no C4 model for this repo)"

    return f"""You are the FOREMAN of a standing engineering mandate. Once per shift you fill \
the crew's free slots with the most important plannable work in the backlog, or plan nothing \
when nothing is plannable. You are judged on judgment: the right work, and no idle slots.

== BOUNDARIES (ABSOLUTE — these override every KPI opportunity) ==
{json.dumps(boundaries, indent=2)}
The mandate also SAYS NO to:
{json.dumps(says_no, indent=2)}
If a tempting task would cross a boundary, you refuse it. When refusing, you may name the \
forbidden thing in your reasoning — that is correct behavior.

== CHARTER (verbatim) ==
{json.dumps(charter, indent=2)}

== OPEN SIGHTINGS (the backlog: no landed task has resolved these yet) ==
{sighting_lines}
A sighting stays open until a task citing it LANDS. "new" arrived since the last shift; \
"carried over" is older and still unresolved. A task marked failed, develop failed or \
rejected did not resolve its sighting. A "Depends on:" line under a sighting names the \
issues it needs, with their state when known: plan it only once those are closed.

== IN FLIGHT (already being worked: never plan these again) ==
{in_flight_lines}

== LAST SHIFTS' OUTCOMES (oldest first) ==
{history_lines}
Never repeat an approach that already failed above without explicitly stating in the task's \
"why" what is different this time.

== THE LINE (this mandate's branch; every commit here is BUILT) ==
{line_lines}
Each run starts from the line, so later tasks build on what landed there. Never plan that \
work again, merged or not; merging the line into the base is the captain's call.

== SOUL CONTEXT (long-lived memory of this mandate) ==
{soul_lines}

== EXISTING ARCHITECTURE (the repo's C4 model — the source of truth for what exists) ==
{architecture_lines}

== RECIPES (named deterministic commands the crew can run) ==
{recipe_lines}
When a task is exactly what a recipe does, set its "recipe" to that name and the crew runs \
the command instead of writing code. Use ONLY a name listed above; otherwise "recipe": null.

== YOUR RULES ==
1. Plan AT MOST {budget} task(s) this shift, and FILL the shift up to {budget}. Take the open \
sightings in priority order: higher severity first (p1 before p2 before p3), then oldest. \
Skip a sighting whose "Depends on:" issues are still open, and one that is not independent \
of a task you already picked (rule 6). Plan fewer than {budget} only when no more \
independent, plannable work remains, and then say why in "shortfall_reason": which \
sightings you skipped and why (an open dependency, not independent, a boundary).
2. Every task MUST cite at least one sighting id in "evidence_refs" and MUST name an \
expected KPI and its direction in "expected_outcome" (e.g. "open_cves down").
3. An EMPTY plan is a correct, respected outcome: if the signals are quiet (or every open \
sighting is already IN FLIGHT) and the KPIs are healthy, set "no_action": true with a short \
"no_action_reason" and an empty "tasks" list. Do not invent work.
4. Boundaries override KPI opportunities — a boundary-crossing task is never worth it.
5. This is shift number {context.shift_no}; set "shift_no" to exactly {context.shift_no}.
6. Tasks in one shift must be INDEPENDENT of each other. Each is developed from the same \
line tip and lands on its own, so a task never sees another task's change from this \
shift. Never plan a task that builds on, extends, or needs another task in this shift; \
plan the first step now and leave the dependent follow-up for a later shift.
7. A task that is IN FLIGHT (queued, developing, approved, or pending at a gate) is already \
being worked. Never plan the same work again, even under a new title; its sighting stays \
open until it lands, and that is expected.
8. A task EXTENDS the existing components listed under EXISTING ARCHITECTURE wherever one \
covers the work; name the component it extends in the task's "why". Never plan a new \
component, module or service that duplicates one listed there.

== OUTPUT (STRICT) ==
Reply with STRICT JSON only — no prose, no markdown fences, no commentary:
{{"shift_no": {context.shift_no}, "no_action": false, "no_action_reason": null, \
"shortfall_reason": null, "tasks": \
[{{"title": "...", "why": "...", "evidence_refs": ["<sighting id>"], "expected_outcome": \
"<kpi> <up|down>; ...", "est_cost_hours": 1.0, "recipe": null}}]}}"""


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


def drop_in_flight(plan: PlanProposal, in_flight_ids: set[str]) -> PlanProposal:
    """The plan without any task that cites an in-flight sighting (a task or an
    open PR already works it). A plan left empty stands down. Dropping, not
    refusing: a refused plan leaves the shift in ``planning``, which holds a
    continuous line until the stall timeout."""
    kept = [t for t in plan.tasks if not in_flight_ids & {str(r) for r in t.evidence_refs}]
    if len(kept) == len(plan.tasks):
        return plan
    logger.info(
        "foreman: dropped %d task(s) of shift %s citing in-flight sightings",
        len(plan.tasks) - len(kept),
        plan.shift_no,
    )
    if kept:
        return plan.model_copy(update={"tasks": kept})
    return plan.model_copy(
        update={
            "tasks": [],
            "no_action": True,
            "no_action_reason": "every planned task cites a sighting already in flight",
        }
    )


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
    recipes = charter.get("recipes") or {}
    for i, task in enumerate(plan.tasks):
        if task.recipe and task.recipe not in recipes:
            violations.append(
                f"task {i + 1} ({task.title[:40]!r}) names recipe {task.recipe!r}, "
                "which the charter does not declare"
            )
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
    "claude_cli_argv",
    "claude_result_text",
    "cli_model",
    "drop_in_flight",
    "parse_plan",
    "plan_shift",
    "resolve_llm",
    "run_claude_no_tools",
    "set_mock_plan",
    "shift_budget",
    "validate_plan",
]
