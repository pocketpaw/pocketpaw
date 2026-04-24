---
{
  "title": "CLI Health Command: Fast Offline Startup Health Check",
  "summary": "The `health` command runs only the startup phase of PocketPaw's health engine — synchronous, network-free checks that verify config file presence, API key format, file permissions, and similar environmental prerequisites. It completes in milliseconds and is designed to be run frequently, for example in shell prompts or startup scripts, unlike the full `doctor` command which also runs slow network connectivity checks.",
  "concepts": [
    "health check",
    "startup checks",
    "offline diagnostics",
    "CLI health",
    "exit codes",
    "fix_hint",
    "health engine",
    "readiness probe",
    "JSON output"
  ],
  "categories": [
    "CLI",
    "Diagnostics and Health"
  ],
  "source_docs": [
    "0e9a68645587ef89"
  ],
  "backlinks": null,
  "word_count": 409,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/cli/health.py` implements the `pocketpaw health` subcommand. It is the fast, offline sibling of `pocketpaw doctor`. Where `doctor` runs the complete two-phase diagnostic suite (startup + connectivity), `health` runs only the first phase — startup checks — making it suitable for frequent polling and low-latency integrations.

## Why Separate `health` from `doctor`?

Connectivity checks require outbound HTTP requests to configured LLM providers, channel APIs, and other external services. In environments with slow networks or where external APIs are intentionally unreachable (air-gapped deployments, local development), these checks can take seconds per endpoint. Running them on every CLI invocation would make the tool feel sluggish.

Separating startup-only checks into `health` lets operators get an immediate answer to "is PocketPaw configured correctly?" without waiting for network probes. The `doctor` command remains available for deeper investigation.

## Result Rendering

Each check result is rendered with a three-state icon: `[OK]` (green), `[WARN]` (yellow), or `[FAIL]` (red). When a check is not `ok`, a `fix_hint` is shown on a second line if present, providing immediate remediation guidance:

```python
if r.fix_hint and r.status != "ok":
    print(f"         {DIM}-> {r.fix_hint}{RESET}")
```

The `fix_hint` field is only rendered when the check is not passing. Showing fix hints on successful checks would add noise.

## JSON Output

The `--json` flag switches to structured output:

```python
data = {
    "status": engine.overall_status,
    "checks": [{"id": ..., "name": ..., "status": ..., "message": ...} for r in results],
}
```

Note that `fix_hint` is included in `doctor`'s JSON output but not in `health`'s. This is a gap — scripts consuming `health --json` cannot surface fix hints without switching to `doctor`.

## Exit Code Semantics

```python
return {"healthy": 0, "degraded": 1, "unhealthy": 2}.get(status, 1)
```

The same three-level exit code as `doctor`: 0 for healthy, 1 for degraded, 2 for unhealthy. The `dict.get(..., 1)` default of `1` means unknown statuses are treated as degraded rather than healthy, erring on the side of caution.

This exit code mapping enables `pocketpaw health` to act as a readiness probe in container orchestration (Kubernetes `readinessProbe.exec.command`) or monitoring systems.

## Known Gaps

- **`fix_hint` missing from JSON output**: The JSON schema omits `fix_hint` from each check entry. This is inconsistent with the doctor command's JSON schema and limits the usefulness of `health --json` for automated remediation workflows.
- **No category grouping**: Startup checks from different categories (config, permissions, dependencies) are rendered in a flat list without section headers. As the number of checks grows, adding category grouping would improve readability.
