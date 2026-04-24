---
{
  "title": "PocketPaw CLI Diagnostics — Doctor, Ollama, and OpenAI-Compatible Health Checks",
  "summary": "diagnostics.py contains the three CLI health check implementations extracted from `__main__.py`: `run_doctor` for a full system health report, `check_ollama` for Ollama connectivity and model tool-calling support, and `check_openai_compatible` for OpenAI-compatible endpoint validation. All three produce ANSI-colored terminal output with structured pass/warn/fail indicators.",
  "concepts": [
    "run_doctor",
    "check_ollama",
    "check_openai_compatible",
    "health checks",
    "CLI diagnostics",
    "ANSI colors",
    "health engine",
    "Ollama",
    "OpenAI-compatible",
    "tool calling support",
    "exit codes"
  ],
  "categories": [
    "diagnostics",
    "cli",
    "health-checks",
    "observability"
  ],
  "source_docs": [
    "2782bcccb6c2d68e"
  ],
  "backlinks": null,
  "word_count": 555,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

When PocketPaw misbehaves, users need a fast way to diagnose the problem without reading logs. `diagnostics.py` provides three CLI commands that walk through common failure scenarios — missing API keys, unreachable models, misconfigured Ollama endpoints — and report status clearly.

This file was extracted from `__main__.py` to keep the entrypoint thin and make the diagnostic logic independently testable.

## `run_doctor` — Full Health Report

`run_doctor()` runs two categories of checks:

1. **Startup checks** (synchronous) — configuration validation, environment variable presence, file system checks
2. **Connectivity checks** (async) — LLM endpoint reachability, model availability

It uses `get_health_engine()` to access the shared health engine singleton, which accumulates check results across the application lifecycle. Results are grouped by category (e.g., `"LLM"`, `"Configuration"`, `"Storage"`) and printed with ANSI color codes: green for pass, yellow for warning, red for failure.

The function returns an integer exit code: `0` (healthy), `1` (degraded — warnings but operational), `2` (unhealthy — failures present). This makes `--doctor` scriptable in CI or deployment pipelines.

```python
async def run_doctor() -> int:
    engine = get_health_engine()
    engine.run_startup_checks()
    await engine.run_connectivity_checks()
    # ... group and print results
```

The ANSI colors are defined inline rather than imported from a color library, keeping the module dependency-free beyond the standard library and PocketPaw's own config.

## `check_ollama` — Ollama Connectivity Check

`check_ollama(settings)` validates:

1. **Endpoint reachability** — can the server reach the configured Ollama URL?
2. **Model availability** — is the configured model present in Ollama's model list?
3. **Tool calling support** — does the model support function calling (required for PocketPaw's tool use)?

The tool calling check is non-trivial: not all Ollama models support tools. A user who installs `llama3.2:1b` expecting it to work as an agent will get silent failures if tool calling isn't supported. The check proactively warns about this.

If Ollama is unreachable, the check returns early with a clear error rather than attempting model checks (which would produce confusing timeout errors).

## `check_openai_compatible` — OpenAI-Compatible Endpoint Check

`check_openai_compatible(settings)` validates any endpoint that speaks the OpenAI API format — local servers (LM Studio, vLLM, Ollama in OpenAI mode), cloud providers (Together, Groq), or custom deployments.

It checks:
1. **URL configuration** — is an endpoint URL configured?
2. **API key presence** — is a key set (even `"none"` for keyless local servers)?
3. **Connectivity** — can the endpoint be reached?
4. **Model listing** — does the `/v1/models` endpoint respond?

The connectivity check uses a minimal request (listing models) rather than a full inference call, so the check is fast and doesn't incur inference costs.

## Extraction Rationale

These functions lived in `__main__.py` through the initial implementation. Extracting them to `diagnostics.py` (referenced in the module docstring as the extraction motivation) keeps the entrypoint clean and makes it possible to call `run_doctor()` from other surfaces — the MCP server, a dashboard health endpoint, or integration tests.

## Known Gaps

- `check_ollama` and `check_openai_compatible` do not share a common interface or base class. Adding a new backend check (e.g., Anthropic API) requires writing another standalone function rather than implementing a protocol.
- Exit code semantics (`0/1/2`) are not documented in the function signatures — callers must read the implementation to understand what each code means.
- ANSI color codes write to `sys.stderr`, not `sys.stdout`. This is intentional (diagnostic output shouldn't pollute stdout pipelines) but not obvious from reading the code.
