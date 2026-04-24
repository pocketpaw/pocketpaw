---
{
  "title": "A2A Protocol Pydantic Models: Tasks, Messages, Parts, and Agent Cards",
  "summary": "This module defines all Pydantic models for the A2A protocol (v0.2.5+), including the task lifecycle state machine, typed content parts (text/file/data), message and artifact models, request parameter schemas, the Agent Card capabilities manifest, and JSON-RPC 2.0 envelope models with streaming event types.",
  "concepts": [
    "TaskState",
    "state machine",
    "VALID_TRANSITIONS",
    "discriminated union",
    "TextPart",
    "FilePart",
    "DataPart",
    "AgentCard",
    "AgentSkill",
    "TaskSendParams",
    "history extension",
    "TaskStatusUpdateEvent",
    "TaskArtifactUpdateEvent",
    "JSONRPCRequest",
    "A2A protocol"
  ],
  "categories": [
    "A2A protocol",
    "Pydantic models",
    "task lifecycle",
    "streaming"
  ],
  "source_docs": [
    "13055e1783bf7437"
  ],
  "backlinks": null,
  "word_count": 471,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Task Lifecycle State Machine

The `TaskState` enum defines eight states with validated transitions:

```
submitted → working → completed
                   → failed
                   → canceled
                   → input_required → working
                                    → canceled
         → rejected
         → canceled
         → auth-required → working
                         → canceled
```

Terminal states (`completed`, `failed`, `canceled`, `rejected`) have no outgoing transitions. The `validate_transition` function enforces these rules:

```python
def validate_transition(from_state: TaskState, to_state: TaskState) -> bool:
    allowed = VALID_TRANSITIONS.get(from_state, set())
    return to_state in allowed
```

This prevents bugs where a completed task is accidentally moved back to `working` — a state machine violation that would confuse any system polling task status.

## Discriminated Union for Content Parts

Messages carry heterogeneous content via a discriminated union on the `type` field:

```python
Part = Annotated[TextPart | FilePart | DataPart, Field(discriminator="type")]
```

Pydantic uses the `type` literal to decide which model to instantiate:
- `TextPart` — plain text with optional metadata
- `FilePart` — file by base64 bytes or URI reference, with media type
- `DataPart` — arbitrary structured JSON

The discriminator approach gives precise error messages when validation fails ("expected type=text, got type=unknown") rather than trying all variants in sequence.

## Agent Card

`AgentCard` is the capabilities manifest that A2A agents publish at `/.well-known/agent.json`:

```python
class AgentCard(BaseModel):
    name: str
    description: str
    url: str
    version: str
    protocol_version: str = "0.2.5"
    capabilities: AgentCapabilities
    skills: list[AgentSkill]
    default_input_modes: list[str]
    default_output_modes: list[str]
    security_schemes: dict[str, Any]
```

The `skills` list advertises what the agent can do, enabling orchestrators to route tasks to the most capable agent. `security_schemes` and `security_requirements` follow the OpenAPI security model for bearer tokens, API keys, and OAuth.

## PocketPaw Extension: `history` in `TaskSendParams`

The standard A2A spec's `TaskSendParams` carries a single message. PocketPaw adds an optional `history` field:

```python
history: list[A2AMessage] = Field(default_factory=list)
```

The comment is explicit: this preserves role/turn boundaries across multi-turn conversations. Without it, callers must flatten prior messages into the current message's parts, losing the distinction between "user" and "agent" turns. Remote agents that don't understand this field ignore it — the extension is backward compatible.

## Streaming Event Models

Two event models carry real-time updates during task execution:

- `TaskStatusUpdateEvent` — emitted when task state changes (e.g., `working` → `completed`). The `final: bool` flag signals the last event.
- `TaskArtifactUpdateEvent` — emitted as artifacts are produced. `append: bool` indicates whether this is an incremental chunk; `last_chunk: bool` marks completion.

## JSON-RPC Envelope Models

`JSONRPCRequest` and `JSONRPCResponse` are Pydantic models for the JSON-RPC 2.0 wire format. They coexist with the `A2ADispatcher`'s raw dict handling — the models are used for documentation and serialization, while the dispatcher works with raw dicts for performance.

## Known Gaps

The `FilePart` model has both `bytes_data` (base64) and `uri` fields but no validation enforcing that exactly one is present. A file part with neither field would be accepted by Pydantic but would be unprocessable by a receiver.