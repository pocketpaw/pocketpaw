---
{
  "title": "fetch.py: Jail-Safe File Browser with Pydantic Validation and Telegram Keyboard Support",
  "summary": "`fetch.py` provides path-traversal-safe file browsing via `is_safe_path()`, a `FetchRequest` Pydantic model, and `handle_path()` / `list_directory()` helpers. The Telegram `InlineKeyboardMarkup` import is optional, enabling interactive directory navigation buttons in Telegram without breaking non-Telegram deployments.",
  "concepts": [
    "is_safe_path",
    "FetchRequest",
    "path_traversal_prevention",
    "Pydantic_validation",
    "file_jail",
    "Telegram_InlineKeyboardMarkup",
    "optional_dependency",
    "handle_path",
    "list_directory",
    "directory_navigation"
  ],
  "categories": [
    "tools",
    "filesystem",
    "security",
    "telegram-integration"
  ],
  "source_docs": [
    "c385dedf00df0bd0"
  ],
  "backlinks": null,
  "word_count": 604,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`fetch.py` is PocketPaw's shared file-access utility. It is not a `BaseTool` subclass — instead it provides foundational helpers used by other components: `is_safe_path()` is imported by `SpeechToTextTool` and `UrlExtractTool`, and `handle_path()` / `list_directory()` serve the web dashboard and Telegram bot channel adapters.

## is_safe_path(): The Core Jail Primitive

```python
def is_safe_path(path: Path, jail: Path) -> bool:
    try:
        resolved_path = path.resolve()
        resolved_jail = jail.resolve()
        return resolved_path.is_relative_to(resolved_jail)
    except (ValueError, OSError):
        return False
```

This function is the system-wide path traversal prevention primitive. Both `path` and `jail` are resolved (symlinks collapsed, `..` normalized) before comparison. The `is_relative_to()` method ensures the path is strictly inside the jail — not equal to the jail root (which would also be "relative to" itself in some implementations), not a sibling directory.

The `except (ValueError, OSError): return False` pattern treats any resolution failure as "not safe." On Windows, cross-drive paths raise `ValueError` from `is_relative_to()`. On any OS, permission errors during `resolve()` raise `OSError`. Both cases are conservatively treated as unsafe — the function never crashes, it only ever returns `True` or `False`.

## FetchRequest Pydantic Model

```python
class FetchRequest(BaseModel):
    path_str: str = Field(..., description="The path to explore. Cannot be empty.")
    jail_str: str = Field(..., description="The strictly enforced jail directory.")
    limit: int = Field(30, ge=1, le=100)

    @field_validator("path_str", "jail_str", mode="before")
    @classmethod
    def prevent_empty(cls, v: Any) -> str:
        if not str(v).strip():
            raise ValueError("Path string cannot be empty or whitespace.")
        return target
```

`FetchRequest` uses Pydantic v2's `field_validator` to reject empty or whitespace-only path strings before any filesystem interaction. An empty `path_str` would resolve to the current working directory — likely not the jail root — creating an accidental traversal. The `mode="before"` ensures the validator runs before Pydantic's type coercion, catching `None` values that Pydantic would otherwise silently convert.

The `limit` field has Pydantic-enforced bounds `ge=1, le=100`, preventing both zero-length listings and excessively large directory enumerations.

The `resolve_paths()` method on the model combines path resolution with the jail check, making it a self-validating value object — constructing a valid `FetchRequest` and calling `resolve_paths()` guarantees both path safety and type correctness.

## Telegram Optional Dependency

```python
try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
except ImportError:
    InlineKeyboardButton = None
    InlineKeyboardMarkup = None
```

The Telegram keyboard types are conditionally imported at module load time. Functions that use them check `if InlineKeyboardMarkup is None: return None` before attempting to build keyboard objects. This pattern makes the Telegram-specific features silently unavailable in non-Telegram deployments without breaking imports or requiring conditional package installation.

## _get_directory_keyboard_resolved()

This internal function generates an `InlineKeyboardMarkup` for Telegram bot directory navigation. It produces two types of buttons:
- A ".." parent directory button (when not at the jail root)
- One button per directory/file in the listing (sorted: dirs first, then files, hidden entries excluded)

The directory sort puts dirs before files — standard file manager convention — using `key=lambda x: (not x.is_dir(), x.name.lower())`. The `not x.is_dir()` converts `True` (is directory) to `0` (sorts first) and `False` (is file) to `1` (sorts after).

## handle_path() and list_directory()

`handle_path()` is the async entry point for path-based navigation: it either returns a directory listing (as a dict suitable for JSON serialization) or a file's contents. `list_directory()` is a synchronous variant that formats output as a human-readable string for the web dashboard. Both enforce the jail via `FetchRequest.resolve_paths()`.

## Known Gaps

- `handle_path()` and `list_directory()` full implementations were not shown — file type handling (binary vs. text, size limits for file reads) is opaque.
- No MIME type detection — binary files might be read as text and returned garbled.
- The `limit` parameter caps directory entries shown but doesn't paginate — entries beyond `limit` are silently dropped.
