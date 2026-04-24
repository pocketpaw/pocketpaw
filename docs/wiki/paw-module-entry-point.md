---
{
  "title": "Paw Module Entry Point",
  "summary": "The paw package's __init__.py exposes only PawConfig from its config submodule, deliberately keeping the import surface minimal to avoid pulling in soul-protocol as a hard dependency for users who install the base pocketpaw package without the soul extra.",
  "concepts": [
    "PawConfig",
    "optional dependency",
    "lazy import",
    "soul-protocol",
    "__init__.py",
    "package entry point",
    "module manifest"
  ],
  "categories": [
    "paw",
    "architecture",
    "dependency-management"
  ],
  "source_docs": [
    "f3fe92b8f02acbd4"
  ],
  "backlinks": null,
  "word_count": 311,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/paw/__init__.py` is the public entry point for the `pocketpaw.paw` package. It exposes exactly one symbol: `PawConfig`. This is a deliberate design decision rooted in dependency isolation.

## Why Only PawConfig?

The `paw` subpackage integrates soul-protocol into PocketPaw's agent loop. Soul-protocol is an optional dependency (`pip install pocketpaw[soul]`). If `__init__.py` eagerly imported from `agent.py`, `cli.py`, or `soul_bridge.py`, those modules would immediately import `soul_protocol` — causing an `ImportError` for any user who installed the base `pocketpaw` package without the `[soul]` extra.

By exporting only `PawConfig`, which has no soul-protocol imports:

```python
from pocketpaw.paw.config import PawConfig
__all__ = ["PawConfig"]
```

...the package becomes importable by anyone. Modules that need soul-protocol (`agent.py`, `soul_bridge.py`, `tools.py`) are imported lazily, only when the user actually calls a command like `paw init` or `paw ask`.

## Module Comment as Manifest

The file comment serves as a manifest for the entire `paw` package:

```python
# Paw module — lightweight CLI entry point for PocketPaw with soul-protocol integration.
# Created: 2026-03-02
# Provides: PawConfig, SoulBridge, SoulBootstrapProvider, soul tools, CLI commands.
```

This is a PocketPaw convention: each file's header comment documents what it provides, so developers can grep the workspace without reading every submodule.

## What the paw Package Provides

| Symbol | Module | Description |
|--------|--------|-------------|
| `PawConfig` | `config.py` | Project-level configuration loaded from `paw.yaml` |
| `SoulBridge` | `soul_bridge.py` | observe/recall facade over soul-protocol |
| `SoulBootstrapProvider` | `soul_bridge.py` | Wires soul state into PocketPaw's bootstrap pipeline |
| Soul tools | `tools.py` | BaseTool implementations for memory and state operations |
| CLI commands | `cli.py` | Click group: init, ask, chat, serve, status, doctor, os, channels |

## Known Gaps

- **`__all__` is minimal**: Only `PawConfig` is listed. Callers who want `SoulBridge` or the tool classes must import them directly from their submodules. This is intentional but can be confusing for new contributors.