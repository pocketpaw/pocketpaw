---
{
  "title": "CLI Config Command: Inspect, Set, Validate, and Locate PocketPaw Settings",
  "summary": "The `config` CLI command exposes four operations on PocketPaw's `Settings` object — displaying the current config with secrets masked, writing a typed key-value pair, running batch API key validation, and printing the config file path. The file is named `config_cmd.py` to avoid a Python module name collision with the `pocketpaw.config` package.",
  "concepts": [
    "CLI config",
    "Settings",
    "secret masking",
    "type coercion",
    "API key validation",
    "config file path",
    "pydantic_settings",
    "mask_value",
    "module naming collision"
  ],
  "categories": [
    "CLI",
    "Configuration"
  ],
  "source_docs": [
    "7f90d587dec73a9e"
  ],
  "backlinks": null,
  "word_count": 536,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/cli/config_cmd.py` implements the `pocketpaw config` subcommand, which acts as the operator's window into `Settings`. It handles four distinct actions dispatched by the `run_config_cmd` entry point.

## Module Naming: Why `config_cmd.py`?

The file is deliberately named `config_cmd.py` rather than `config.py`. A file named `config.py` inside the `pocketpaw.cli` package would shadow `pocketpaw.config` in certain import resolution paths, causing subtle import failures that are hard to diagnose. The naming convention prevents this class of bug entirely.

## Showing Config: `_show_config`

The function calls `get_settings()` and dumps all fields via `model_dump()`. Before rendering, it masks any field whose name contains `key`, `token`, `secret`, or `password` using `mask_value` from `cli.utils`. This masking applies in both human-readable table mode and the `--json` output path, preventing secrets from leaking into logs or CI artifacts.

The grouping logic splits the flat settings dictionary into visual sections by the first `_`-delimited prefix (e.g., `discord_`, `slack_`, `anthropic_`). Fields that are empty, `None`, `[]`, or `{}` are silently omitted — this keeps the display focused on what is actually configured and avoids scrolling past dozens of blank entries.

```python
prefix = k.split("_")[0] if "_" in k else "general"
```

Fields starting with `_` are excluded as internal/computed. Values longer than 80 characters are truncated at 77 characters with `...` to avoid line wrapping.

## Setting Config Values: `_set_config`

The `set` action validates the key against the live `Settings` instance using `hasattr` before writing. This prevents creating phantom keys that would be silently ignored by Pydantic on the next load. If the key exists, the current field value is inspected to determine the target type, and the incoming string is coerced:

- `bool`: accepts `true`, `1`, `yes` (case-insensitive)
- `int` / `float`: parsed directly
- `list`: comma-separated values, stripped of whitespace
- Everything else: stored as a string

This coercion layer exists because all CLI inputs arrive as strings. Without it, setting a boolean field like `health_check_on_startup` to `true` would store the string `"true"` rather than `True`, breaking any downstream `if settings.health_check_on_startup` check.

After writing, `settings.save()` is called and the masked final value is printed.

## Validating API Keys: `_validate_config`

`_validate_config` calls `validate_api_keys(settings)` which performs prefix-based format checks on Anthropic, OpenAI, and Telegram credentials. The result is advisory: warnings are shown but a non-zero exit code is only returned if there are warnings. This means scripts can use `pocketpaw config validate` as a pre-flight check before starting the agent.

The `--json` output produces `{"valid": bool, "warnings": [...]}`, which integrates cleanly with CI health checks.

## Showing the Config Path: `_show_path`

`_show_path` prints the config file path via `get_config_path()`. This exists because the config directory (`~/.pocketpaw/`) is computed at runtime and may vary across environments. Exposing it as a CLI command lets operators reliably locate and back up the config file without hardcoding the path.

## Known Gaps

- **No `unset` action**: There is no `pocketpaw config unset <key>` subcommand. To clear a value, an operator must `set` it to an empty string and hope the downstream code handles empty string as "not configured."
- **Type coercion for dicts**: The `set` action handles `list` but not `dict` fields. Settings fields like `pii_type_actions` (a `dict[str, str]`) cannot be set via the CLI without editing the JSON file directly.
