---
{
  "title": "Identity API GET and PUT Endpoint Tests",
  "summary": "This test module verifies the `/api/identity` REST endpoints that allow users to read and update the five identity files (identity, soul, style, instructions, user_profile) that shape the agent's persona. It also confirms that changes saved via the API are immediately reflected in the system prompt built by `AgentContextBuilder`.",
  "concepts": [
    "identity API",
    "GET /api/identity",
    "PUT /api/identity",
    "identity files",
    "user_profile",
    "soul file",
    "style file",
    "instructions file",
    "AgentContextBuilder",
    "DefaultBootstrapProvider",
    "partial update",
    "system prompt ordering"
  ],
  "categories": [
    "testing",
    "REST API",
    "identity system",
    "agent persona",
    "prompt engineering",
    "test"
  ],
  "source_docs": [
    "5429f8e5575a217f"
  ],
  "backlinks": null,
  "word_count": 544,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's identity system lets users customize their AI companion's persona through five structured text files. The Identity API (`GET /api/identity` and `PUT /api/identity`) provides the dashboard and external tools with a way to read and write these files programmatically. The tests in `test_identity_api.py` enforce both the API contract and the end-to-end effect on agent behavior.

## File Inventory

The five identity files covered are:
- **identity** — who the agent is (name, background)
- **soul** — the agent's values and personality
- **style** — communication style preferences
- **instructions** — custom behavioral directives
- **user_profile** — information about the user

All five must be returned by `GET /api/identity`. The `instructions` file was added in the 2026-02-18 update, and dedicated tests ensure it is included alongside the original four.

## `TestGetIdentity`

- **`test_returns_all_five_files`** — asserts that the response contains keys for all five files. Missing a file silently would mean the user cannot edit it from the dashboard.
- **`test_returns_default_user_profile`** — when no user profile exists on disk, the API must return a sensible default (from `DefaultBootstrapProvider`) rather than an empty string or an error. This prevents the UI from showing a blank field that confuses users.
- **`test_returns_default_instructions`** — the same contract for the instructions file.

## `TestSaveIdentity`

- **`test_saves_all_files`** — `PUT` with all five file keys writes each to the correct path on disk.
- **`test_partial_update`** — only the submitted keys are written; unsubmitted files are left untouched. This prevents accidental overwrites when the user edits a single field.
- **`test_ignores_non_string_values`** — non-string values in the payload are silently skipped. This guards against malformed API calls setting file contents to `null`, `0`, or arrays.
- **`test_creates_identity_dir_if_missing`** — the identity directory is created on first write if it does not exist. First-run users have no pre-existing identity directory.
- **`test_ignores_unknown_keys`** — extra keys in the payload (typos, future fields) are silently ignored. This makes the API forward-compatible.
- **`test_invalid_json_returns_400`** — malformed JSON in the request body must return HTTP 400. Without this, the server would crash with an unhandled parse error.

## `TestIdentityAgentIntegration`

These tests patch the filesystem to write identity files, then invoke `AgentContextBuilder.build_system_prompt()` and assert that the written content appears in the output.

- **`test_saved_user_profile_in_system_prompt`** — user profile content must appear in the prompt so the agent knows who it is talking to.
- **`test_instructions_between_style_and_knowledge`** — this test pins the *ordering* of identity sections in the prompt. Instructions appear after style and before knowledge sections. Ordering matters because earlier context influences how the model interprets later context.
- **`test_all_files_in_system_prompt`** — a composite test verifying all five file contents are present simultaneously.

## Design Rationale

The partial update behavior (`test_partial_update`) is particularly important for mobile/dashboard UX: a user editing only the "style" section in a small form should not risk losing their carefully written "identity" file because the form did not include it in the payload.

The integration tests use `DefaultBootstrapProvider` to generate predictable default content, then patch config paths to a `tmp_path` directory. This pattern avoids touching the developer's real identity files during testing.

## Known Gaps

No tests cover concurrent writes (race conditions if two dashboard tabs save simultaneously). No tests validate maximum file size limits — an identity file could theoretically consume the entire system prompt token budget.