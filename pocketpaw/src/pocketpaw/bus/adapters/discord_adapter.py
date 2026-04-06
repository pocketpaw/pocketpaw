"""
Discord Channel Adapter — powered by discli serve.

Spawns `discli serve` as a subprocess, communicates via stdin/stdout JSONL.
Replaces the direct discord.py adapter with a thin process bridge.
"""

import asyncio
import json
import logging
import shutil
import time
from typing import Any

from pocketpaw.bus import BaseChannelAdapter, Channel, InboundMessage, OutboundMessage

logger = logging.getLogger(__name__)

DISCORD_MSG_LIMIT = 2000
_NO_RESPONSE_MARKER = "[NO_RESPONSE]"

# Slash commands that map directly to "/{command}" with no extra args
_SIMPLE_SLASH_COMMANDS: frozenset[str] = frozenset(
    {"new", "sessions", "clear", "status", "help", "kill", "delete", "backends"}
)
_BOT_AUTHOR_KEY = "__bot__"
_CONVERSATION_HISTORY_SIZE = 30
_CONVERSATION_CHAR_BUDGET = 12_000
_IDLE_CHANNEL_TTL = 3600
# Buffer stream chunks until we can confirm it's not [NO_RESPONSE] (13 chars + margin)
_STREAM_BUFFER_THRESHOLD = 25


