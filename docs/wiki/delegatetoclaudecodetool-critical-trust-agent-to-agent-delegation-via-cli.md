---
{
  "title": "DelegateToClaudeCodeTool: Critical-Trust Agent-to-Agent Delegation via CLI",
  "summary": "`DelegateToClaudeCodeTool` gives PocketPaw's agent the ability to hand off complex coding tasks to Claude Code CLI, the Anthropic autonomous coding agent. It is marked `critical` trust — the highest level in PocketPaw's trust hierarchy — because Claude Code has unrestricted filesystem, shell, and web access.",
  "concepts": [
    "DelegateToClaudeCodeTool",
    "critical trust",
    "Claude Code CLI",
    "ExternalAgentDelegate",
    "subprocess",
    "task delegation",
    "trust hierarchy",
    "availability check",
    "timeout",
    "multi-agent"
  ],
  "categories": [
    "tool-system",
    "security",
    "multi-agent",
    "agent-delegation"
  ],
  "source_docs": [
    "36c2333877c815b7"
  ],
  "backlinks": null,
  "word_count": 404,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Some tasks are better suited to a specialized agent than to a generalist conversation agent. Multi-step file editing, debugging across a codebase, and project scaffolding all require the kind of persistent, tool-rich environment that Claude Code CLI provides. `DelegateToClaudeCodeTool` creates a bridge: PocketPaw's conversational agent can recognize when a task exceeds its own capabilities and hand it off to Claude Code for autonomous execution.

## Critical Trust Level

This tool carries `trust_level = "critical"` — the highest classification in PocketPaw's trust hierarchy. This is because `ExternalAgentDelegate.run("claude", task)` ultimately spawns a subprocess that executes `claude --print "<task>"`, giving Claude Code full access to:

- The user's filesystem (read and write)
- The shell (arbitrary command execution)
- The web (HTTP requests)
- Installed development tools

A compromised prompt that reaches this tool could exfiltrate files, execute malicious commands, or install packages. Critical trust means this tool is excluded from all but explicitly configured "power" agent profiles where the operator has consciously decided to allow subprocess-level delegation.

## Availability Check

The tool checks for Claude Code CLI availability before attempting delegation:

```python
if not ExternalAgentDelegate.is_available("claude"):
    return self._error(
        "Claude Code CLI not found. Install with: "
        "npm install -g @anthropic-ai/claude-code"
    )
```

This prevents confusing errors when the tool is available in the registry but the system dependency is missing. The check runs `which claude` or equivalent on each call (not cached) to handle the case where the CLI is installed mid-session.

## Configurable Timeout

The `timeout` parameter (default 300 seconds) caps how long the tool will wait for Claude Code to complete. For complex tasks, 5 minutes may not be enough; callers can pass a higher value. On timeout, the tool returns an error string rather than hanging the agent session indefinitely.

## ExternalAgentDelegate Pattern

The actual subprocess management is delegated to `pocketpaw.agents.delegation.ExternalAgentDelegate`, keeping the tool thin. This separation means the subprocess lifecycle (environment setup, stdout/stderr capture, exit code handling) can be improved without touching the tool itself.

## Known Gaps

- **No streaming output** — the tool waits for Claude Code to finish and returns the full output at once. For long-running tasks, the user gets no progress feedback during the wait.
- **No task cancellation** — there is no mechanism for the agent or user to cancel an in-flight Claude Code invocation once started. If the task hangs (e.g., waiting for a build), the only option is to wait for the timeout.