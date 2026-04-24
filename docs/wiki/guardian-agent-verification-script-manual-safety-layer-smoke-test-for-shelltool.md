---
{
  "title": "Guardian Agent Verification Script — Manual Safety Layer Smoke Test for ShellTool",
  "summary": "This standalone script manually validates that the Guardian safety layer correctly permits safe shell commands while blocking dangerous ones, providing a human-readable pass/fail report for the tool permission model. It distinguishes between two blocking mechanisms — regex-based pre-screening in ShellTool and the Guardian agent's semantic analysis — to help developers diagnose which layer is active.",
  "concepts": [
    "Guardian agent",
    "ShellTool",
    "command safety",
    "regex pre-screening",
    "semantic safety analysis",
    "ToolRegistry",
    "verify_guardian",
    "shell command blocking",
    "safety layer architecture",
    "dangerous command detection",
    "smoke test",
    "tool permission model"
  ],
  "categories": [
    "testing",
    "security",
    "tooling",
    "developer utilities"
  ],
  "source_docs": [
    "d10f51711599a5cd"
  ],
  "backlinks": null,
  "word_count": 703,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/verify_guardian.py` is a diagnostic script that manually exercises the Guardian agent's command safety evaluation. It exists to answer a question that unit tests cannot easily answer: "Is the Guardian actually running in the real execution path, or is some earlier filter intercepting commands before they reach it?"

Like `verify_audit.py`, this script runs outside pytest and is designed for direct invocation by a developer who wants a live proof that the safety architecture is functioning. It prints labeled test results with status indicators, making it readable in both interactive terminal sessions and CI log output.

## Why the Guardian vs. Regex Distinction Matters

PocketPaw's `ShellTool` has two layers of command safety enforcement:

1. **Regex pre-screening** — a hardcoded pattern list in `ShellTool` that blocks the most obviously destructive patterns (e.g., `rm -rf /`, `rm -rf *`). This is a fast, local, deterministic filter.

2. **Guardian agent** — a higher-level semantic analyzer that evaluates commands the regex did not catch. The Guardian can block contextually dangerous commands that regex cannot express (e.g., `rm important_file.txt` — deleting a specific file is not dangerous in general, but may be prohibited in a particular agent's permission scope).

If both layers produce the same "blocked" result, it is impossible from the output alone to tell which one fired. This script is specifically designed to surface that ambiguity.

## Execution Flow

```python
registry = ToolRegistry()
registry.register(ShellTool())
```

A minimal runtime is constructed: a single registry with `ShellTool` registered. No mocks, no overrides — the real Guardian path is active.

**Test 1 — Safe Command:**
```python
result = await registry.execute("shell", command="ls -la")
```
`ls -la` is universally harmless. The test checks that the result does not contain `"blocked"`. If it does, something is wrong with the allowlist — the Guardian is over-triggering on safe commands, which would make the shell tool unusable.

**Test 2 — Dangerous Command:**
```python
cmd = "rm important_file.txt"
result = await registry.execute("shell", command=cmd)
```
`rm important_file.txt` is the crucial test case. The inline comment explains the design intention precisely:

- `rm -rf /` and `rm -rf *` are caught by the ShellTool regex.
- `rm important_file.txt` (single specific file) is **not** caught by the regex.
- The Guardian **should** catch it based on the semantic understanding that file deletion is potentially harmful.

The result is evaluated with three branches:
- `"blocked by Guardian"` → Guardian is working correctly (PASSED).
- `"Dangerous command blocked"` → The regex caught it first (NOTED but not a pass — the test comment says "try a subtler command").
- Neither → The command actually executed (FAILED — dangerous behavior).

## Why This Architecture Is Significant

The three-branch output is not just documentation — it is an assertion about the *depth* of the safety stack. If the Guardian is disabled or broken, `rm important_file.txt` would execute. If only the regex is working, the test would report the intermediate warning. Only a "blocked by Guardian" response confirms the full safety architecture is active.

This is particularly important after refactoring: if the Guardian dependency is accidentally removed or the registry's execute path changes such that Guardian no longer intercepts tool calls, the existing regex tests would all still pass (since the regex layer is inside `ShellTool` itself) but this script would catch the regression.

## Known Gaps

- **No `rm important_file.txt` fallback** — the test assumes this file does not exist in the working directory. If it does, the "executed" path would produce unexpected behavior (the file would be deleted and the test would mark it as a failure, but the filesystem damage would already be done).
- **Single dangerous command** — the script tests only one subtler dangerous command. There is no coverage for other Guardian-only blocks (e.g., network exfiltration commands, process escalation).
- **No success path verification** — the script does not check the audit log to confirm that Guardian blocks are recorded. A Guardian that silently drops commands without logging would pass this script but fail an audit compliance check.
- **Guardian not imported directly** — the script assumes the Guardian is wired into the registry via `ShellTool`'s execution path. If the Guardian became a separate middleware that must be explicitly composed, this script would not detect the missing composition.
