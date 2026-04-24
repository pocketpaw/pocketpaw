---
{
  "title": "MCP Server Presets: Catalog Registry, Config Conversion, and Dashboard Installation",
  "summary": "The MCP presets test suite validates PocketPaw's built-in catalog of popular MCP servers — covering unique ID enforcement, required field validation, category filtering, config generation from presets, and the dashboard API endpoints for browsing and installing presets. The Notion preset's hosted-OAuth path and transform functions receive dedicated coverage.",
  "concepts": [
    "MCP presets",
    "MCPPreset",
    "preset catalog",
    "preset_to_config",
    "transform function",
    "EnvKeySpec",
    "OAuth preset",
    "Notion",
    "Google Workspace",
    "dashboard API",
    "unique ID enforcement",
    "config generation"
  ],
  "categories": [
    "MCP integration",
    "configuration",
    "test"
  ],
  "source_docs": [
    "9f206e27b92e85b5"
  ],
  "backlinks": null,
  "word_count": 482,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Rather than requiring users to manually configure MCP servers (transport, command, env vars), PocketPaw ships a preset catalog of popular integrations. Each preset is a declarative `MCPPreset` object that encodes how to connect to a specific service. The `preset_to_config()` factory converts a preset into an `MCPServerConfig` ready for persistence and connection.

## Registry Integrity

`TestPresetRegistry` enforces invariants across the entire catalog:

- **Unique IDs**: `test_all_presets_have_unique_ids` uses a set comparison to catch duplicate preset IDs, which would cause silent overwrites in the registry dict.
- **Required fields**: `test_all_presets_have_required_fields` iterates every preset and asserts that `id`, `name`, `description`, `category`, and `transport` are non-empty. An incomplete preset would appear in the UI with missing labels.

## Notable Presets

`test_get_preset_notion_oauth` verifies that the Notion preset uses `transport="http"` with `oauth=True` and requires no env keys — Notion's MCP server is hosted by Notion itself and uses browser-based OAuth, so users do not need to manage API keys. This is architecturally distinct from stdio presets that run a local npm package.

`test_get_preset_google_workspace` verifies another hosted HTTP preset with its specific URL and env key requirements.

## Config Generation

`preset_to_config(preset, env_values)` creates an `MCPServerConfig` from a preset plus user-supplied env values:

- **Basic stdio**: command, args, and env are populated from the preset and user input.
- **Basic http**: transport and URL are preserved; no command or args.
- **Extra args**: preset-defined extra args are included.
- **No env**: when a preset has no env key specs, the resulting config has an empty env dict.
- **Immutability**: `test_preset_to_config_does_not_mutate_original` patches a preset and asserts that the original preset object is not modified — the factory must create a fresh dict, not modify the preset in place.

## Transform Functions

Some presets define a `transform` function that post-processes env values before injection — for example, extracting a project ID from a full URL, or normalizing a key format. Tests verify:

- `test_preset_to_config_applies_transform`: the transform runs and its output is used.
- `test_preset_to_config_no_transform_passthrough`: when no transform is defined, the raw user input is used.
- `test_preset_to_config_transform_skips_empty_value`: transforms are not called for empty user inputs, preventing transform functions from producing garbled values from blank fields.

## Dashboard API

`TestPresetRoutes` tests the HTTP API for the presets dashboard:

- **`GET /api/mcp/presets`**: returns the full catalog list; requires auth.
- **`POST /api/mcp/presets/{preset_id}/install`**: validates required env keys, creates an `MCPServerConfig`, and adds it via `MCPManager`. Returns 404 for unknown presets, 400 for missing required env vars.
- **`test_install_preset_no_required_env`**: a preset with no required env keys installs without needing any user input.
- **`test_presets_include_oauth_field`**: the API response includes the `oauth` flag so the frontend can show the OAuth flow button instead of an env-var form.

## Known Gaps

- `test_install_preset_success` uses a mock `MCPManager` and does not verify that the server actually connects after installation.
- There are no tests for updating or removing an installed preset — the install endpoint only handles first-time addition.