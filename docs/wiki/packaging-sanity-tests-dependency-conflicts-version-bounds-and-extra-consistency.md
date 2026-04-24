---
{
  "title": "Packaging Sanity Tests: Dependency Conflicts, Version Bounds, and Extra Consistency",
  "summary": "Tests that parse `pyproject.toml` directly to catch dependency version conflicts and packaging invariants before they cause runtime failures. Specifically targets the FastAPI/Starlette version conflict introduced by MCP's transitive Starlette dependency, and verifies that dashboard-mode dependencies are included in the core package.",
  "concepts": [
    "pyproject.toml",
    "dependency conflicts",
    "FastAPI",
    "Starlette",
    "MCP",
    "Uvicorn",
    "version bounds",
    "packaging sanity",
    "optional extras",
    "backward compatibility",
    "tomllib",
    "CI"
  ],
  "categories": [
    "packaging",
    "testing",
    "dependency management",
    "CI",
    "test"
  ],
  "source_docs": [
    "4609e235"
  ],
  "backlinks": null,
  "word_count": 494,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Dependency version conflicts in Python are notoriously hard to debug at runtime — packages install cleanly but fail mysteriously at import time. `test_packaging.py` addresses this by testing `pyproject.toml` directly during CI, catching conflicts before users encounter them. The file header explicitly names the bugs it was written to prevent.

## The FastAPI/Starlette Conflict (Bug 1)

The original trigger: `claude-agent-sdk` depends on `mcp 1.26`, which depends on `starlette>=0.52`. But `fastapi>=0.109.0` (the old lower bound) required `starlette<0.36`. This created an irreconcilable conflict for any user installing PocketPaw with MCP support.

- `test_fastapi_version_allows_modern_starlette`: Reads all dependency specs (core + all extras) and finds any `fastapi>=X` constraint. Asserts the minimum version is `>= 0.115.0`, which lifted the Starlette upper bound to allow 0.52+. The test searches both core deps and optional dependencies because FastAPI may live in the `[dashboard]` extra rather than core.

This test documents the minimum acceptable FastAPI version permanently in the test suite. If someone accidentally downgrades the lower bound in `pyproject.toml`, CI fails immediately rather than at user install time.

## Dashboard Dependencies in Core (Bug 2)

PocketPaw's "default mode" starts the dashboard. If dashboard dependencies (FastAPI, Uvicorn, etc.) are only in the `[dashboard]` optional extra rather than core, a plain `pip install pocketpaw` installation fails when the user tries to start the dashboard.

- `test_default_mode_deps_in_core`: Asserts that all packages required for the default startup mode are present in `project.dependencies` (core), not just in optional extras.

## Duplicate Detection

- `test_no_duplicate_core_deps`: Duplicate entries in the dependency list are silently accepted by pip but indicate a copy-paste error. A duplicate could be a stale entry with a different version pin, causing confusion about which constraint applies.

## Version Consistency

- `test_version_consistency`: The version string shown by `pocketpaw --version` must match `pyproject.toml`. This is typically enforced by `importlib.metadata` dynamic versioning, but if someone hard-codes a version in `__main__.py`, it can drift. The test catches this by comparing the two sources.

## Dashboard Extra Backward Compatibility

- `test_dashboard_extra_exists`: The `[dashboard]` optional dependency group must exist even if it is empty. Users who pin `pocketpaw[dashboard]` in their requirements files would get an install error if the extra is removed. The empty extra preserves backward compatibility while allowing core to include the deps directly.

## Uvicorn Version Bound

- `test_uvicorn_version_not_too_old`: Uvicorn's lower bound must be `>= 0.31.1` to satisfy MCP's Uvicorn requirement. An older lower bound would allow pip to install an incompatible Uvicorn version on fresh installs.

## Testing Strategy

The file uses `tomllib` (stdlib in Python 3.11+) to parse `pyproject.toml` without any external dependencies. `PYPROJECT` is a module-level constant resolving to the project root's `pyproject.toml`. This design means the test always runs against the actual package metadata, not a fixture copy that could drift.

## Known Gaps

No TODOs in the file. The tests check lower bounds but not upper bounds — a future library could release a breaking major version that PocketPaw does not yet pin against, passing these tests while breaking at runtime.
