---
{
  "title": "LLM Backend Schemas — Capability and Installation Contracts",
  "summary": "The backend schemas define how PocketPaw describes available LLM backends to clients. `BackendInfo` captures a backend's capabilities and installation requirements, while `BackendInstallRequest` provides the payload for triggering a backend installation.",
  "concepts": [
    "LLM backends",
    "BackendInfo",
    "BackendInstallRequest",
    "capabilities",
    "requiredKeys",
    "beta backends",
    "installHint",
    "available flag",
    "builtinTools"
  ],
  "categories": [
    "backends",
    "schemas",
    "configuration"
  ],
  "source_docs": [
    "80a1d5cd40c0907e"
  ],
  "backlinks": null,
  "word_count": 388,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw supports multiple LLM backends — Ollama, LM Studio, Claude, OpenAI, and others. The `backends.py` schemas define the data shapes used when the dashboard queries which backends are available, what each can do, and how to install missing ones.

## `BackendInfo`

```python
class BackendInfo(BaseModel):
    name: str
    displayName: str
    available: bool
    capabilities: list[str] = []
    builtinTools: list[str] = []
    requiredKeys: list[str] = []
    supportedProviders: list[str] = []
    installHint: dict = {}
    beta: bool = False
```

This model represents a single LLM backend from the user's perspective.

**`available: bool`** is the most critical field — it tells the dashboard whether the backend can actually be used right now. A backend with `available=False` might be unsupported on the current OS, have unmet dependencies, or require API keys that are not yet configured.

**`capabilities`** is a string list that describes what the backend supports beyond basic text generation — for example, `["vision", "function_calling", "streaming"]`. The dashboard uses this to filter backends when the user's task requires specific features.

**`builtinTools`** lists tools that the backend provides natively (e.g., some backends include web search or code execution without PocketPaw needing to supply them).

**`requiredKeys`** names the settings fields that must be populated before the backend can be used (e.g., `["openai_api_key"]` for OpenAI). This lets the dashboard surface a contextual "configure this first" prompt without hardcoding which backends require keys.

**`installHint`** is an untyped dict that can carry arbitrary installation instructions — package names, shell commands, download URLs — structured for the frontend to render. Leaving it as `dict` rather than a typed model provides flexibility as installation flows differ significantly across backends.

**`beta: bool`** flags experimental backends that may change behaviour or have stability issues. The dashboard can visually distinguish beta backends.

**Naming conventions**: The schema mixes snake_case (`display_name` would be idiomatic Pydantic) with camelCase (`displayName`, `builtinTools`). This reflects direct serialisation for the frontend without transformation.

## `BackendInstallRequest`

```python
class BackendInstallRequest(BaseModel):
    backend: str
```

A minimal payload identifying which backend to install. The installation logic (downloading packages, configuring system dependencies) lives in the backend manager; this schema simply names the target.

## Known Gaps

- `installHint` is an untyped `dict`. A typed model (e.g., `InstallHint(type: str, command: str | None, url: str | None)`) would make frontend rendering more predictable.
- There is no schema for backend removal or update operations.
