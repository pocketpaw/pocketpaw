---
{
  "title": "MCP Discord Server — stdio Bridge to discli",
  "summary": "`discord_server.py` implements a stdio-based MCP server that wraps the `discli` CLI tool, exposing Discord operations as MCP tools callable by any MCP-capable agent backend. It runs as a subprocess and communicates via JSON-RPC over stdin/stdout.",
  "concepts": [
    "MCP server",
    "stdio transport",
    "JSON-RPC",
    "discli",
    "Discord",
    "command building",
    "shlex",
    "tool registration",
    "asyncio subprocess",
    "cross-platform stdin"
  ],
  "categories": [
    "MCP Integration",
    "Discord Integration"
  ],
  "source_docs": [
    "6b984a6435257ec9"
  ],
  "backlinks": null,
  "word_count": 555,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw supports multiple agent backends (`claude_agent_sdk`, `codex_cli`, `google_adk`). Rather than building a separate Discord integration for each backend, `discord_server.py` creates a single MCP server that any backend can connect to through the standard MCP stdio transport. The server translates MCP tool calls into `discli` CLI commands, executes them, and returns the output.

Run it with:

```
python -m pocketpaw.mcp.discord_server
```

## Architecture: MCP over stdio

The server speaks JSON-RPC 2.0 on stdin/stdout, following the MCP specification. The message loop reads a line, dispatches to `_handle_request()`, writes the JSON response, and loops. This keeps the server stateless and easy to restart.

Cross-platform stdin reading is handled carefully: `_read_stdin` and `_read_exact` run blocking I/O in a thread executor rather than using asyncio's native file I/O, which is unreliable on Windows. This prevents the server from hanging on platforms where asyncio has no native stdin support.

## Tool Registration

Tools are declared as a static `TOOLS` list of JSON Schema objects. When an MCP client calls `tools/list`, the server returns this list verbatim. This design means adding a new Discord operation requires only appending to `TOOLS` and adding a branch in `_build_command`.

## Command Building

The `_build_command` function maps a tool name and argument dict to a `discli` CLI string. Helper functions like `_opt`, `_flag`, `_build_poll_create`, and `_build_send_embed` assemble individual flag strings. Using `shlex.quote` on user-supplied values prevents shell injection when the command is passed to `asyncio.create_subprocess_shell`.

## Tool Capabilities

| Tool | Discord Operation |
|------|-------------------|
| `discord_send_message` | Send a text message to a channel |
| `discord_send_embed` | Send a rich embed |
| `discord_create_poll` | Create a poll |
| `discord_channel_edit` | Edit channel properties |
| `discord_channel_set_permissions` | Set permission overwrites |
| `discord_member_timeout` | Timeout a member |
| `discord_role_edit` | Edit a role |
| `discord_webhook_list` | List webhooks |
| `discord_event_create` | Create a guild scheduled event |

## Defensive Patterns

`_run_discli` checks that `discli` is on `PATH` via `shutil.which` before attempting execution. If it is not found, it returns a structured error JSON rather than raising an exception, surfacing as a tool error to the agent rather than crashing the server.

## Why a Separate MCP Server?

Discord integration could have been implemented as a PocketPaw `ToolProtocol` class. The MCP server approach was chosen instead because it makes the Discord tools available to any MCP-capable agent — including third-party agents and future backends — without requiring them to be PocketPaw-native. It also separates the Discord logic from PocketPaw's core process: if `discli` crashes or hangs, the MCP subprocess dies without affecting the main application.

## Message Framing

MCP over stdio uses newline-delimited JSON for messages. The server reads a full line, parses it as JSON-RPC, processes the request, and writes a single JSON line followed by a newline. `_read_exact` handles the case where the client sends a Content-Length header (binary MCP framing), reading exactly that many bytes rather than a line — this makes the server compatible with both text-mode and binary-mode MCP clients.

## Known Gaps

No authentication model: `discli` must already be authenticated in the environment (bot token set via env var). The MCP server has no mechanism to prompt for or validate credentials. No rate-limit handling: rapid tool calls will hit Discord API limits and the error will be returned verbatim without retry logic.