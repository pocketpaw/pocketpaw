"""MCP server that exposes Discord operations via discli.

Run as: python -m pocketpaw.mcp.discord_server

This is a stdio-based MCP server that wraps the discli CLI tool,
making Discord operations available to any MCP-capable agent backend
(claude_agent_sdk, codex_cli, google_adk, etc.).
"""

import asyncio
import json
import logging
import shlex
import shutil
import sys

logger = logging.getLogger(__name__)

# Tool definitions exposed via MCP
TOOLS = [
    {
        "name": "discord_send_message",
        "description": "Send a message to a Discord channel.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel name with # (e.g. #general)"},
                "text": {"type": "string", "description": "Message text to send"},
            },
            "required": ["channel", "text"],
        },
    },
    {
        "name": "discord_dm",
        "description": "Send a direct message to a Discord user.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "Discord user ID"},
                "text": {"type": "string", "description": "Message text"},
            },
            "required": ["user_id", "text"],
        },
    },
    {
        "name": "discord_create_thread",
        "description": "Create a thread on a message in a channel.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel name with #"},
                "message_id": {"type": "string", "description": "Message ID to attach thread to"},
                "title": {"type": "string", "description": "Thread title"},
            },
            "required": ["channel", "message_id", "title"],
        },
    },
    {
        "name": "discord_send_thread",
        "description": "Send a message in a thread.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "thread_id": {"type": "string", "description": "Thread ID"},
                "text": {"type": "string", "description": "Message text"},
            },
            "required": ["thread_id", "text"],
        },
    },
    {
        "name": "discord_create_poll",
        "description": "Create a poll in a Discord channel.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel name with #"},
                "question": {"type": "string", "description": "Poll question"},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Poll options (2-10)",
                },
                "multiple": {
                    "type": "boolean",
                    "description": "Allow multiple selections",
                    "default": False,
                },
            },
            "required": ["channel", "question", "options"],
        },
    },
    {
        "name": "discord_add_reaction",
        "description": "Add an emoji reaction to a message.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel name with #"},
                "message_id": {"type": "string", "description": "Message ID"},
                "emoji": {"type": "string", "description": "Emoji to react with"},
            },
            "required": ["channel", "message_id", "emoji"],
        },
    },
    {
        "name": "discord_list_messages",
        "description": "List recent messages in a channel.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel name with #"},
                "limit": {"type": "integer", "description": "Max messages", "default": 20},
            },
            "required": ["channel"],
        },
    },
    {
        "name": "discord_search_messages",
        "description": "Search messages in a channel.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel name with #"},
                "query": {"type": "string", "description": "Search query"},
                "limit": {"type": "integer", "description": "Max results", "default": 50},
            },
            "required": ["channel", "query"],
        },
    },
    {
        "name": "discord_list_channels",
        "description": "List channels in a server.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": {"type": "string", "description": "Server name"},
            },
            "required": ["server"],
        },
    },
    {
        "name": "discord_member_info",
        "description": "Get info about a server member.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": {"type": "string", "description": "Server name"},
                "username": {"type": "string", "description": "Username to look up"},
            },
            "required": ["server", "username"],
        },
    },
    {
        "name": "discord_list_members",
        "description": "List members of a server.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": {"type": "string", "description": "Server name"},
                "limit": {"type": "integer", "description": "Max members", "default": 50},
            },
            "required": ["server"],
        },
    },
    {
        "name": "discord_role_list",
        "description": "List roles in a server.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": {"type": "string", "description": "Server name"},
            },
            "required": ["server"],
        },
    },
    {
        "name": "discord_role_assign",
        "description": "Assign a role to a member.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": {"type": "string", "description": "Server name"},
                "username": {"type": "string", "description": "Username"},
                "role": {"type": "string", "description": "Role name"},
            },
            "required": ["server", "username", "role"],
        },
    },
    {
        "name": "discord_server_info",
        "description": "Get information about a Discord server.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": {"type": "string", "description": "Server name"},
            },
            "required": ["server"],
        },
    },
]


