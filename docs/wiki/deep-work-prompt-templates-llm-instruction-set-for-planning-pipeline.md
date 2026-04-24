---
{
  "title": "Deep Work Prompt Templates — LLM Instruction Set for Planning Pipeline",
  "summary": "This module centralizes all LLM prompt templates used by the Deep Work planning pipeline as module-level string constants. Separating prompts from logic enables iteration on prompt text without touching orchestration code, and makes the planning pipeline's LLM behavior fully visible in one file.",
  "concepts": [
    "GOAL_PARSE_PROMPT",
    "RESEARCH_PROMPT",
    "PRD_PROMPT",
    "TASK_BREAKDOWN_PROMPT",
    "TEAM_ASSEMBLY_PROMPT",
    "prompt templates",
    "research depth variants",
    "LLM prompts",
    "Deep Work pipeline",
    "prompt constants"
  ],
  "categories": [
    "deep-work",
    "llm-integration",
    "prompts",
    "planning"
  ],
  "source_docs": [
    "412ffb31a957d3ee"
  ],
  "backlinks": null,
  "word_count": 523,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Every LLM call in the Deep Work pipeline is driven by a prompt template defined here. Keeping prompts as module-level constants rather than embedding them in method bodies serves two purposes: the prompt text can be read, reviewed, and tuned independently of the Python code that invokes it, and changes to prompt behavior are clearly visible in diff history without wading through orchestration logic.

## Prompt Inventory

### `GOAL_PARSE_PROMPT`

Added 2026-02-18. Instructs the LLM to analyze a user's raw goal and return a structured JSON object with fields: `goal`, `domain`, `sub_domains`, `complexity`, `estimated_phases`, `ai_capabilities`, `human_requirements`, `constraints_detected`, `clarifications_needed`, `suggested_research_depth`, and `confidence`.

The prompt specifies valid enum values inline (e.g., `"One of: code, business, creative, education, events, home, hybrid"`) to guide the model without requiring a schema — this reduces hallucinated domain values that would fail `_validate_domain()` in GoalParser.

### `RESEARCH_PROMPT`

Standard research template. Provides domain context gathering with moderate depth. Used when `research_depth = "standard"`.

### `RESEARCH_PROMPT_QUICK`

Minimal research variant — instructs the LLM to skip web search and work from its training knowledge only. Used for simple tasks where research overhead outweighs benefit (`research_depth = "quick"`).

### `RESEARCH_PROMPT_DEEP`

Thorough research variant — explicitly enables web search and asks for comprehensive domain coverage. Used for complex projects where gaps in domain knowledge would produce poor plans (`research_depth = "deep"`).

The three research variants exist because research is often the longest phase (network calls, extensive generation). Matching depth to task complexity keeps the planner fast for small tasks without shortchanging complex ones.

### `PRD_PROMPT`

Generates a Product Requirements Document from the user's goal and research notes. The PRD format includes sections: Overview, Goals, Non-Goals, Functional Requirements, Technical Constraints, and Success Criteria. The structured format matters because `_extract_title()` in `session.py` parses the first heading line to derive a project title.

### `TASK_BREAKDOWN_PROMPT`

Decomposes the PRD into an ordered JSON array of task objects. Each task must include: `key` (unique identifier within this plan), `title`, `description`, `role`, `complexity`, `blocked_by_keys` (dependency list), and `estimated_minutes`.

Requiring `blocked_by_keys` in the prompt schema ensures the LLM produces a dependency graph, not a flat task list. The scheduler needs this to determine dispatch order.

### `TEAM_ASSEMBLY_PROMPT`

Recommends agents for the project given the task list. Output is a JSON array of agent specs with `name`, `role`, `capabilities`, and `assigned_task_keys`. Running this after task breakdown (not before) ensures recommendations reflect actual task content.

## Prompt-as-Constants Pattern

All templates use Python f-string-compatible `{placeholder}` syntax — they're not f-strings themselves, just strings with `{}` markers. Callers use `.format(**kwargs)` to inject values. This avoids accidental early evaluation of dynamic content when the module loads.

## Known Gaps

- Prompts are not versioned or tagged. If a model upgrade changes optimal prompt phrasing, there's no history of which prompt version was used for a given plan.
- The `GOAL_PARSE_PROMPT` specifies a fixed JSON schema inline. If `GoalAnalysis` fields change, the prompt must be updated manually — there's no derived schema generation.
- There is no fallback prompt for when a model produces structurally invalid JSON — the parsers in `goal_parser.py` and `planner.py` handle validation, but the prompt itself doesn't instruct the model on error recovery.
