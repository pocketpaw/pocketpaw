---
{
  "title": "Model Router: Task Complexity Classification and Tiered Model Selection",
  "summary": "Tests for ModelRouter, which classifies incoming messages by complexity and routes them to the appropriate LLM tier (Haiku for simple, Sonnet for moderate, Opus for complex). Validates classification boundaries, edge cases like empty messages, and that the model names match the configured tiers.",
  "concepts": [
    "ModelRouter",
    "task complexity",
    "model selection",
    "tiered routing",
    "Haiku",
    "Sonnet",
    "Opus",
    "classification",
    "SIMPLE",
    "MODERATE",
    "COMPLEX",
    "cost optimization",
    "LLM routing"
  ],
  "categories": [
    "agent runtime",
    "model selection",
    "testing",
    "cost optimization",
    "test"
  ],
  "source_docs": [
    "ef50733b2aa26db9"
  ],
  "backlinks": null,
  "word_count": 502,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Running every agent message through the most capable (and most expensive) model is wasteful. `ModelRouter` solves this by classifying the complexity of each incoming message and dispatching it to the appropriate model tier. The test suite pins the classification boundaries to prevent regressions where a simple greeting gets routed to Opus, or a complex planning request gets undersized to Haiku.

## Classifier Tiers

The router exposes three tiers configured via settings:
- **SIMPLE** → `model_tier_simple` (e.g., `claude-haiku-4-5-20251001`)
- **MODERATE** → `model_tier_moderate` (e.g., `claude-sonnet-4-5-20250929`)
- **COMPLEX** → `model_tier_complex` (e.g., `claude-opus-4-6`)

The `ModelSelection` returned by `classify()` carries both the `complexity` enum value and the resolved `model` string.

## Simple Classification (`TestSimple`)

Greetings and one-word inputs like "Hi", "Hello", "Thanks!" are routed to SIMPLE. These do not need reasoning or tool use — Haiku responds faster and at lower cost.

`test_short_question` is a deliberate boundary test: "What is Python?" is short but deserves a real answer, so it routes to MODERATE rather than SIMPLE. This distinction prevents the Haiku tier from handling knowledge questions that require accurate recall.

`test_reminder_request` routes to MODERATE because reminders require tool use (writing to the memory store). Simple-tier models may have restricted tool access or less reliable tool-call formatting.

## Complex Classification (`TestComplex`)

Long messages, planning requests, debugging tasks, and research assignments route to COMPLEX. The `test_very_long_message` test confirms that message length alone can push a request to Opus — a proxy for "this will require extended reasoning."

## Moderate Classification (`TestModerate`)

The moderate tier catches everything between simple greetings and full planning sessions: coding questions, medium-length queries, file operations. `test_file_operation` is notable — file reads and writes need tools but not multi-step reasoning, so Sonnet is the right fit.

## Edge Cases

- `test_model_selection_fields`: Confirms the `ModelSelection` object has both `complexity` and `model` fields. If a new field is added to the dataclass and consumers start relying on it, this test catches the structural contract.
- `test_empty_message`: An empty string must not raise — the router returns a default tier rather than crashing.
- `test_whitespace_message`: Whitespace-only input is treated as empty, not as a meaningful prompt.

## Fixture Design

The `settings` fixture creates a `MagicMock` with specific model names for each tier. This decouples the classification logic from real Anthropic model IDs — if model names change, only the fixture needs updating, not the classification tests.

The `router` fixture instantiates `ModelRouter(settings)` fresh for each test, ensuring no state leaks between classification decisions.

## Why This Matters

Without a model router, every message routes to the configured default model. In production, that default tends to be the most capable model to avoid quality regressions. The router lets PocketPaw reduce per-message costs significantly for the majority of interactions that are simple conversational turns.

## Known Gaps

No TODOs in this file. The classification logic is heuristic (likely keyword matching or length thresholds) — the tests do not cover adversarial inputs designed to fool the classifier, such as a long but trivial message or a short but deeply complex one.
