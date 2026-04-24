---
{
  "title": "Deep Work Planner Agent — Four-Phase LLM Project Planning",
  "summary": "PlannerAgent orchestrates the four-phase Deep Work planning pipeline — research, PRD generation, task breakdown, and team assembly — through sequential LLM calls via AgentRouter. It produces a PlannerResult containing the PRD markdown, a list of TaskSpec objects with dependency keys, and recommended AgentSpec team members.",
  "concepts": [
    "PlannerAgent",
    "PlannerResult",
    "research depth",
    "PRD generation",
    "task breakdown",
    "team assembly",
    "AgentRouter",
    "TaskSpec",
    "AgentSpec",
    "code fence stripping",
    "phase broadcasting",
    "Deep Work planning"
  ],
  "categories": [
    "deep-work",
    "planning",
    "llm-integration",
    "orchestration"
  ],
  "source_docs": [
    "27651d2ecc8c998a"
  ],
  "backlinks": null,
  "word_count": 589,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

When a user submits a goal to Deep Work, the planning phase turns that goal into an actionable project plan. `PlannerAgent` runs four LLM calls sequentially, each building on the previous, to produce a complete plan that the user can review and approve.

## Four Planning Phases

### Phase 1: Research

The planner runs one of three research prompts based on `research_depth`:

- `"none"` — skips the LLM call entirely, passes empty notes to the PRD phase
- `"quick"` — `RESEARCH_PROMPT_QUICK`, no web search, faster
- `"standard"` — `RESEARCH_PROMPT`, standard domain research
- `"deep"` — `RESEARCH_PROMPT_DEEP`, extensive web search enabled

Skipping research for `"none"` depth was added deliberately: trivial tasks (rename a file, write a config) don't benefit from domain research, and running an unnecessary LLM call wastes tokens and slows the user experience.

### Phase 2: PRD Generation

Uses `PRD_PROMPT` to produce a Product Requirements Document from the goal and research notes. The PRD becomes the authoritative description of the project, stored as an MC Document and linked to the Project.

### Phase 3: Task Breakdown

`TASK_BREAKDOWN_PROMPT` asks the LLM to decompose the PRD into atomic tasks as a JSON array. `_parse_tasks()` strips code fences and calls `TaskSpec.from_dict()` on each item. If the JSON parse fails, the planner logs a warning and returns an empty task list rather than raising — the session layer detects the empty list and surfaces an error to the user.

### Phase 4: Team Assembly

`TEAM_ASSEMBLY_PROMPT` asks the LLM to recommend agents for the project, outputting a JSON array parsed by `_parse_team()` into `AgentSpec` objects. Team assembly is the final phase — it runs after task breakdown so the LLM can see the actual task list when making role recommendations.

## `_run_prompt` Error Surfacing

A key fix (2026-02-16) addressed silent error swallowing. Before the fix, if the agent router returned an error event (e.g., "API key not configured"), `_run_prompt` would collect zero message content and return an empty string. The caller would then fail with a cryptic `"Planner produced no tasks."` message.

After the fix, `_run_prompt` checks for error events and raises a `RuntimeError` with the actual error message if no content was produced. This surfaces `"API key not configured"` or `"Model not available"` directly to the user.

## Phase Broadcasting

`_broadcast_phase(project_id, phase)` publishes a `dw_planning_phase` event to the MessageBus after each phase completes. The dashboard listens for these events to show a live progress indicator during planning. Without broadcasting, the UI would show a spinner with no feedback during a potentially 30-second planning run.

## `ensure_profile`

The planner creates (or reuses) a dedicated `"deep-work-planner"` AgentProfile in Mission Control. This is an idempotency guard: calling `ensure_profile()` multiple times is safe because it checks for an existing profile first. The profile is needed so the AgentRouter has an identity to run prompts under, and so planning activity appears in the agent's task history.

## Code Fence Stripping

Both `_parse_tasks` and `_parse_team` apply `_CODE_FENCE_RE` to strip ` ```json ``` ` wrappers before calling `json.loads`. LLMs frequently wrap JSON output in code fences even when instructed not to — this regex handles both ` ```json` and bare ` ``` ` variants.

## Known Gaps

- If the task breakdown JSON is valid but contains `blocked_by_keys` references to keys that don't exist in the task list, the scheduler will deadlock. There is no graph validation at parse time.
- Research notes are passed as raw text to the PRD prompt without length capping — very deep research could produce prompts that exceed model context limits.
