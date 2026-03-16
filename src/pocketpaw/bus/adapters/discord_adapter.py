"""
Discord Channel Adapter.
Created: 2026-02-06
Modified: 2026-03-10 - admin gate on /info, setname sanitization,
    conversation history char budget, sentinel bot author key.
"""

import asyncio
import logging
import time
from collections import deque
from typing import Any

from pocketpaw.bus import BaseChannelAdapter, Channel, InboundMessage, OutboundMessage

logger = logging.getLogger(__name__)

DISCORD_MSG_LIMIT = 2000
_CONVERSATION_HISTORY_SIZE = 30
_CONVERSATION_CHAR_BUDGET = 12_000  # Max total chars in conversation context sent to LLM
_NO_RESPONSE_MARKER = "[NO_RESPONSE]"
_BOT_AUTHOR_KEY = "__bot__"
_MAX_BOT_NAME_LENGTH = 64
_IDLE_CHANNEL_TTL = 3600  # Evict conversation history for channels idle > 1 hour
_CODE_BLOCK_FILE_THRESHOLD = 800  # Upload code blocks larger than this as files


# Valid activity types for the /setstatus command
_ACTIVITY_TYPES = {"playing", "watching", "listening", "competing"}
_STATUS_TYPES = {"online", "idle", "dnd", "invisible"}


