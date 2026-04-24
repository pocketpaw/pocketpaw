---
{
  "title": "Config File Health Checks: Existence, JSON Validity, and Permission Auditing",
  "summary": "The `config.py` health check module verifies three properties of PocketPaw's configuration file: that it exists, that it contains valid JSON, and that its filesystem permissions are restricted to 600. These checks run at startup to catch configuration problems before they produce cryptic runtime failures.",
  "concepts": [
    "check_config_exists",
    "check_config_valid_json",
    "check_config_permissions",
    "HealthCheckResult",
    "POSIX permissions",
    "JSON validation",
    "deferred import",
    "startup health checks",
    "config.json"
  ],
  "categories": [
    "health monitoring",
    "configuration",
    "diagnostics",
    "security"
  ],
  "source_docs": [
    "b000c9dfdecdbef7"
  ],
  "backlinks": null,
  "word_count": 517,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`config.py` (`src/pocketpaw/health/checks/config.py`) implements the configuration layer of PocketPaw's health check system. The three functions it exports — `check_config_exists`, `check_config_valid_json`, and `check_config_permissions` — are all included in `STARTUP_CHECKS` and run before any user-facing operation begins.

## check_config_exists

```python
def check_config_exists() -> HealthCheckResult:
    path = get_config_path()
    if path.exists():
        return HealthCheckResult(status="ok", message=f"Config file exists at {path}", ...)
    return HealthCheckResult(
        status="warning",
        message="No config file found — using defaults",
        fix_hint="Open the dashboard Settings to create a config file.",
    )
```

A missing config file is treated as a `warning` rather than `critical`. This is intentional: PocketPaw can run on defaults for basic operations. The warning prompts users to configure the application without blocking them from using it at all.

## check_config_valid_json

```python
def check_config_valid_json() -> HealthCheckResult:
    if not path.exists():
        return HealthCheckResult(status="ok", message="No config file (defaults used)", ...)
    try:
        json.loads(path.read_text(encoding="utf-8"))
        return HealthCheckResult(status="ok", ...)
    except (json.JSONDecodeError, Exception) as e:
        return HealthCheckResult(
            status="critical",
            message=f"Config file has invalid JSON: {e}",
            fix_hint="Fix the JSON syntax in ~/.pocketpaw/config.json or delete it to reset.",
        )
```

This check returns `critical` on JSON errors because a corrupt config file means PocketPaw cannot load its settings at all — it cannot fall back to defaults when the file exists but is unreadable. The broad `except Exception` beyond `json.JSONDecodeError` also catches file permission errors and encoding issues.

Note the edge case handling: if the config file doesn't exist, the check returns `ok` rather than `warning` — because the absence of a file is already reported by `check_config_exists`. Running two warnings for the same root cause (missing file) would be confusing.

## check_config_permissions

```python
def check_config_permissions() -> HealthCheckResult:
    if sys.platform == "win32":
        return HealthCheckResult(
            status="warning",
            message="Permission check skipped on Windows",
            fix_hint="Ensure your user profile is protected by a password.",
        )
    mode = path.stat().st_mode & 0o777
    if mode <= 0o600:
        return HealthCheckResult(status="ok", ...)
    return HealthCheckResult(
        status="warning",
        message=f"Config file permissions too open: {oct(mode)} (should be 600)",
        fix_hint="Run: chmod 600 ~/.pocketpaw/config.json",
    )
```

The permission check uses `mode <= 0o600` rather than `mode == 0o600`. This allows more restrictive permissions (e.g., 0o400 read-only) to pass without warning. On Linux servers, some deployment tools set config files to 0o400, and failing this check for those deployments would produce a false positive.

Windows is explicitly excluded because POSIX file permission modes don't apply. The `warning` status with a user-friendly message still draws attention to the security concern without providing a command that would fail on Windows.

## Defensive Import Pattern

All three functions use deferred imports (`from pocketpaw.config import get_config_path` inside the function body) rather than module-level imports. This prevents a failure in the config module from breaking the health check module's import — the checks can load and report errors even if the config system is partially broken.

## Known Gaps

- `check_config_permissions` is skipped entirely on Windows. There is no equivalent Windows ACL check, leaving Windows users without any signal that their config file may be world-readable.
- The `fix_hint` for JSON errors suggests deleting the file to reset, which would also erase any correct settings present in other JSON fields. A more targeted fix (e.g., running a JSON linter) would be more user-friendly.