---
{
  "title": "CLI Doctor Command: Full Diagnostic Report with Connectivity Checks",
  "summary": "The `doctor` command runs PocketPaw's complete two-phase diagnostic suite — startup checks (environment, config, permissions) followed by async connectivity checks (API reachability, channel credentials) — and reports results in either rich human-readable format or structured JSON. It delegates to the health engine rather than reimplementing checks, keeping the CLI thin.",
  "concepts": [
    "diagnostics",
    "health engine",
    "startup checks",
    "connectivity checks",
    "async",
    "JSON output",
    "exit codes",
    "fix_hint",
    "pocketpaw doctor"
  ],
  "categories": [
    "CLI",
    "Diagnostics and Health"
  ],
  "source_docs": [
    "83b354fa83623a2b"
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

`src/pocketpaw/cli/doctor.py` implements the `pocketpaw doctor` subcommand. Unlike `pocketpaw health`, which runs only fast, offline startup checks, `doctor` runs both startup checks and network connectivity checks, making it a comprehensive pre-flight diagnostic tool.

## Two-Phase Diagnostic Design

The health engine exposes two distinct check phases:

1. **Startup checks** — synchronous, no network, fast (config file presence, API key format, directory permissions, Python version). These run first because there is no point testing network connectivity if basic configuration is broken.
2. **Connectivity checks** — async, network-dependent, slower (HTTP pings to configured LLM providers, channel API reachability). These only run after startup checks pass.

`doctor` runs both phases. This design exists because a user reporting "the agent isn't responding" may have a config issue, a network issue, or both. Running all checks in sequence gives a complete picture in one command.

## Human-Readable vs. JSON Output

```python
async def run_doctor_cmd(as_json: bool = False) -> int:
    if as_json:
        return await _doctor_json()
    from pocketpaw.diagnostics import run_doctor
    return await run_doctor()
```

The human-readable path delegates entirely to `pocketpaw.diagnostics.run_doctor`, which produces a rich, colored terminal report. The `--json` path uses `_doctor_json`, which calls the health engine directly and serializes results into a structured dict.

This split keeps the CLI layer thin: formatting logic lives in `diagnostics`, not here. The `doctor` module only needs to know how to route between the two output modes.

## JSON Output Schema

```python
data = {
    "status": engine.overall_status,  # "healthy" | "degraded" | "unhealthy"
    "checks": [
        {
            "id": r.check_id,
            "name": r.name,
            "category": r.category,
            "status": r.status,
            "message": r.message,
            "fix_hint": r.fix_hint,
        }
        for r in engine.results
    ],
}
```

Including `fix_hint` in the JSON output is deliberate — automated systems (CI scripts, monitoring dashboards) can surface actionable remediation steps without parsing human prose.

## Exit Code Semantics

The function maps `overall_status` to a conventional exit code:

- `0` — healthy
- `1` — degraded (some checks warn)
- `2` — unhealthy (at least one check failed)

This mirrors the `nagios`/`icinga` monitoring plugin convention, making `pocketpaw doctor` usable as a health probe in infrastructure monitoring setups.

## Known Gaps

- **No timeout on connectivity checks**: If a configured API endpoint (e.g., a self-hosted LiteLLM proxy) is unreachable, connectivity checks may hang for the default HTTP timeout (likely 5–10 seconds per check). For large configurations with many channels, this can make `doctor` feel slow.
- **Category field not surfaced in human mode**: The `category` field on each check result is included in JSON output but the human-readable `diagnostics.run_doctor` rendering may not group or label checks by category consistently. This is a rendering concern in the diagnostics module, not here, but worth noting for contributors building on the JSON output.
