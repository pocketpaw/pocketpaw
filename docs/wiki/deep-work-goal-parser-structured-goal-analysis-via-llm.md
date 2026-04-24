---
{
  "title": "Deep Work Goal Parser — Structured Goal Analysis via LLM",
  "summary": "GoalParser is the first primitive in the Deep Work pipeline, taking raw user input and producing a structured GoalAnalysis that captures domain, complexity, AI/human role split, constraints, and clarification questions. It routes the LLM prompt through AgentRouter, strips code fences from the response, and defensively validates every field before constructing the dataclass.",
  "concepts": [
    "GoalParser",
    "GoalAnalysis",
    "goal parsing",
    "domain detection",
    "complexity estimation",
    "research depth",
    "clarification questions",
    "LLM structured output",
    "code fence stripping",
    "defensive validation",
    "Deep Work pipeline"
  ],
  "categories": [
    "deep-work",
    "planning",
    "llm-integration",
    "data-models"
  ],
  "source_docs": [
    "a96cf781738f5342"
  ],
  "backlinks": null,
  "word_count": 577,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`goal_parser.py` is the entry point for the Deep Work pipeline. Before any research or planning begins, the system needs to understand *what* the user actually wants — not just the words they typed, but the domain, complexity, and what parts require human involvement vs. AI automation. GoalParser answers those questions.

## GoalAnalysis Dataclass

`GoalAnalysis` holds the parsed output. Key fields:

- **`goal`** — A clean one-sentence restatement of the user's intent, preventing downstream prompts from inheriting ambiguous language
- **`domain`** — One of seven values: `code`, `business`, `creative`, `education`, `events`, `home`, `hybrid`
- **`complexity`** — T-shirt size: `S`, `M`, `L`, `XL` — feeds directly into how many planning phases the planner generates
- **`ai_capabilities` / `human_requirements`** — Explicitly separating what AI can automate from what requires human action prevents the planner from generating all-AI task lists for inherently human work
- **`clarifications_needed`** — Questions to surface to the user *before* planning starts, avoiding plans built on wrong assumptions
- **`suggested_research_depth`** — Lets the parser recommend `none`, `quick`, `standard`, or `deep`, so trivial tasks don't spend tokens on unnecessary research
- **`confidence`** — A 0.0–1.0 score the parser uses to decide whether clarification is worth asking

```python
@dataclass
class GoalAnalysis:
    goal: str = ""
    domain: str = "code"
    complexity: str = "M"
    suggested_research_depth: str = "standard"
    confidence: float = 0.7
    clarifications_needed: list[str] = field(default_factory=list)
    # ... more fields
```

## Parsing Flow

`GoalParser.parse(user_input)` runs the `GOAL_PARSE_PROMPT` through `AgentRouter`, collecting streamed text. The response may be wrapped in ` ```json ``` ` fences — `_strip_code_fences()` handles this with a regex rather than string splitting to handle both `\`\`\`json` and bare ` ``` ` variants.

`parse_raw(raw_json)` accepts a pre-formed JSON string, enabling unit testing without an LLM call.

## Defensive Validation

The `from_dict` classmethod applies several guards that prevent malformed LLM output from corrupting the pipeline:

- **`_validate_domain()`** — normalizes to lowercase, falls back to `"code"` for unknown values. Without this, a response like `"Code"` or `"software"` would store an invalid enum value
- **`_validate_complexity()`** — coerces unrecognized values to `"M"`, preventing the planner from receiving `"medium"` instead of `"M"`
- **`_validate_research_depth()`** — same pattern for depth values
- **`_sanitize_str_list()`** — filters list fields to non-empty strings, preventing empty-string items from appearing as clarification questions
- **`_clamp()`** — bounds `estimated_phases` between 1 and 10, preventing division-by-zero or absurdly large phase counts
- **Minimum phases by complexity** — enforces `L` → min 2 phases, `XL` → min 3 phases, since a single-phase XL project is almost certainly a parsing error
- **Clarification cap** — truncates `clarifications_needed` to 4 items with a warning log. More than 4 clarifications is a UX problem — it signals that the parser is confused, not that the user needs to answer 12 questions

## `needs_clarification()` and `domain_label()`

`needs_clarification()` returns `True` when confidence is below 0.7 and there are actual questions to ask. This prevents the system from blocking on clarification when the parser is merely uncertain but has no concrete questions.

`domain_label()` returns a human-readable domain name for display, mapping `"hybrid"` to `"Multi-domain"` etc.

## Known Gaps

- The confidence threshold (0.7) is hardcoded. There is no mechanism to tune it per deployment.
- The clarification cap of 4 is also hardcoded. A project requiring extensive clarification (e.g., very vague XL goals) may silently lose questions.
- `parse_raw()` does not validate that the input is parseable JSON before calling `from_dict`; a malformed JSON string raises a raw `json.JSONDecodeError` rather than a descriptive pipeline error.
