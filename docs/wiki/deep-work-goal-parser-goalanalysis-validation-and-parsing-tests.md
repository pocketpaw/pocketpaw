---
{
  "title": "Deep Work Goal Parser: GoalAnalysis Validation and Parsing Tests",
  "summary": "This test suite covers the GoalParser and GoalAnalysis components in PocketPaw's Deep Work system, validating JSON parsing, domain/complexity/research-depth validation, field clamping, and serialization round-trips. It ensures robust defensive behavior when LLM-generated goal payloads contain invalid or out-of-range values.",
  "concepts": [
    "GoalAnalysis",
    "GoalParser",
    "goal_parser",
    "domain_validation",
    "complexity_validation",
    "research_depth",
    "estimated_phases",
    "confidence_clamping",
    "code_fence_stripping",
    "LLM_output_sanitization",
    "Deep_Work",
    "from_dict",
    "to_dict",
    "round_trip"
  ],
  "categories": [
    "testing",
    "deep-work",
    "goal-parsing",
    "LLM-integration",
    "test"
  ],
  "source_docs": [
    "2ed16696f85df871"
  ],
  "backlinks": null,
  "word_count": 528,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `test_deep_work_goal_parser.py` suite exercises `pocketpaw.deep_work.goal_parser`, which is responsible for taking raw LLM output and transforming it into a structured `GoalAnalysis` dataclass. Because PocketPaw's Deep Work feature relies on LLM responses that may contain malformed JSON, invalid enum values, or out-of-range numbers, the goal parser must be heavily defensive.

## Why This Module Exists

When a user submits a high-level project description, the Deep Work system routes it through a `GoalParser` that calls an LLM to classify the goal: domain (code, creative, research, hybrid), complexity (XS, S, M, L, XL), estimated phases, confidence score, required human steps, and clarifications needed. Because LLMs are non-deterministic, the parser cannot trust any field value and must sanitize every response.

## GoalAnalysis Defaults and Properties

`TestGoalAnalysisDefaults` verifies that a `GoalAnalysis` constructed with no arguments has sensible fallbacks: domain defaults to `hybrid`, complexity to `M`, research depth to `standard`, and `needs_clarification` is `False` when no clarifications are present. The `domain_label()` property is also tested, including an unknown-domain fallback, which prevents `KeyError` crashes if future LLM responses introduce unexpected domain strings.

## from_dict() Validation

`TestGoalAnalysisFromDict` is the largest test class and documents all the defensive rules the parser enforces:

- **Invalid domain**: falls back to `hybrid` rather than raising an error.
- **Invalid complexity**: falls back to `M`.
- **Invalid research_depth**: falls back to `standard`.
- **estimated_phases clamping**: values below 1 or above 20 are clamped to prevent absurd phase counts that could overload the scheduler.
- **confidence clamping**: kept in the 0.0–1.0 range; LLMs sometimes return percentages (e.g., 95 instead of 0.95).
- **clarifications truncation**: capped at 4 items, preventing the UI from being flooded with excessive clarification requests.
- **estimated_phases integer coercion**: LLMs sometimes return floats; the parser ensures integer types for downstream scheduling math.

Each fallback prevents a class of silent failures where invalid LLM output would propagate into task scheduling with nonsensical parameters.

## to_dict() Serialization and Round-Trips

`TestGoalAnalysisToDict` confirms that `to_dict()` followed by `from_dict()` produces an identical object. This round-trip guarantee is critical because `GoalAnalysis` objects are persisted to the file store as JSON and must survive process restarts.

## Validation Helpers

Four helper test classes (`TestValidateDomain`, `TestValidateComplexity`, `TestValidateResearchDepth`, `TestClamp`) each test the corresponding private validators:

- **Case-insensitive matching**: LLMs often return `"Code"` or `"CODE"` instead of `"code"`. The validators normalize before comparing.
- **Whitespace stripping**: Prevents failures from LLM responses with leading/trailing spaces.
- **Safe fallbacks**: Every validator returns a valid default rather than raising, keeping the parser fault-tolerant.

## GoalParser.parse_raw() and _strip_code_fences()

`GoalParser.parse_raw()` handles both plain JSON and markdown code-fenced JSON (common in LLM responses). The `_strip_code_fences()` tests verify edge cases: fences with and without language tags, fences embedded in surrounding prose, and empty input. This is necessary because Claude and other LLMs frequently wrap JSON in triple-backtick blocks.

## GoalParser.parse() — Full Flow

The integration test for `parse()` uses a mocked `_run_prompt` to avoid real LLM calls while verifying that the full pipeline (prompt execution → JSON extraction → validation → `GoalAnalysis` construction) works end-to-end.

## Known Gaps

None flagged in the source. The test for `parse()` mocks `_run_prompt`, so real LLM latency, token limits, and multi-turn clarification flows are not covered by this suite.
