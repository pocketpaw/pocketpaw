---
{
  "title": "Agent Identity File Schemas",
  "summary": "Defines Pydantic models for reading and updating the five identity files that shape an agent's persona: identity, soul, style, instructions, and user context. The partial-update pattern on the save request ensures callers only need to supply the files they want to change.",
  "concepts": [
    "IdentityResponse",
    "IdentitySaveRequest",
    "IdentitySaveResponse",
    "identity files",
    "soul file",
    "style file",
    "instructions file",
    "partial update",
    "agent persona",
    "Pydantic"
  ],
  "categories": [
    "api-schemas",
    "agent-identity",
    "configuration"
  ],
  "source_docs": [
    "4508172c26fa60ff"
  ],
  "backlinks": null,
  "word_count": 477,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw agents are configured through a set of plain-text or markdown files that collectively define who the agent is. This module provides the schema layer for the identity API — fetching the current file contents and updating one or more of them atomically.

## Identity File Roles

Each file serves a distinct purpose in the agent's runtime behaviour:

- **`identity_file`** — Core persona definition: name, role, backstory.
- **`soul_file`** — Persistent memory and personality traits (used by Soul Protocol integration).
- **`style_file`** — Communication style rules: tone, verbosity, formatting preferences.
- **`instructions_file`** — Operational instructions: how the agent should approach tasks.
- **`user_file`** — Information about the human user: preferences, context, relationships.

Separating these concerns into distinct files means operators can update communication style without touching the core persona, or swap user context between clients without altering the agent's instructions.

## Models

### `IdentityResponse`

```python
class IdentityResponse(BaseModel):
    identity_file: str = ""
    soul_file: str = ""
    style_file: str = ""
    instructions_file: str = ""
    user_file: str = ""
```

All fields default to empty strings. This means the API can return a valid response even when some identity files haven't been created yet — the agent is operational but unconfigured for that dimension. The dashboard can then prompt the user to fill in missing files.

### `IdentitySaveRequest`

```python
class IdentitySaveRequest(BaseModel):
    identity_file: str | None = None
    soul_file: str | None = None
    style_file: str | None = None
    instructions_file: str | None = None
    user_file: str | None = None
```

Every field is `Optional`, defaulting to `None`. This implements a **partial-update pattern**: a caller sending only `{"style_file": "Be concise."}` should not inadvertently wipe the identity or soul files. The backend interprets `None` as "leave unchanged" and only writes files for non-`None` fields. This prevents accidental data loss from incomplete PATCH payloads — a failure mode that would silently erase carefully crafted persona definitions.

### `IdentitySaveResponse`

```python
class IdentitySaveResponse(BaseModel):
    ok: bool = True
    updated: list[str] = []
```

`updated` lists the names of files actually written (e.g. `["style_file"]`). This confirmation is important: without it, clients can't distinguish between a no-op (all fields were `None`) and a successful write. The dashboard uses this to show which files were saved.

## Defensive Patterns

- `None`-defaulting on `IdentitySaveRequest` fields enforces partial-update semantics and prevents silent overwrites.
- `updated: list[str] = []` gives the client explicit confirmation of what changed, supporting idempotency checks.
- Empty-string defaults on `IdentityResponse` prevent `None` propagation to UI rendering code.

## Known Gaps

- No size limit is enforced on file content strings. Large identity files could cause memory pressure or exceed HTTP body limits.
- No validation that file content is valid UTF-8 or well-formed markdown — malformed content would be written as-is.
- The `updated` list uses field names (`"soul_file"`) rather than human-readable labels, which may require a mapping layer in the UI.