class DiscordAdapter(BaseChannelAdapter):
    """Adapter for Discord Bot API using discord.py."""

    def __init__(
        self,
        token: str,
        allowed_guild_ids: list[int] | None = None,
        allowed_user_ids: list[int] | None = None,
        allowed_channel_ids: list[int] | None = None,
        conversation_channel_ids: list[int] | None = None,
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
        self.conversation_channel_ids = conversation_channel_ids or []
        self.bot_name = bot_name or "Paw"
        self.status_type = status_type if status_type in _STATUS_TYPES else "online"
        self.activity_type = activity_type if activity_type in _ACTIVITY_TYPES else ""
        self.activity_text = activity_text or ""
        self._client: Any = None
        self._tree: Any = None
        self._bot_task: asyncio.Task | None = None
        self._buffers: dict[str, dict[str, Any]] = {}
        self._pending_interactions: dict[str, Any] = {}  # chat_id -> interaction
        self._source_messages: dict[str, Any] = {}  # chat_id -> discord.Message (for replies)
        self._typing_tasks: dict[str, asyncio.Task] = {}  # chat_id -> typing refresh task
        self._start_time: float = 0.0
        # Rolling message history for conversation channels (bounded per channel)
        self._conversation_history: dict[int, deque[dict[str, str]]] = {}
        self._conversation_last_active: dict[int, float] = {}
        self._eviction_task: asyncio.Task | None = None

    @property
    def channel(self) -> Channel:
        return Channel.DISCORD

    # ── Presence helpers ────────────────────────────────────────────────

    def _build_activity(self, discord_module: Any) -> Any | None:
        """Build a discord.Activity from current settings."""
        if not self.activity_type or not self.activity_text:
            return None
        type_map = {
            "playing": discord_module.ActivityType.playing,
            "watching": discord_module.ActivityType.watching,
            "listening": discord_module.ActivityType.listening,
            "competing": discord_module.ActivityType.competing,
        }
        activity_enum = type_map.get(self.activity_type)
        if not activity_enum:
            return None
        return discord_module.Activity(type=activity_enum, name=self.activity_text)

    def _build_status(self, discord_module: Any) -> Any:
        """Build a discord.Status from current settings."""
        status_map = {
            "online": discord_module.Status.online,
            "idle": discord_module.Status.idle,
            "dnd": discord_module.Status.dnd,
            "invisible": discord_module.Status.invisible,
        }
        return status_map.get(self.status_type, discord_module.Status.online)

    async def _update_presence(self) -> None:
        """Update bot presence with current status/activity settings."""
        if not self._client:
            return
        try:
            import discord

            activity = self._build_activity(discord)
            status = self._build_status(discord)
            await self._client.change_presence(activity=activity, status=status)
        except Exception as e:
            logger.warning("Failed to update Discord presence: %s", e)

    # ── Conversation channel helpers ────────────────────────────────────

    def _add_to_conversation_history(self, channel_id: int, author: str, content: str) -> None:
        """Append a message to the rolling conversation history."""
        if channel_id not in self._conversation_history:
            self._conversation_history[channel_id] = deque(maxlen=_CONVERSATION_HISTORY_SIZE)
        self._conversation_history[channel_id].append({"author": author, "content": content})
        self._conversation_last_active[channel_id] = time.monotonic()

    def _evict_idle_channels(self) -> None:
        """Remove conversation history for channels idle longer than the TTL."""
        now = time.monotonic()
        stale = [
            cid
            for cid, last in self._conversation_last_active.items()
            if now - last > _IDLE_CHANNEL_TTL
        ]
        for cid in stale:
            self._conversation_history.pop(cid, None)
            self._conversation_last_active.pop(cid, None)
        if stale:
            logger.debug("Evicted conversation history for %d idle channel(s)", len(stale))

    async def _eviction_loop(self) -> None:
        """Periodically evict idle channel histories."""
        try:
            while True:
                await asyncio.sleep(_IDLE_CHANNEL_TTL // 2 or 300)
                self._evict_idle_channels()
        except asyncio.CancelledError:
            pass

    def _should_respond(self, channel_id: int, latest: str) -> str | None:
        """Decide if the bot should respond. Returns response mode or None.

        Returns:
            'addressed' - bot name mentioned, must respond
            'engaged'   - bot recently active, likely should respond
            None        - skip, don't send to the LLM
        """
        lower = latest.lower()
        name_lower = self.bot_name.lower()

        # Bot name mentioned anywhere in the message -> always respond
        if name_lower in lower:
            return "addressed"

        # Check if message is a reply to the bot (bot was recent speaker)
        history = self._conversation_history.get(channel_id, [])
        if len(history) >= 2:
            prev = history[-2]
            if prev["author"] == _BOT_AUTHOR_KEY:
                return "engaged"

        # Question mark and bot was active in last 4 messages
        if lower.rstrip().endswith("?"):
            recent = list(history)[-4:]
            for msg in recent:
                if msg["author"] == _BOT_AUTHOR_KEY:
                    return "engaged"

        return None

    def _format_conversation_context(
        self, channel_id: int, channel_name: str, mode: str = "engaged"
    ) -> str:
        """Build a context string from recent conversation history."""
        history = self._conversation_history.get(channel_id, [])
        if not history:
            return ""

        # Scan user-authored messages through the injection scanner to prevent
        # prompt injection via conversation history. Bot messages are trusted.
        from pocketpaw.config import get_settings

        settings = get_settings()
        scanner = None
        if settings.injection_scan_enabled:
            from pocketpaw.security.injection_scanner import ThreatLevel, get_injection_scanner

            scanner = get_injection_scanner()

        # Build lines from most recent, staying within the character budget
        all_lines: list[str] = []
        for m in history:
            author = m["author"]
            content = m["content"]
            display_name = self.bot_name if author == _BOT_AUTHOR_KEY else author

            # Scan non-bot messages for injection attempts
            if scanner is not None and author != _BOT_AUTHOR_KEY:
                scan_result = scanner.scan(content, source=f"discord-history:{author}")
                if scan_result.threat_level == ThreatLevel.HIGH:
                    # Drop high-threat messages from context entirely
                    logger.warning(
                        "Dropped HIGH threat message from conversation history "
                        "(channel=%s, author=%s, patterns=%s)",
                        channel_id,
                        author,
                        scan_result.matched_patterns,
                    )
                    continue
                if scan_result.threat_level != ThreatLevel.NONE:
                    # Use sanitized content for medium/low threats
                    content = scan_result.sanitized_content

            all_lines.append(f"{display_name}: {content}")

        if not all_lines:
            return ""

        # Walk backwards, keeping lines that fit the budget
        kept: list[str] = []
        budget = _CONVERSATION_CHAR_BUDGET
        for line in reversed(all_lines):
            if budget - len(line) < 0 and kept:
                break
            kept.append(line)
            budget -= len(line)
        kept.reverse()
        history_block = "Recent messages:\n" + "\n".join(kept)

        if mode == "addressed":
            # Bot was directly called by name -> just respond, no skip option
            return (
                f"[You are {self.bot_name} in a Discord group chat "
                f"#{channel_name}. Someone is talking to you. "
                f"Respond naturally and conversationally.]\n\n" + history_block
            )

        # Engaged mode: bot was recently active, continue if relevant.
        # The [NO_RESPONSE] instruction must be extremely explicit for weaker models.
        return (
            f"[You are {self.bot_name} in a Discord group chat "
            f"#{channel_name}. You've been part of this conversation.\n\n"
            "IMPORTANT RULE: If the latest message is NOT directed at you, "
            "NOT about a topic you were discussing, and NOT asking you a question, "
            f"you MUST reply with ONLY this exact text: {_NO_RESPONSE_MARKER}\n"
            f"Do NOT add anything before or after {_NO_RESPONSE_MARKER}. "
            "Do NOT explain why. Just output that exact marker and nothing else.\n\n"
            "Only respond with a real answer if someone is clearly talking to you "
            f"or asking about something you ({self.bot_name}) can help with.]\n\n" + history_block
        )

    # ── Settings persistence ────────────────────────────────────────────

    def _save_restrictions(self) -> None:
        """Persist current restriction lists and presence to config."""
        try:
            from pocketpaw.config import Settings

            settings = Settings.load()
            settings.discord_allowed_guild_ids = self.allowed_guild_ids
            settings.discord_allowed_user_ids = self.allowed_user_ids
            settings.discord_allowed_channel_ids = self.allowed_channel_ids
            settings.discord_conversation_channel_ids = self.conversation_channel_ids
            settings.discord_bot_name = self.bot_name
            settings.discord_status_type = self.status_type
            settings.discord_activity_type = self.activity_type
            settings.discord_activity_text = self.activity_text
            settings.save()
        except Exception as e:
            logger.warning("Failed to persist Discord settings: %s", e)

    # ── Auth ────────────────────────────────────────────────────────────

    def _check_auth(self, guild: Any, user: Any, channel_id: int | None = None) -> bool:
        """Check if guild, user, and channel are authorized."""
        if self.allowed_guild_ids and guild and guild.id not in self.allowed_guild_ids:
            return False
        if self.allowed_user_ids and user.id not in self.allowed_user_ids:
            return False
        if self.allowed_channel_ids and channel_id and channel_id not in self.allowed_channel_ids:
            return False
        return True

    @staticmethod
    def _is_admin(interaction: Any) -> bool:
        """Check if the interaction user has administrator permission."""
        if not interaction.guild:
            return True  # DMs: treat as admin (they already passed user auth)
        perms = interaction.user.guild_permissions
        return perms.administrator

    # ── Start ───────────────────────────────────────────────────────────

    async def _on_start(self) -> None:
        """Initialize and start Discord bot."""
        if not self.token:
            raise RuntimeError("Discord bot token missing")

        try:
            import discord
        except ImportError:
            from pocketpaw.bus.adapters import auto_install

            auto_install("discord", "discord")
            import discord

        intents = discord.Intents.default()
        intents.message_content = True

        client = discord.Client(intents=intents)
        tree = discord.app_commands.CommandTree(client)

        adapter = self  # closure reference
        self._start_time = time.time()

        # ── Chat commands ───────────────────────────────────────────

        @tree.command(name="paw", description="Send a message to PocketPaw")
        async def paw_command(interaction: discord.Interaction, message: str):
            if not adapter._check_auth(interaction.guild, interaction.user, interaction.channel_id):
                await interaction.response.send_message("Unauthorized.", ephemeral=True)
                return

            await interaction.response.defer()
            chat_id = str(interaction.channel_id)
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
            if not adapter._check_auth(interaction.guild, interaction.user, interaction.channel_id):
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
        @discord.app_commands.describe(target="Session name or number to resume")
        async def resume_command(interaction: discord.Interaction, target: str | None = None):
            content = "/resume" if not target else f"/resume {target}"
            await _slash_to_inbound(interaction, content)

        @resume_command.autocomplete("target")
        async def _resume_autocomplete(
            interaction: discord.Interaction, current: str
        ) -> list[discord.app_commands.Choice[str]]:
            try:
                from pocketpaw.memory import get_memory_manager

                mgr = get_memory_manager()
                chat_id = str(interaction.channel_id)
                session_key = f"discord:{chat_id}"
                sessions = await mgr.list_sessions_for_chat(session_key)
                choices = []
                for s in sessions[:25]:  # Discord limit: 25 choices
                    title = s.get("title") or s.get("session_key", "untitled")
                    if current and current.lower() not in title.lower():
                        continue
                    display = title[:100]
                    choices.append(discord.app_commands.Choice(name=display, value=title))
                return choices
            except Exception:
                return []

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

        @tree.command(name="backend", description="Show or switch agent backend")
        @discord.app_commands.describe(name="Backend to switch to")
        async def backend_command(interaction: discord.Interaction, name: str | None = None):
            content = "/backend" if not name else f"/backend {name}"
            await _slash_to_inbound(interaction, content)

        @backend_command.autocomplete("name")
        async def _backend_autocomplete(
            interaction: discord.Interaction, current: str
        ) -> list[discord.app_commands.Choice[str]]:
            try:
                from pocketpaw.agents.registry import list_backends

                backends = list_backends()
                return [
                    discord.app_commands.Choice(name=b, value=b)
                    for b in backends
                    if not current or current.lower() in b.lower()
                ][:25]
            except Exception:
                return []

        @tree.command(name="backends", description="List available backends")
        async def backends_command(interaction: discord.Interaction):
            await _slash_to_inbound(interaction, "/backends")

        @tree.command(name="model", description="Show or switch model")
        async def model_command(interaction: discord.Interaction, name: str | None = None):
            content = "/model" if not name else f"/model {name}"
            await _slash_to_inbound(interaction, content)

        @tree.command(name="tools", description="Show or switch tool profile")
        @discord.app_commands.describe(profile="Tool profile to switch to")
        async def tools_command(interaction: discord.Interaction, profile: str | None = None):
            content = "/tools" if not profile else f"/tools {profile}"
            await _slash_to_inbound(interaction, content)

        @tools_command.autocomplete("profile")
        async def _tools_autocomplete(
            interaction: discord.Interaction, current: str
        ) -> list[discord.app_commands.Choice[str]]:
            try:
                from pocketpaw.tools.policy import TOOL_PROFILES

                return [
                    discord.app_commands.Choice(name=p, value=p)
                    for p in TOOL_PROFILES
                    if not current or current.lower() in p.lower()
                ][:25]
            except Exception:
                return []

        @tree.command(name="help", description="Show PocketPaw help")
        async def help_command(interaction: discord.Interaction):
            await _slash_to_inbound(interaction, "/help")

        @tree.command(name="kill", description="Cancel the current in-flight request")
        async def kill_command(interaction: discord.Interaction):
            if not adapter._check_auth(interaction.guild, interaction.user, interaction.channel_id):
                await interaction.response.send_message("Unauthorized.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            chat_id = str(interaction.channel_id)
            adapter._stop_typing(chat_id)
            adapter._pending_interactions[chat_id] = interaction
            msg = InboundMessage(
                channel=Channel.DISCORD,
                sender_id=str(interaction.user.id),
                chat_id=chat_id,
                content="/kill",
                metadata={
                    "username": str(interaction.user),
                    "guild_id": (str(interaction.guild_id) if interaction.guild_id else None),
                    "interaction_id": str(interaction.id),
                },
            )
            await adapter._publish_inbound(msg)

        # ── Utility commands ────────────────────────────────────────

        @tree.command(name="ping", description="Check bot latency")
        async def ping_command(interaction: discord.Interaction):
            latency_ms = round(client.latency * 1000)
            await interaction.response.send_message(
                f"Pong! Latency: **{latency_ms}ms**", ephemeral=True
            )

        @tree.command(name="info", description="Show PocketPaw bot info (admin only)")
        async def info_command(interaction: discord.Interaction):
            if not adapter._is_admin(interaction):
                await interaction.response.send_message(
                    "You need **Administrator** permission to use this.", ephemeral=True
                )
                return

            uptime_secs = int(time.time() - adapter._start_time)
            hours, remainder = divmod(uptime_secs, 3600)
            minutes, secs = divmod(remainder, 60)
            uptime_str = f"{hours}h {minutes}m {secs}s"

            try:
                from pocketpaw.config import Settings

                settings = Settings.load()
                backend_name = settings.agent_backend
                model_name = settings.model or "default"
            except Exception:
                backend_name = "unknown"
                model_name = "unknown"

            guild_count = len(client.guilds)
            lines = [
                f"**{adapter.bot_name} - Bot Info**",
                f"Backend: `{backend_name}`",
                f"Model: `{model_name}`",
                f"Uptime: {uptime_str}",
                f"Servers: {guild_count}",
                f"Latency: {round(client.latency * 1000)}ms",
            ]
            if adapter.status_type != "online":
                lines.append(f"Status: {adapter.status_type}")
            if adapter.activity_type and adapter.activity_text:
                lines.append(f"Activity: {adapter.activity_type} {adapter.activity_text}")
            await interaction.response.send_message("\n".join(lines), ephemeral=True)

        # ── Admin commands (require administrator permission) ───────

        @tree.command(name="setstatus", description="Set bot status and activity (admin only)")
        @discord.app_commands.describe(
            status="Bot status: online, idle, dnd, invisible",
            activity="Activity type: playing, watching, listening, competing",
            text="Activity text to display",
        )
        @discord.app_commands.choices(
            status=[
                discord.app_commands.Choice(name="Online", value="online"),
                discord.app_commands.Choice(name="Idle", value="idle"),
                discord.app_commands.Choice(name="Do Not Disturb", value="dnd"),
                discord.app_commands.Choice(name="Invisible", value="invisible"),
            ],
            activity=[
                discord.app_commands.Choice(name="Playing", value="playing"),
                discord.app_commands.Choice(name="Watching", value="watching"),
                discord.app_commands.Choice(name="Listening to", value="listening"),
                discord.app_commands.Choice(name="Competing in", value="competing"),
                discord.app_commands.Choice(name="None (clear activity)", value="none"),
            ],
        )
        async def setstatus_command(
            interaction: discord.Interaction,
            status: str | None = None,
            activity: str | None = None,
            text: str | None = None,
        ):
            if not adapter._is_admin(interaction):
                await interaction.response.send_message(
                    "You need **Administrator** permission to use this.", ephemeral=True
                )
                return

            changed = []
            if status and status in _STATUS_TYPES:
                adapter.status_type = status
                changed.append(f"Status: **{status}**")
            if activity == "none":
                adapter.activity_type = ""
                adapter.activity_text = ""
                changed.append("Activity: **cleared**")
            elif activity and activity in _ACTIVITY_TYPES:
                adapter.activity_type = activity
                if text:
                    adapter.activity_text = text
                changed.append(f"Activity: **{activity} {adapter.activity_text}**")
            elif text:
                if not adapter.activity_type:
                    await interaction.response.send_message(
                        "Set an `activity` type first (e.g. `/setstatus activity:playing "
                        "text:something`).",
                        ephemeral=True,
                    )
                    return
                adapter.activity_text = text
                changed.append(f"Activity text: **{text}**")

            if not changed:
                await interaction.response.send_message(
                    "No changes made. Use `/setstatus status:online "
                    "activity:playing text:something`.",
                    ephemeral=True,
                )
                return

            await adapter._update_presence()
            adapter._save_restrictions()
            await interaction.response.send_message(
                "Updated:\n" + "\n".join(changed), ephemeral=True
            )

        @tree.command(
            name="allowchannel",
            description="Add a channel to the bot's allowlist (admin only)",
        )
        @discord.app_commands.describe(
            channel="The channel to allow the bot in",
        )
        async def allowchannel_command(
            interaction: discord.Interaction, channel: discord.TextChannel
        ):
            if not adapter._is_admin(interaction):
                await interaction.response.send_message(
                    "You need **Administrator** permission to use this.", ephemeral=True
                )
                return

            if channel.id in adapter.allowed_channel_ids:
                await interaction.response.send_message(
                    f"{channel.mention} is already in the allowlist.", ephemeral=True
                )
                return

            adapter.allowed_channel_ids.append(channel.id)
            adapter._save_restrictions()
            await interaction.response.send_message(
                f"Added {channel.mention} to the allowlist. "
                f"Bot is now restricted to **{len(adapter.allowed_channel_ids)}** channel(s).",
                ephemeral=True,
            )

        @tree.command(
            name="blockchannel",
            description="Remove a channel from the bot's allowlist (admin only)",
        )
        @discord.app_commands.describe(
            channel="The channel to remove from the allowlist",
        )
        async def blockchannel_command(
            interaction: discord.Interaction, channel: discord.TextChannel
        ):
            if not adapter._is_admin(interaction):
                await interaction.response.send_message(
                    "You need **Administrator** permission to use this.", ephemeral=True
                )
                return

            if channel.id not in adapter.allowed_channel_ids:
                await interaction.response.send_message(
                    f"{channel.mention} is not in the allowlist.", ephemeral=True
                )
                return

            adapter.allowed_channel_ids.remove(channel.id)
            adapter._save_restrictions()
            count = len(adapter.allowed_channel_ids)
            note = (
                f"Bot is now restricted to **{count}** channel(s)."
                if count
                else "No channel restrictions active. Bot responds everywhere."
            )
            await interaction.response.send_message(
                f"Removed {channel.mention} from the allowlist. {note}", ephemeral=True
            )

        @tree.command(
            name="allowuser",
            description="Add a user to the bot's allowlist (admin only)",
        )
        @discord.app_commands.describe(user="The user to allow")
        async def allowuser_command(interaction: discord.Interaction, user: discord.User):
            if not adapter._is_admin(interaction):
                await interaction.response.send_message(
                    "You need **Administrator** permission to use this.", ephemeral=True
                )
                return

            if user.id in adapter.allowed_user_ids:
                await interaction.response.send_message(
                    f"{user.mention} is already in the allowlist.", ephemeral=True
                )
                return

            adapter.allowed_user_ids.append(user.id)
            adapter._save_restrictions()
            await interaction.response.send_message(
                f"Added {user.mention} to the user allowlist. "
                f"**{len(adapter.allowed_user_ids)}** user(s) allowed.",
                ephemeral=True,
            )

        @tree.command(
            name="blockuser",
            description="Remove a user from the bot's allowlist (admin only)",
        )
        @discord.app_commands.describe(user="The user to remove from the allowlist")
        async def blockuser_command(interaction: discord.Interaction, user: discord.User):
            if not adapter._is_admin(interaction):
                await interaction.response.send_message(
                    "You need **Administrator** permission to use this.", ephemeral=True
                )
                return

            if user.id not in adapter.allowed_user_ids:
                await interaction.response.send_message(
                    f"{user.mention} is not in the allowlist.", ephemeral=True
                )
                return

            adapter.allowed_user_ids.remove(user.id)
            adapter._save_restrictions()
            count = len(adapter.allowed_user_ids)
            note = (
                f"**{count}** user(s) in allowlist."
                if count
                else "No user restrictions active. All users can interact."
            )
            await interaction.response.send_message(
                f"Removed {user.mention} from the allowlist. {note}", ephemeral=True
            )

        @tree.command(
            name="restrictions",
            description="View current bot restrictions (admin only)",
        )
        async def restrictions_command(interaction: discord.Interaction):
            if not adapter._is_admin(interaction):
                await interaction.response.send_message(
                    "You need **Administrator** permission to use this.", ephemeral=True
                )
                return

            lines = ["**Bot Restrictions**\n"]

            # Guild restrictions
            if adapter.allowed_guild_ids:
                guild_names = []
                for gid in adapter.allowed_guild_ids:
                    g = client.get_guild(gid)
                    guild_names.append(f"`{g.name}` ({gid})" if g else f"`{gid}`")
                count = len(adapter.allowed_guild_ids)
                lines.append(f"**Guilds ({count}):** {', '.join(guild_names)}")
            else:
                lines.append("**Guilds:** No restrictions (all guilds)")

            # Channel restrictions
            if adapter.allowed_channel_ids:
                ch_mentions = []
                for cid in adapter.allowed_channel_ids:
                    ch = client.get_channel(cid)
                    ch_mentions.append(ch.mention if ch else f"`{cid}`")
                count = len(adapter.allowed_channel_ids)
                lines.append(f"**Channels ({count}):** {', '.join(ch_mentions)}")
            else:
                lines.append("**Channels:** No restrictions (all channels)")

            # User restrictions
            if adapter.allowed_user_ids:
                user_mentions = [f"<@{uid}>" for uid in adapter.allowed_user_ids]
                count = len(adapter.allowed_user_ids)
                lines.append(f"**Users ({count}):** {', '.join(user_mentions)}")
            else:
                lines.append("**Users:** No restrictions (all users)")

            # Conversation channels
            if adapter.conversation_channel_ids:
                conv_mentions = []
                for cid in adapter.conversation_channel_ids:
                    ch = client.get_channel(cid)
                    conv_mentions.append(ch.mention if ch else f"`{cid}`")
                count = len(adapter.conversation_channel_ids)
                lines.append(f"**Conversation channels ({count}):** {', '.join(conv_mentions)}")
            else:
                lines.append("**Conversation channels:** None")

            # Presence info
            lines.append("")
            lines.append(f"**Status:** {adapter.status_type}")
            if adapter.activity_type and adapter.activity_text:
                lines.append(f"**Activity:** {adapter.activity_type} {adapter.activity_text}")

            await interaction.response.send_message("\n".join(lines), ephemeral=True)

        @tree.command(
            name="converse",
            description="Toggle conversation mode in this channel (admin only)",
        )
        async def converse_command(interaction: discord.Interaction):
            if not adapter._is_admin(interaction):
                await interaction.response.send_message(
                    "You need **Administrator** permission to use this.",
                    ephemeral=True,
                )
                return

            if not interaction.channel_id:
                await interaction.response.send_message("Cannot use this in DMs.", ephemeral=True)
                return

            cid = interaction.channel_id
            if cid in adapter.conversation_channel_ids:
                adapter.conversation_channel_ids.remove(cid)
                adapter._conversation_history.pop(cid, None)
                adapter._conversation_last_active.pop(cid, None)
                adapter._save_restrictions()
                await interaction.response.send_message(
                    "Conversation mode **disabled** for this channel. "
                    "Bot will only respond to mentions and slash commands.",
                    ephemeral=True,
                )
            else:
                adapter.conversation_channel_ids.append(cid)
                adapter._save_restrictions()
                await interaction.response.send_message(
                    "Conversation mode **enabled** for this channel. "
                    "The bot will now participate naturally in the "
                    "conversation without needing mentions or commands.",
                    ephemeral=True,
                )

        @tree.command(
            name="setname",
            description="Set the bot's display name (admin only)",
        )
        @discord.app_commands.describe(name="The name the bot goes by")
        async def setname_command(interaction: discord.Interaction, name: str):
            if not adapter._is_admin(interaction):
                await interaction.response.send_message(
                    "You need **Administrator** permission to use this.",
                    ephemeral=True,
                )
                return

            # Sanitize: strip brackets to prevent prompt injection, enforce length cap
            sanitized = name.strip().replace("[", "").replace("]", "")
            sanitized = sanitized[:_MAX_BOT_NAME_LENGTH].strip()
            if not sanitized:
                await interaction.response.send_message(
                    "Invalid name. Provide a name without brackets.", ephemeral=True
                )
                return

            old_name = adapter.bot_name
            adapter.bot_name = sanitized
            adapter._save_restrictions()
            await interaction.response.send_message(
                f"Bot name changed from **{old_name}** to **{adapter.bot_name}**.",
                ephemeral=True,
            )

        # ── Events ──────────────────────────────────────────────────

        @client.event
        async def on_ready():
            logger.info(f"Discord bot connected as {client.user}")
            # Set presence
            await adapter._update_presence()
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
                # Track bot's own messages using a sentinel key so _should_respond
                # still works correctly even if /setname changes the display name.
                ch_id = message.channel.id
                if ch_id in adapter.conversation_channel_ids:
                    adapter._add_to_conversation_history(ch_id, _BOT_AUTHOR_KEY, message.content)
                return

            is_dm = message.guild is None
            is_mention = client.user in message.mentions if message.mentions else False
            is_conversation = not is_dm and message.channel.id in adapter.conversation_channel_ids

            # Always track messages in conversation channels
            if is_conversation:
                display = message.author.display_name or str(message.author)
                adapter._add_to_conversation_history(message.channel.id, display, message.content)

            # For conversation channels, check if bot should respond
            convo_mode: str | None = None
            if is_conversation and not is_mention:
                convo_mode = adapter._should_respond(message.channel.id, message.content)
                if convo_mode is None:
                    return

            # Only respond to DMs, mentions, or conversation channels
            if not is_dm and not is_mention and not is_conversation:
                return

            # Conversation channels bypass the allowed_channel_ids check because
            # they have their own explicit allowlist (conversation_channel_ids).
            # Passing None for channel_id skips only the channel restriction;
            # guild and user restrictions still apply.
            ch_for_auth = None if is_conversation else (message.channel.id if not is_dm else None)
            if not adapter._check_auth(message.guild, message.author, ch_for_auth):
                return

            content = message.content
            # Strip the bot mention from the message
            if client.user and is_mention:
                content = content.replace(f"<@{client.user.id}>", "").strip()

            # For conversation channels, wrap with context
            if convo_mode:
                ch_name = getattr(message.channel, "name", "chat")
                content = adapter._format_conversation_context(
                    message.channel.id, ch_name, mode=convo_mode
                )

            # Download attachments
            media_paths: list[str] = []
            if message.attachments:
                try:
                    from pocketpaw.bus.media import (
                        build_media_hint,
                        get_media_downloader,
                    )

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
            metadata: dict[str, Any] = {
                "username": str(message.author),
                "display_name": message.author.display_name or str(message.author),
                "guild_id": str(message.guild.id) if message.guild else None,
            }
            # Include user's roles for context (guild channels only)
            if message.guild and hasattr(message.author, "roles"):
                role_names = [r.name for r in message.author.roles if r.name != "@everyone"]
                if role_names:
                    metadata["roles"] = role_names
            if is_conversation:
                metadata["conversation_mode"] = True

            msg = InboundMessage(
                channel=Channel.DISCORD,
                sender_id=str(message.author.id),
                chat_id=chat_id,
                content=content,
                media=media_paths,
                metadata=metadata,
            )
            adapter._source_messages[chat_id] = message
            adapter._start_typing(chat_id)

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

        # Give the bot a moment to connect -- surface immediate auth errors
        await asyncio.sleep(2)
        if not self._running:
            raise RuntimeError("Discord bot failed to connect -- check token and intents")
        if client.is_closed():
            self._running = False
            raise RuntimeError("Discord bot closed immediately -- check token and intents")

        self._eviction_task = asyncio.create_task(self._eviction_loop())
        logger.info("Discord Adapter started")

    async def _on_stop(self) -> None:
        """Stop Discord bot."""
        # Cancel all typing indicator tasks
        for task in self._typing_tasks.values():
            task.cancel()
        self._typing_tasks.clear()
        if self._eviction_task and not self._eviction_task.done():
            self._eviction_task.cancel()
            try:
                await self._eviction_task
            except asyncio.CancelledError:
                pass
        if self._client and not self._client.is_closed():
            await self._client.close()
        if self._bot_task and not self._bot_task.done():
            self._bot_task.cancel()
            try:
                await self._bot_task
            except asyncio.CancelledError:
                pass
        self._conversation_history.clear()
        self._conversation_last_active.clear()
        logger.info("Discord Adapter stopped")

    def _is_no_response(self, text: str) -> bool:
        """Check if the AI decided not to respond (conversation mode)."""
        stripped = text.strip()
        # Exact match
        if stripped in (_NO_RESPONSE_MARKER, f"{_NO_RESPONSE_MARKER}."):
            return True
        # Sometimes the AI wraps it in backticks or adds minor decoration
        clean = stripped.strip("`*_ .")
        return clean == _NO_RESPONSE_MARKER

    # ── Typing indicator ─────────────────────────────────────────────

    def _start_typing(self, chat_id: str) -> None:
        """Start a background typing indicator loop for a channel."""
        if chat_id in self._typing_tasks:
            return

        async def _typing_loop() -> None:
            try:
                channel = self._client.get_channel(int(chat_id))
                if not channel:
                    return
                while True:
                    try:
                        await channel.typing()
                    except Exception:
                        pass
                    # Discord typing lasts 10s, refresh at 8s
                    await asyncio.sleep(8)
            except asyncio.CancelledError:
                pass

        self._typing_tasks[chat_id] = asyncio.create_task(_typing_loop())

    def _stop_typing(self, chat_id: str) -> None:
        """Stop the typing indicator for a channel."""
        task = self._typing_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()

    # ── Presence ──────────────────────────────────────────────────────

    async def _update_activity_from_sessions(self) -> None:
        """Update bot activity to reflect current session count."""
        if not self._client:
            return
        try:
            import discord

            # Count active buffers as a proxy for active sessions
            active = len(self._typing_tasks) + len(self._buffers)
            if active > 0:
                activity = discord.Activity(
                    type=discord.ActivityType.playing,
                    name=f"Helping {active} user{'s' if active != 1 else ''}",
                )
            elif self.activity_type and self.activity_text:
                activity = self._build_activity(discord)
            else:
                activity = None
            status = self._build_status(discord)
            await self._client.change_presence(activity=activity, status=status)
        except Exception:
            pass

    # ── Code block extraction ─────────────────────────────────────────

    @staticmethod
    def _extract_large_code_blocks(text: str) -> tuple[str, list[tuple[str, str, str]]]:
        """Extract large code blocks from text, replacing them with placeholders.

        Returns (cleaned_text, [(filename, language, code), ...]).
        """
        import re

        pattern = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)
        files: list[tuple[str, str, str]] = []
        counter = 0

        def _replacer(match: re.Match) -> str:
            nonlocal counter
            lang = match.group(1) or "txt"
            code = match.group(2).strip()
            if len(code) < _CODE_BLOCK_FILE_THRESHOLD:
                return match.group(0)  # Keep small blocks inline
            counter += 1
            ext = {"python": "py", "javascript": "js", "typescript": "ts", "bash": "sh"}.get(
                lang, lang or "txt"
            )
            filename = f"code{counter}.{ext}" if counter > 1 else f"code.{ext}"
            files.append((filename, lang, code))
            return f"(see attached `{filename}`)"

        cleaned = pattern.sub(_replacer, text)
        return cleaned, files

    async def send(self, message: OutboundMessage) -> None:
        """Send message to Discord channel."""
        if not self._client:
            return

        try:
            # Skip [NO_RESPONSE] from conversation mode
            if (
                not message.is_stream_chunk
                and not message.is_stream_end
                and self._is_no_response(message.content)
            ):
                self._pending_interactions.pop(message.chat_id, None)

                self._source_messages.pop(message.chat_id, None)
                self._stop_typing(message.chat_id)
                return

            if message.is_stream_chunk:
                await self._handle_stream_chunk(message)
                return

            if message.is_stream_end:
                await self._flush_stream_buffer(message.chat_id)
                # Send any attached media files
                for path in message.media or []:
                    await self._send_media_file(message.chat_id, path)
                return

            # Normal (non-streaming) message
            self._stop_typing(message.chat_id)
            interaction = self._pending_interactions.pop(message.chat_id, None)
            if interaction:
                try:
                    for chunk in self._split_message(message.content):
                        await interaction.followup.send(chunk)
                except Exception:
                    logger.warning("Interaction expired, falling back to channel.send")
                    await self._send_to_channel(message.chat_id, message.content)
            else:
                await self._send_to_channel(message.chat_id, message.content)

        except Exception as e:
            logger.error(f"Failed to send Discord message: {e}")

    async def _send_to_channel(self, chat_id: str, text: str) -> None:
        """Send text to a channel, replying to the source message if available."""
        source_msg = self._source_messages.pop(chat_id, None)
        channel = self._client.get_channel(int(chat_id))
        if not channel:
            return
        chunks = self._split_message(text)
        for i, chunk in enumerate(chunks):
            if i == 0 and source_msg:
                try:
                    await source_msg.reply(chunk, mention_author=False)
                except Exception:
                    await channel.send(chunk)
            else:
                await channel.send(chunk)

    # --- Media sending ---

    async def _send_media_file(self, chat_id: str, file_path: str) -> None:
        """Send a media file to a Discord channel."""
        import os

        if not self._client or not os.path.isfile(file_path):
            return

        try:
            import discord

            channel = self._client.get_channel(int(chat_id))
            if channel:
                await channel.send(file=discord.File(file_path))
        except Exception as e:
            logger.warning("Failed to send Discord media file: %s", e)

    async def _send_code_files(self, channel: Any, code_files: list[tuple[str, str, str]]) -> None:
        """Upload extracted code blocks as file attachments."""
        if not code_files:
            return
        try:
            import io

            import discord

            for filename, _lang, code in code_files:
                buf = io.BytesIO(code.encode("utf-8"))
                await channel.send(file=discord.File(buf, filename=filename))
        except Exception as e:
            logger.warning("Failed to send code file attachment: %s", e)

    # --- Stream buffering ---

    async def _handle_stream_chunk(self, message: OutboundMessage) -> None:
        chat_id = message.chat_id
        content = message.content
        is_convo = int(chat_id) in self.conversation_channel_ids

        # Stop typing once we start streaming content
        self._stop_typing(chat_id)

        if chat_id not in self._buffers:
            if is_convo:
                # Conversation mode: buffer silently, no placeholder
                self._buffers[chat_id] = {
                    "discord_message": None,
                    "text": content,
                    "last_update": asyncio.get_running_loop().time(),
                    "conversation_mode": True,
                }
                return

            # Normal mode: send initial placeholder, reply to source if available
            sent_msg = None
            interaction = self._pending_interactions.pop(chat_id, None)
            if interaction:
                try:
                    sent_msg = await interaction.followup.send("...", wait=True)
                except Exception:
                    logger.warning("Interaction expired, falling back to channel reply")
                    interaction = None

            if sent_msg is None:
                channel = self._client.get_channel(int(chat_id))
                if not channel:
                    return
                source_msg = self._source_messages.pop(chat_id, None)
                if source_msg:
                    try:
                        sent_msg = await source_msg.reply("...", mention_author=False)
                    except Exception:
                        sent_msg = await channel.send("...")
                else:
                    sent_msg = await channel.send("...")

            self._buffers[chat_id] = {
                "discord_message": sent_msg,
                "text": content,
                "last_update": asyncio.get_running_loop().time(),
                "conversation_mode": False,
            }
        else:
            self._buffers[chat_id]["text"] += content

        buf = self._buffers[chat_id]
        # Don't do periodic edits for conversation mode (no message to edit)
        if buf.get("conversation_mode"):
            return
        # Already overflowed past the message limit, skip edits until flush
        if buf.get("overflow_at"):
            return

        now = asyncio.get_running_loop().time()
        if now - buf["last_update"] > 1.5:
            await self._update_buffer_message(chat_id)
            buf["last_update"] = now

    async def _flush_stream_buffer(self, chat_id: str) -> None:
        self._pending_interactions.pop(chat_id, None)
        self._stop_typing(chat_id)

        if chat_id not in self._buffers:
            self._source_messages.pop(chat_id, None)
            return

        buf = self._buffers[chat_id]
        text = buf["text"].strip()

        # If AI decided not to respond, silently discard
        if self._is_no_response(text) or not text:
            if buf["discord_message"]:
                try:
                    await buf["discord_message"].delete()
                except Exception:
                    pass
            del self._buffers[chat_id]
            self._source_messages.pop(chat_id, None)
            return

        # Extract large code blocks as file attachments
        cleaned_text, code_files = self._extract_large_code_blocks(text)

        # Conversation mode: send the full accumulated text now, reply to source
        if buf.get("conversation_mode") and buf["discord_message"] is None:
            source_msg = self._source_messages.pop(chat_id, None)
            channel = self._client.get_channel(int(chat_id))
            if channel:
                send_text = cleaned_text if code_files else text
                chunks = self._split_message(send_text)
                for i, chunk in enumerate(chunks):
                    if i == 0 and source_msg:
                        try:
                            await source_msg.reply(chunk, mention_author=False)
                        except Exception:
                            await channel.send(chunk)
                    else:
                        await channel.send(chunk)
                await self._send_code_files(channel, code_files)
            del self._buffers[chat_id]
            return

        # Normal mode: final edit (with code files extracted if any)
        self._source_messages.pop(chat_id, None)
        if code_files:
            buf["text"] = cleaned_text
        await self._update_buffer_message(chat_id)
        if code_files:
            channel = self._client.get_channel(int(chat_id))
            if channel:
                await self._send_code_files(channel, code_files)
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
            if len(text) <= DISCORD_MSG_LIMIT:
                await discord_msg.edit(content=text)
            else:
                # Edit the original message with first chunk, send overflow separately
                await discord_msg.edit(content=text[:DISCORD_MSG_LIMIT])
                channel = self._client.get_channel(int(chat_id))
                if channel:
                    for chunk in self._split_message(text[DISCORD_MSG_LIMIT:]):
                        await channel.send(chunk)
                # Mark overflow so periodic edits stop (avoids re-sending overflow)
                buf["overflow_at"] = DISCORD_MSG_LIMIT
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
