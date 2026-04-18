# Discord CLI tool — wraps discli for Discord server management.
# Runs discli commands via subprocess with --json output.

import asyncio
import json
import logging
import shlex
import shutil
from typing import Any

from pocketpaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)


class DiscordCLITool(BaseTool):
    """Manage Discord servers via the discli CLI tool."""

    @property
    def name(self) -> str:
        return "discord_cli"

    @property
    def description(self) -> str:
        return (
            "Run Discord management commands via the discli CLI. "
            "Supports sending messages (with rich embeds), searching history, "
            "managing channels/roles/members, adding reactions, sending DMs, "
            "working with threads, creating polls, managing webhooks, "
            "scheduling events, and moderation actions. "
            "All commands return JSON. "
            "Use this for Discord operations beyond normal chat responses."
        )

    @property
    def trust_level(self) -> str:
        return "high"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "The discli subcommand and arguments as a single string. "
                        "Do NOT include 'discli' or '--json' — they are added automatically.\n"
                        "Examples:\n"
                        '  message send #general "Hello world!"\n'
                        '  message send #general "See attached" --file report.pdf\n'
                        '  message send #general "Fancy!" --embed-title "News"'
                        ' --embed-desc "Details" --embed-color ff0000\n'
                        "  message list #general --limit 20\n"
                        '  message search #general "bug" --limit 50\n'
                        "  message history #general --days 7\n"
                        '  message reply #general 123456789 "Thanks!"\n'
                        "  message bulk-delete #general 10 --yes\n"
                        '  dm send alice "Hey there"\n'
                        '  channel list --server "My Server"\n'
                        '  channel create "My Server" new-channel --type text\n'
                        '  channel create "My Server" ideas --type forum --topic "Share ideas"\n'
                        '  channel edit "My Server" #general --topic "Welcome" --slowmode 5\n'
                        '  channel set-permissions "My Server" #private Moderator'
                        " --allow send_messages,read_messages\n"
                        '  channel forum-post "My Server" #ideas "New Feature" "Description here"\n'
                        "  thread list #general\n"
                        '  thread create #general 123456789 "Thread Name"\n'
                        '  thread send 987654321 "Following up"\n'
                        "  thread archive 987654321\n"
                        "  thread unarchive 987654321\n"
                        '  thread rename 987654321 "Better Name"\n'
                        "  thread add-member 987654321 123456789\n"
                        "  thread remove-member 987654321 123456789\n"
                        '  poll create #general "Favorite AI?" Claude Gemini ChatGPT\n'
                        '  poll create #general "Best language?" Python Rust Go --multiple\n'
                        "  poll results #general 123456789\n"
                        "  poll end #general 123456789\n"
                        "  reaction add #general 123456789 👍\n"
                        "  reaction users #general 123456789 👍 --limit 50\n"
                        '  role list "My Server"\n'
                        '  role assign "My Server" alice Moderator\n'
                        '  role edit "My Server" Moderator --color ff0000 --hoist --mentionable\n'
                        '  member list "My Server" --limit 50\n'
                        '  member timeout "My Server" troublemaker 300 --reason "Cool down"\n'
                        "  server list\n"
                        '  server info "My Server"\n'
                        '  webhook list "My Server"\n'
                        '  webhook create "My Server" #logs "Audit Logger"\n'
                        '  webhook delete "My Server" 123456789 --yes\n'
                        '  event list "My Server"\n'
                        '  event create "My Server" "Game Night"'
                        ' "2026-03-25T20:00:00Z" --location "Voice Chat"\n'
                        '  event delete "My Server" 123456789 --yes'
                    ),
                },
            },
            "required": ["command"],
        }

    async def execute(self, command: str, **kwargs: Any) -> str:
        if not shutil.which("discli"):
            return self._error(
                "discli is not installed. Install it with: pip install discord-cli-agent"
            )

        try:
            args = shlex.split(command)
        except ValueError as e:
            return self._error(f"Invalid command syntax: {e}")

        try:
            proc = await asyncio.create_subprocess_exec(
                "discli",
                "--json",
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except TimeoutError:
            proc.kill()
            return self._error(f"Command timed out after 30s: {command}")
        except Exception as e:
            return self._error(f"Failed to run discli: {e}")

        output = stdout.decode().strip() if stdout else ""
        errors = stderr.decode().strip() if stderr else ""

        if proc.returncode != 0:
            msg = errors or output or f"discli exited with code {proc.returncode}"
            return self._error(msg)

        # Try to parse and re-format JSON for cleaner output
        try:
            data = json.loads(output)
            return json.dumps(data, indent=2)
        except json.JSONDecodeError:
            return output if output else self._success("Command completed.")
