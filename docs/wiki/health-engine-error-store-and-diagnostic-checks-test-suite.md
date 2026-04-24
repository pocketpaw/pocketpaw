---
{
  "title": "Health Engine, Error Store, and Diagnostic Checks Test Suite",
  "summary": "This comprehensive test module covers the full health monitoring stack: `HealthCheckResult` data model, `ErrorStore` JSONL persistence with rotation, individual diagnostic checks (config, API keys, secrets, disk, LLM reachability), the `HealthEngine` orchestrator, playbook-driven diagnostics, agent tools (`HealthCheckTool`, `ErrorLogTool`, `ConfigDoctorTool`), and the ContextHub `health_status` integration.",
  "concepts": [
    "HealthEngine",
    "ErrorStore",
    "HealthCheckResult",
    "JSONL rotation",
    "health checks",
    "API key validation",
    "secrets encryption",
    "Fernet",
    "ContextHub",
    "health_status",
    "playbooks",
    "diagnose_config",
    "singleton",
    "agent tools",
    "check registries"
  ],
  "categories": [
    "testing",
    "health monitoring",
    "diagnostics",
    "error handling",
    "agent tools",
    "test"
  ],
  "source_docs": [
    "82ca80de2d4a0735"
  ],
  "backlinks": null,
  "word_count": 614,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's health subsystem gives users and the agent itself visibility into runtime issues — missing API keys, corrupt config, insecure file permissions, stale software versions, and more. The `test_health.py` module is the single source of truth for all behavioral contracts in this subsystem.

## `HealthCheckResult` and `ErrorStore`

`HealthCheckResult` is a dataclass capturing a single check's outcome. Tests verify that `timestamp` is auto-populated in ISO format when not provided, that `to_dict()` serializes all fields, and that a custom timestamp is preserved verbatim. These tests prevent a common mistake: forgetting to set a timestamp, which would make error logs unsortable.

`ErrorStore` persists errors to a JSONL file (one JSON object per line). Key behavioral contracts tested:

- **Idempotent creation**: writing to a non-existent path creates the file and parent directories silently.
- **Write error resilience**: if the path is non-writable (e.g., a deep missing parent), `record()` must not raise — it logs a warning and returns a string ID anyway. This prevents a health-monitoring failure from cascading into an application crash.
- **Corrupt line handling**: `get_recent()` skips malformed JSONL lines rather than raising. A single corrupt entry must not make the entire error log unreadable.
- **Log rotation**: when the file exceeds `max_size_mb`, it is renamed to `.1` (shifting any existing `.1` to `.2`). Tests verify both the rotation trigger and the shift behavior.
- **Search**: case-insensitive substring search over the message field, returning newest-first.

## Individual Health Checks

Each check function returns a `HealthCheckResult` with a `status` of `"ok"`, `"warning"`, or `"critical"`. Tests patch at the source module (`pocketpaw.config.*`) rather than at the check module import site, because check functions do local imports.

Notable checks covered:

- **`check_config_permissions`**: files with mode `0o644` are flagged as "too open" on Unix; the test is skipped on Windows where Unix permissions don't apply.
- **`check_secrets_encrypted`**: validates a real Fernet-encrypted `secrets.enc` file, and flags plaintext JSON as insecure. The docstring notes this was the original bug that motivated the check.
- **`check_api_key_primary`**: covers six backends (claude_sdk, google_adk, openai_agents, subprocess, legacy native, legacy open_interpreter). Legacy backends emit a warning that they have been removed rather than failing silently.
- **`check_llm_reachable`**: unknown or unimplemented backends must return `warning` not `ok` — closing issue #746 where unknown providers were optimistically reported as healthy.
- **`check_version_update`**: returns `warning` when a newer version is available, `ok` when current, and `ok` (not error) when the version check itself fails (network unavailable).

## `HealthEngine` Orchestrator

The engine runs startup, connectivity, and integration check registries. Tests verify:

- **Exception isolation**: if a check raises an unhandled exception, `test_startup_check_exception_handled` confirms the engine catches it and records the error without aborting remaining checks.
- **Overall status aggregation**: `unhealthy` if any check is critical; `degraded` if any is warning; `healthy` if all are ok.
- **Prompt section generation**: `health_prompt_section` produces text injected into the system prompt, giving the agent awareness of its own health state.
- **Error recording**: `record_error` and `get_recent_errors` must never raise regardless of input.

## Singleton, Tools, and ContextHub

`get_health_engine()` is tested to return the same instance on repeated calls — a singleton pattern that prevents multiple engines running parallel check cycles.

The three agent tools (`HealthCheckTool`, `ErrorLogTool`, `ConfigDoctorTool`) are tested for correct `tool_definition` schema and async `execute` behavior.

The `ContextHub` integration tests verify that `health_status` is a registered source, that `gather_health_status()` returns a formatted string, and that import failures are handled gracefully (the source returns an empty string rather than crashing the context build).

## Known Gaps

The GWS binary check (`check_gws_binary`) is tested to belong to `INTEGRATION_CHECKS` and NOT to `STARTUP_CHECKS` — the registry placement test (`test_gws_not_in_startup_checks`) documents the intentional categorization but does not verify the actual binary detection behavior beyond `shutil.which` mocking.