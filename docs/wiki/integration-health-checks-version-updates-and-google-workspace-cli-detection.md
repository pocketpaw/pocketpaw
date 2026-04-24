---
{
  "title": "Integration Health Checks — Version Updates and Google Workspace CLI Detection",
  "summary": "This module provides two synchronous health check functions: one that queries PyPI to detect whether a newer PocketPaw version is available, and one that verifies whether the `gws` Google Workspace CLI binary is installed in PATH. Both return structured `HealthCheckResult` objects consumed by the health engine.",
  "concepts": [
    "version update check",
    "PyPI",
    "gws binary",
    "Google Workspace CLI",
    "health checks",
    "importlib.metadata",
    "shutil.which",
    "HealthCheckResult",
    "update detection",
    "MCP preset",
    "defensive exception handling"
  ],
  "categories": [
    "health monitoring",
    "integrations"
  ],
  "source_docs": [
    "f42c4564a5b15365"
  ],
  "backlinks": null,
  "word_count": 493,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`integrations.py` groups health checks that fall outside the core config/connectivity/storage categories. Currently it covers two concerns: keeping PocketPaw up to date and verifying that optional external tooling is available.

## Version Update Check

`check_version_update()` uses `importlib.metadata` to read the currently installed PocketPaw version, then calls `check_for_updates()` from the internal `pocketpaw.update_check` module. This avoids a direct PyPI HTTP call inside the health check itself — the update check logic (caching, rate limiting, network call) is encapsulated in the dedicated module.

```python
current = get_version("pocketpaw")
config_dir = get_config_dir()
info = check_for_updates(current, config_dir)
```

The config directory is passed because the update checker caches its PyPI response there, preventing repeated network calls across health check runs.

The result is classified as `warning` (not `critical`) when an update is available. An available update is not a system failure — the agent still works. The `fix_hint` includes both the exact pip upgrade command and a link to the GitHub releases changelog for that version, giving operators everything they need in one place.

When `check_for_updates()` returns `None` (network unavailable, cache expired, etc.), the function reports `ok` with the message "update check unavailable". This is intentional: a flaky network should not turn the version check red on every startup.

If any exception is raised during the check, it is caught and also returned as `ok`. The rationale: a broken version check should never interfere with the agent starting up. The user sees a soft informational message rather than a alarming failure.

## Google Workspace CLI Check

`check_gws_binary()` uses `shutil.which("gws")` to test whether the `gws` binary is in PATH.

```python
if shutil.which("gws"):
    return HealthCheckResult(..., status="ok", message="gws binary found in PATH")
return HealthCheckResult(..., status="warning", message="gws not found — Google Workspace MCP preset won't work")
```

The `gws` binary is the Google Workspace CLI (`@googleworkspace/cli`). PocketPaw includes a built-in MCP preset that uses `gws` as a subprocess. Without it, that preset silently fails at runtime — users see no useful error, just an agent that cannot access Google Workspace. This health check surfaces that gap proactively at startup.

The fix hint points directly to the install command: `npm i -g @googleworkspace/cli`.

## Design Pattern: Defensive Exceptions

Both checks wrap their logic in broad `except Exception` handlers. This pattern is consistent across all PocketPaw health checks: a check that crashes is itself a failure mode, and crashing must not propagate up to the health engine. Instead, the exception is captured and returned as a non-blocking result. The health engine is designed to run all checks regardless of individual failures.

## Known Gaps

- `check_version_update()` relies on the `pocketpaw.update_check` module being correctly implemented with caching. If that module has bugs, the version check will silently swallow errors and return `ok`, which is correct behavior for health resilience but can mask broken update detection.
- There is no check for the `gws` binary version — an outdated `gws` could still be found in PATH but might be incompatible with the current MCP preset expectations.
