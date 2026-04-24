---
{
  "title": "Deep Agents Backend — LangChain/LangGraph Agent Framework Integration",
  "summary": "Implements `DeepAgentsBackend`, which runs agents via the `deepagents` SDK built on LangChain and LangGraph. It provides durable execution, built-in planning and filesystem tools, multi-provider LLM routing, MCP tool integration, and streaming via LangGraph's event stream.",
  "concepts": [
    "DeepAgentsBackend",
    "LangChain",
    "LangGraph",
    "deepagents",
    "Overwrite unwrapping",
    "content block normalisation",
    "MCP tools",
    "FunctionTool",
    "provider mapping",
    "durable execution"
  ],
  "categories": [
    "agent-runtime",
    "langchain",
    "langgraph",
    "streaming"
  ],
  "source_docs": [
    "28ac890d294b1bbd"
  ],
  "backlinks": null,
  "word_count": 434,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`DeepAgentsBackend` integrates the `deepagents` SDK — built on LangChain and LangGraph — into PocketPaw. LangGraph provides durable, graph-structured execution with stateful checkpointing, making this backend well-suited for long-running tasks that need to survive interruptions or retry failed steps.

## Provider Normalisation

`_LANGCHAIN_PROVIDER_MAP` translates PocketPaw provider names to LangChain's `init_chat_model` provider strings. PocketPaw uses `google` or `gemini` to mean Google Generative AI; LangChain requires `google_genai`. Similarly, `openai_compatible` and `openrouter` both map to LangChain's `openai` provider string. Without this translation layer, users with `provider = "google"` in PocketPaw config would get a `ValueError` from LangChain even though the intent is clear.

## LangGraph State Unwrapping

LangGraph wraps state updates in `Overwrite` objects to signal full state replacement versus incremental append. `_unwrap()` extracts the inner `.value` attribute from any such object before the backend tries to iterate over streamed content. Without this, the streaming loop would receive an `Overwrite(["some text"])` object that is not directly iterable, causing a `TypeError` mid-stream.

## Content Block Normalisation

`_extract_content_text()` handles two shapes of `AIMessageChunk.content`: a plain string (standard OpenAI format) or a list of typed content blocks (`[{"type": "text", "text": "..."}]`, which Anthropic models return). Without this normaliser, streaming would break for Anthropic-backed LangChain agents because the backend would attempt to display a raw dict as text.

## MCP Tool Integration

`_build_mcp_tools()` is async and constructs MCP tool wrappers from PocketPaw's MCP server config. It is called lazily on the first `run()` to avoid blocking the event loop during import. MCP tools participate in LangGraph's planning loop alongside built-in and custom tools.

## Custom Tool Bridge

`_build_custom_tools()` wraps PocketPaw's registered tool functions as LangChain `FunctionTool` objects. This bridge lets PocketPaw's tool registry (memory, calendar, etc.) participate in LangGraph agent execution without any LangChain-specific code in the tool implementations.

## Agent Caching

`_get_or_create_agent()` returns a cached agent for a given `(model, instructions, mcp_tools)` combination, avoiding repeated initialisation overhead when the same configuration is reused across requests.

## Known Gaps

- LangGraph's durable execution requires a persistent checkpointer (SQLite, Redis); the current implementation uses in-memory state that is lost on process restart.
- No `Capability.MULTI_TURN` flag — history must be injected via the instruction string.
- Session isolation per conversation is not implemented; multiple concurrent conversations may share LangGraph state.


## Streaming Event Mapping

`run()` iterates LangGraph's async event stream and maps event types to `AgentEvent` variants:
- `on_chat_model_stream` with text content → `TextChunk`
- `on_tool_start` → `ToolUse`
- `on_tool_end` → `ToolResult`
- Stream completion → `Done`
- Exceptions → `Error`

The `_extract_content_text()` normaliser is applied to every stream chunk before emitting, handling both plain strings and Anthropic-style content block lists.
