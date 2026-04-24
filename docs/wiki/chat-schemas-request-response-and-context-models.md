---
{
  "title": "Chat Schemas — Request, Response, and Context Models",
  "summary": "The chat schemas define the core request and response contracts for PocketPaw's chat API, including file and pocket context injection, camelCase/snake_case aliasing to prevent session ID loss, and the SSE chunk model for streaming responses.",
  "concepts": [
    "ChatRequest",
    "camelCase aliasing",
    "session ID",
    "PocketContext",
    "FileContext",
    "ChatChunk",
    "SSE streaming",
    "populate_by_name",
    "agent context",
    "enterprise overrides"
  ],
  "categories": [
    "chat",
    "schemas",
    "API"
  ],
  "source_docs": [
    "dee14f8ff46ccc91"
  ],
  "backlinks": null,
  "word_count": 534,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The chat schemas are the most frequently used models in the v1 API. They define what a chat message looks like on the wire, what optional context can accompany it, and how streaming and non-streaming responses are shaped.

## `FileContext`

```python
class FileContext(BaseModel):
    current_dir: str | None = None
    open_file: str | None = None
    open_file_name: str | None = None
    open_file_extension: str | None = None
    open_file_size: int | None = None
    selected_files: list[str] | None = None
    source: str | None = None
```

Carries file system context from the desktop client. When a user has a file open in their editor, the desktop client sends the file name, extension, size, and selected files alongside the chat message. The agent uses this to provide contextually relevant assistance without requiring the user to copy-paste file details manually.

## `PocketContext`

```python
class PocketContext(BaseModel):
    id: str
    name: str
    widgets: list[dict] = []
    tool_policy: dict = {}
    model: str | None = None
```

Carries a lightweight descriptor of the pocket workspace the user is currently viewing. Critically, this is **not** the full pocket document — only the metadata needed to identify the pocket. The agent fetches the full document via `get_pocket` when it needs to answer questions about the pocket's content. This keeps the system prompt size bounded regardless of how large the pocket's `rippleSpec.ui` tree is.

## `ChatRequest`

This is the most design-rich schema in the file. The class comment explains the key problem it solves:

> Accepts both snake_case and camelCase keys on the wire so the FE can post `sessionId`/`agentId` without silently losing the value — Pydantic defaults to dropping unknown fields, which previously caused every `sessionId: "websocket_xxx"` payload to be treated as a brand new chat with a freshly generated id.

```python
model_config = ConfigDict(populate_by_name=True)

session_id: str | None = Field(default=None, alias="sessionId")
agent_id: str | None = Field(default=None, alias="agentId")
file_context: FileContext | None = Field(default=None, alias="fileContext")
pocket_context: PocketContext | None = Field(default=None, alias="pocketContext")
```

`populate_by_name=True` means the field can be set by either the Python name (`session_id`) or the alias (`sessionId`). Without this, a payload using `sessionId` would silently default to `None`, causing a new session to be created on every message — a regression that was hard to diagnose because no error was raised.

The `content` field has explicit bounds (`min_length=1, max_length=100000`) that prevent empty messages (which would waste an agent invocation) and extremely large payloads (which could cause memory pressure or exceed LLM context windows).

## `ChatChunk` and `ChatResponse`

`ChatChunk` is the per-event SSE payload:
```python
class ChatChunk(BaseModel):
    text: str
    session_id: str | None = None
```

`ChatResponse` is the complete non-streaming response, returned when the client requests a synchronous result rather than a stream. Both are kept minimal — the client reconstructs the full text by concatenating chunks.

## Known Gaps

- `ChatRequest.media` is a `list[str]` with no documented format. It likely carries base64-encoded image data or file references, but the schema gives no indication of which encoding is expected.
- Enterprise overrides (`agent_id`) are described in the comment as "ignored in community mode" but there is no schema-level annotation (e.g., a `deprecated` marker or separate community/enterprise request models) to make this visible to API consumers.
