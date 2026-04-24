---
{
  "title": "DiscordCLITool: Full Discord Management via the discli CLI Subprocess",
  "summary": "`DiscordCLITool` wraps the `discli` command-line tool to give PocketPaw's agent comprehensive Discord server management capabilities — messaging, channel management, moderation, polls, webhooks, and more — all through a single tool that constructs safe subprocess invocations with shell-split argument parsing.",
  "concepts": [
    "DiscordCLITool",
    "discli",
    "subprocess",
    "shlex",
    "asyncio subprocess",
    "Discord API",
    "high trust",
    "channel management",
    "moderation",
    "webhook",
    "JSON output"
  ],
  "categories": [
    "tool-system",
    "discord-integration",
    "channel-adapters"
  ],
  "source_docs": [
    "c2746731bd1b1b9b"
  ],
  "backlinks": null,
  "word_count": 450,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Discord operations beyond basic chat responses require Discord API access that is complex to implement natively. `DiscordCLITool` takes a pragmatic approach: it delegates to `discli`, a pre-built Discord CLI tool, via subprocess. This gives the agent the full `discli` command surface without PocketPaw needing to implement Discord API bindings itself.

## Command Construction and Safety

The tool receives a `command` string (e.g., `"message send #general "Hello!""`) and constructs the full subprocess invocation by prepending `discli` and appending `--json`:

```python
full_command = ["discli", "--json"] + shlex.split(command)
proc = await asyncio.create_subprocess_exec(
    *full_command,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
)
```

`shlex.split()` is critical here — it correctly handles quoted strings in the command (e.g., `"Hello world!"` stays as one argument rather than splitting on the space). Without `shlex.split`, a message containing spaces would corrupt the argument list.

`--json` is always appended automatically so the tool receives structured output it can parse and reformat, rather than human-readable text.

## Availability Check

Before attempting any command, the tool checks that `discli` is installed:

```python
if not shutil.which("discli"):
    return self._error("discli not found. Install with: pip install discli")
```

This produces a clear, actionable error rather than a confusing `FileNotFoundError` from the subprocess layer.

## Async Subprocess Pattern

Discord operations (sending messages, fetching history) involve network I/O that could block for hundreds of milliseconds. Using `asyncio.create_subprocess_exec` keeps these operations non-blocking — the event loop continues processing other sessions while waiting for `discli` to complete.

## Trust Level: High

The tool is marked `trust_level = "high"` because it can send messages, manage channels, issue moderation actions, and interact with webhooks on behalf of the bot's token. A misconfigured or compromised prompt could use this tool to spam a Discord server, delete channels, or ban members. High trust ensures it is only available in sessions where the operator has explicitly enabled Discord management capabilities.

## Broad Command Surface

The tool's description lists the full supported command set as examples — this is intentional LLM guidance. By showing the agent concrete examples of valid commands in the tool description itself, the tool reduces hallucination of invalid subcommands:

- Message operations: send, reply, list, search, history, bulk-delete, react
- DM operations: send DMs to users
- Channel operations: list, create (text/forum), edit, set-permissions, post to forums
- Thread operations, polls, webhooks, scheduled events, moderation

## Known Gaps

- **No output streaming** — `discli` output is buffered until the process exits. For commands that take several seconds (large history fetches), there is no incremental output.
- **Shell injection surface** — while `shlex.split` prevents most injection via argument splitting, the `command` string is still passed to `discli` which interprets it. If `discli` has any command injection vulnerabilities, they would be reachable through this tool.