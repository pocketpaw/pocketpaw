---
{
  "title": "Launcher Shared Constants and Helpers: Paths, Package Metadata, and Python Discovery",
  "summary": "The `common.py` module is the single source of truth for paths, package names, and utility functions shared across all launcher components. Centralizing these definitions prevents the bootstrap, server manager, updater, and uninstaller from each hard-coding `~/.pocketpaw` and drifting out of sync.",
  "concepts": [
    "POCKETPAW_HOME",
    "VENV_DIR",
    "UV_DIR",
    "DEV_MODE_MARKER",
    "StatusCallback",
    "noop_status",
    "venv_python",
    "find_uv",
    "get_installed_version",
    "path constants",
    "shared helpers",
    "launcher configuration"
  ],
  "categories": [
    "installer",
    "launcher",
    "shared-utilities",
    "configuration"
  ],
  "source_docs": [
    "35a00ddb9b83b807"
  ],
  "backlinks": null,
  "word_count": 498,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`installer/launcher/common.py` is a deliberately minimal shared module. Its job is to prevent four separate launcher files (`bootstrap.py`, `server.py`, `updater.py`, `uninstall.py`) from each independently defining where PocketPaw lives on disk and what the package is called. Any inconsistency between those definitions would cause silent bugs: the bootstrap might create a venv in one location while the server manager looks in another.

## Path Constants

```python
POCKETPAW_HOME = Path.home() / ".pocketpaw"
VENV_DIR       = POCKETPAW_HOME / "venv"
UV_DIR         = POCKETPAW_HOME / "uv"
```

All three derive from `Path.home()` rather than an environment variable, which makes them consistent across the entire launcher session. The home directory is resolved once at import time — there is no risk of the path changing mid-run due to environment mutation.

## Package Metadata

`PACKAGE_NAME = "pocketpaw"` and `GIT_REPO_URL` are used by the bootstrap and updater when invoking pip/uv. Having them here means version-pinning or renaming the package only requires one edit.

`DEV_MODE_MARKER = POCKETPAW_HOME / ".dev-mode"` is a sentinel file that switches the launcher from PyPI-based installs to git-branch-based installs. The bootstrap and updater both check for its existence. Using a file rather than an environment variable means dev mode survives launcher restarts.

## Callback Type Alias

```python
StatusCallback = Callable[[str], None]
```

This type alias appears in every launcher component that reports progress. It is defined here so all components reference the same signature. `noop_status()` is the null implementation — used when no UI is attached (headless installs, unit tests).

## venv_python() Helper

`venv_python()` returns the correct path to the Python executable inside the venv, accounting for the Windows vs. Unix layout difference:

- Windows: `venv/Scripts/python.exe`
- macOS/Linux: `venv/bin/python`

This function is called by `ServerManager` and `Updater` before every subprocess invocation. Inlining this logic in each caller would risk Windows/Unix divergence as the codebase evolves.

## find_uv() and get_installed_version()

`find_uv()` searches `UV_DIR` first (the launcher's local uv install) and then falls back to the system PATH. This priority order matters: the system `uv` may be a different version than the one the bootstrap downloaded, and version mismatch can cause subtle install behavior differences.

`get_installed_version()` runs `pip show pocketpaw` inside the venv and parses the `Version:` line. It returns `None` if pocketpaw is not installed, allowing callers to distinguish "not installed" from "installed at version X".

## Design Rationale

This module has no classes and no side effects at import time — all it does is define constants and pure functions. This makes it safe to import from bootstrap before the venv exists, from tests without a running server, and from the uninstaller after the server has been stopped. The absence of any business logic in this file is intentional: it is infrastructure, not behavior.

## Known Gaps

- `POCKETPAW_HOME` is not configurable via environment variable, which prevents running multiple PocketPaw instances side-by-side (e.g., staging vs. production) on the same machine.
- `get_installed_version()` relies on `pip show` output format, which is not a stable API — minor pip versions have historically changed this output.