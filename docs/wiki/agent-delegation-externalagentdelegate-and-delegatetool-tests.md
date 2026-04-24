---
{
  "title": "Agent Delegation: ExternalAgentDelegate and DelegateTool Tests",
  "summary": "This suite tests PocketPaw's agent delegation system, which allows a running agent to hand off subtasks to external CLI-based agents (initially Claude Code). It covers the DelegationResult dataclass, availability detection via PATH lookup, unknown agent rejection, and the DelegateTool's trust level, parameter schema, and execution paths including error handling for missing installations.",
  "concepts": [
    "ExternalAgentDelegate",
    "DelegationResult",
    "DelegateTool",
    "DelegateToClaudeCodeTool",
    "shutil_which",
    "trust_level",
    "tool_protocol",
    "subprocess_delegation",
    "agent_delegation",
    "Claude_Code",
    "availability_check",
    "error_handling",
    "tool_parameters"
  ],
  "categories": [
    "testing",
    "agent-delegation",
    "tools",
    "external-agents",
    "test"
  ],
  "source_docs": [
    "62325bc6e7112748"
  ],
  "backlinks": null,
  "word_count": 522,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`test_delegation.py` tests `pocketpaw.agents.delegation` and `pocketpaw.tools.builtin.delegate`, the components that enable PocketPaw agents to delegate work to external agent runtimes installed on the host machine. The initial implementation targets Claude Code (`claude` CLI), but the architecture is designed to support any external agent available on PATH.

## Why This Module Exists

Some tasks are better handled by specialized external agents than by the PocketPaw runtime directly. For example, a PocketPaw agent might delegate a complex code refactoring task to Claude Code, which has deeper tool access and a different execution model. The delegation system provides a controlled interface for this cross-runtime handoff.

## DelegationResult

`TestDelegationResult` tests the result dataclass returned by every delegation call:

- `test_fields`: A successful result has `agent`, `output`, and `exit_code=0`. The `error` field defaults to `""` (empty string) rather than `None`, so callers can check `if result.error` without null-guard boilerplate.
- `test_with_error`: A failed result carries the error message in `result.error` alongside `exit_code=1`. This allows callers to distinguish between "agent ran but produced no output" and "agent failed to run."

## ExternalAgentDelegate — Availability Detection

`TestExternalAgentDelegate` tests the delegate's `is_available()` class method, which uses `shutil.which()` to check whether the target agent binary exists on PATH:

- `test_is_available_not_installed`: When `shutil.which()` returns `None`, `is_available()` returns `False`. This is the guard that prevents delegation attempts to agents the user hasn't installed.
- `test_is_available_installed`: When `shutil.which()` returns a path, `is_available()` returns `True`.

The `shutil.which()` check is patched to avoid filesystem dependencies in tests. In production, this check runs synchronously before any subprocess is spawned, failing fast with a clear error rather than hanging on a missing binary.

- `test_unknown_agent`: Requesting an unknown agent name returns a falsy result immediately. The delegate maintains a registry of known agent names and rejects unknown strings defensively.
- `test_run_unknown_agent`: Calling `run()` on an unknown agent raises immediately without attempting subprocess execution.
- `test_run_claude_not_installed`: Calling `run()` for Claude when it isn't installed raises with an installation hint, giving the user actionable feedback.

## DelegateTool

`TestDelegateTool` tests the `DelegateToClaudeCodeTool`, which wraps `ExternalAgentDelegate` in PocketPaw's tool protocol:

- `test_name`: The tool registers under the correct name string used in the agent's tool registry.
- `test_trust_level`: The delegation tool requires a specific trust level — delegation is a privileged operation since it spawns external processes. This prevents low-trust agent contexts from arbitrarily spawning subprocesses.
- `test_parameters`: The tool's JSON schema has the expected parameters (`task`, optionally `timeout`), matching what the LLM needs to construct a valid tool call.
- `test_execute_not_installed`: When Claude Code isn't installed, `execute()` returns a user-readable error string rather than raising an exception. Tools in PocketPaw return error strings rather than raising so that agents receive the error as content and can react (e.g., try an alternative approach).
- `test_execute_with_error`: When the external agent exits with a non-zero code, the error is captured in the result string.
- `test_execute_success`: A successful delegation returns the agent's output as the tool result.

## Known Gaps

The tests mock subprocess execution — there are no integration tests running a real `claude` binary. Timeout handling for long-running delegated tasks is referenced in the parameter schema but not explicitly tested for enforcement.
