---
{
  "title": "Soul Protocol Package Root: Integration Anchor for PocketPaw",
  "summary": "The `soul/__init__.py` file is the namespace anchor for PocketPaw's Soul Protocol integration sub-package. It signals the package boundary and establishes the import root from which `cognitive.py` and `manager.py` are accessible.",
  "concepts": [
    "Soul Protocol",
    "Python package",
    "namespace",
    "optional dependency",
    "SoulManager",
    "PocketPawCognitiveEngine",
    "import isolation",
    "sub-package"
  ],
  "categories": [
    "soul-protocol",
    "package-structure"
  ],
  "source_docs": [
    "51705ea0847ea7ac"
  ],
  "backlinks": null,
  "word_count": 321,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `src/pocketpaw/soul/__init__.py` file is intentionally minimal — its primary role is to declare that `pocketpaw.soul` is a Python package. Despite its near-empty state, this file carries significant architectural meaning: it separates Soul Protocol concerns from the rest of PocketPaw's core runtime, making the soul subsystem an opt-in dependency zone.

## Why a Separate Sub-Package?

The Soul Protocol integration introduces a runtime dependency on the external `soul-protocol` SDK. By isolating all soul-related code inside `pocketpaw/soul/`, the team achieves two benefits:

1. **Import isolation** — Code in other parts of PocketPaw (the agent loop, tools, bus) does not need to know about soul functionality. They import from `pocketpaw.soul` only when explicitly working with memory or personality features.
2. **Optional activation** — If the `soul-protocol` package is not installed, only the `pocketpaw.soul` sub-package fails to import. The rest of PocketPaw continues functioning. This is important for deployment environments that do not need persistent memory.

## Package Relationship

The `pocketpaw.soul` package contains two substantive modules:

- `pocketpaw.soul.cognitive` — The `PocketPawCognitiveEngine`, which bridges soul-protocol's cognitive tasks to PocketPaw's agent backends.
- `pocketpaw.soul.manager` — The `SoulManager`, which handles the full lifecycle of a `.soul` file including loading, observing interactions, auto-saving, and shutdown.

Both modules are imported lazily (by callers that explicitly need them) rather than re-exported from `__init__.py`. This keeps the package init fast and avoids premature dependency errors if `soul-protocol` is not installed.

## Docstring as Contract

The module docstring — "Soul Protocol integration for PocketPaw." — doubles as documentation for anyone browsing the package tree. It immediately communicates that this sub-package is the integration layer between PocketPaw and the Soul Protocol SDK, not a standalone implementation of soul logic.

## Known Gaps

The `__init__.py` currently exports nothing. As the soul integration matures, it may make sense to re-export the most commonly used symbols (`SoulManager`, `PocketPawCognitiveEngine`) here for ergonomic imports like `from pocketpaw.soul import SoulManager`. This is a future convenience, not a current gap.