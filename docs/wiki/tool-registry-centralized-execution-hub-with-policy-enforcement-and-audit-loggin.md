---
{
  "title": "Tool Registry: Centralized Execution Hub with Policy Enforcement and Audit Logging",
  "summary": "The `ToolRegistry` is PocketPaw's central runtime for tool management -- it stores registered tools, enforces `ToolPolicy` access control, validates parameters, executes tools under a configurable timeout, scrubs sensitive data from logs, and writes to the audit trail. Multiple iterative hardening passes reflect its role as the primary security enforcement point at execution time.",
  "concepts": [
    "ToolRegistry",
    "tool execution",
    "ToolPolicy",
    "audit logging",
    "parameter validation",
    "timeout",
    "injection scanner",
    "scrub_params",
    "trust_level",
    "AuditSeverity",
    "issue #793",
    "issue #890"
  ],
  "categories": [
    "tools",
    "security",
    "agent runtime",
    "audit"
  ],
  "source_docs": [
    "1421ce961eaf75f9"
  ],
  "backlinks": null,
  "word_count": 431,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Every tool call in PocketPaw passes through `ToolRegistry.execute()`. This single choke point makes it the right place to enforce access control, validate inputs, track security-sensitive operations, and handle failures uniformly. The registry's changelog tells a story of progressive hardening: created in February 2026 with three targeted security improvements since.

## Registration and Lookup

Tools are stored by name in a plain dict (`self._tools`). `register()` and `unregister()` are straightforward; `get()` and `has()` support read access. `get_definitions()` filters the registered tools through the active `ToolPolicy` before serializing to OpenAI or Anthropic format, so the LLM only ever sees tools it is permitted to call.

## The Execution Pipeline

`execute()` implements a defense-in-depth pipeline:

1. **Tool lookup** -- returns a descriptive error listing available tools if the name is unknown.
2. **Policy check (pre-execution)** -- blocks the call even if `get_definitions` already filtered it. This is a belt-and-braces check against paths that bypass definition filtering.
3. **Severity mapping** -- maps `trust_level` (`standard` -> INFO, `high` -> WARNING, `critical` -> CRITICAL) to audit severity.
4. **Parameter validation** -- checks that all `required` parameters are present, non-None, and non-empty strings. Empty/whitespace strings were added as a rejection condition in March 2026 (issue #793).
5. **Audit log (attempt)** -- recorded before execution so crashes and timeouts still leave an audit trail.
6. **Execution with timeout** -- wraps the async tool call in `asyncio.wait_for` with a per-tool configurable timeout (default `DEFAULT_TOOL_TIMEOUT = 60` seconds).
7. **Injection scan** -- if `injection_scan_enabled` is set, tool results are passed through the injection scanner before being returned.
8. **Audit log (result)** -- success, timeout, or error is recorded with the appropriate status.

## Log Scrubbing

In April 2026, a fix for issue #890 added `scrub_params(params)` to the debug log line before execution. Without this, enabling DEBUG logging would print raw tool parameters -- including credentials passed to tools like `install_package` -- to stdout.

```python
logger.debug("Executing %s with %s", name, scrub_params(params))
```

## Policy at Two Layers

The registry checks policy both when generating definitions (`get_definitions`) and at execution time (`execute`). This matters because tool names can arrive at the registry through channels that bypass definition generation -- for example, a replay attack or future MCP bridge.

## Known Gaps

The injection scanner is wrapped in a broad `except Exception: pass` block to prevent scanner failures from breaking tool execution. This means a bug in the injection scanner is silently swallowed. Additionally, the audit log records success before the injection scan runs -- if the scanner replaces the result, the audit log still shows the original "success" status rather than "sanitized."