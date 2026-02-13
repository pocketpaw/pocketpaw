"""
Telegram Channel Adapter.
Created: 2026-02-02
"""

import asyncio
import logging
from typing import Any

try:
    from telegram import Update
    from telegram.ext import (
        Application,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
except ImportError as _exc:
    raise ImportError(
        "'python-telegram-bot' is required but not installed. "
        "Install it with: pip install 'pocketpaw[telegram]'"
    ) from _exc

from pocketclaw.bus import (
    BaseChannelAdapter,
    Channel,
    InboundMessage,
    OutboundMessage,
)
from pocketclaw.bus.format import convert_markdown

logger = logging.getLogger(__name__)


class TelegramAdapter(BaseChannelAdapter):
    """Adapter for Telegram Bot API."""

    def __init__(self, token: str, allowed_user_id: int | None = None):
        super().__init__()
        self.token = token
        self.allowed_user_id = allowed_user_id
        self.app: Application | None = None

    @property
    def channel(self) -> Channel:
        return Channel.TELEGRAM

    async def _on_start(self) -> None:
        """Initialize and start Telegram bot."""
        if not self.token:
            raise RuntimeError("Telegram bot token missing")

        builder = Application.builder().token(self.token)
        self.app = builder.build()

        # Add Handlers
        self.app.add_handler(CommandHandler("start", self._handle_start))
        _cmds = ("new", "sessions", "resume", "clear", "rename", "status", "delete", "help")
        for cmd_name in _cmds:
            self.app.add_handler(CommandHandler(cmd_name, self._handle_command))
        media_filter = (
            filters.PHOTO
            | filters.Document.ALL
            | filters.AUDIO
            | filters.VIDEO
            | filters.VOICE
            | filters.VIDEO_NOTE
        )
        self.app.add_handler(
            MessageHandler((filters.TEXT | media_filter) & ~filters.COMMAND, self._handle_message)
        )

        # Initialize
        await self.app.initialize()
        await self.app.start()

        # Start polling (non-blocking)
        await self.app.updater.start_polling(drop_pending_updates=True)

        # Set bot menu commands for autocomplete in Telegram UI
        try:
            from telegram import BotCommand

            await self.app.bot.set_my_commands(
                [
                    BotCommand("new", "Start a fresh conversation"),
                    BotCommand("sessions", "List your conversation sessions"),
                    BotCommand("resume", "Resume a previous session"),
                    BotCommand("clear", "Clear session history"),
                    BotCommand("rename", "Rename the current session"),
                    BotCommand("status", "Show session info"),
                    BotCommand("delete", "Delete the current session"),
                    BotCommand("help", "Show available commands"),
                ]
            )
        except Exception as e:
            logger.warning("Failed to set Telegram bot commands: %s", e)

        logger.info("📡 Telegram Adapter started")

    async def _on_stop(self) -> None:
        """Stop Telegram bot."""
        if self.app:
            if self.app.updater.running:
                await self.app.updater.stop()
            if self.app.running:
                await self.app.stop()
            await self.app.shutdown()
            logger.info("🛑 Telegram Adapter stopped")

    @staticmethod
    def _parse_chat_id(raw_chat_id: str) -> tuple[str, int | None]:
        """Parse a chat_id that may contain a topic suffix.

        Returns (real_chat_id, topic_id_or_None).
        """
        if ":topic:" in raw_chat_id:
            parts = raw_chat_id.split(":topic:")
            return parts[0], int(parts[1])
        return raw_chat_id, None

    async def send(self, message: OutboundMessage) -> None:
        """Send message to Telegram."""
        if not self.app:
            return

        chat_id = message.chat_id

        # Basic security check - though AgentLoop should handle it via logic,
        # the adapter enforces the channel pipe.
        # If message.chat_id matches our user, we send.

        try:
            # Handle stream chunks? Telegram doesn't support streaming well.
            # We should probably accumulate or ignore 'is_stream_chunk' unless we do live editing.
            # For simplicity in Phase 2, we IGNORE stream chunks and only send the final message?
            # OR we implement a simple buffer.
            # `AgentLoop` sends "is_stream_chunk=True" for deltas, and "is_stream_end=True" (empty) at end.
            # BUT, it DOESN'T validly send the "Full" message as a separate event in current loop implementation.
            # Loop implementation:
            #   - Yields chunks.
            #   - DOES NOT yield full text outbound message.
            #   - Stores full text in memory.
            # Wait, `AgentLoop` sends `OutboundMessage(..., is_stream_chunk=True)`
            # If I ignore chunks, I get NOTHING.
            # So I MUST handle tokens.
            # Telegram Rate Limits will kill us if we edit message for every token.
            # Strategy: Accumulate tokens and edit message every 1-2 seconds.
            pass  # placeholder comment

            if message.is_stream_chunk:
                # TODO: Implement "Typing..." or smart buffering.
                # For now, just print to console? No, user needs to see it.
                # Robust solution:
                # 1. On first chunk, send a "..." message.
                # 2. Buffer chunks.
                # 3. Every 2 seconds, edit the message with buffer.
                # 4. On stream_end, final edit.

                # Given strict time/complexity, let's try a simpler approach:
                # Just ignore chunks for now and wait for a "Done" message?
                # BUT AgentLoop DOES NOT send a "Done" message with content.
                # It sends empty content with is_stream_end=True.

                # I should modify AgentLoop to send a "Final" message?
                # Or keep state here.
                # Keeping state in Adapter is complex (concurrency).

                # Helper: Let's hack it. If it's a stream chunk, we ignore it for Telegram
                # UNLESS we implement the "Live Edit" feature.
                # Users expect streaming.
                # Let's Implement a crude accumulator or just rely on `AgentLoop` sending the full thing?
                # The `AgentLoop` code I wrote:
                #   current_response_text += text_chunk
                #   publish_outbound(..., text_chunk, is_stream_chunk=True)
                #   ...
                #   (After loop) publish_outbound(..., "", is_stream_end=True)

                # Use Case: User wants to see output.
                # I will modify `AgentLoop` to send the COMPLETE message at the end as well?
                # Or just update the adapter to buffer?
                # Let's update `AgentLoop` to be friendlier to non-streaming adapters?
                # Actually, `AgentLoop` code is "Unified". Dashboard LOVES streaming. Telegram HATES it.
                # I should handle this in the Adapter.

                # Simple Buffer Implementation:
                # Use a dict `_buffers[chat_id] = {"msg_id": ..., "text": "", "last_update": ...}`

                await self._handle_stream_chunk(message)
                return

            if message.is_stream_end:
                # Flush buffer
                await self._flush_stream_buffer(message.chat_id)
                return

            # Normal message (not stream)
            real_chat_id, topic_id = self._parse_chat_id(chat_id)
            send_kwargs: dict[str, Any] = {
                "chat_id": real_chat_id,
                "text": convert_markdown(message.content, self.channel),
                "parse_mode": "Markdown",
            }
            if topic_id is not None:
                send_kwargs["message_thread_id"] = topic_id
            await self.app.bot.send_message(**send_kwargs)

        except Exception as e:
            logger.error(f"Failed to send telegram message: {e}")

    # --- buffering logic ---

    _buffers: dict[str, Any] = {}

    async def _handle_stream_chunk(self, message: OutboundMessage) -> None:
        chat_id = message.chat_id
        content = message.content

        if chat_id not in self._buffers:
            # Send initial message (topic-aware)
            real_chat_id, topic_id = self._parse_chat_id(chat_id)
            send_kwargs: dict[str, Any] = {"chat_id": real_chat_id, "text": "🧠 ..."}
            if topic_id is not None:
                send_kwargs["message_thread_id"] = topic_id
            sent_msg = await self.app.bot.send_message(**send_kwargs)
            self._buffers[chat_id] = {
                "message_id": sent_msg.message_id,
                "text": content,
                "last_update": asyncio.get_event_loop().time(),
            }
        else:
            self._buffers[chat_id]["text"] += content

        # Rate limited update
        now = asyncio.get_event_loop().time()
        buf = self._buffers[chat_id]
        if now - buf["last_update"] > 1.5:  # Update every 1.5s
            await self._update_message(chat_id, buf["message_id"], buf["text"])
            buf["last_update"] = now

    async def _flush_stream_buffer(self, chat_id: str) -> None:
        if chat_id in self._buffers:
            buf = self._buffers[chat_id]
            text = convert_markdown(buf["text"], self.channel)
            await self._update_message(chat_id, buf["message_id"], text)
            del self._buffers[chat_id]

    async def _update_message(self, chat_id: str, message_id: int, text: str) -> None:
        try:
            if not text.strip():
                return
            real_chat_id, _topic_id = self._parse_chat_id(chat_id)
            await self.app.bot.edit_message_text(
                chat_id=real_chat_id,
                message_id=message_id,
                text=text,
                parse_mode=None,  # Markdown can break easily with partial streams
            )
        except Exception as e:
            logger.warning(f"Failed to update message: {e}")

    # --- Handlers ---

    async def _handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start."""
        if not update.effective_user:
            return

        user_id = update.effective_user.id
        # Simple auth check logic (can be expanded)
        if self.allowed_user_id and user_id != self.allowed_user_id:
            await update.message.reply_text("⛔ Unauthorized.")
            return

        await update.message.reply_text(
            "🐾 **PocketPaw**\n\nI am listening. Just type to chat!",
            parse_mode="Markdown",
        )

    async def _handle_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /new, /sessions, /resume, /help by forwarding to the bus."""
        if not update.effective_user or not update.message:
            return

        user_id = update.effective_user.id
        if self.allowed_user_id and user_id != self.allowed_user_id:
            return

        # Reconstruct the full command text (e.g. "/resume 3")
        text = update.message.text or ""

        # Build topic-aware chat_id for forum groups
        base_chat_id = str(update.effective_chat.id)
        topic_id = getattr(update.message, "message_thread_id", None)
        chat_id = f"{base_chat_id}:topic:{topic_id}" if topic_id else base_chat_id

        msg = InboundMessage(
            channel=Channel.TELEGRAM,
            sender_id=str(user_id),
            chat_id=chat_id,
            content=text,
            metadata={"username": update.effective_user.username},
        )
        await self._publish_inbound(msg)

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Forward message to Bus, downloading any attached media."""
        if not update.effective_user or not update.message:
            return

        user_id = update.effective_user.id
        if self.allowed_user_id and user_id != self.allowed_user_id:
            return

        tg_msg = update.message
        content = tg_msg.text or tg_msg.caption or ""
        media_paths: list[str] = []

        # Download attached media
        file_obj = None
        file_name = "media"
        mime = None
        if tg_msg.photo:
            file_obj = await tg_msg.photo[-1].get_file()
            file_name = "photo.jpg"
            mime = "image/jpeg"
        elif tg_msg.document:
            file_obj = await tg_msg.document.get_file()
            file_name = tg_msg.document.file_name or "document"
            mime = tg_msg.document.mime_type
        elif tg_msg.audio:
            file_obj = await tg_msg.audio.get_file()
            file_name = tg_msg.audio.file_name or "audio"
            mime = tg_msg.audio.mime_type
        elif tg_msg.video:
            file_obj = await tg_msg.video.get_file()
            file_name = tg_msg.video.file_name or "video"
            mime = tg_msg.video.mime_type
        elif tg_msg.voice:
            file_obj = await tg_msg.voice.get_file()
            file_name = "voice.ogg"
            mime = tg_msg.voice.mime_type or "audio/ogg"
        elif tg_msg.video_note:
            file_obj = await tg_msg.video_note.get_file()
            file_name = "video_note.mp4"
            mime = "video/mp4"

        if file_obj:
            try:
                from pocketclaw.bus.media import build_media_hint, get_media_downloader

                data = await file_obj.download_as_bytearray()
                downloader = get_media_downloader()
                path = await downloader.save_from_bytes(bytes(data), file_name, mime)
                media_paths.append(path)
                content += build_media_hint([file_name])
            except Exception as e:
                logger.warning("Failed to download Telegram media: %s", e)

        if not content and not media_paths:
            return

        # Build topic-aware chat_id for forum groups
        base_chat_id = str(update.effective_chat.id)
        topic_id = getattr(tg_msg, "message_thread_id", None)
        chat_id = f"{base_chat_id}:topic:{topic_id}" if topic_id else base_chat_id

        msg = InboundMessage(
            channel=Channel.TELEGRAM,
            sender_id=str(user_id),
            chat_id=chat_id,
            content=content,
            media=media_paths,
            metadata={"username": update.effective_user.username},
        )

        await self._publish_inbound(msg)
