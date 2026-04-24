---
{
  "title": "Codex CLI Backend — Subprocess Wrapper for OpenAI's Terminal Agent",
  "summary": "Implements `CodexCLIBackend`, which spawns OpenAI's `codex` CLI as an async subprocess and parses its NDJSON event stream. Prompts are passed via stdin rather than as command-line arguments to avoid OS argument-length limits, and a 10 MiB subprocess buffer accommodates large MCP tool results.",
  "concepts": [
    "CodexCLIBackend",
    "subprocess",
    "NDJSON",
    "stdin prompt delivery",
    "StreamReader buffer",
    "model name injection guard",
    "history injection",
    "AgentEvent",
    "tool_call",
    "SIGTERM"
  ],
  "categories": [
    "agent-runtime",
    "subprocess",
    "openai",
    "security"
  ],
  "source_docs": [
    "c997ff040e8cd46f"
  ],
  "backlinks": null,
  "word_count": 418,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`CodexCLIBackend` integrates OpenAI's `codex` command-line agent into PocketPaw by running it as a managed subprocess. Codex CLI is analogous to Gemini CLI and Claude Code CLI: it accepts a prompt, executes tool calls (shell commands, file edits, web search, MCP calls), and streams NDJSON events back to stdout.

## Stdin-Based Prompt Delivery

The prompt is written to the process's stdin (using `"-"` as the prompt argument) rather than passed on the command line. This matters on Windows where the command-line argument limit is ~8,191 characters. Long system prompts or conversation histories would silently truncate on Windows if passed as argv. Codex CLI added stdin support in v0.1.2504; the backend enforces a minimum version check.

## 10 MiB Subprocess Buffer

`_SUBPROCESS_BUFFER_LIMIT = 10 * 1024 * 1024` overrides asyncio's default 64 KiB `StreamReader` buffer. Codex CLI emits NDJSON events that can be very large — Playwright MCP tool results, code completions with long diffs, and inline file content all routinely exceed 64 KiB. Without this override, `asyncio.StreamReader` would raise `LimitOverrunError` and terminate the stream mid-response.

## Model Name Injection Guard

`_MODEL_NAME_RE = re.compile(r"^[\w\-.:]+$")` validates the model name before it is passed as a `--model` flag to the subprocess. Without this, a model name containing shell metacharacters (e.g., `; rm -rf /`) could result in command injection via the argv list. Even though `asyncio.create_subprocess_exec` does not invoke a shell, the validation provides defence-in-depth and catches misconfigured settings early.

## NDJSON Event Parsing

`run()` reads lines from subprocess stdout and parses each as a JSON object. Recognised event types map to `AgentEvent` variants:

- `assistant` message content → `TextChunk`
- `tool_call` → `ToolUse`
- `tool_result` → `ToolResult`
- `error` → `Error`
- process exit code 0 → `Done`

Unknown event types are logged and skipped so a new Codex CLI version with additional event types does not crash the backend.

## History Injection

`_inject_history()` serialises conversation history into the instruction string. Codex CLI does not accept a structured history API; the prior turns are prepended as a formatted conversation transcript. This is lossy for very long histories but necessary because the CLI is stateless between invocations.

## Known Gaps

- Codex CLI does not expose a formal session/context API, so memory is only as good as what fits in the injected history string.
- `stop()` sends `SIGTERM` to the subprocess but does not wait for graceful shutdown; child processes may linger briefly.
- MCP tool result size is bounded by `_SUBPROCESS_BUFFER_LIMIT`; payloads larger than 10 MiB will still fail.
