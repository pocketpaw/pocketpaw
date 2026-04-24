---
{
  "title": "Realtime Test Package Marker",
  "summary": "An empty `__init__.py` that designates `tests/cloud/realtime/` as a Python package for stable pytest discovery and import resolution. No logic or fixtures live here; all test content is in the sibling test modules.",
  "concepts": [
    "package marker",
    "__init__.py",
    "pytest discovery",
    "realtime tests",
    "import resolution",
    "test package"
  ],
  "categories": [
    "testing",
    "project structure",
    "test"
  ],
  "source_docs": [
    "e3b0c44298fc1c14"
  ],
  "backlinks": null,
  "word_count": 151,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/cloud/realtime/__init__.py` is an empty package marker with no code content. It makes `tests/cloud/realtime/` a proper Python package, enabling pytest and the standard import system to resolve test modules by their dotted path.

## Why This File Exists

In projects using `importmode=prepend` or `importmode=append`, pytest requires `__init__.py` files in test directories to prevent name collisions between identically-named test files in different subdirectories. Without it, `test_bus.py` in `tests/cloud/realtime/` could shadow another `test_bus.py` elsewhere.

## What Lives in This Package

- `test_events.py` — Structural tests for the base `Event` model and typed subclass construction.
- `test_audience.py` — Behavioral tests for `AudienceResolver` across all event types.
- `test_audience_coverage.py` — Exhaustive coverage test ensuring every `Event` subclass resolves without raising.
- `test_bus.py` — Tests for `InProcessBus` fan-out, error isolation, and the module singleton.
- `test_emit.py` — Tests for the `emit()` facade and its interaction with the bus singleton.

## Known Gaps

None. Intentionally empty.