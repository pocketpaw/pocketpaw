---
{
  "title": "Launcher Updater: PyPI Version Checking and In-Place Upgrade Flow",
  "summary": "The updater test suite covers PocketPaw's self-update mechanism, validating version comparison logic, PyPI metadata fetching, installed version detection from the venv, and the full apply-upgrade path. It ensures the updater degrades gracefully when the network is unavailable or the package is not yet installed.",
  "concepts": [
    "self-update",
    "PyPI version checking",
    "venv pip upgrade",
    "version comparison",
    "semver",
    "cross-platform venv",
    "graceful degradation",
    "subprocess",
    "Updater class"
  ],
  "categories": [
    "installer",
    "lifecycle management",
    "test"
  ],
  "source_docs": [
    "c6027de682a9f8e1"
  ],
  "backlinks": null,
  "word_count": 543,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw ships with a background updater that periodically checks PyPI for newer releases and applies upgrades by running `pip install --upgrade` inside the managed venv. The `Updater` class encapsulates this three-step flow: detect the installed version, fetch the latest version from PyPI, compare them, and optionally apply the upgrade. The test suite covers each step in isolation and end-to-end.

## Version Comparison

`Updater._version_newer(candidate, current)` implements semver-style comparison using integer segment parsing. Tests cover all directional cases — newer patch, minor, and major — plus the important edge case of comparing a three-segment version against a two-segment one (`"0.3.0"` vs `"0.2"`). Handling mismatched segment counts prevents an `IndexError` that would silently suppress update detection.

## PyPI Version Fetching

`_get_pypi_version()` makes an HTTP request to the PyPI JSON API and parses the `info.version` field. The tests cover three scenarios:

1. **Successful fetch** — the method returns the version string from the parsed JSON.
2. **Network error** — `ConnectionError` causes the method to return `None`, allowing the caller to treat the update check as inconclusive rather than fatal.
3. **Invalid JSON** — a malformed response body returns `None` for the same reason.

Returning `None` on failure rather than raising is deliberate: a failed update check must not interrupt the user's session. The GUI can suppress or log the failure silently.

## Installed Version Detection

`_get_installed_version()` runs `pip show pocketpaw` inside the managed venv and parses the `Version:` line from stdout. The test helper `_make_venv_python()` creates the platform-appropriate binary path (`bin/python` on Unix, `Scripts/python.exe` on Windows), ensuring the detection logic finds the interpreter before invoking it. When the venv does not exist, the method returns `None` — correctly indicating "not installed" rather than crashing.

## Full Check Flow

`Updater.check()` composes the above methods:

- **Update available**: installed < latest → returns an update-available result.
- **Up to date**: installed == latest → returns a no-update result.
- **Not installed**: `_get_installed_version()` returns `None` → raises or returns an error state, ensuring the caller does not attempt an upgrade that cannot be applied.
- **PyPI unreachable**: `_get_pypi_version()` returns `None` → check is inconclusive; no exception.

## Apply Update

`Updater.apply()` calls `pip install --upgrade pocketpaw` inside the venv Python. Tests cover:

- **Success**: subprocess returns exit code 0.
- **Upgrade failure**: non-zero exit code is surfaced as a failure result without raising.
- **No venv**: when the venv directory does not exist, `apply()` returns a failure result rather than crashing with a `FileNotFoundError`.

The no-venv guard is important because a user might trigger an update check before the initial installation completes, or after a failed install left the venv directory absent.

## Cross-Platform Fixture

`_make_venv_python()` is a module-level helper (not a test class method) that creates the venv binary in the location `Updater` expects. It branches on `platform.system()` to handle Windows's `Scripts/` layout vs Unix's `bin/`. This is essential because CI runs on multiple platforms and the path resolution must match the production code.

## Known Gaps

- There are no tests for retry logic on transient network errors — a single `ConnectionError` returns `None` immediately with no retry. If PyPI is briefly flaky, the update is silently skipped.
- The test for `test_upgrade_failure` does not verify the specific error message returned, leaving the user-facing failure message under-specified.