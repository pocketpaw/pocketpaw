---
{
  "title": "Notifications Test Package Marker",
  "summary": "An empty `__init__.py` that marks `tests/cloud/notifications/` as a Python package for stable pytest discovery. No logic or fixtures live here; all test content is in the sibling test modules test_service.py and test_derived.py.",
  "concepts": [
    "package marker",
    "__init__.py",
    "pytest discovery",
    "test package",
    "import resolution",
    "notifications tests"
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
  "word_count": 138,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/cloud/notifications/__init__.py` is an empty package marker. Its purpose is to make `tests/cloud/notifications/` a proper Python package, enabling pytest and the standard import system to resolve test modules by their dotted path (e.g., `tests.cloud.notifications.test_service`).

## Why This File Exists

In projects using `importmode=prepend` or `importmode=append`, pytest requires `__init__.py` files in test directories to avoid name conflicts between identically-named test files in sibling directories. Without it, `test_service.py` in `tests/cloud/notifications/` could shadow another `test_service.py` elsewhere in the tree.

## What Lives in This Package

- `test_service.py` — Unit tests for `NotificationService`: create, list, mark-read, and clear-all CRUD plus real-time fan-out.
- `test_derived.py` — Derivation tests verifying that mention, reaction, and invite events correctly trigger `NotificationService.create`.

## Relationship to Production Layout

This package mirrors `ee/cloud/notifications/` in the production source tree, making test-to-source navigation straightforward.

## Known Gaps

None. Intentionally empty.