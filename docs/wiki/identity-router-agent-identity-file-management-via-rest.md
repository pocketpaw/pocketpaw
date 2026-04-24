---
{
  "title": "Identity Router — Agent Identity File Management via REST",
  "summary": "The identity router exposes PocketPaw's five agent identity files (IDENTITY.md, SOUL.md, STYLE.md, INSTRUCTIONS.md, USER.md) through a simple read/write REST interface, allowing dashboard users to customize the agent's persona, communication style, and behavioral instructions without editing files directly. Changes take effect on the next inbound message without requiring a server restart.",
  "concepts": [
    "identity files",
    "IDENTITY.md",
    "SOUL.md",
    "STYLE.md",
    "INSTRUCTIONS.md",
    "USER.md",
    "DefaultBootstrapProvider",
    "agent persona",
    "hot-reload",
    "bootstrap context",
    "admin scope"
  ],
  "categories": [
    "API",
    "Identity",
    "Agent Configuration"
  ],
  "source_docs": [
    "e43dd49bd5f8af87"
  ],
  "backlinks": null,
  "word_count": 415,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw agents are shaped by a set of identity files that define who they are, how they communicate, what they know about the user, and how they should behave. The identity router provides the HTTP interface for reading and editing these files — the agent's "personality editor" in the dashboard.

## The Five Identity Files

The router manages exactly five files, each serving a distinct purpose:

| File | Purpose |
|------|---------|
| `IDENTITY.md` | Core persona — name, role, background |
| `SOUL.md` | Values, beliefs, emotional character |
| `STYLE.md` | Communication style, tone, formatting preferences |
| `INSTRUCTIONS.md` | Behavioral rules and task-specific guidance |
| `USER.md` | User profile — preferences the agent should remember |

This separation allows fine-grained customization: a user might frequently update `STYLE.md` while leaving `IDENTITY.md` unchanged for months.

## Read: Bootstrap Provider Integration

`GET /identity` loads all five files via `DefaultBootstrapProvider`:

```python
provider = DefaultBootstrapProvider(get_config_path().parent)
context = await provider.get_context()
```

Rather than reading files directly, the router delegates to the same bootstrap provider the AgentLoop uses at conversation start. This ensures that the dashboard always displays the same identity content the agent will actually use — there's no risk of the API and the agent reading from different paths.

## Write: Lazy Directory Creation

`PUT /identity` maps each request field to a filename and writes only the fields present in the request body (fields absent or `None` are skipped via `exclude_none=True`). The identity directory is created with `mkdir(parents=True, exist_ok=True)` before any writes — this handles fresh installs where the identity directory doesn't exist yet without failing.

## Hot-Reload Semantics

The endpoint docstring explicitly states: "Changes take effect on the next message." The AgentLoop reads identity files at the start of each conversation turn, not at startup. This means identity edits are effective immediately without restarting the server — a key usability property for users who are actively tuning their agent's behavior.

## Admin Scope Guard

Both endpoints require `admin` scope. This prevents non-admin API keys from modifying the agent's identity — an important guard since identity files directly influence the agent's behavior and could be used to inject malicious instructions if left unprotected.

## Known Gaps

The write endpoint overwrites files entirely — there is no patch/merge semantic. A client that wants to add one line to `INSTRUCTIONS.md` must read the current content, append locally, and write the full file back. A concurrent write from two dashboard tabs could silently overwrite one party's changes.