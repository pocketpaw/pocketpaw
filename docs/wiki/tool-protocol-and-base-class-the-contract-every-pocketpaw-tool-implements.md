---
{
  "title": "Tool Protocol and Base Class: The Contract Every PocketPaw Tool Implements",
  "summary": "The `protocol.py` module defines the structural contract that all PocketPaw tools must satisfy -- a `ToolDefinition` dataclass for LLM schema representation, a `ToolProtocol` for structural subtyping, and a `BaseTool` abstract class providing default implementations and helper methods. Cross-backend schema normalization is handled here to prevent silent failures with strict OpenAI-style validators.",
  "concepts": [
    "ToolProtocol",
    "BaseTool",
    "ToolDefinition",
    "normalize_schema",
    "OpenAI schema",
    "Anthropic schema",
    "trust_level",
    "media_result",
    "structural subtyping",
    "abstract base class",
    "tool contract"
  ],
  "categories": [
    "tools",
    "agent runtime",
    "architecture",
    "LLM integration"
  ],
  "source_docs": [
    "d2c77522e9457ca5"
  ],
  "backlinks": null,
  "word_count": 473,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw supports multiple LLM backends (Anthropic, OpenAI-compatible). Each backend has a slightly different format for declaring callable tools. `protocol.py` is the abstraction layer that isolates tool implementations from these backend differences. A tool author writes one class; the protocol layer handles schema translation.

## ToolDefinition

`ToolDefinition` is a dataclass that holds a tool's name, description, JSON Schema for its parameters, and a trust level string (`standard`, `high`, or `critical`). It provides two schema serialization methods:

- `to_openai_schema()` -- wraps the definition in `{"type": "function", "function": {...}}` and runs `normalize_schema` on the parameters.
- `to_anthropic_schema()` -- uses `{"name": ..., "description": ..., "input_schema": ...}` without normalization, because Anthropic's API is more permissive.

## Schema Normalization: Why It Exists

The `normalize_schema()` function addresses a silent failure mode with OpenAI-style strict validators. When a tool takes no parameters, its schema is `{"type": "object"}` -- a valid JSON Schema. However, OpenAI's function calling validators require that object schemas include an explicit `properties` key, even if it's empty. Without it, the schema is rejected at call time with an opaque validation error.

```python
def normalize_schema(schema: dict[str, Any]) -> dict[str, Any]:
    if schema.get("type") == "object":
        schema.setdefault("properties", {})
        if not schema["properties"]:
            schema["required"] = []
    return schema
```

Setting `required` to `[]` ensures zero-arg tools remain callable rather than being treated as having unresolvable required fields.

## ToolProtocol vs. BaseTool

The module offers two extension points:

**`ToolProtocol`** (structural subtyping via `typing.Protocol`) -- any class with a `name` property, a `definition` property, and an async `execute` method satisfies the protocol without inheriting anything. This is used for type annotations in `ToolRegistry` and `filter_tools`, making the system open to duck-typed tool implementations from plugins or MCP adapters.

**`BaseTool`** (nominal subtyping via `ABC`) -- provides abstract requirements (`name`, `description`, `execute`) and concrete defaults (`parameters`, `definition`, `trust_level`). It also provides three helper methods that standardize how tool results are formatted:

- `_media_result(path, text)` -- embeds a `<!-- media:path -->` HTML comment that `AgentLoop` parses to attach files to outbound messages.
- `_error(message)` -- returns `"Error: {message}"`, giving the LLM a consistent error prefix.
- `_success(message)` -- returns the message as-is (a pass-through that communicates intent in code).

## Design Philosophy: Strings All the Way Down

The `ToolProtocol` docstring is explicit: "Tools are simple: they take parameters and return a string result. No streaming, no complex event types." This simplicity is intentional -- it makes tools trivially composable and testable. The string return type means any tool result can be included directly in conversation history without deserialization.

## Known Gaps

The `_media_result` HTML comment protocol (`<!-- media:path -->`) is an informal contract between `BaseTool` and `AgentLoop`. It is not validated or documented in a schema. If the comment format changes in `AgentLoop`, existing tools silently break. A typed `ToolResult` return type with a discriminated union would be safer but would require updating all existing tools.