class DiscliAdapter(BaseChannelAdapter):
    """Discord adapter that delegates to discli serve subprocess."""

    def __init__(
        self,
        token: str,
        allowed_guild_ids: list[int] | None = None,
        allowed_user_ids: list[int] | None = None,
        allowed_channel_ids: list[int] | None = None,
        conversation_channel_ids: list[int] | None = None,
        conversation_all_channels: bool = False,
        conversation_exclude_channel_ids: list[int] | None = None,
        bot_name: str = "Paw",
        status_type: str = "online",
        activity_type: str = "",
        activity_text: str = "",
    ):
        super().__init__()
        self.token = token
        self.allowed_guild_ids = allowed_guild_ids or []
        self.allowed_user_ids = allowed_user_ids or []
        self.allowed_channel_ids = allowed_channel_ids or []
        self.conversation_channel_ids: set[int] = set(conversation_channel_ids or [])
        self.conversation_all_channels = conversation_all_channels
        self.conversation_exclude_channel_ids: set[int] = set(
            conversation_exclude_channel_ids or []
        )
        self.bot_name = bot_name or "Paw"
        self.status_type = (
            status_type if status_type in {"online", "idle", "dnd", "invisible"} else "online"
        )
        self.activity_type = activity_type
        self.activity_text = activity_text

        self._proc: asyncio.subprocess.Process | None = None
        self._slash_config_path: str | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._bot_id: str | None = None
        self._req_counter = 0
        self._pending_requests: dict[str, asyncio.Future] = {}
        self._active_streams: dict[str, str] = {}  # chat_id -> stream_id
        self._stream_buffer: dict[str, str] = {}  # chat_id -> buffered content

        # Conversation history (same as original adapter)
        self._conversation_history: dict[int, list[dict[str, str]]] = {}
        self._conversation_last_active: dict[int, float] = {}
        self._eviction_task: asyncio.Task | None = None
        self._start_time: float = 0.0

    @property
    def channel(self) -> Channel:
        return Channel.DISCORD

    # ── Process Management ──────────────────────────────────────────

    async def _on_start(self) -> None:
        if not self.token:
            raise RuntimeError("Discord bot token missing")

        discli_path = shutil.which("discli")
        if not discli_path:
            raise RuntimeError(
                "discli is not installed. Install it with: pip install discord-cli-agent"
            )

        # Build slash commands config
        self._slash_config_path = await self._write_slash_config()
        slash_file = self._slash_config_path

        cmd = [
            discli_path,
            "--json",
            "serve",
            "--include-self",
            "--status",
            self.status_type,
        ]
        if self.activity_type:
            cmd += ["--activity", self.activity_type]
        if self.activity_text:
            cmd += ["--activity-text", self.activity_text]
        if slash_file:
            cmd += ["--slash-commands", slash_file]

        import os

        # Set token in parent env so DiscordCLITool subprocesses inherit it
        os.environ["DISCORD_BOT_TOKEN"] = self.token

        env = {
            "DISCORD_BOT_TOKEN": self.token,
            "PYTHONUNBUFFERED": "1",
        }
        full_env = {**os.environ, **env}

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=full_env,
        )

        self._start_time = time.time()
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        self._eviction_task = asyncio.create_task(self._eviction_loop())

        # Wait for ready event
        for _ in range(30):
            if self._bot_id:
                break
            await asyncio.sleep(1)

        if not self._bot_id:
            # Clean up the spawned process before raising
            await self._on_stop()
            raise RuntimeError("discli serve failed to connect — check token and intents")

        logger.info("Discli Adapter started (bot: %s)", self._bot_id)

        # Auto-register Discord MCP server so all backends can use it
        self._register_discord_mcp()

    @staticmethod
    def _register_discord_mcp() -> None:
        """Auto-register the Discord MCP server if not already configured."""
        try:
            from pocketpaw.mcp.config import MCPServerConfig, load_mcp_config, save_mcp_config

            configs = load_mcp_config()
            if any(c.name == "pocketpaw-discord" for c in configs):
                logger.debug("Discord MCP server already registered")
                return

            import sys

            python = sys.executable
            configs.append(
                MCPServerConfig(
                    name="pocketpaw-discord",
                    transport="stdio",
                    command=python,
                    args=["-m", "pocketpaw.mcp.discord_server"],
                    env={},
                    enabled=True,
                )
            )
            save_mcp_config(configs)
            logger.info("Auto-registered Discord MCP server")
        except Exception as e:
            logger.warning("Failed to register Discord MCP server: %s", e)

    async def _write_slash_config(self) -> str | None:
        """Write slash command definitions to a temp file."""
        import tempfile

        commands = [
            {
                "name": "paw",
                "description": "Send a message to PocketPaw",
                "params": [
                    {
                        "name": "message",
                        "type": "string",
                        "description": "Your message",
                    }
                ],
            },
            {"name": "new", "description": "Start a fresh conversation"},
            {"name": "sessions", "description": "List your conversation sessions"},
            {
                "name": "resume",
                "description": "Resume a previous session",
                "params": [
                    {
                        "name": "target",
                        "type": "string",
                        "description": "Session name or number",
                        "required": False,
                    }
                ],
            },
            {"name": "clear", "description": "Clear the current session history"},
            {
                "name": "rename",
                "description": "Rename the current session",
                "params": [
                    {
                        "name": "title",
                        "type": "string",
                        "description": "New session title",
                    }
                ],
            },
            {"name": "status", "description": "Show current session info"},
            {"name": "delete", "description": "Delete the current session"},
            {
                "name": "backend",
                "description": "Show or switch agent backend",
                "params": [
                    {
                        "name": "name",
                        "type": "string",
                        "description": "Backend name to switch to",
                        "required": False,
                    }
                ],
            },
            {"name": "backends", "description": "List all available backends"},
            {
                "name": "model",
                "description": "Show or switch model for current backend",
                "params": [
                    {
                        "name": "name",
                        "type": "string",
                        "description": "Model name to switch to",
                        "required": False,
                    }
                ],
            },
            {
                "name": "tools",
                "description": "Show or switch tool profile",
                "params": [
                    {
                        "name": "name",
                        "type": "string",
                        "description": "Tool profile name",
                        "required": False,
                    }
                ],
            },
            {"name": "help", "description": "Show PocketPaw help"},
            {"name": "kill", "description": "Cancel the current request"},
            {
                "name": "converse",
                "description": "Toggle conversation mode in this channel",
            },
        ]

        f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(commands, f)
        f.close()
        return f.name

    async def _drain_stderr(self) -> None:
        """Read and log stderr to prevent pipe buffer from blocking the process."""
        if not self._proc or not self._proc.stderr:
            return
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                text = line.decode().strip()
                if text:
                    logger.debug("discli stderr: %s", text)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("discli stderr reader error: %s", e)

    async def _on_stop(self) -> None:
        if self._eviction_task and not self._eviction_task.done():
            self._eviction_task.cancel()
        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except TimeoutError:
                self._proc.kill()
        if self._slash_config_path:
            import os

            try:
                os.unlink(self._slash_config_path)
            except OSError:
                pass
            self._slash_config_path = None
        self._conversation_history.clear()
        self._conversation_last_active.clear()
        self._stream_buffer.clear()
        logger.info("Discli Adapter stopped")

    # ── stdin/stdout Communication ──────────────────────────────────

    async def _send_command(self, action: str, **kwargs: Any) -> dict:
        """Send a command to discli serve via stdin, wait for response."""
        if not self._proc or not self._proc.stdin:
            return {"error": "discli process not running"}

        self._req_counter += 1
        req_id = str(self._req_counter)

        cmd = {"action": action, "req_id": req_id, **kwargs}
        line = json.dumps(cmd, default=str) + "\n"

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_requests[req_id] = future

        try:
            self._proc.stdin.write(line.encode())
            await self._proc.stdin.drain()
        except Exception as e:
            self._pending_requests.pop(req_id, None)
            return {"error": str(e)}

        try:
            return await asyncio.wait_for(future, timeout=30)
        except TimeoutError:
            self._pending_requests.pop(req_id, None)
            return {"error": "Command timed out"}

    async def _read_stdout(self) -> None:
        """Read JSONL events from discli serve stdout."""
        if not self._proc or not self._proc.stdout:
            return

        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    logger.warning("discli serve stdout closed")
                    break
                try:
                    data = json.loads(line.decode().strip())
                except json.JSONDecodeError:
                    continue

                event = data.get("event")

                # Response to a command we sent
                if event == "response":
                    req_id = data.get("req_id")
                    future = self._pending_requests.pop(req_id, None)
                    if future and not future.done():
                        future.set_result(data)
                    continue

                # Handle events — fire as tasks to avoid deadlocking
                # the reader (handlers may call _send_command which reads
                # from the same stdout this loop consumes).
                if event == "ready":
                    self._bot_id = data.get("bot_id")
                elif event == "message":
                    asyncio.create_task(self._handle_message_event(data))
                elif event == "slash_command":
                    asyncio.create_task(self._handle_slash_event(data))
                elif event == "error":
                    logger.error("discli serve error: %s", data.get("message"))

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("discli stdout reader crashed: %s", e)

    # ── Event Handlers ──────────────────────────────────────────────

    def _check_auth(self, guild_id: str | None, user_id: str, channel_id: str | None) -> bool:
        if self.allowed_guild_ids and guild_id:
            if int(guild_id) not in self.allowed_guild_ids:
                return False
        if self.allowed_user_ids:
            if int(user_id) not in self.allowed_user_ids:
                return False
        if self.allowed_channel_ids and channel_id:
            if int(channel_id) not in self.allowed_channel_ids:
                return False
        return True

    async def _handle_message_event(self, data: dict) -> None:
        author_id = data.get("author_id", "")
        channel_id = data.get("channel_id", "")
        guild_id = data.get("server_id")
        is_bot = data.get("is_bot", False)
        is_dm = data.get("is_dm", False)
        content = data.get("content", "")
        mentions_bot = data.get("mentions_bot", False)

        # Track bot's own messages for conversation history
        if is_bot and author_id == self._bot_id:
            ch_id = int(channel_id)
            _own_guild_ok = not self.allowed_guild_ids or (
                guild_id and int(guild_id) in self.allowed_guild_ids
            )
            _not_excluded = ch_id not in self.conversation_exclude_channel_ids
            if _not_excluded and (
                (self.conversation_all_channels and _own_guild_ok)
                or ch_id in self.conversation_channel_ids
            ):
                self._add_to_history(ch_id, _BOT_AUTHOR_KEY, content)
            return

        # Skip other bots
        if is_bot:
            return

        _in_allowed_guild = not self.allowed_guild_ids or (
            guild_id and int(guild_id) in self.allowed_guild_ids
        )
        _ch_id = int(channel_id)
        _not_excluded = _ch_id not in self.conversation_exclude_channel_ids
        is_conversation = (
            not is_dm
            and _not_excluded
            and (
                (self.conversation_all_channels and _in_allowed_guild)
                or _ch_id in self.conversation_channel_ids
            )
        )

        # Track conversation history
        if is_conversation:
            author_name = data.get("author", "unknown")
            self._add_to_history(int(channel_id), author_name, content)

        # Check if bot should respond in conversation channels
        convo_mode = None
        if is_conversation and not mentions_bot:
            convo_mode = self._should_respond(int(channel_id), content)
            if convo_mode is None:
                return

        # Only respond to DMs, mentions, or conversation channels
        if not is_dm and not mentions_bot and not is_conversation:
            return

        # Auth check
        ch_for_auth = None if is_conversation else channel_id
        if not self._check_auth(guild_id, author_id, ch_for_auth):
            return

        # Strip bot mention
        if mentions_bot and self._bot_id:
            content = content.replace(f"<@{self._bot_id}>", "").strip()

        # Format conversation context
        if convo_mode:
            ch_name = data.get("channel", "chat")
            content = self._format_conversation_context(int(channel_id), ch_name, convo_mode)

        # Download attachments
        media_paths: list[str] = []
        if data.get("attachments"):
            try:
                from pocketpaw.bus.media import build_media_hint, get_media_downloader

                downloader = get_media_downloader()
                names = []
                for att in data["attachments"]:
                    try:
                        path = await downloader.download_url(att["url"], att["filename"])
                        media_paths.append(path)
                        names.append(att["filename"])
                    except Exception as e:
                        logger.warning("Failed to download attachment: %s", e)
                if names:
                    content += build_media_hint(names)
            except Exception as e:
                logger.warning("Media download error: %s", e)

        if not content and not media_paths:
            return

        metadata: dict[str, Any] = {
            "username": data.get("author", ""),
            "guild_id": guild_id,
        }
        if is_conversation:
            metadata["conversation_mode"] = True

        # Start typing
        await self._send_command("typing_start", channel_id=channel_id)

        msg = InboundMessage(
            channel=Channel.DISCORD,
            sender_id=author_id,
            chat_id=channel_id,
            content=content,
            media=media_paths,
            metadata=metadata,
        )
        await self._publish_inbound(msg)

    async def _handle_slash_event(self, data: dict) -> None:
        command = data.get("command", "")
        args = data.get("args", {})
        channel_id = data.get("channel_id", "")
        user_id = data.get("user_id", "")
        guild_id = data.get("guild_id")
        interaction_token = data.get("interaction_token", "")

        if not self._check_auth(guild_id, user_id, channel_id):
            await self._send_command(
                "interaction_followup",
                interaction_token=interaction_token,
                content="Unauthorized.",
            )
            return

        # Handle /converse locally — requires admin or manage_guild
        if command == "converse":
            is_admin = data.get("is_admin", False)
            member_perms = data.get("member_permissions", 0)
            # Discord permission bit 0x20 = manage_guild
            has_manage_guild = bool(member_perms & 0x20)
            if not is_admin and not has_manage_guild:
                await self._send_command(
                    "interaction_followup",
                    interaction_token=interaction_token,
                    content="You need **Administrator** or **Manage Server** permission.",
                )
                return
            ch_id = int(channel_id)
            if ch_id in self.conversation_channel_ids:
                self.conversation_channel_ids.discard(ch_id)
                self._conversation_history.pop(ch_id, None)
                self._conversation_last_active.pop(ch_id, None)
                reply = "Conversation mode **disabled** for this channel."
            else:
                self.conversation_channel_ids.add(ch_id)
                reply = (
                    "Conversation mode **enabled** for this channel. "
                    f"I'll respond when mentioned or addressed as {self.bot_name}."
                )
            await self._send_command(
                "interaction_followup",
                interaction_token=interaction_token,
                content=reply,
            )
            return

        # Map slash commands to content
        if command == "paw":
            content = args.get("message", "")
        elif command == "resume":
            target = args.get("target", "")
            content = f"/resume {target}" if target else "/resume"
        elif command == "rename":
            title = args.get("title", "")
            content = f"/rename {title}" if title else "/rename"
        elif command == "backend":
            name = args.get("name", "")
            content = f"/backend {name}" if name else "/backend"
        elif command == "model":
            name = args.get("name", "")
            content = f"/model {name}" if name else "/model"
        elif command == "tools":
            name = args.get("name", "")
            content = f"/tools {name}" if name else "/tools"
        elif command in _SIMPLE_SLASH_COMMANDS:
            content = f"/{command}"
        else:
            content = f"/{command}"

        metadata: dict[str, Any] = {
            "username": data.get("user", ""),
            "guild_id": guild_id,
            "interaction_token": interaction_token,
        }

        msg = InboundMessage(
            channel=Channel.DISCORD,
            sender_id=user_id,
            chat_id=channel_id,
            content=content,
            metadata=metadata,
        )
        await self._publish_inbound(msg)

    # ── Send (OutboundMessage → discli) ─────────────────────────────

    async def send(self, message: OutboundMessage) -> None:
        if not self._proc:
            return

        try:
            # Skip [NO_RESPONSE]
            if (
                not message.is_stream_chunk
                and not message.is_stream_end
                and self._is_no_response(message.content)
            ):
                await self._send_command("typing_stop", channel_id=message.chat_id)
                return

            if message.is_stream_chunk:
                await self._handle_stream_chunk(message)
                return

            if message.is_stream_end:
                await self._handle_stream_end(message)
                return

            # Normal message
            await self._send_command("typing_stop", channel_id=message.chat_id)
            interaction_token = (message.metadata or {}).get("interaction_token")

            if interaction_token:
                await self._send_command(
                    "interaction_followup",
                    interaction_token=interaction_token,
                    content=message.content,
                )
            else:
                reply_to = message.reply_to
                if reply_to:
                    await self._send_command(
                        "reply",
                        channel_id=message.chat_id,
                        message_id=reply_to,
                        content=message.content,
                    )
                else:
                    await self._send_command(
                        "send",
                        channel_id=message.chat_id,
                        content=message.content,
                    )

            # Send media files
            for path in message.media or []:
                await self._send_command(
                    "send",
                    channel_id=message.chat_id,
                    content="",
                    files=[path],
                )

        except Exception as e:
            logger.error("Failed to send Discord message: %s", e)

    # ── Streaming ───────────────────────────────────────────────────

    async def _flush_stream_buffer(self, chat_id: str, message: OutboundMessage) -> None:
        """Flush the stream buffer by starting a Discord stream and sending buffered text."""
        buffered = self._stream_buffer.pop(chat_id, "")
        if not buffered:
            return

        interaction_token = (message.metadata or {}).get("interaction_token")
        result = await self._send_command(
            "stream_start",
            channel_id=chat_id,
            reply_to=message.reply_to,
            interaction_token=interaction_token,
        )
        stream_id = result.get("stream_id")
        if not stream_id:
            logger.error("Failed to start stream: %s", result)
            return
        self._active_streams[chat_id] = stream_id
        await self._send_command("stream_chunk", stream_id=stream_id, content=buffered)

    async def _handle_stream_chunk(self, message: OutboundMessage) -> None:
        chat_id = message.chat_id
        content = message.content

        # If stream already started, send chunks directly
        if chat_id in self._active_streams:
            stream_id = self._active_streams[chat_id]
            await self._send_command("stream_chunk", stream_id=stream_id, content=content)
            return

        # Buffer content until we can confirm it's not [NO_RESPONSE].
        # This prevents the marker from leaking when split across chunks.
        self._stream_buffer[chat_id] = self._stream_buffer.get(chat_id, "") + content
        buffered = self._stream_buffer[chat_id]

        # Already looks like [NO_RESPONSE] — keep buffering, will be caught at stream_end
        if self._is_no_response(buffered):
            return

        # Buffer is large enough that it can't be [NO_RESPONSE] — flush to Discord
        if len(buffered) >= _STREAM_BUFFER_THRESHOLD:
            await self._flush_stream_buffer(chat_id, message)

    async def _handle_stream_end(self, message: OutboundMessage) -> None:
        chat_id = message.chat_id
        buffered = self._stream_buffer.pop(chat_id, "")

        # Stream was never started — check if we should suppress or send the buffered content
        if chat_id not in self._active_streams:
            if not buffered.strip() or self._is_no_response(buffered):
                # Suppress [NO_RESPONSE] and stop typing
                await self._send_command("typing_stop", channel_id=chat_id)
                return

            # Short real response that never hit the buffer threshold — send as regular message
            await self._send_command("typing_stop", channel_id=chat_id)
            interaction_token = (message.metadata or {}).get("interaction_token")
            if interaction_token:
                await self._send_command(
                    "interaction_followup",
                    interaction_token=interaction_token,
                    content=buffered,
                )
            else:
                reply_to = message.reply_to
                if reply_to:
                    await self._send_command(
                        "reply",
                        channel_id=chat_id,
                        message_id=reply_to,
                        content=buffered,
                    )
                else:
                    await self._send_command("send", channel_id=chat_id, content=buffered)

            for path in message.media or []:
                await self._send_command("send", channel_id=chat_id, content="", files=[path])
            return

        # Stream was started — flush any remaining buffer and end normally
        if buffered:
            stream_id = self._active_streams.get(chat_id)
            if stream_id:
                await self._send_command("stream_chunk", stream_id=stream_id, content=buffered)

        stream_id = self._active_streams.pop(chat_id, None)
        if stream_id:
            await self._send_command("stream_end", stream_id=stream_id)

        # Send media files after stream
        for path in message.media or []:
            await self._send_command("send", channel_id=chat_id, content="", files=[path])

    # ── Conversation History ────────────────────────────────────────

    def _add_to_history(self, channel_id: int, author: str, content: str) -> None:
        if channel_id not in self._conversation_history:
            self._conversation_history[channel_id] = []
        history = self._conversation_history[channel_id]
        history.append({"author": author, "content": content})
        if len(history) > _CONVERSATION_HISTORY_SIZE:
            self._conversation_history[channel_id] = history[-_CONVERSATION_HISTORY_SIZE:]
        self._conversation_last_active[channel_id] = time.monotonic()

    def _should_respond(self, channel_id: int, latest: str) -> str | None:
        lower = latest.lower()
        name_lower = self.bot_name.lower()

        if name_lower in lower:
            return "addressed"

        history = self._conversation_history.get(channel_id, [])
        if len(history) >= 2:
            prev = history[-2]
            if prev["author"] == _BOT_AUTHOR_KEY:
                return "engaged"

        if lower.rstrip().endswith("?"):
            recent = history[-4:]
            for msg in recent:
                if msg["author"] == _BOT_AUTHOR_KEY:
                    return "engaged"

        return None

    def _format_conversation_context(self, channel_id: int, channel_name: str, mode: str) -> str:
        history = self._conversation_history.get(channel_id, [])
        if not history:
            return ""

        lines: list[str] = []
        for m in history:
            author = m["author"]
            display = self.bot_name if author == _BOT_AUTHOR_KEY else author
            lines.append(f"{display}: {m['content']}")

        # Trim to budget
        kept: list[str] = []
        budget = _CONVERSATION_CHAR_BUDGET
        for line in reversed(lines):
            if budget - len(line) < 0 and kept:
                break
            kept.append(line)
            budget -= len(line)
        kept.reverse()
        history_block = "Recent messages:\n" + "\n".join(kept)

        if mode == "addressed":
            return (
                f"[You are {self.bot_name} in a Discord group chat "
                f"#{channel_name}. Someone is talking to you. "
                f"Respond naturally and conversationally.]\n\n" + history_block
            )

        return (
            f"[You are {self.bot_name} in a Discord group chat "
            f"#{channel_name}. You've been part of this conversation.\n\n"
            f"IMPORTANT RULE: If the latest message is NOT directed at you, "
            f"NOT about a topic you were discussing, and NOT asking you a question, "
            f"you MUST reply with ONLY this exact text: {_NO_RESPONSE_MARKER}\n"
            f"Only respond if someone is clearly talking to you.]\n\n" + history_block
        )

    async def _eviction_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(_IDLE_CHANNEL_TTL // 2 or 300)
                now = time.monotonic()
                stale = [
                    cid
                    for cid, last in self._conversation_last_active.items()
                    if now - last > _IDLE_CHANNEL_TTL
                ]
                for cid in stale:
                    self._conversation_history.pop(cid, None)
                    self._conversation_last_active.pop(cid, None)
        except asyncio.CancelledError:
            pass

    @staticmethod
    def _is_no_response(text: str) -> bool:
        stripped = text.strip()
        if not stripped:
            return False
        if stripped in (_NO_RESPONSE_MARKER, f"{_NO_RESPONSE_MARKER}."):
            return True
        # Strip markdown formatting (backticks, bold, italic, underscores, periods)
        cleaned = stripped.strip("`*_ .\n\r\t")
        return cleaned == _NO_RESPONSE_MARKER
