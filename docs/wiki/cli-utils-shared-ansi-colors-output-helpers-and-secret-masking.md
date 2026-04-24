---
{
  "title": "CLI Utils: Shared ANSI Colors, Output Helpers, and Secret Masking",
  "summary": "The `cli.utils` module provides the shared visual primitives used by every PocketPaw CLI subcommand — ANSI color constants, formatted print helpers, JSON output, and a `mask_value` function that automatically redacts secrets before display. Centralizing these in one module ensures consistent styling and prevents accidental secret exposure across all CLI commands.",
  "concepts": [
    "ANSI colors",
    "CLI utils",
    "secret masking",
    "mask_value",
    "print helpers",
    "JSON output",
    "TTY detection",
    "output formatting",
    "shared utilities"
  ],
  "categories": [
    "CLI",
    "Utilities"
  ],
  "source_docs": [
    "39f334c3c7144447"
  ],
  "backlinks": null,
  "word_count": 547,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/cli/utils.py` is the foundation layer for PocketPaw's CLI presentation. Every subcommand imports from this module rather than defining its own ANSI sequences or output formatting. This centralization is a deliberate architectural decision: changing the color scheme, masking policy, or JSON formatting requires editing one file rather than a dozen.

## ANSI Color Constants

```python
GREEN = "[32m"
YELLOW = "[33m"
RED = "[31m"
BOLD = "[1m"
DIM = "[2m"
RESET = "[0m"
```

These six constants cover the full visual vocabulary of the CLI. The consistent color semantics across all commands (green = success, yellow = warning, red = error, dim = supplementary info) allow operators to parse output at a glance without reading every word.

No TTY detection is applied to these constants. Commands use `is_tty()` separately when they need to conditionally emit ANSI codes.

## Structured Print Helpers

`print_header(title, subtitle)` renders a section heading with optional subtitle. `print_row(label, value, indent)` renders a key-value pair with fixed-width label padding (24 characters). `print_ok`, `print_warn`, and `print_fail` render prefixed status messages:

```
  [OK]   All checks passed.
  [WARN] Telegram token format is unusual.
  [FAIL] Anthropic API key not configured.
```

The consistent prefix format means operators can scan a wall of output for `[FAIL]` entries without reading every line — a meaningful usability improvement over raw `print` calls.

## JSON Output: `output_json`

```python
def output_json(data: object) -> None:
    print(json.dumps(data, indent=2, default=str))
```

The `default=str` parameter serializes any non-JSON-native type (datetime, Path, enum) as its string representation rather than raising `TypeError`. This is a defensive choice: CLI commands deal with data from multiple sources (file system, memory manager, health engine) that may include types not anticipatable at the time of writing.

## TTY Detection: `is_tty`

```python
def is_tty() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
```

The `hasattr` check guards against environments where `sys.stdout` has been replaced with an object that does not have an `isatty` method (e.g., some test runners or stream wrappers). This prevents `AttributeError` in unusual execution contexts.

## Secret Masking: `mask_value`

```python
_SECRET_SUBSTRINGS = ("key", "token", "secret", "password")

def mask_value(key: str, value: str) -> str:
    is_secret = any(s in key.lower() for s in _SECRET_SUBSTRINGS)
    if is_secret and len(value) > 12:
        return f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"
    if is_secret and value:
        return "****"
    return value
```

This function is the last line of defense against secrets appearing in terminal output, CI logs, or screenshots. It identifies sensitive fields by checking whether the field name contains any of four substrings (`key`, `token`, `secret`, `password`), then masks the middle portion of the value, leaving four characters visible at each end. For short secrets (12 characters or fewer), the entire value is replaced with `****`.

The 4+mask+4 pattern is intentional: enough characters are visible to confirm the correct key was entered (e.g., `sk-a****ntx` for an Anthropic key) without exposing usable credential material.

## Known Gaps

- **ANSI codes are always emitted**: ANSI constants are unconditional module-level strings. Commands that pipe output to files or non-TTY consumers must handle stripping ANSI themselves or use `--json`. A `strip_ansi()` utility or conditional constant initialization would improve composability.
- **`_SECRET_SUBSTRINGS` is a fixed list**: New field types (e.g., `bearer`, `credential`) are not covered unless added to this list. A contributor adding a new sensitive field type must remember to update this list.
