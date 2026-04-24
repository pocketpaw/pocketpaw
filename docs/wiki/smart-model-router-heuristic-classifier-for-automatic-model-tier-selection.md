---
{
  "title": "Smart Model Router — Heuristic Classifier for Automatic Model Tier Selection",
  "summary": "Implements `ModelRouter`, a zero-API-call heuristic classifier that maps each incoming message to a `TaskComplexity` tier (SIMPLE, MODERATE, COMPLEX) and selects the appropriate model (Haiku, Sonnet, Opus). Pattern matching prevents false-positive simple classifications for messages that require tools or deep reasoning.",
  "concepts": [
    "ModelRouter",
    "TaskComplexity",
    "heuristic classification",
    "regex patterns",
    "SIMPLE",
    "MODERATE",
    "COMPLEX",
    "model tier selection",
    "needs-tools prevention",
    "ModelSelection"
  ],
  "categories": [
    "agent-runtime",
    "model-selection",
    "routing"
  ],
  "source_docs": [
    "2ff596f76ab7fc05"
  ],
  "backlinks": null,
  "word_count": 412,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ModelRouter` answers the question "which model should handle this message?" without making any LLM API calls. It uses compiled regex patterns and message length thresholds to classify tasks into three complexity tiers, each mapped to a cost and capability tier of Claude models.

## Complexity Tiers

| Tier | Typical Model | Use Case |
|------|--------------|---------|
| `SIMPLE` | claude-haiku | Short greetings, acknowledgements, one-word replies |
| `MODERATE` | claude-sonnet | Coding tasks, analysis, multi-sentence questions |
| `COMPLEX` | claude-opus | Multi-step reasoning, research, planning |

## Why Heuristics Over a Classifier Model

Calling a classifier LLM to decide which model to use would add latency and cost — defeating the purpose of routing cheaper requests to cheaper models. Pure regex and length thresholds add sub-millisecond overhead with no API calls.

## False-Positive Prevention for Tool-Requiring Messages

`_SIMPLE_PATTERNS` correctly catches greetings like "hi" or "thanks", but some messages that look superficially simple actually require tool calls. A message like "what's the current stock price?" is short but needs a web search. `_NEEDS_TOOLS_PATTERNS` is evaluated before SIMPLE classification — any match here escalates to MODERATE regardless of message length. Covered signal categories:

- Stock, market, forecast, and prediction queries
- File/report/chart creation requests
- Explicit search/browse intent
- Install, download, fetch, scrape requests
- Code execution or calculation requests
- Email, message, post, or upload actions

## Configuration Override

`Settings` exposes `model_router_enabled`, `model_simple`, `model_moderate`, and `model_complex`. Operators can pin all traffic to a single model or remap tiers without code changes — useful for providers that don't offer multiple model tiers, or for cost-fixed deployments.

## ModelSelection Output

```python
@dataclass
class ModelSelection:
    complexity: TaskComplexity
    model: str
    reason: str
```

The `reason` field is a human-readable explanation logged at DEBUG level (e.g., `"matched simple greeting pattern"` or `"length 847 chars exceeds MODERATE threshold"`). This makes routing decisions auditable without adding a separate logging layer.

## Integration with AgentRouter

`AgentRouter` calls `ModelRouter.classify()` once per message before dispatching to the backend. The selected `model` string is passed to the backend's `run()` call. Backends that support dynamic model selection (Claude SDK, OpenAI Agents) honour this; fixed-model backends (Codex CLI, Copilot SDK) ignore it.

## Known Gaps

- Pattern set is English-only; non-English greetings are misclassified as MODERATE.
- No feedback loop: if a SIMPLE-classified message triggers tool calls, the model is not upgraded for that exchange.
- COMPLEX detection relies on explicit keywords; domain-specific complex queries that avoid those keywords land in MODERATE.
