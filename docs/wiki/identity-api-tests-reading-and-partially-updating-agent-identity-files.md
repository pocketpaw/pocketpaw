---
{
  "title": "Identity API Tests: Reading and Partially Updating Agent Identity Files",
  "summary": "This test file covers PocketPaw's `/api/v1/identity` router, which exposes the agent's persistent identity configuration — the markdown files that define personality, soul, style, instructions, and user profile — as a readable and partially updatable API resource.",
  "concepts": [
    "identity files",
    "IDENTITY.md",
    "SOUL.md",
    "STYLE.md",
    "INSTRUCTIONS.md",
    "USER.md",
    "DefaultBootstrapProvider",
    "partial update",
    "agent personality",
    "config directory",
    "markdown identity"
  ],
  "categories": [
    "identity",
    "agent configuration",
    "API",
    "testing",
    "test"
  ],
  "source_docs": [
    "c18108efc4a7f2dd"
  ],
  "backlinks": null,
  "word_count": 436,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's identity system stores an agent's personality and behavioural context in a set of markdown files (`IDENTITY.md`, `SOUL.md`, `STYLE.md`, `INSTRUCTIONS.md`, `USER.md`) within the config directory. The identity API lets the dashboard read and edit these files without the user having to locate them manually. This test file verifies that reading returns all five fields and that writing is partial-update-safe.

## Reading Identity (`GET /identity`)

`TestGetIdentity` patches both the config path and the `DefaultBootstrapProvider` to return a controlled `context` object. The route reads the context fields and maps them to API response keys:

| Context field | API response key |
|---|---|
| `context.identity` | `identity_file` |
| `context.soul` | `soul_file` |
| `context.style` | `style_file` |
| `context.instructions` | `instructions_file` |
| `context.user_profile` | `user_file` |

The naming convention (`_file` suffix) signals to API consumers that these are raw file contents, not structured data, helping avoid confusion with other identity-related objects in the system.

## Writing Identity (`PUT /identity`)

`TestSaveIdentity` exercises three scenarios that together define the update semantics:

### Full update

Sending `identity_file` and `soul_file` creates or overwrites `IDENTITY.md` and `SOUL.md` under `<config_dir>/identity/`. The response confirms `ok: true` and lists both filenames in `updated`. The test verifies the actual file contents on disk:

```python
assert (identity_dir / "IDENTITY.md").read_text(encoding="utf-8") == "Updated identity"
assert (identity_dir / "SOUL.md").read_text(encoding="utf-8") == "Updated soul"
```

This direct filesystem assertion is important: it catches bugs where the route acknowledges the save but does not actually write the file.

### Partial update

Sending only `style_file` writes only `STYLE.md`, and the response lists only `["STYLE.md"]`. The other files are left untouched. This matters because the dashboard edits identity files one section at a time — a save of the style section must not reset the soul or instructions files to empty.

### Empty body

An empty JSON object `{}` returns 200 with `updated: []`. This is a valid no-op rather than an error, allowing clients to call the endpoint without knowing in advance whether they have changes to commit.

## Why File-Based Identity

Storing identity in markdown files rather than a database makes the configuration human-readable and version-controllable. Users can open `SOUL.md` in a text editor, commit it to git, and diff changes over time. The API layer is a convenience wrapper, not the authoritative store.

## Known Gaps

No `TODO` or `FIXME` markers are present. The tests do not cover: what happens if the `identity/` subdirectory does not exist and cannot be created, concurrent writes to the same file, or writing values that exceed a reasonable size limit. There is also no test for `GET /identity` when the bootstrap provider fails.