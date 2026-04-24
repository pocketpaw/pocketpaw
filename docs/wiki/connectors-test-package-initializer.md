---
{
  "title": "Connectors Test Package Initializer",
  "summary": "This `__init__.py` marks `tests/connectors/` as a Python package and documents its scope as the test home for `pocketpaw.connectors.*` adapter implementations. Its single comment line serves as a breadcrumb for developers adding new connector tests.",
  "concepts": [
    "package marker",
    "connector adapters",
    "SourceAdapter",
    "test organization",
    "pytest discovery"
  ],
  "categories": [
    "testing",
    "project structure",
    "connectors",
    "test"
  ],
  "source_docs": [
    "b463e6da63566b30"
  ],
  "backlinks": null,
  "word_count": 445,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `tests/connectors/__init__.py` file contains a single comment: `# Tests for pocketpaw.connectors.* adapters.` This comment is the entire substantive content beyond the file's role as a Python package marker. Its presence is required for pytest to correctly discover and import tests across the `tests/connectors/` subtree.

## Why It Exists

Like all `__init__.py` files in the `tests/` hierarchy, this file enables Python's package import system to treat `tests/connectors/` as a proper subpackage of `tests/`. Without it, pytest may use a different import mode that causes fixture shadowing or module name collisions when multiple test directories contain files with the same name. For example, if both `tests/connectors/test_service.py` and `tests/cloud/uploads/test_service.py` existed, a flat-namespace importer would import both as `test_service` and the second import would silently shadow the first, causing tests to report unexpected results.

## Scope Documentation

The comment serves as lightweight convention documentation: this directory is the canonical home for tests of `pocketpaw.connectors.*` — the pluggable source adapter system that connects PocketPaw to external data sources like Google Drive, Slack, and relational databases. When a developer adds a new connector (e.g., `pocketpaw.connectors.notion`), the corresponding tests belong at `tests/connectors/test_notion.py`, not scattered elsewhere in the test tree.

## Relationship to the Connector Architecture

PocketPaw's connector system is built around the `SourceAdapter` protocol. Each adapter must satisfy the same behavioral contract:

- `supports_dataref` is `True` — adapters return `dataref`-kind payloads pointing to external documents, not inline text content.
- `query()` returns a list of `CandidateSource` objects ranked by relevance score.
- Auth errors surface as `DriveAuthError` or equivalent typed exceptions (not generic `Exception`) so the `RetrievalRouter` can record `sources_failed` entries rather than propagating exceptions to the caller.
- Point-in-time queries produce results that reference the correct historical file revision, not the current head.

The `tests/connectors/` package consolidates tests for each adapter so these behavioral contracts can be verified consistently as the connector catalog grows. The currently tested connector is `pocketpaw.connectors.drive` (Google Drive), covered by `test_drive.py`.

## Package Boundary and Fixture Scoping

Because `tests/connectors/` is a proper package, any `conftest.py` added here provides fixtures only to tests within this subtree. This is the correct isolation boundary: connector tests share HTTP scripting helpers (`ScriptedClient`, `FakeResponse`) and `SourceAdapter` contract helpers that should not leak into unrelated subsystems like workspace or upload tests. The package boundary enforces this separation structurally, without relying on naming conventions or developer discipline.

## Known Gaps

The comment mentions `pocketpaw.connectors.*` in the plural, but only one connector (`drive`) currently has tests. As additional connectors are implemented (Slack, Notion, database adapters), corresponding test files should be added here following the same multi-class structure established by `test_drive.py`: HTTP client tests, adapter query tests, token resolution tests, and router integration tests.