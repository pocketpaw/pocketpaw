---
{
  "title": "PocketPaw Package Root and Version Declaration",
  "summary": "The top-level `__init__.py` for the `pocketpaw` package declares the current version string and the package tagline. It is intentionally minimal — no imports, no symbols exported — following the principle that the package root should not trigger side effects at import time.",
  "concepts": [
    "__version__",
    "package initialization",
    "version string",
    "importlib.metadata",
    "package root",
    "minimal imports"
  ],
  "categories": [
    "package structure",
    "versioning"
  ],
  "source_docs": [
    "d6789f61af25900d"
  ],
  "backlinks": null,
  "word_count": 204,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `pocketpaw/__init__.py` file serves two purposes: it declares the package to Python's import system, and it provides the canonical `__version__` string.

## Version String

The current version is declared as a module-level constant:

```python
__version__ = "0.4.15"
```

This string is read by `importlib.metadata.version("pocketpaw")` at runtime (used in the CLI `--version` flag and the startup version check), but the constant itself exists as a single source of truth that tooling (release scripts, changelogs) can update programmatically.

## Package Tagline

The module docstring contains the human-facing tagline:

> PocketPaw - The AI agent that runs on your laptop, not a datacenter.

This framing — "laptop, not a datacenter" — is the product's positioning. PocketPaw runs locally with local LLMs (Ollama) or self-supplied API keys, as opposed to cloud SaaS agents that require vendor infrastructure.

## Why Keep It Minimal

Importing `pocketpaw` should be instant and side-effect-free. If the `__init__.py` imported heavy modules (FastAPI, Beanie, LLM clients), any script that did `import pocketpaw` would pay that startup cost immediately — including test runners, CLI tools, and other utilities that don't need the full runtime. The minimal approach means each consumer imports only what it needs.

## Known Gaps

None. This file is intentionally sparse.