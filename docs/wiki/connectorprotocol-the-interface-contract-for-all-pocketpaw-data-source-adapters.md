---
{
  "title": "ConnectorProtocol: The Interface Contract for All PocketPaw Data Source Adapters",
  "summary": "`protocol.py` defines the structural typing interface that all connector adapters must satisfy, along with the shared result dataclasses and enums they return. The addition of `IngestACL` and `IngestAdapter` in Move 7 ensures that documents pulled into Single Brain carry their source-side permissions, preventing data from a private Slack channel from becoming accessible to unauthorized users.",
  "concepts": [
    "ConnectorProtocol",
    "IngestAdapter",
    "IngestACL",
    "TrustLevel",
    "ConnectorStatus",
    "ActionSchema",
    "ActionResult",
    "structural typing",
    "Protocol",
    "Single Brain permissions"
  ],
  "categories": [
    "connectors",
    "architecture",
    "access control"
  ],
  "source_docs": [
    "17afb2be0b6d6c6c"
  ],
  "backlinks": null,
  "word_count": 425,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`protocol.py` is the contract layer for PocketPaw's connector system. It defines what every adapter must look like (via Python's `Protocol`) and what they must return (via dataclasses). No adapter logic lives here — only shapes and enums.

## Core Enums

```python
class ConnectorStatus(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTED    = "connected"
    SYNCING      = "syncing"
    ERROR        = "error"

class TrustLevel(StrEnum):
    AUTO    = "auto"     # agent executes without asking
    CONFIRM = "confirm"  # requires user approval
    DENY    = "deny"     # never execute
```

`TrustLevel` is the per-action safety gate. It prevents an LLM from autonomously deleting files or deploying code without human confirmation. Adapters tag each `ActionSchema` with the appropriate level, and the router enforces it before dispatching.

## Result Dataclasses

| Class | Purpose |
|---|---|
| `ConnectionResult` | Outcome of `connect()` — success flag, status, message |
| `ActionSchema` | Descriptor for one action (name, method, parameters, trust level) |
| `ActionResult` | Outcome of `execute()` — success flag, data payload, error message |
| `SyncResult` | Outcome of `sync()` — records synced, errors |

Using dataclasses rather than raw dicts gives IDE auto-completion, type checking, and prevents adapters from returning inconsistent shapes that the router might silently mishandle.

## ConnectorProtocol

```python
class ConnectorProtocol(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def display_name(self) -> str: ...
    async def connect(self, pocket_id: str, config: dict[str, Any]) -> ConnectionResult: ...
    async def disconnect(self, pocket_id: str) -> bool: ...
    async def actions(self) -> list[ActionSchema]: ...
    async def execute(self, action: str, params: dict[str, Any]) -> ActionResult: ...
    async def sync(self, pocket_id: str) -> SyncResult: ...
```

Python's structural typing means adapters do not need to inherit from `ConnectorProtocol` — they just need to implement the methods. This keeps adapter code free of base-class coupling.

## IngestACL and IngestAdapter (Move 7)

```python
@dataclass
class IngestACL:
    allowed_user_ids: list[str]
    allowed_group_ids: list[str]
    is_public: bool

class IngestAdapter(ConnectorProtocol, Protocol):
    async def permissions(self, pocket_id: str, record_id: str) -> IngestACL: ...
```

`IngestAdapter` extends `ConnectorProtocol` with a `permissions()` method that returns the source-side ACL for each document. When an adapter pulls a Slack message into Single Brain, it also fetches the channel's membership list and tags the document with an `IngestACL`. The Fabric layer then enforces these permissions when the document is later retrieved — a private channel's messages stay private even inside PocketPaw's unified knowledge store.

This design prevents a common data-integration vulnerability: pulling data into a shared system and losing the source permissions in the process.

## Known Gaps

None flagged. The file is marked as updated through Move 7 and the `IngestACL` design is complete.
