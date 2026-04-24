---
{
  "title": "StreamEvent Processing Tests: Token-by-Token Streaming and Thinking Integration",
  "summary": "Tests for PocketPaw's handling of `StreamEvent` objects from the Claude Agent SDK, covering text delta emission, thinking block lifecycle, tool-use start events, duplicate suppression, multi-turn state reset, and integration of thinking events into the AgentLoop's system event bus.",
  "concepts": [
    "StreamEvent",
    "ClaudeAgentSDK",
    "text delta",
    "thinking block",
    "tool-use start",
    "duplicate suppression",
    "multi-turn reset",
    "AgentLoop",
    "SystemEvent"
  ],
  "categories": [
    "testing",
    "streaming",
    "Claude SDK integration",
    "test"
  ],
  "source_docs": [
    "13dc01ce82cf5996"
  ],
  "backlinks": null,
  "word_count": 489,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's Claude SDK backend (`claude_sdk.py`) processes a stream of `StreamEvent` objects produced by the Claude Agent SDK. Each event represents a fragment of the model's output: a text token, a thinking block, a tool invocation, or a completion signal. This test file pins the adapter's behaviour for each event type and the state machines that govern them.

## Fake Infrastructure

The test file opens with lightweight fake classes — `FakeStreamEvent`, `FakeAssistantMessage`, `FakeTextBlock`, `FakeToolUseBlock`, and `FakeResultMessage` — that mimic the Claude Agent SDK's types without importing the SDK. The `_make_sdk(settings)` helper constructs a `ClaudeAgentSDK` with all SDK imports mocked, and `_collect(sdk, message)` drives the chat coroutine and collects the emitted `AgentEvent` objects. This architecture lets the tests run without the Claude SDK being installed.

## Text Delta Emission

`test_text_delta_yields_message` confirms that a text-delta stream event causes the adapter to emit an `AgentEvent` with type `message` and the correct content. This is the primary data path — every token the model generates passes through this code.

## Thinking Block Lifecycle

`test_thinking_delta_yields_thinking` and `test_thinking_done_on_block_stop` test the two-phase thinking block protocol. When the model enters a thinking block, the adapter emits a `thinking` event. When the block closes (`block_stop`), the adapter emits a `thinking_done` event. Consumers (the UI, the status tracker) use these paired signals to show and hide thinking indicators.

## Tool Use

`test_tool_use_start_yields_tool_use` verifies that a tool-use block start event emits an `AgentEvent` with the tool's name and input. The agent loop uses this to dispatch the tool call.

## Duplicate Suppression

`test_no_duplicate_text` and `test_no_duplicate_tool_use` guard against a subtle streaming bug: the Claude SDK may emit both a `StreamEvent` containing partial content and an `AssistantMessage` containing the complete assembled content for the same turn. Without deduplication, the adapter would emit the text twice. These tests confirm that once the stream path has emitted content, the fallback path is suppressed.

## Multi-Turn State Reset

`test_multi_turn_state_reset` verifies that per-turn state (which blocks have been opened, what content has been accumulated) is cleared between turns. Without this reset, state from one conversation turn would contaminate the next.

## Fallback Without StreamEvent

`test_fallback_without_stream_event` handles the case where the SDK does not yield `StreamEvent` objects but still produces an `AssistantMessage`. The adapter must fall back to extracting content from the message's content blocks.

## AgentLoop Integration

`TestLoopThinkingIntegration` lifts the tests to the `AgentLoop` level:

- `test_loop_thinking_publishes_system_event`: When a thinking event is processed, the loop publishes a `thinking` `SystemEvent` to the internal bus, which the `StatusTracker` consumes.
- `test_loop_thinking_not_in_memory`: Thinking blocks must not be written to conversation memory. They are ephemeral reasoning artifacts, not part of the dialogue history.

## Known Gaps

The test file was created on 2026-02-06, suggesting it was written concurrently with the streaming feature. No known gaps are annotated in the source.

```python
# Fake used to simulate SDK stream events without importing the SDK
class FakeStreamEvent:
    """Mimics claude_agent_sdk.StreamEvent with an .event dict."""
    def __init__(self, event: dict):
        self.event = event
```
