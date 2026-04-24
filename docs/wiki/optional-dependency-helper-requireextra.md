---
{
  "title": "Optional Dependency Helper: `require_extra`",
  "summary": "`_compat.py` provides a single utility function that raises a clean `ImportError` with pip install instructions when an optional PocketPaw feature's dependency is missing. This replaces scattered `try/except ImportError` blocks across channel adapters and integrations with a single, consistent error message pattern.",
  "concepts": [
    "optional dependencies",
    "extras",
    "ImportError",
    "require_extra",
    "pip install",
    "compatibility",
    "channel adapters"
  ],
  "categories": [
    "package structure",
    "error handling",
    "developer experience"
  ],
  "source_docs": [
    "3f0983f112484b7e"
  ],
  "backlinks": null,
  "word_count": 250,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Purpose

PocketPaw uses optional extras (`pocketpaw[discord]`, `pocketpaw[telegram]`, etc.) so users only install the dependencies they need. When code that requires an optional package is executed without it installed, Python raises a bare `ImportError` that typically says something like `No module named 'discord'` — unhelpful to a user who doesn't know which PocketPaw extra provides it.

`require_extra` standardizes this into an actionable error:

```python
def require_extra(package: str, extra: str) -> None:
    raise ImportError(
        f"'{package}' is required but not installed. "
        f"Install it with: pip install 'pocketpaw[{extra}]'"
    )
```

## Usage Pattern

Channel adapters and optional integrations call this at the top of their guarded import blocks:

```python
try:
    import discord
except ImportError:
    require_extra("discord", "discord")
```

When `discord` is missing, the user sees:

```
ImportError: 'discord' is required but not installed.
Install it with: pip install 'pocketpaw[discord]'
```

The function always raises — it has no return path. The `-> None` return type annotation reflects this (it never returns normally).

## Why a Separate Module

Placing this in `_compat.py` rather than inline in each adapter keeps the pattern consistent and testable. It also signals intent: this module exists for forward/backward compatibility shims and optional-dependency helpers, separate from the main package logic.

## Known Gaps

The function signature accepts `package` and `extra` as separate strings, which allows cases like `require_extra("motor", "cloud")` where the PyPI package name differs from the PocketPaw extra name. However, there is no registry of which extras provide which packages — the mapping is implicit in each call site.