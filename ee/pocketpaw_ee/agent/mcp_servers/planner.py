# Created: 2026-05-17 — pocketpaw#1118 P1. In-process MCP server exposing
#   the cloud planner as a single ``plan_project`` tool the running agent
#   can invoke from inside an SSE chat stream. Mirrors the pattern in
#   ``sdk_mcp_tasks.py`` and ``sdk_mcp_pocket.py``: the agent identity
#   comes from the per-stream ContextVars in
#   ``ee.cloud.chat.agent_service``; outside an SSE stream the tool
#   returns a clear MCP error rather than silently mis-tenanting.
# Updated: 2026-05-25 (feat/pocket-planner-skill) — added the ``plan_pocket``
#   sibling tool. Reuses ``PlannerAgent.plan()`` with the POCKET_*
#   prompt family from ``pocketpaw.deep_work.prompts`` and skips the
#   team-assembly phase by calling the helper ``_plan_pocket_pipeline``
#   here rather than ``PlannerAgent.plan()`` (which always assembles a
#   team). Output is a structured brief the chat agent renders in
#   markdown — no project_id, no DB persistence, no DeepWorkSession
#   state machine. Iteration is stateless: the chat agent passes
#   ``prior_plan`` + ``iteration_delta`` back in on the next call.
# Updated: 2026-05-25 (R2 fix for PR #1223) — split the planner module
#   into TWO in-process MCP servers so the OPT_IN gate stays accurate:
#     * ``pocketpaw_planner``         hosts ``plan_project`` only (opt-in)
#     * ``pocketpaw_pocket_planner``  hosts ``plan_pocket`` only (ambient)
#   The original single-server design conflated two policy regimes —
#   the per-server OPT_IN_MCP_SERVERS frozenset could not gate one
#   tool ambient + one opt-in on a single server. Splitting keeps
#   plan_project under the existing opt-in regime (Mission Control
#   only) while letting plan_pocket reach the pocket-create flow with
#   no extra opt-in plumbing. Also hardened the PRD parser
#   (heading-level / case / trailing-punctuation tolerance) and added
#   a balanced-bracket JSON extractor for the task-breakdown step so
#   LLM drift (trailing prose, ``#`` comments, trailing commas) no
#   longer silently produces an empty todo list.
"""Agent-side MCP surface for the cloud Planner entities.

Two in-process MCP servers live in this module:

  - ``pocketpaw_planner`` — hosts ``plan_project(project_id, goal,
    deep_research=False)``. Invokes
    ``ee.cloud.planner.service.agent_plan_project`` to drive the full
    deep_work planner against a workspace Project. **Opt-in** via the
    per-agent ``mcp_servers_allow`` policy (Mission Control only).
  - ``pocketpaw_pocket_planner`` — hosts ``plan_pocket(intent,
    prior_plan=None, iteration_delta=None, deep_research=False)``.
    Runs the deep_work planner pipeline (sans team assembly) against
    pocket-flavored prompts and returns a structured brief
    (narrative + widgets + state + sources + actions + ordered
    todos). The chat agent renders it as markdown, iterates with the
    user, then walks the todos calling /spec/merge per todo.
    Ephemeral: no Project, no PlanSession, no DB row. **Ambient** —
    the pocket-create skill needs to call it without explicit opt-in.

The agent identity is resolved through the same chokepoint the pocket
and tasks MCP servers use — see ``sdk_mcp_tasks._identity`` for the
contract. ``mcp__pocketpaw_planner__plan_project`` and
``mcp__pocketpaw_pocket_planner__plan_pocket`` are the canonical
allowlist ids.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_planner"
POCKET_PLANNER_SERVER_NAME = "pocketpaw_pocket_planner"

PLAN_PROJECT_TOOL_ID = f"mcp__{SERVER_NAME}__plan_project"
PLAN_POCKET_TOOL_ID = f"mcp__{POCKET_PLANNER_SERVER_NAME}__plan_pocket"

# ``PLANNER_TOOL_IDS`` is the allowlist tuple for the opt-in
# ``pocketpaw_planner`` server. It used to carry both tool ids; the
# split moved ``plan_pocket`` onto ``POCKET_PLANNER_TOOL_IDS`` (its own
# always-on server). Callers iterating tool ids for the allowlist must
# concatenate both tuples — done in ``extensions.py`` via two
# providers, one per server.
PLANNER_TOOL_IDS = (PLAN_PROJECT_TOOL_ID,)
POCKET_PLANNER_TOOL_IDS = (PLAN_POCKET_TOOL_ID,)


def _error_response(message: str) -> dict[str, Any]:
    """MCP error envelope. Matches the shape Claude's SDK expects."""

    return {
        "content": [{"type": "text", "text": f"Error: {message}"}],
        "is_error": True,
    }


