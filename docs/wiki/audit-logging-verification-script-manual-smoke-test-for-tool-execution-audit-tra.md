---
{
  "title": "Audit Logging Verification Script — Manual Smoke Test for Tool Execution Audit Trail",
  "summary": "This standalone script provides a manual, human-readable verification that the audit logging pipeline records both `attempt` and `success` entries when a tool is executed through the ToolRegistry. It runs outside the pytest harness and is designed to be invoked directly to confirm audit infrastructure is wired correctly in a live environment.",
  "concepts": [
    "audit logging",
    "ToolRegistry",
    "ShellTool",
    "verify_audit",
    "JSONL log",
    "attempt entry",
    "success entry",
    "get_audit_logger",
    "smoke test",
    "tool execution lifecycle",
    "manual verification",
    "log_path"
  ],
  "categories": [
    "testing",
    "audit and compliance",
    "tooling",
    "developer utilities"
  ],
  "source_docs": [
    "36d90469818bd213"
  ],
  "backlinks": null,
  "word_count": 641,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/verify_audit.py` is a diagnostic script, not a pytest test. Its purpose is to give a developer an instant, printable answer to the question: "Is audit logging actually working right now?" It was written as an early integration check for the audit system — a way to verify that the plumbing from `ToolRegistry.execute` through `AuditLogger` to the JSONL log file is connected correctly without needing to understand the full pytest fixture hierarchy.

The script is invoked directly (`python tests/verify_audit.py`) and prints emoji-decorated status lines to stdout, making it suitable for quick terminal checks and CI smoke-test stages that report to a human reader.

## Why This Exists Alongside Pytest Tests

Pytest tests mock dependencies and run in isolation. That is good for correctness, but it means a passing pytest suite does not prove that all the wiring is correct in a real runtime. `verify_audit.py` uses real production objects — a real `ToolRegistry`, a real `ShellTool` registered into it, and a real `AuditLogger` with a real log file on disk. If any import, dependency injection, or file-path resolution is broken in the live runtime, this script will surface it immediately.

## Execution Flow

```python
registry = ToolRegistry()
registry.register(ShellTool())

audit_logger = get_audit_logger()
log_path = audit_logger.log_path
```

The script first builds the minimal runtime: a registry with a single registered tool (`ShellTool`) and an audit logger whose `log_path` is printed so the operator knows where to look if verification fails.

```python
await registry.execute("shell", command="echo 'AUDIT CHECK'")
```

Executing the `shell` tool through the registry triggers the tool lifecycle: a pre-execution `attempt` audit entry is written, the command runs, and a post-execution `success` (or `failure`) entry is written.

```python
for line in reversed(new_lines):
    entry = json.loads(line)
    if entry.get("action") == "tool_use" and entry.get("target") == "shell":
        if entry.get("status") == "attempt":
            found_attempt = True
        elif entry.get("status") == "success":
            found_success = True
```

The verification loop reads the log file from the end backward, looking for both an `attempt` entry and a `success` entry with `action == "tool_use"` and `target == "shell"`. The two-entry requirement is meaningful: the `attempt` entry proves that pre-execution logging fired (a defense against situations where the tool crashes before the success entry can be written, leaving no evidence of the invocation); the `success` entry proves the tool completed and the post-execution hook ran.

## What Failure Looks Like

- **Log file not created** — the `AuditLogger` is not writing to disk at all, likely a misconfigured `log_path` or a permissions issue.
- **`attempt` found but not `success`** — the tool executed but the post-execution hook did not run, suggesting an exception is being swallowed somewhere in the registry's finally block.
- **`success` found but not `attempt`** — the pre-execution hook is not registered, meaning a class of tool failures (crashes before execution) would be completely unaudited.
- **Neither found** — the JSONL parser is broken, the log path changed, or the entire audit middleware was accidentally disabled.

## Known Gaps

- **Simplified line range** — the comment `new_lines = lines[0:]` notes this is a simplified check that reads all lines rather than only lines written after the script started. In a long-running audit log, this means the script could find `attempt`/`success` entries from a *previous* run and incorrectly report success. A more robust implementation would record the file length before the tool execution and slice from that offset afterward.
- **No failure injection** — the script only tests the success path. There is no variant that deliberately fails the tool execution to verify that a `failure` audit entry is written instead.
- **Single tool** — only `ShellTool` is tested. If another tool has a broken audit hook, this script would not catch it.
- **Hardcoded log scan** — because the script does not use pytest parametrize or fixtures, it cannot be easily extended to test multiple tools or audit categories without duplicating code.