# Map tool names to discli command builders
def _build_command(tool_name: str, args: dict) -> str:
    """Convert an MCP tool call into a discli command string."""
    match tool_name:
        case "discord_send_message":
            return f"message send {args['channel']} {shlex.quote(args['text'])}"
        case "discord_dm":
            return f"dm send {args['user_id']} {shlex.quote(args['text'])}"
        case "discord_create_thread":
            return (
                f"thread create {args['channel']} {args['message_id']} {shlex.quote(args['title'])}"
            )
        case "discord_send_thread":
            return f"thread send {args['thread_id']} {shlex.quote(args['text'])}"
        case "discord_create_poll":
            opts = " ".join(shlex.quote(o) for o in args["options"])
            cmd = f"poll create {args['channel']} {shlex.quote(args['question'])} {opts}"
            if args.get("multiple"):
                cmd += " --multiple"
            return cmd
        case "discord_add_reaction":
            return f"reaction add {args['channel']} {args['message_id']} {args['emoji']}"
        case "discord_list_messages":
            limit = args.get("limit", 20)
            return f"message list {args['channel']} --limit {limit}"
        case "discord_search_messages":
            limit = args.get("limit", 50)
            return f"message search {args['channel']} {shlex.quote(args['query'])} --limit {limit}"
        case "discord_list_channels":
            return f"channel list --server {shlex.quote(args['server'])}"
        case "discord_member_info":
            return f"member info {shlex.quote(args['server'])} {args['username']}"
        case "discord_list_members":
            limit = args.get("limit", 50)
            return f"member list {shlex.quote(args['server'])} --limit {limit}"
        case "discord_role_list":
            return f"role list {shlex.quote(args['server'])}"
        case "discord_role_assign":
            return f"role assign {shlex.quote(args['server'])} {args['username']} {args['role']}"
        case "discord_server_info":
            return f"server info {shlex.quote(args['server'])}"
        case _:
            raise ValueError(f"Unknown tool: {tool_name}")


async def _run_discli(command: str) -> str:
    """Run a discli command and return the output."""
    discli = shutil.which("discli")
    if not discli:
        return json.dumps({"error": "discli not installed"})

    try:
        cmd_args = shlex.split(command)
        proc = await asyncio.create_subprocess_exec(
            discli,
            "--json",
            *cmd_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except TimeoutError:
        proc.kill()
        return json.dumps({"error": f"Command timed out: {command}"})
    except Exception as e:
        return json.dumps({"error": str(e)})

    output = stdout.decode().strip() if stdout else ""
    errors = stderr.decode().strip() if stderr else ""

    if proc.returncode != 0:
        return json.dumps({"error": errors or output or f"Exit code {proc.returncode}"})

    try:
        data = json.loads(output)
        return json.dumps(data, indent=2)
    except json.JSONDecodeError:
        return output or json.dumps({"status": "ok"})


async def _handle_request(request: dict) -> dict:
    """Handle a single JSON-RPC request."""
    method = request.get("method", "")
    req_id = request.get("id")
    params = request.get("params", {})

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "pocketpaw-discord", "version": "1.0.0"},
            },
        }

    if method == "notifications/initialized":
        return None  # No response for notifications

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": TOOLS},
        }

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        try:
            command = _build_command(tool_name, arguments)
            result = await _run_discli(command)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": result}],
                },
            }
        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps({"error": str(e)})}],
                    "isError": True,
                },
            }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


async def main():
    """Run the MCP server on stdio."""
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await asyncio.get_event_loop().connect_read_pipe(lambda: protocol, sys.stdin.buffer)

    writer_transport, writer_protocol = await asyncio.get_event_loop().connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout.buffer
    )
    writer = asyncio.StreamWriter(writer_transport, writer_protocol, None, asyncio.get_event_loop())

    while True:
        header = await reader.readline()
        if not header:
            break

        header_str = header.decode().strip()
        if header_str.startswith("Content-Length:"):
            content_length = int(header_str.split(":")[1].strip())
            await reader.readline()  # empty line
            body = await reader.readexactly(content_length)
            request = json.loads(body.decode())

            response = await _handle_request(request)
            if response is None:
                continue

            response_bytes = json.dumps(response).encode()
            out = f"Content-Length: {len(response_bytes)}\r\n\r\n".encode() + response_bytes
            writer.write(out)
            await writer.drain()


if __name__ == "__main__":
    asyncio.run(main())