def _success_response(body: dict[str, Any]) -> dict[str, Any]:
    """MCP success envelope. Body is JSON-encoded into a text block."""

    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(body, separators=(",", ":"), default=str),
            }
        ]
    }


def _identity() -> tuple[str | None, str | None]:
    """Resolve workspace + agent (user) id from per-stream ContextVars.

    Mirrors ``sdk_mcp_tasks._identity`` so when the planner tool is
    invoked from outside an SSE chat stream, both values come back
    ``None`` and the handler returns a clear error instead of
    silently mis-tenanting.
    """

    try:
        from pocketpaw_ee.cloud.chat.agent_service import current_user_id, current_workspace_id

        return current_workspace_id(), current_user_id()
    except Exception:
        return None, None


def _build_ctx(workspace_id: str, user_id: str):
    """Build a ``RequestContext`` for service calls from the MCP tool
    channel. Same approach the tasks MCP server uses."""

    from datetime import UTC, datetime

    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind

    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="mcp",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


async def _plan_project_handler(args: dict) -> dict:
    workspace_id, agent_id = _identity()
    if not workspace_id or not agent_id:
        return _error_response(
            "no active workspace/agent — plan_project can only be called "
            "from inside a cloud SSE chat stream"
        )

    project_id = (args or {}).get("project_id") or ""
    goal = (args or {}).get("goal") or ""
    deep_research = bool((args or {}).get("deep_research", False))

    if not project_id:
        return _error_response("project_id is required")
    if not goal:
        return _error_response("goal is required")

    try:
        from pocketpaw_ee.cloud._core.errors import CloudError
        from pocketpaw_ee.cloud.planner import service as planner_service
        from pocketpaw_ee.cloud.planner.dto import PlanProjectRequest
    except ImportError as exc:  # pragma: no cover — defensive
        return _error_response(f"planner module not installed: {exc}")

    ctx = _build_ctx(workspace_id, agent_id)
    try:
        response = await planner_service.agent_plan_project(
            ctx,
            PlanProjectRequest(
                project_id=project_id,
                goal=goal,
                deep_research=deep_research,
            ),
        )
    except CloudError as exc:
        return _error_response(f"{exc.code}: {exc.message}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("plan_project failed", exc_info=True)
        return _error_response(f"plan_project failed: {exc}")

    return _success_response({"ok": True, "plan": response.model_dump()})


# ---------------------------------------------------------------------------
# plan_pocket — pocket-flavored planner that returns an ephemeral brief.
#
# Reuses ``PlannerAgent._run_prompt`` + ``_parse_tasks`` for the LLM
# orchestration and JSON-coercion. The pipeline is inlined here (rather
# than a method on PlannerAgent) because pocket planning skips the team-
# assembly phase, builds no Project, and the brief schema is shaped for
# markdown rendering, not Mission Control materialization.
# ---------------------------------------------------------------------------


_HEADING_RE = re.compile(r"^(#{2,3})\s+(.+?)\s*$")


def _normalize_heading_token(text: str) -> str:
    """Strip Markdown/punctuation noise an LLM may add around a heading
    name and upper-case the result. Tolerates ``**bold**`` wrappers,
    trailing colons, lowercase, leading/trailing whitespace. Returns
    the cleaned token (still upper-cased) so the caller can match it
    against the canonical section names.
    """

    cleaned = text.strip()
    # Repeatedly peel ``**...**`` / ``*...*`` wrappers so ``**Narrative**``
    # and ``*Narrative*`` both reduce to ``Narrative``.
    while len(cleaned) >= 4 and cleaned.startswith("**") and cleaned.endswith("**"):
        cleaned = cleaned[2:-2].strip()
    while len(cleaned) >= 2 and cleaned.startswith("*") and cleaned.endswith("*"):
        cleaned = cleaned[1:-1].strip()
    cleaned = cleaned.rstrip(":").rstrip("-").strip()
    return cleaned.upper()


def _parse_pocket_prd_sections(prd_text: str) -> dict[str, str]:
    """Split the PRD into its five labelled sections.

    The PRD prompt instructs the LLM to use exactly these headings:
    ``## NARRATIVE``, ``## WIDGETS``, ``## STATE``, ``## SOURCES``,
    ``## ACTIONS``. In practice LLM drift produces variants the brain
    accepts but the parser would silently drop: lowercased
    (``## narrative``), trailing colons (``## NARRATIVE:``), one extra
    hash level (``### NARRATIVE``), bold wrappers (``**Narrative**``).
    We accept all of those — the contract is "the heading text equals
    one of the canonical names, case-insensitively, after stripping
    noise punctuation and Markdown emphasis markers".

    Missing sections come back as empty strings so the caller can
    branch on that rather than KeyError-handling.
    """

    sections: dict[str, str] = {
        "NARRATIVE": "",
        "WIDGETS": "",
        "STATE": "",
        "SOURCES": "",
        "ACTIONS": "",
    }
    if not prd_text:
        return sections

    # Walk the text line by line, capture lines under the most recent
    # heading we recognise. Headings we don't recognise close the
    # current section (so stray content does not leak).
    current: str | None = None
    buffers: dict[str, list[str]] = {k: [] for k in sections}
    for line in prd_text.splitlines():
        stripped = line.strip()
        heading_match = _HEADING_RE.match(stripped)
        if heading_match:
            name = _normalize_heading_token(heading_match.group(2))
            if name in sections:
                current = name
            else:
                current = None
            continue

        # Tolerate the LLM emitting ``**Narrative**`` as its own line
        # (no leading ``##``) — treat it as a heading too.
        if (stripped.startswith("**") and stripped.endswith("**")) or (
            stripped.startswith("*") and stripped.endswith("*")
        ):
            candidate = _normalize_heading_token(stripped)
            if candidate in sections:
                current = candidate
                continue
            # Fall through — it might be normal emphasis inside prose.

        if current is None:
            continue
        buffers[current].append(line)

    for name, lines in buffers.items():
        sections[name] = "\n".join(lines).strip()
    return sections


def _extract_first_json_array(text: str) -> str | None:
    """Pull the first ``[...]`` block out of ``text`` via balanced-bracket
    counting, tolerating prose / comments / multiple fenced blocks
    around it.

    The original task-breakdown parser ran ``re.search`` over a single
    ``` ```json ... ``` `` fence and called ``json.loads`` on the result.
    LLM drift broke that:
      * Trailing prose after the array (``[...] -- and here's why``)
      * Multiple separate code fences (the model emits a fenced thought
        block, THEN the JSON in a second fence)
      * Python-style ``#`` comments inside the array
      * Trailing commas (JSON5-style)

    This walks the text scanning for the first ``[``, then counts
    brackets — respecting double-quoted strings (with backslash
    escapes) — until the matching outer ``]``. Returns the inclusive
    substring or ``None`` if no balanced array exists. Strings inside
    the array are scanned but their content is untouched; this is just
    extraction, not validation.
    """

    start = text.find("[")
    if start < 0:
        return None

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


_HASH_COMMENT_RE = re.compile(r"(?<!\\)#.*?$", re.MULTILINE)
_TRAILING_COMMA_RE = re.compile(r",(\s*[\]\}])")


def _strip_json_noise(text: str) -> str:
    """Remove Python-style ``# comments`` and JSON5 trailing commas.

    Run this AFTER ``_extract_first_json_array`` peeled the right
    substring; the inputs here are the bracketed array body. We do not
    try to be exhaustive — JSON5 supports several relaxations but the
    two that bite us in practice are ``#`` comments (the model leaves
    inline annotations) and trailing commas after the last item.
    Removing both is safe because real JSON disallows them; if a
    string literal happens to contain ``#`` we leave it alone (we only
    strip when not inside a string — same string-scanning logic as the
    bracket extractor).
    """

    # Strip ``#`` comments only when not inside a string literal. We
    # walk the text once, copying chars into a buffer and skipping
    # comment runs. The regex form would also clip ``#`` inside a JSON
    # string ("foo#bar"), which is a no-go.
    out: list[str] = []
    in_string = False
    escape = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "#":
            # Skip to end of line (do not consume the newline so block
            # structure is preserved).
            while i < n and text[i] != "\n":
                i += 1
            continue
        out.append(ch)
        i += 1
    stripped = "".join(out)
    # Trailing commas: ``,]`` → ``]`` and ``,}`` → ``}``. Safe outside
    # strings because we've already stripped comments; remaining
    # commas in strings are preserved by the regex (it requires the
    # following character to be a closing bracket).
    return _TRAILING_COMMA_RE.sub(r"\1", stripped)


def _parse_lenient_json_list(raw: str) -> tuple[list[dict] | None, str | None]:
    """Lenient JSON-list parser used by the pocket task-breakdown step.

    Returns ``(parsed_list, error_message)`` where ``parsed_list`` is
    None on failure and ``error_message`` carries a short diagnostic
    (suitable for surfacing in MCP warnings so the captain can see
    what was attempted). On success ``error_message`` is None.

    Strategy:
      1. Pull the first balanced ``[...]`` block out of the raw text.
      2. Strip ``#`` comments and trailing commas inside it.
      3. ``json.loads`` the result.

    Any of those steps can fail — we keep the original raw text in
    the diagnostic so a follow-up retry can include the surface error.
    """

    if not raw:
        return None, "empty model output"
    extracted = _extract_first_json_array(raw)
    if extracted is None:
        return None, f"no balanced JSON array in model output: {raw[:200]!r}"
    cleaned = _strip_json_noise(extracted)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        return None, f"JSON parse failed ({exc}); attempted: {cleaned[:300]!r}"
    if not isinstance(data, list):
        return None, f"expected a JSON array, got {type(data).__name__}: {cleaned[:200]!r}"
    return [item for item in data if isinstance(item, dict)], None


def _parse_bullet_pairs(section_text: str) -> list[tuple[str, str]]:
    """Parse a ``- left: right`` bullet list into (left, right) tuples.

    Used by the WIDGETS / STATE / SOURCES / ACTIONS section parsers. A
    line missing the colon becomes ``(line, "")`` so downstream callers
    can still surface the raw text in the brief. Empty / non-bullet
    lines are skipped silently.
    """

    pairs: list[tuple[str, str]] = []
    for raw_line in section_text.splitlines():
        line = raw_line.strip()
        if not line.startswith("- "):
            continue
        body = line[2:].strip()
        if not body:
            continue
        if ":" in body:
            left, right = body.split(":", 1)
            pairs.append((left.strip(), right.strip()))
        else:
            pairs.append((body, ""))
    return pairs


def _build_pocket_brief(
    *,
    prd: str,
    tasks: list[Any],
    research_notes: str,
) -> dict[str, Any]:
    """Turn the planner's raw PRD + parsed tasks into the brief schema.

    Brief shape (see the design doc in P7):
      ``{narrative, widgets, state, sources, actions, todos, research_notes}``
    Each entry is a plain dict — the chat agent renders the brief as
    markdown straight from this dict. We keep ``research_notes`` in the
    brief so the iteration call can show the user why a widget was
    chosen.
    """

    sections = _parse_pocket_prd_sections(prd)

    widgets = [
        {"type": left, "purpose": right} for left, right in _parse_bullet_pairs(sections["WIDGETS"])
    ]

    state: dict[str, dict[str, str]] = {}
    for left, right in _parse_bullet_pairs(sections["STATE"]):
        # Right side is ``<type> — <purpose>``. Em-dash and hyphen-
        # surrounded-by-spaces both come back from LLMs, so we split on
        # whichever variant the model emitted.
        purpose = ""
        type_part = right
        for sep in (" — ", " - ", " — "):
            if sep in right:
                type_part, purpose = right.split(sep, 1)
                break
        state[left] = {
            "type": type_part.strip(),
            "purpose": purpose.strip(),
        }

    sources: list[dict[str, str]] = []
    for connector, body in _parse_bullet_pairs(sections["SOURCES"]):
        sources.append({"connector": connector, "feeds": body})

    actions: list[dict[str, str]] = []
    for trigger, body in _parse_bullet_pairs(sections["ACTIONS"]):
        actions.append({"trigger": trigger, "effect": body})

    todos: list[dict[str, Any]] = []
    for t in tasks:
        # TaskSpec dataclass — use to_dict() so we don't depend on the
        # internal field set here. We project a small subset (label,
        # success_criteria, preconditions, depends_on, description) into
        # the brief — that's all the build phase needs.
        d = t.to_dict() if hasattr(t, "to_dict") else dict(t)
        todos.append(
            {
                "id": d.get("key", ""),
                "label": d.get("title", ""),
                "description": d.get("description", ""),
                "success_criteria": list(d.get("success_criteria", [])),
                "preconditions": list(d.get("preconditions", [])),
                "depends_on": list(d.get("blocked_by_keys", [])),
                "tags": list(d.get("tags", [])),
                "estimated_minutes": d.get("estimated_minutes", 0),
            }
        )

    return {
        "narrative": sections["NARRATIVE"],
        "widgets": widgets,
        "state": state,
        "sources": sources,
        "actions": actions,
        "todos": todos,
        "research_notes": research_notes,
    }


def _compose_intent_with_iteration(
    intent: str,
    prior_plan: dict[str, Any] | None,
    iteration_delta: str | None,
) -> str:
    """Splice iteration context into the brief the LLM sees.

    On the first call, ``prior_plan`` and ``iteration_delta`` are both
    None and we pass the intent through. On a follow-up call the chat
    agent supplies the previous brief plus the user's revision text;
    we append both as labelled blocks so the LLM iterates instead of
    re-planning from scratch.
    """

    if not prior_plan and not iteration_delta:
        return intent

    parts: list[str] = [f"USER BRIEF:\n{intent}"]
    if iteration_delta:
        parts.append(f"\nUSER REVISION REQUEST:\n{iteration_delta}")
    if prior_plan:
        # Serialize concisely so we don't blow the context window.
        parts.append(
            "\nPRIOR PLAN (the user just revised this — keep what they did "
            "not call out, apply the revision above):\n"
            + json.dumps(prior_plan, separators=(",", ":"), default=str)
        )
    return "\n".join(parts)


def _lenient_parse_taskspecs(raw: str) -> tuple[list[Any], list[str]]:
    """Parse the task-breakdown LLM output into TaskSpec instances.

    Tries the lenient extractor first (balanced-bracket + comment /
    trailing-comma scrub); on failure falls back to PlannerAgent's
    ``_parse_tasks`` so the original behaviour is preserved.

    Returns ``(tasks, warnings)``. ``warnings`` carries any diagnostic
    messages from the parse attempts — empty on a clean pass. The
    handler surfaces these so the captain can see what was attempted
    when the LLM drifts.
    """

    from pocketpaw.deep_work.models import TaskSpec
    from pocketpaw.deep_work.planner import PlannerAgent

    warnings: list[str] = []
    data, err = _parse_lenient_json_list(raw)
    if data is None:
        if err:
            warnings.append(f"lenient parse: {err}")
        # Fall back to the strict regex parser — if the model emitted a
        # clean ``` ```json``` ``` fence this still works.
        legacy = PlannerAgent(manager=None)  # type: ignore[arg-type]
        fallback = legacy._parse_tasks(raw)
        if fallback:
            return fallback, warnings
        return [], warnings
    return [TaskSpec.from_dict(item) for item in data], warnings


async def _run_pocket_planner_pipeline(
    intent: str,
    *,
    deep_research: bool,
) -> tuple[dict[str, Any], list[str]]:
    """Run the pocket-flavored 3-phase planner (research → PRD → todos).

    Returns ``(brief, warnings)``. The brief is the dict the MCP tool
    body wraps; warnings are diagnostic strings the handler surfaces
    so the captain can see what the LLM emitted when a parse failed.

    Mirrors PlannerAgent.plan() but with the POCKET_* prompt family
    and no team-assembly phase. We do NOT broadcast SystemEvents here —
    the MCP tool boundary is the progress surface, not the deep_work
    bus.
    """

    from pocketpaw.deep_work.planner import PlannerAgent
    from pocketpaw.deep_work.prompts import (
        POCKET_PRD_PROMPT,
        POCKET_RESEARCH_PROMPT,
        POCKET_TASK_BREAKDOWN_PROMPT,
    )

    # ``manager=None`` is safe here even though PlannerAgent's __init__
    # types it as required: the only PlannerAgent methods we touch
    # (``_run_prompt`` and ``_parse_tasks``) never reach into
    # ``self.manager`` — ``ensure_profile`` and ``_broadcast_phase``
    # are the two that do, and we don't call them. TODO(PR #1223
    # follow-up): refactor PlannerAgent so the prompt-runner + task
    # parser are static helpers on a separate class and the optional-
    # manager landmine goes away.
    planner = PlannerAgent(manager=None)  # type: ignore[arg-type]

    from pocketpaw.agents.router import AgentRouter
    from pocketpaw.config import get_settings

    router = AgentRouter(get_settings())

    warnings: list[str] = []

    # Phase 1 — research. ``deep_research`` only toggles a couple of
    # paragraph-count knobs; pocket research is always opinionated.
    research_prompt = POCKET_RESEARCH_PROMPT.format(project_description=intent)
    if deep_research:
        research_prompt = (
            research_prompt + "\n\nBe THOROUGH — include 3-4 similar pockets, "
            "not 0-3. Add an extra paragraph on tradeoffs between the focal "
            "widget options."
        )
    research_notes = await planner._run_prompt(research_prompt, router=router)

    # Phase 2 — PRD.
    prd = await planner._run_prompt(
        POCKET_PRD_PROMPT.format(
            project_description=intent,
            research_notes=research_notes,
        ),
        router=router,
    )

    # Phase 3 — todos. JSON output. Use the lenient parser so common
    # LLM drift (trailing prose, ``#`` comments, trailing commas,
    # multiple code fences) does not silently produce an empty todo
    # list. On failure we retry with the explicit-JSON hint — same
    # one-retry budget the upstream planner uses.
    tasks_raw = await planner._run_prompt(
        POCKET_TASK_BREAKDOWN_PROMPT.format(
            project_description=intent,
            prd_content=prd,
            research_notes=research_notes,
        ),
        router=router,
    )
    tasks, parse_warnings = _lenient_parse_taskspecs(tasks_raw)
    warnings.extend(parse_warnings)
    if not tasks:
        # One retry with the explicit-JSON hint, same logic plan() uses.
        # Include the original output snippet in the warning so a
        # debugging captain can see exactly what the model emitted.
        logger.info("plan_pocket: retrying task breakdown with explicit JSON hint")
        warnings.append(
            "task-breakdown first attempt produced no todos; "
            f"raw output (first 300 chars): {tasks_raw[:300]!r}"
        )
        tasks_raw = await planner._run_prompt(
            "Your previous response was not valid JSON. Return ONLY a "
            "JSON array of todo objects, no markdown, no explanation — "
            "just the raw JSON array.\n\n"
            + POCKET_TASK_BREAKDOWN_PROMPT.format(
                project_description=intent,
                prd_content=prd,
                research_notes=research_notes,
            ),
            router=router,
        )
        tasks, retry_warnings = _lenient_parse_taskspecs(tasks_raw)
        warnings.extend(retry_warnings)
        if not tasks:
            warnings.append(
                "task-breakdown retry also failed; "
                f"raw output (first 300 chars): {tasks_raw[:300]!r}"
            )

    brief = _build_pocket_brief(
        prd=prd,
        tasks=tasks,
        research_notes=research_notes,
    )
    return brief, warnings


async def _plan_pocket_handler(args: dict) -> dict:
    """Resolve the args envelope, run the pipeline, return the brief.

    Identity check matches ``_plan_project_handler``: outside an SSE
    chat stream this tool must NOT silently mis-tenant. The pocket
    planner doesn't actually write any tenant-scoped data, but the
    identity gate is the contract every cloud MCP tool honours so the
    tool surface stays uniform — and if we later wire bundled-KB
    lookup off the workspace id, the gate is already in place.
    """

    workspace_id, agent_id = _identity()
    if not workspace_id or not agent_id:
        return _error_response(
            "no active workspace/agent — plan_pocket can only be called "
            "from inside a cloud SSE chat stream"
        )

    args = args or {}
    intent = (args.get("intent") or "").strip()
    if not intent:
        return _error_response("intent is required")

    prior_plan = args.get("prior_plan")
    if prior_plan is not None and not isinstance(prior_plan, dict):
        return _error_response("prior_plan must be a dict (a brief returned from a previous call)")

    iteration_delta = args.get("iteration_delta")
    if iteration_delta is not None and not isinstance(iteration_delta, str):
        return _error_response("iteration_delta must be a string")

    deep_research = bool(args.get("deep_research", False))

    composed_intent = _compose_intent_with_iteration(
        intent,
        prior_plan if isinstance(prior_plan, dict) else None,
        iteration_delta if isinstance(iteration_delta, str) else None,
    )

    try:
        brief, warnings = await _run_pocket_planner_pipeline(
            composed_intent,
            deep_research=deep_research,
        )
    except RuntimeError as exc:
        # LLM call failed (API key missing, transport error, …) —
        # _run_prompt raises this so the failure surfaces cleanly
        # rather than silently returning an empty brief.
        return _error_response(f"plan_pocket failed: {exc}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("plan_pocket failed", exc_info=True)
        return _error_response(f"plan_pocket failed: {exc}")

    body: dict[str, Any] = {"ok": True, "brief": brief}
    if warnings:
        # Surface parser drift so the captain can see what the model
        # emitted. The chat agent treats this as informational —
        # ``ok: True`` still means the brief is renderable.
        body["warnings"] = warnings
    return _success_response(body)


def build_planner_context_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server for the **opt-in** project
    planner (Mission Control). Returns ``None`` when the Claude Agent
    SDK isn't installed.

    This server hosts ``plan_project`` only. The sibling
    ``pocketpaw_pocket_planner`` server (built by
    :func:`build_pocket_planner_context_server`) hosts the ambient
    ``plan_pocket`` tool. The split is what restores the per-server
    OPT_IN_MCP_SERVERS gate to working order — see the module
    docstring and PR #1223 R2 review for the why.

    Matches the shape returned by ``build_tasks_context_server`` so the
    backend's MCP registration loop in ``claude_sdk.py`` treats this
    identically to the other in-process servers.
    """

    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_planner MCP disabled")
        return None

    @tool(
        "plan_project",
        (
            "Plan a cloud Project end-to-end: research the domain, draft a "
            "PRD, decompose the work into Mission Control Tasks, and "
            "recommend a team. Wraps PocketPaw's deep_work planner. "
            "Returns ``{ok: True, plan}`` where ``plan`` carries the "
            "materialized ``prd_file_id``, ``task_ids``, and any "
            "``agent_gaps`` (planner-recommended agents missing from the "
            "workspace). The operator should re-route human tasks from the "
            "Mission Control tray and decide whether each agent_gap is "
            "worth creating a new cloud Agent for. Long-running — make a "
            "single call per project and let the FE show progress."
        ),
        {"project_id": str, "goal": str, "deep_research": bool},
    )
    async def plan_project(args):  # type: ignore[no-untyped-def]
        return await _plan_project_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.1.0",
        tools=[plan_project],
    )
    return SERVER_NAME, server


def build_pocket_planner_context_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server for the **ambient** pocket
    planner. Returns ``None`` when the Claude Agent SDK isn't
    installed.

    This server hosts ``plan_pocket`` only. It is registered under a
    DIFFERENT name (``pocketpaw_pocket_planner``) from the project
    planner so the per-server OPT_IN_MCP_SERVERS gate can keep
    ``plan_project`` opt-in while leaving ``plan_pocket`` reachable
    for the pocket-create flow without explicit opt-in plumbing.
    """

    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_pocket_planner MCP disabled")
        return None

    @tool(
        "plan_pocket",
        (
            "Plan a complex pocket BEFORE creating it. Use when the "
            "pocket_specialist create flow returned a ``plan_kit`` (the "
            "template-match step did not find a starter and the brief "
            "looks like a custom multi-widget app). Returns ``{ok: True, "
            "brief}`` where ``brief`` carries ``{narrative, widgets, "
            "state, sources, actions, todos}`` — render that as markdown "
            "in the chat panel, iterate with the user, then walk the "
            "todos calling POST /api/v1/pockets/<id>/spec/merge per todo. "
            "Stateless: no Project, no plan-session row. Pass ``prior_plan`` "
            "and ``iteration_delta`` on follow-up calls so the LLM "
            "iterates the prior brief instead of replanning from scratch."
        ),
        {
            "intent": str,
            "prior_plan": dict,
            "iteration_delta": str,
            "deep_research": bool,
        },
    )
    async def plan_pocket(args):  # type: ignore[no-untyped-def]
        return await _plan_pocket_handler(args)

    server = create_sdk_mcp_server(
        name=POCKET_PLANNER_SERVER_NAME,
        version="1.0.0",
        tools=[plan_pocket],
    )
    return POCKET_PLANNER_SERVER_NAME, server


__all__ = [
    "PLAN_POCKET_TOOL_ID",
    "PLAN_PROJECT_TOOL_ID",
    "PLANNER_TOOL_IDS",
    "POCKET_PLANNER_SERVER_NAME",
    "POCKET_PLANNER_TOOL_IDS",
    "SERVER_NAME",
    "build_planner_context_server",
    "build_pocket_planner_context_server",
]
