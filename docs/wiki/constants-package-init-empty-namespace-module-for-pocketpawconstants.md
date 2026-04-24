---
{
  "title": "Constants Package Init: Empty Namespace Module for pocketpaw.constants",
  "summary": "The `pocketpaw/constants/__init__.py` is an intentionally empty module that exists solely to make `pocketpaw.constants` a Python package. Sub-modules like `tool_categories.py` are imported directly by consumers; no symbols are re-exported at the package level.",
  "concepts": [
    "package init",
    "empty module",
    "Python package",
    "constants namespace",
    "import isolation",
    "circular import prevention"
  ],
  "categories": [
    "constants",
    "module structure"
  ],
  "source_docs": [
    "e3b0c44298fc1c14"
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

`src/pocketpaw/constants/__init__.py` contains no code. Its presence tells Python that the `constants/` directory is a package, enabling imports like `from pocketpaw.constants.tool_categories import CATEGORY_TO_GROUPS`.

## Why an Empty `__init__.py`?

Several design goals motivate keeping the constants package init empty:

1. **Avoiding premature coupling**: If `__init__.py` re-exported every constant from every sub-module, any consumer importing from `pocketpaw.constants` would implicitly import all sub-modules. Adding a new sub-module would silently expand that import surface.

2. **Explicit imports**: Python's convention for constants packages is to have consumers import from the specific sub-module (`from pocketpaw.constants.tool_categories import ...`). This makes the dependency explicit in the importing file and keeps IDEs able to trace the origin of constants.

3. **Avoiding circular imports**: Constants are often imported early in the module graph. An `__init__.py` that imports from sub-modules that in turn import from the rest of pocketpaw would create circular-import risks.

## Package Structure

```
pocketpaw/constants/
    __init__.py          ← this file (empty)
    tool_categories.py   ← CATEGORY_TO_GROUPS, CATEGORY_DIRECT_TOOLS
```

Additional sub-modules may be added here as the codebase grows (e.g., default timeouts, API version strings, feature flags).

## SHA-256 Note

The hash `e3b0c44298fc1c14...` is the SHA-256 of an empty file — the well-known "null hash" in many hashing systems. This confirms the file has no content beyond what Python requires to recognise the directory as a package.

## Known Gaps

None. An empty `__init__.py` is correct and intentional for this pattern.
