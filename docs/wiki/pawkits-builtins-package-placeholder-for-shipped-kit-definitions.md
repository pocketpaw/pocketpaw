---
{
  "title": "PawKits Builtins Package: Placeholder for Shipped Kit Definitions",
  "summary": "The `pocketpaw.kits.builtins` package is a reserved namespace for built-in PawKit definitions that ship with PocketPaw. Currently it contains only a module docstring, indicating the built-in kit definitions are loaded from YAML files via the catalog rather than from Python modules.",
  "concepts": [
    "PawKits",
    "builtins",
    "YAML-first architecture",
    "package namespace",
    "built-in kits",
    "catalog",
    "get_builtin_yaml"
  ],
  "categories": [
    "kits",
    "package structure"
  ],
  "source_docs": [
    "149cf47afb9a216b"
  ],
  "backlinks": null,
  "word_count": 230,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/kits/builtins/__init__.py` marks the `pocketpaw.kits.builtins` namespace as a Python package with the single docstring:

```
Built-in PawKit definitions shipped with PocketPaw.
```

The file contains no imports, no classes, and no functions — its current role is purely structural.

## Why This Package Exists

In Python, a directory must contain an `__init__.py` to be importable as a package. The `builtins/` subdirectory exists to house built-in kit definitions that ship with PocketPaw, separate from user-installed kits. Keeping them in a dedicated subpackage allows future contributors to add Python-defined kits here without touching the catalog or store layers.

## Current State: YAML-First Architecture

The built-in kits are currently defined as YAML files loaded by `kits.catalog` via `get_builtin_yaml()`. This means the `builtins/` package directory is a placeholder — the actual kit data lives in YAML, not Python modules.

This is a deliberate design choice: YAML is easier for non-engineers to author and review than Python dataclasses, and YAML files can be read by the frontend for preview purposes without running Python.

## Future Direction

If built-in kits ever need programmatic logic (dynamic metrics, conditional panels), they would be implemented as Python modules inside this package. The `__init__.py` would then export them for the catalog to discover.

## Known Gaps

- **Entirely empty**: No built-in kit modules exist yet. All built-ins are loaded from YAML in `kits/builtins/*.yaml`. The package is a forward-looking stub.