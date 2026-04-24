---
{
  "title": "External Agent Delegation — Subprocess Execution of CLI Agents",
  "summary": "Provides `ExternalAgentDelegate`, a utility for dispatching tasks to external CLI agents (currently only Claude Code CLI) via `asyncio` subprocesses. It captures structured JSON output and surfaces it as a `DelegationResult`, enabling PocketPaw to use external agents as heavyweight tools.",
  "concepts": [
    "ExternalAgentDelegate",
    "DelegationResult",
    "subprocess",
    "Claude Code CLI",
    "shutil.which",
    "timeout handling",
    "output parsing",
    "JSON output format",
    "Phase 2 integration"
  ],
  "categories": [
    "agent-runtime",
    "delegation",
    "subprocess",
    "security"
  ],
  "source_docs": [
    "af646f4069cfc771"
  ],
  "backlinks": null,
  "word_count": 470,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ExternalAgentDelegate` solves the problem of needing a more capable or specialised agent for a subtask without switching the entire PocketPaw backend. It runs an external CLI agent in a subprocess, waits for completion, and returns the output as a structured `DelegationResult`.

## Motivation

Not every task is well-served by the active backend. A PocketPaw instance running a lightweight model might delegate a complex coding task to Claude Code CLI, which has its own tool set and reasoning capabilities. This delegation pattern is part of Phase 2's integration ecosystem goal: PocketPaw as a hub that can orchestrate other agents.

## Security Posture

The docstring explicitly flags this as a "critical-trust operation." Launching a subprocess with `claude --print` grants the child process the same filesystem and network access as the PocketPaw process. The caller is responsible for validating the prompt before delegation — `ExternalAgentDelegate` does not sanitise inputs.

`is_available()` uses `shutil.which()` to check for the CLI before attempting to run it. This prevents a `FileNotFoundError` from propagating as an unhandled exception and gives callers a clean availability check.

## Output Parsing

Claude Code CLI is invoked with `--output-format json` and `--print` (non-interactive mode). The backend reads stdout and attempts to parse it as JSON. If parsing fails, the raw text is returned as the output field. This graceful fallback means partial or malformed output (e.g., from an early crash) still surfaces useful information rather than raising an exception.

## DelegationResult

```python
@dataclass
class DelegationResult:
    agent: str
    output: str
    exit_code: int
    error: str = ""
```

`exit_code` allows callers to distinguish clean completion (0) from agent errors (non-zero). `error` captures stderr content so debugging information is not lost.

## Timeout Handling

`asyncio.wait_for()` wraps the subprocess call with a configurable `timeout` (default 300 seconds). If the agent does not complete within the timeout, `asyncio.TimeoutError` is raised. The caller must handle this — `ExternalAgentDelegate` does not retry.

## Known Gaps

- Only `claude` CLI is supported. The architecture supports multiple agents (`is_available()` checks by name) but only `_run_claude()` is implemented.
- No retry logic on transient failures.
- No resource accounting — concurrent delegations will each spawn a separate subprocess with full system access.


## Error Surface and Structured Returns

Rather than raising exceptions on agent failures, `ExternalAgentDelegate.run()` always returns a `DelegationResult`. Non-zero `exit_code` and non-empty `error` fields let callers distinguish transient failures (process killed by OOM) from permanent ones (agent rejected the prompt) without catching exceptions. This keeps the delegation API clean and predictable for callers that chain multiple delegation attempts.

## Phase 2 Integration Context

`delegation.py` was created as part of Phase 2's integration ecosystem goal, where PocketPaw acts as a hub that can orchestrate other specialised agents. The design anticipates adding more agents (e.g., `gemini`, `gpt-engineer`) by adding entries to `is_available()` and corresponding `_run_<agent>()` methods without changing the public API.
