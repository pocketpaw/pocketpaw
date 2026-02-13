# Smart Model Router

Automatic model selection based on task complexity. Routes simple questions to fast/cheap models and complex tasks to powerful models.

## Overview

When `smart_routing_enabled` is `true`, the Model Router classifies each incoming message and selects the appropriate model tier before the agent processes it. This saves cost and latency on simple tasks while ensuring complex work gets the best model.

## Complexity Tiers

| Tier | Default Model | Use Case |
|------|---------------|----------|
| **SIMPLE** | `claude-haiku-4-5-20251001` | Greetings, simple facts, yes/no questions |
| **MODERATE** | `claude-sonnet-4-5-20250929` | Coding, analysis, general tasks |
| **COMPLEX** | `claude-opus-4-6` | Multi-step planning, debugging, architecture |

## Classification Rules

The router uses pure heuristic classification — no API calls:

### Simple Signals

Short messages (≤30 chars) matching patterns like:
- Greetings: "hi", "hello", "thanks", "bye"
- Simple questions: "what is X?", "who is Y?", "when was Z?"
- Reminder commands: "remind me ...", "set a reminder"

### Complex Signals

Messages matching any of these keyword patterns:
- Planning: "plan", "architect", "design", "strategy", "refactor"
- Debugging: "debug", "investigate", "diagnose", "root cause"
- Long implementations: "implement/build/create" + 20+ chars
- Analysis: "analyze", "compare", "evaluate", "trade-off"
- Multi-step: "multi-step", "step by step", "detailed"
- Performance: "optimize", "performance", "scale", "security audit"
- Research: "research", "deep dive", "comprehensive"

### Decision Logic

1. Short message (≤30 chars) + simple pattern match → **SIMPLE**
2. ≥2 complex signals, OR ≥1 complex signal + message >30 chars → **COMPLEX**
3. Very long message (>400 chars) → **COMPLEX**
4. Everything else → **MODERATE**

## Configuration

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `smart_routing_enabled` | bool | `false` | Enable automatic model selection |
| `model_tier_simple` | string | `"claude-haiku-4-5-20251001"` | Model for simple tasks |
| `model_tier_moderate` | string | `"claude-sonnet-4-5-20250929"` | Model for moderate tasks |
| `model_tier_complex` | string | `"claude-opus-4-6"` | Model for complex tasks |

## Example

```json
{
  "smart_routing_enabled": true,
  "model_tier_simple": "claude-haiku-4-5-20251001",
  "model_tier_moderate": "claude-sonnet-4-5-20250929",
  "model_tier_complex": "claude-opus-4-6"
}
```

With this config:
- "Hello!" → Haiku (fast, cheap)
- "Write a Python function to parse CSV" → Opus (complex signal + moderate length)
- "What time is it?" → Haiku (simple question, short)
- "Thanks!" → Haiku (greeting, short)
- Default fallback → Sonnet

## Implementation

| File | Description |
|------|-------------|
| `agents/model_router.py` | `ModelRouter` class with `classify()` method |

## Tests

- `tests/test_model_router.py`
