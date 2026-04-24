---
{
  "title": "RunPythonTool Test Suite — Sandboxed Code Execution with Guardian and File Jail",
  "summary": "This test file covers `RunPythonTool`, which executes arbitrary Python code in a sandboxed subprocess controlled by a file jail and a guardian policy engine. Tests validate stdout capture, stderr surfacing, exit code reporting, timeout enforcement, guardian blocking, filesystem isolation, temp file cleanup, and tool metadata.",
  "concepts": [
    "RunPythonTool",
    "sandboxed execution",
    "file jail",
    "guardian policy",
    "subprocess",
    "timeout enforcement",
    "stderr capture",
    "exit code",
    "temp file cleanup",
    "trust level elevated",
    "code execution security"
  ],
  "categories": [
    "testing",
    "security",
    "tools",
    "code execution",
    "test"
  ],
  "source_docs": [
    "269d507ab042ad50"
  ],
  "backlinks": null,
  "word_count": 521,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_run_python.py` tests `RunPythonTool` from `pocketpaw.tools.builtin.python_exec`. This tool allows PocketPaw agents to execute Python code at runtime — a powerful capability that requires strict sandboxing. The test suite was created on 2026-03-12 and reflects the layered security model: code must clear a guardian check before any subprocess is spawned, and all filesystem activity must remain inside a configured jail directory.

## Fixtures and Test Infrastructure

Three pytest fixtures establish the mocked environment:

- **`mock_guardian_safe`** — a `MagicMock` whose `check_command` async method returns `(True, "")`, representing a guardian that approves all code unconditionally. Used by happy-path tests.
- **`jail(tmp_path)`** — returns `tmp_path` directly, using pytest's built-in temp directory as the file jail. This gives tests a real filesystem path where scripts can create files.
- **`mock_settings(jail)`** — a settings mock with `file_jail_path` pointing to the jail. This overrides config file loading so tests run without any installed PocketPaw configuration.

Both `get_guardian` and `get_settings` are patched inside each test to inject these mocks.

## Happy-Path Execution Tests

`test_run_python_basic` — prints `"hello"` and asserts the string appears in the result. The most fundamental check.

`test_run_python_multiline` — runs `import math; print(f'sqrt={math.sqrt(9)}')` to confirm stdlib imports work inside the sandbox.

`test_run_python_stderr` — writes to `sys.stderr` and asserts the result contains a `"STDERR"` section. This matters because agents need to diagnose failing scripts; a tool that silently swallows stderr hides the error.

`test_run_python_exit_code` — calls `sys.exit(1)` and asserts `"Exit code: 1"` appears. Non-zero exits without this would look like successful empty output.

`test_run_python_timeout` — runs an infinite loop with `timeout=1` and asserts `"timed out"` appears in the result. Without timeout enforcement, a buggy script could stall the entire agent process.

`test_run_python_syntax_error` — passes deliberately broken Python and confirms that either `"SyntaxError"` or `"Error"` surfaces in the result.

## Security Tests

`test_run_python_guardian_block` uses a guardian mock that returns `(False, "blocked by policy")`. The test asserts the tool returns a `"blocked"` message without ever spawning a subprocess. The guardian is the outermost security gate — if it is bypassed or its rejection is ignored, any subsequent sandboxing is irrelevant.

```python
blocking_guardian.check_command = AsyncMock(return_value=(False, "blocked by policy"))
result = await tool.execute(code='print("hello")')
assert "blocked" in result.lower()
```

## Filesystem Isolation Tests

`test_run_python_file_creation` — runs a script that writes `output.txt` to its working directory, then asserts the file exists inside the `jail` path and contains the expected content. This confirms the tool sets `cwd` to the jail before spawning the subprocess, so user scripts cannot write to arbitrary paths.

`test_run_python_cleanup` — after execution, scans the jail for files matching `_pocketpaw_run_*.py` (the temp script pattern) and asserts none remain. Without cleanup, repeated executions would accumulate temp files, leaking code content and eventually exhausting disk space.

## Tool Definition Test

`test_run_python_definition` asserts that the tool's definition has `name == "run_python"`, `trust_level == "elevated"` (not standard — executing arbitrary code warrants an elevated trust check), and that `code` is a required parameter while `timeout` is optional.

## Known Gaps

No `TODO` or `FIXME` markers appear. The test suite does not cover resource limits (CPU/memory), network access blocking inside the sandbox, or behavior when the jail directory itself does not exist or is not writable.
