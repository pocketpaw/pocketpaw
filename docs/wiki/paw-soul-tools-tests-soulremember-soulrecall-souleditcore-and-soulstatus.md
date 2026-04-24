---
{
  "title": "Paw Soul Tools Tests: SoulRemember, SoulRecall, SoulEditCore, and SoulStatus",
  "summary": "These tests cover the four built-in soul tools that PocketPaw exposes to LLMs: `soul_remember`, `soul_recall`, `soul_edit_core`, and `soul_status`. Each tool is validated for its parameter schema, execution behavior, error handling, and JSON Schema export in both Anthropic and OpenAI formats.",
  "concepts": [
    "soul_remember",
    "soul_recall",
    "soul_edit_core",
    "soul_status",
    "tool schema",
    "JSON Schema",
    "Anthropic format",
    "OpenAI format",
    "tool-use",
    "memory importance",
    "error handling"
  ],
  "categories": [
    "testing",
    "soul tools",
    "LLM integration",
    "test"
  ],
  "source_docs": [
    "380ab98d7f752e31"
  ],
  "backlinks": null,
  "word_count": 522,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's soul tools are the interface through which LLMs interact with a soul's persistent memory. They are exposed as tool-use function calls and must satisfy two audiences: the LLM (via JSON Schema definitions) and the PocketPaw runtime (via `execute()` return contracts). This test suite validates both.

## SoulRememberTool

`TestSoulRememberTool` verifies:

- **Schema**: `content` is a required parameter; `importance` is an integer, not a string.
- **Execution**: `soul.remember()` is called with the provided content.
- **Default importance**: when `importance` is omitted, it defaults to 5 — a deliberate mid-tier default that avoids flooding high-importance recall results.
- **Long content truncation**: the result message truncates very long content strings to keep tool responses readable in the LLM's context window.
- **Error return**: if `soul.remember()` raises, the tool returns an error string rather than propagating the exception — preserving the agent loop.

```python
async def test_execute_returns_error_on_soul_failure(mock_soul):
    mock_soul.remember.side_effect = RuntimeError("disk full")
    tool = SoulRememberTool(mock_soul)
    result = await tool.execute(content="test")
    assert "error" in result.lower()
```

## SoulRecallTool

`TestSoulRecallTool` validates the recall interface:

- **Schema**: `query` is required.
- **Formatted results**: memories are returned as readable text, not raw JSON objects.
- **No memories**: returns a "not found" message, not an empty string or None.
- **Limit respected**: the `limit` parameter is forwarded to the soul.
- **Importance shown**: each result includes its importance score so the LLM can weight results.
- **Emotion included**: when a memory has an associated emotion, it appears in the output.
- **Error return**: same as remember — exceptions become error strings.

## SoulEditCoreTool

`TestSoulEditCoreTool` covers core memory editing — the mechanism for changing the soul's `persona` or `human` core memory fields:

- **Schema**: both `persona` and `human` fields are present.
- **Persona only, human only, both**: all three combinations are valid.
- **Neither**: when both fields are absent or empty, returns an error — a no-op edit is rejected to prevent accidental memory clears.
- **Failure handling**: soul exceptions are returned as error strings.

## SoulStatusTool

`TestSoulStatusTool` validates the status introspection tool:

- **Schema**: empty parameters (no inputs required).
- **JSON output**: returns a JSON string containing `mood` and `energy`.
- **Self-model domains**: when the soul has a self-model, domains are included in the output.
- **No state attributes**: if the soul has no state, returns an "active" placeholder message.
- **Error handling**: consistent with other tools.

## Tool Definition Export

`TestToolDefinitions` validates the JSON Schema export used when registering tools with LLM providers:

- Each tool's `definition` property returns the correct `name`.
- Every definition includes a `description` field (required by both Anthropic and OpenAI).
- **Anthropic format**: schema matches the `input_schema` structure Anthropic expects.
- **OpenAI format**: schema matches the `function.parameters` structure OpenAI expects.
- **Zero-argument normalization**: tools with no parameters (like `soul_status`) must export a valid empty schema, not `null` — OpenAI rejects null parameter schemas.

## Known Gaps

- No test verifies that `SoulRememberTool` stores the correct `memory_type` (episodic vs. semantic).
- No test covers concurrent tool execution — two simultaneous `soul_remember` calls could race on the same soul file.
- The truncation threshold for long content is tested but the threshold value itself is not asserted.