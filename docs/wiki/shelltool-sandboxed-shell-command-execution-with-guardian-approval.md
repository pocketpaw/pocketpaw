---
{
  "title": "ShellTool: Sandboxed Shell Command Execution with Guardian Approval",
  "summary": "`ShellTool` wraps `asyncio.subprocess` with two security layers — a precompiled dangerous-pattern regex rail and a Guardian Agent check — before executing any shell command. It operates at `critical` trust level, the highest in PocketPaw's trust hierarchy, and defaults to a jailed working directory.",
  "concepts": [
    "ShellTool",
    "COMPILED_DANGEROUS_PATTERNS",
    "Guardian",
    "trust_level_critical",
    "file_jail",
    "asyncio_subprocess",
    "security_rails",
    "command_blocking",
    "working_directory",
    "timeout"
  ],
  "categories": [
    "tools",
    "security",
    "shell-execution",
    "agent-safety"
  ],
  "source_docs": [
    "152d2c19c66ef1ba"
  ],
  "backlinks": null,
  "word_count": 497,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`shell.py` exposes a `shell` tool that lets agents run arbitrary shell commands on the host machine. Because arbitrary execution is the highest-risk capability an agent can have, the implementation layers multiple mitigations before a command ever reaches the OS.

## Trust Level: Critical

```python
@property
def trust_level(self) -> str:
    return "critical"
```

`critical` is the highest trust tier in PocketPaw's trust model. Tools at this level require explicit user opt-in and may trigger additional confirmation flows depending on the deployment configuration. Marking shell execution as `critical` ensures it can't be silently included in a low-trust agent context.

## Working Directory Jail

```python
def __init__(self, working_dir: str | None = None, timeout: int = 120):
    self.working_dir = working_dir or str(get_settings().file_jail_path)
```

The default working directory is `file_jail_path` from settings — a configured root directory that limits where commands run. This doesn't prevent the command itself from reading files outside the jail (a shell `cat /etc/passwd` still works), but it anchors relative paths to a known-safe location and prevents trivial directory traversal via relative `../` paths in commands.

## Two-Layer Security

**Layer 1 — Compiled Regex Rail:**

```python
DANGEROUS_PATTERNS = COMPILED_DANGEROUS_PATTERNS  # from security/rails.py

for pattern in self.DANGEROUS_PATTERNS:
    if pattern.search(command):
        return self._error(f"Dangerous command blocked: {command}")
```

The patterns are precompiled at module load time (imported from `security/rails.py`) and shared across all tools that need them. Precompilation avoids repeated regex compilation overhead in hot paths. The patterns block well-known destructive commands (e.g., `rm -rf /`, fork bombs, `dd if=/dev/zero`).

**Layer 2 — Guardian Agent:**

```python
is_safe, reason = await get_guardian().check_command(command)
if not is_safe:
    return self._error(f"Guardian blocked: {reason}")
```

After the static regex check, the command goes to `Guardian` — a secondary LLM-based safety evaluator. The Guardian can reason about semantic danger that regexes can't catch: a command that constructs a destructive invocation dynamically, or one that leaks credentials through environment variables. This is the "belt and suspenders" principle — static patterns catch known-bad patterns fast, the Guardian catches novel threats.

## The `pwd` Special Case

```python
if command.strip() == "pwd":
    return self.working_dir
```

`pwd` is handled natively before any security checks. The reason is cross-platform compatibility: on Windows (or in sandboxed environments where subprocess is restricted), the real `pwd` command might not exist or might return a path that conflicts with the tool's logical working directory. Returning `self.working_dir` directly makes the tool's behavior deterministic regardless of host OS.

## Timeout

The default 120-second timeout prevents runaway commands from blocking the agent indefinitely. Long-running tasks (e.g., compilation, test runs) will be killed after two minutes. This is a pragmatic tradeoff — agents can always spawn background processes for long jobs.

## Known Gaps

- The jail is a default working directory, not a true sandbox: a command can still `cd /` and operate outside it.
- There is no output size cap — a command that produces gigabytes of output would exhaust memory.
- The Guardian LLM call adds latency to every shell invocation, including benign ones like `ls`.
