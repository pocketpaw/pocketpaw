"""
Discord Channel Adapter.
Created: 2026-02-06
"""

import asyncio
import logging
from typing import Any

from pocketclaw.bus import BaseChannelAdapter, Channel, InboundMessage, OutboundMessage

logger = logging.getLogger(__name__)

DISCORD_MSG_LIMIT = 2000


class DiscordAdapter(BaseChannelAdapter):
    """Adapter for Discord Bot API using discord.py."""

    def __init__(
        self,
        token: str,
        allowed_guild_ids: list[int] | None = None,
        allowed_user_ids: list[int] | None = None,
    ):
        super().__init__()
        self.token = token
        self.allowed_guild_ids = allowed_guild_ids or []
        self.allowed_user_ids = allowed_user_ids or []
        self._client: Any = None
        self._bot_task: asyncio.Task | None = None
        self._buffers: dict[str, dict[str, Any]] = {}
        # Track pending slash command interactions so responses go through followup
        self._pending_interactions: dict[str, Any] = {}  # chat_id -> interaction

    @property
    def channel(self) -> Channel:
        return Channel.DISCORD

    async def _on_start(self) -> None:
        """Initialize and start Discord bot."""
        if not self.token:
            raise RuntimeError("Discord bot token missing")

        try:
            import discord
        except ImportError:
            from pocketclaw.bus.adapters import auto_install

            auto_install("discord", "discord")
            import discord

        intents = discord.Intents.default()
        intents.message_content = True

        client = discord.Client(intents=intents)
        tree = discord.app_commands.CommandTree(client)

        adapter = self  # closure reference

        @tree.command(name="paw", description="Send a message to PocketPaw")
        async def paw_command(interaction: discord.Interaction, message: str):
            if not adapter._check_auth(interaction.guild, interaction.user):
                await interaction.response.send_message("Unauthorized.", ephemeral=True)
                return

            await interaction.response.defer()
            chat_id = str(interaction.channel_id)
            # Store interaction so send() uses followup instead of channel.send()
            adapter._pending_interactions[chat_id] = interaction
            msg = InboundMessage(
                channel=Channel.DISCORD,
                sender_id=str(interaction.user.id),
                chat_id=chat_id,
                content=message,
                metadata={
                    "username": str(interaction.user),
                    "guild_id": str(interaction.guild_id) if interaction.guild_id else None,
                    "interaction_id": str(interaction.id),
                },
            )
            await adapter._publish_inbound(msg)

        async def _slash_to_inbound(interaction: discord.Interaction, content: str):
            """Helper: defer interaction, store it, and publish as InboundMessage."""
            if not adapter._check_auth(interaction.guild, interaction.user):
                await interaction.response.send_message("Unauthorized.", ephemeral=True)
                return
            await interaction.response.defer()
            chat_id = str(interaction.channel_id)
            adapter._pending_interactions[chat_id] = interaction
            msg = InboundMessage(
                channel=Channel.DISCORD,
                sender_id=str(interaction.user.id),
                chat_id=chat_id,
                content=content,
                metadata={
                    "username": str(interaction.user),
                    "guild_id": (str(interaction.guild_id) if interaction.guild_id else None),
                    "interaction_id": str(interaction.id),
                },
            )
            await adapter._publish_inbound(msg)

        @tree.command(name="new", description="Start a fresh PocketPaw conversation")
        async def new_command(interaction: discord.Interaction):
            await _slash_to_inbound(interaction, "/new")

        @tree.command(name="sessions", description="List your conversation sessions")
        async def sessions_command(interaction: discord.Interaction):
            await _slash_to_inbound(interaction, "/sessions")

        @tree.command(name="resume", description="Resume a previous conversation session")
        async def resume_command(interaction: discord.Interaction, target: str | None = None):
            content = "/resume" if not target else f"/resume {target}"
            await _slash_to_inbound(interaction, content)

        @tree.command(name="clear", description="Clear the current session history")
        async def clear_command(interaction: discord.Interaction):
            await _slash_to_inbound(interaction, "/clear")

        @tree.command(name="rename", description="Rename the current session")
        async def rename_command(interaction: discord.Interaction, title: str):
            await _slash_to_inbound(interaction, f"/rename {title}")

        @tree.command(name="status", description="Show current session info")
        async def status_command(interaction: discord.Interaction):
            await _slash_to_inbound(interaction, "/status")

        @tree.command(name="delete", description="Delete the current session")
        async def delete_command(interaction: discord.Interaction):
            await _slash_to_inbound(interaction, "/delete")

        @tree.command(name="help", description="Show PocketPaw help")
        async def help_command(interaction: discord.Interaction):
            await _slash_to_inbound(interaction, "/help")

        @client.event
        async def on_ready():
            logger.info(f"Discord bot connected as {client.user}")
            # Sync slash commands per-guild for instant availability
            for guild in client.guilds:
                if adapter.allowed_guild_ids and guild.id not in adapter.allowed_guild_ids:
                    continue
                try:
                    tree.copy_global_to(guild=guild)
                    await tree.sync(guild=guild)
                except Exception as e:
                    logger.warning(f"Failed to sync commands to guild {guild.name}: {e}")

        @client.event
        async def on_message(message: discord.Message):
            if message.author == client.user:
                return

            # Only respond to DMs or mentions
            is_dm = message.guild is None
            is_mention = client.user in message.mentions if message.mentions else False

            if not is_dm and not is_mention:
                return

            if not adapter._check_auth(message.guild, message.author):
                return

            content = message.content
            # Strip the bot mention from the message
            if client.user and is_mention:
                content = content.replace(f"<@{client.user.id}>", "").strip()

            # Download attachments
            media_paths: list[str] = []
            if message.attachments:
                try:
                    from pocketclaw.bus.media import build_media_hint, get_media_downloader

                    downloader = get_media_downloader()
                    names = []
                    for att in message.attachments:
                        try:
                            path = await downloader.download_url(
                                att.url, att.filename, att.content_type
                            )
                            media_paths.append(path)
                            names.append(att.filename)
                        except Exception as e:
                            logger.warning("Failed to download Discord attachment: %s", e)
                    if names:
                        content += build_media_hint(names)
                except Exception as e:
                    logger.warning("Discord media download error: %s", e)

            if not content and not media_paths:
                return

            chat_id = str(message.channel.id)
            msg = InboundMessage(
                channel=Channel.DISCORD,
                sender_id=str(message.author.id),
                chat_id=chat_id,
                content=content,
                media=media_paths,
                metadata={
                    "username": str(message.author),
                    "guild_id": str(message.guild.id) if message.guild else None,
                },
            )
            await adapter._publish_inbound(msg)

        self._client = client
        self._tree = tree

        # Start the bot and wait briefly for the connection to establish
        async def _run_bot():
            try:
                await client.start(self.token)
            except Exception as e:
                logger.error(f"Discord bot connection failed: {e}")
                self._running = False

        self._bot_task = asyncio.create_task(_run_bot())

        # Give the bot a moment to connect — surface immediate auth errors
        await asyncio.sleep(2)
        if not self._running:
            raise RuntimeError("Discord bot failed to connect — check token and intents")
        if client.is_closed():
            self._running = False
            raise RuntimeError("Discord bot closed immediately — check token and intents")

        logger.info("Discord Adapter started")

    async def _on_stop(self) -> None:
        """Stop Discord bot."""
        if self._client and not self._client.is_closed():
            await self._client.close()
        if self._bot_task and not self._bot_task.done():
            self._bot_task.cancel()
            try:
                await self._bot_task
            except asyncio.CancelledError:
                pass
        logger.info("Discord Adapter stopped")

    def _check_auth(self, guild: Any, user: Any) -> bool:
        """Check if guild and user are authorized."""
        if self.allowed_guild_ids and guild and guild.id not in self.allowed_guild_ids:
            return False
        if self.allowed_user_ids and user.id not in self.allowed_user_ids:
            return False
        return True

    async def send(self, message: OutboundMessage) -> None:
        """Send message to Discord channel."""
        if not self._client:
            return

        try:
            if message.is_stream_chunk:
                await self._handle_stream_chunk(message)
                return

            if message.is_stream_end:
                await self._flush_stream_buffer(message.chat_id)
                return

            # Normal (non-streaming) message
            interaction = self._pending_interactions.pop(message.chat_id, None)
            if interaction:
                # Respond via interaction followup (replaces "thinking...")
                for chunk in self._split_message(message.content):
                    await interaction.followup.send(chunk)
            else:
                channel = self._client.get_channel(int(message.chat_id))
                if channel:
                    for chunk in self._split_message(message.content):
                        await channel.send(chunk)

        except Exception as e:
            logger.error(f"Failed to send Discord message: {e}")

    # --- Stream buffering ---

    async def _handle_stream_chunk(self, message: OutboundMessage) -> None:
        chat_id = message.chat_id
        content = message.content

        if chat_id not in self._buffers:
            # First chunk — send initial placeholder message
            interaction = self._pending_interactions.pop(chat_id, None)
            if interaction:
                # Use followup.send so it replaces "thinking..." from defer()
                sent_msg = await interaction.followup.send("...", wait=True)
            else:
                channel = self._client.get_channel(int(chat_id))
                if not channel:
                    return
                sent_msg = await channel.send("...")
            self._buffers[chat_id] = {
                "discord_message": sent_msg,
                "text": content,
                "last_update": asyncio.get_event_loop().time(),
            }
        else:
            self._buffers[chat_id]["text"] += content

        now = asyncio.get_event_loop().time()
        buf = self._buffers[chat_id]
        if now - buf["last_update"] > 1.5:
            await self._update_buffer_message(chat_id)
            buf["last_update"] = now

    async def _flush_stream_buffer(self, chat_id: str) -> None:
        # Clean up any unused interaction if stream ends without chunks
        self._pending_interactions.pop(chat_id, None)
        if chat_id in self._buffers:
            await self._update_buffer_message(chat_id)
            del self._buffers[chat_id]

    async def _update_buffer_message(self, chat_id: str) -> None:
        buf = self._buffers.get(chat_id)
        if not buf:
            return
        text = buf["text"]
        if not text.strip():
            return
        try:
            discord_msg = buf["discord_message"]
            # If text exceeds limit, edit with truncated and send overflow as new messages
            if len(text) <= DISCORD_MSG_LIMIT:
                await discord_msg.edit(content=text)
            else:
                await discord_msg.edit(content=text[:DISCORD_MSG_LIMIT])
                channel = self._client.get_channel(int(chat_id))
                if channel:
                    for chunk in self._split_message(text[DISCORD_MSG_LIMIT:]):
                        await channel.send(chunk)
        except Exception as e:
            logger.warning(f"Failed to update Discord message: {e}")

    @staticmethod
    def _split_message(text: str) -> list[str]:
        """Split text into chunks respecting the Discord 2000-char limit."""
        if not text:
            return []
        chunks = []
        while len(text) > DISCORD_MSG_LIMIT:
            # Try to split at a newline
            split_at = text.rfind("\n", 0, DISCORD_MSG_LIMIT)
            if split_at == -1:
                split_at = DISCORD_MSG_LIMIT
            chunks.append(text[:split_at])
            text = text[split_at:].lstrip("\n")
        if text:
            chunks.append(text)
        return chunks
