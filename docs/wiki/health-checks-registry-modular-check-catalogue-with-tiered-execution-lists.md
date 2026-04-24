---
{
  "title": "Health Checks Registry: Modular Check Catalogue with Tiered Execution Lists",
  "summary": "The health checks `__init__.py` is both a re-export facade and a check registry, defining three typed lists — `STARTUP_CHECKS`, `INTEGRATION_CHECKS`, and `CONNECTIVITY_CHECKS` — that the `HealthEngine` iterates to run diagnostics at the right time and in the right order. All individual check functions are re-exported so that existing imports remain stable across refactors.",
  "concepts": [
    "STARTUP_CHECKS",
    "INTEGRATION_CHECKS",
    "CONNECTIVITY_CHECKS",
    "HealthCheckResult",
    "check registry",
    "tiered execution",
    "re-export facade",
    "health check architecture",
    "diagnostic pipeline"
  ],
  "categories": [
    "health monitoring",
    "diagnostics",
    "infrastructure",
    "startup"
  ],
  "source_docs": [
    "ec50679747e7054e"
  ],
  "backlinks": null,
  "word_count": 400,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `pocketpaw.health.checks` package (`src/pocketpaw/health/checks/__init__.py`) organizes PocketPaw's health check functions into three execution tiers and re-exports them under a stable public namespace. The tiered structure ensures that fast synchronous checks run at startup, slow async checks run in the background, and integration-specific checks only run when relevant.

## Three-Tier Check Architecture

```python
STARTUP_CHECKS = [
    check_config_exists, check_config_valid_json, check_config_permissions,
    check_api_key_primary, check_api_key_format, check_backend_deps,
    check_secrets_encrypted, check_disk_space, check_audit_log_writable,
    check_memory_dir_accessible, check_version_update,
]

INTEGRATION_CHECKS = [
    check_gws_binary,
]

CONNECTIVITY_CHECKS = [
    check_llm_reachable,
]
```

**STARTUP_CHECKS** are synchronous, fast, and run at process startup. They verify that the local environment is correctly configured before any user request is served. If a startup check fails critically (e.g., `check_backend_deps` returns `critical`), the engine can block startup or show a prominent warning in the dashboard.

**INTEGRATION_CHECKS** are optional — they only matter when specific presets are enabled (e.g., Google Workspace integration). Running `check_gws_binary` on a deployment that doesn't use GWS would produce a misleading failure, so these checks are run selectively.

**CONNECTIVITY_CHECKS** are async and potentially slow (network calls). `check_llm_reachable` makes an actual HTTP request to the configured LLM endpoint, which could take seconds. Running this in the background avoids blocking startup or increasing request latency.

## Re-Export Stability

The `__all__` list in this module is comprehensive — it includes every public symbol from all check sub-modules. This allows code that previously imported directly from a sub-module to continue working if the symbol is moved:

```python
# Before refactor: specific import
from pocketpaw.health.checks.result import HealthCheckResult

# After refactor: stable import
from pocketpaw.health.checks import HealthCheckResult
```

The module docstring explicitly documents this stability guarantee, signaling to future developers that the `__all__` list must be kept complete.

## HealthCheckResult

`HealthCheckResult` (from `pocketpaw.health.checks.result`) is the common return type for all check functions. Every check returns an instance indicating `status` (`"ok"`, `"warning"`, `"critical"`), a human-readable `message`, a `fix_hint`, and a `check_id` for deduplication.

## Known Gaps

- The ordering of `STARTUP_CHECKS` matters if the engine runs them sequentially and stops on the first critical failure. The current order puts config checks before API key checks, which is correct (you need a config file before API keys). But this ordering is implicit — no comments explain why `check_config_exists` must come before `check_api_key_primary`.
- `INTEGRATION_CHECKS` has only one entry (`check_gws_binary`). The mechanism for selectively running integration checks based on active presets is not visible in this file — it presumably lives in the `HealthEngine` itself.