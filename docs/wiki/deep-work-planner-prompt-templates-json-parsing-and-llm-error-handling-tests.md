---
{
  "title": "Deep Work Planner: Prompt Templates, JSON Parsing, and LLM Error Handling Tests",
  "summary": "This suite tests PocketPaw's PlannerAgent, covering prompt template validation, robust JSON parsing of LLM responses (plain and code-fenced), PlannerResult construction, and a regression test for silent error swallowing when the LLM returns only error events. It documents the four-phase planning flow: research, PRD generation, task breakdown, and team assembly.",
  "concepts": [
    "PlannerAgent",
    "RESEARCH_PROMPT",
    "PRD_PROMPT",
    "TASK_BREAKDOWN_PROMPT",
    "TEAM_ASSEMBLY_PROMPT",
    "_parse_tasks",
    "_parse_team",
    "PlannerResult",
    "code_fence_parsing",
    "_run_prompt",
    "error_event_handling",
    "ensure_profile",
    "_broadcast_phase",
    "Deep_Work"
  ],
  "categories": [
    "testing",
    "deep-work",
    "planning",
    "LLM-integration",
    "error-handling",
    "test"
  ],
  "source_docs": [
    "f214c6f9867a7ce7"
  ],
  "backlinks": null,
  "word_count": 583,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`test_deep_work_planner.py` tests the `PlannerAgent` class in `pocketpaw.deep_work.planner`, which orchestrates the four-phase AI planning pipeline that converts a project description into a structured list of tasks, agents, and dependencies. The planner is among the most complex components in Deep Work, and this suite defends every parsing and error path.

## Why This Module Exists

The `PlannerAgent` makes four sequential LLM calls (research → PRD → task breakdown → team assembly) and must parse structured JSON from each. Because LLMs frequently wrap JSON in markdown code blocks and sometimes return error events instead of content, the planner needs robust extraction and error handling at every step.

## Prompt Template Tests

`TestPromptTemplates` verifies that each of the four prompt templates (`RESEARCH_PROMPT`, `PRD_PROMPT`, `TASK_BREAKDOWN_PROMPT`, `TEAM_ASSEMBLY_PROMPT`) contains the required `{placeholder}` strings. This prevents silent planning failures where a template was accidentally broken—missing a placeholder would cause Python's `.format()` to raise `KeyError` at runtime, crashing the entire planning session.

Each template is also tested for successful `.format()` invocation with sample values, confirming the templates are syntactically valid Python format strings.

## JSON Parsing — Plain and Fenced

`_parse_tasks` must handle two formats because LLMs inconsistently wrap JSON:

- **TestParseTasksPlain**: raw JSON arrays, including tasks with dependency references.
- **TestParseTasksFenced**: JSON wrapped in ` ```json `, ` ``` ` (plain fence), or surrounded by prose text.

The fence-stripping tests specifically cover the "fence with surrounding text" case, which is common when LLMs add preamble like "Here is the task breakdown:" before the JSON block.

**TestParseTasksInvalid** covers failure modes:
- Invalid JSON → returns empty list rather than raising.
- Empty string → returns empty list.
- JSON object (not list) → returns empty list.
- JSON list containing non-dict items → those items are filtered out.

These safe fallbacks prevent planning sessions from crashing on malformed LLM output. The planner can then retry or degrade gracefully.

## _parse_team

`TestParseTeam` mirrors the task parsing tests for the team assembly phase, which returns a list of `AgentSpec` objects. The same fenced-vs-plain and invalid-input patterns apply.

## PlannerResult Construction

`TestPlannerResult` confirms that a fully constructed `PlannerResult` (containing research notes, PRD text, task list, and agent specs) serializes correctly via `to_dict()`. This matters because `PlannerResult` is stored alongside the project record so the session can reference it after restart.

## ensure_profile

`TestEnsureProfile` tests the profile creation/reuse logic with a mocked `MissionControlManager`. The planner must check whether an agent profile already exists before creating a new one, preventing duplicate agent records from accumulating across multiple planning sessions.

## _broadcast_phase Resilience

Tests verify that `_broadcast_phase` does not crash when the bus is unavailable. This is important because the planner broadcasts progress updates ("Researching...", "Writing PRD...") and a missing bus connection should not abort the entire planning session.

## _run_prompt Error Event Handling (Bug Reproduction)

A comment in the source notes that `TestRunPromptErrorHandling` was added on 2026-02-16 to reproduce a specific bug: when the LLM streaming endpoint returns only error events (e.g., rate limit exceeded, context overflow), `_run_prompt` was silently returning an empty string instead of raising. Downstream code would then try to parse `""` as JSON, get an empty task list, and create a project with zero tasks — a silent failure that was difficult to diagnose.

The fix required `_run_prompt` to detect all-error event streams and raise an appropriate exception.

## Known Gaps

The four-phase flow is tested with mocked `_run_prompt` — there are no integration tests running real LLM calls. Multi-turn clarification (when the planner asks follow-up questions) is not tested here.
