"""
Telegram Channel Adapter.
Created: 2026-02-02
"""

import asyncio
import logging
import time
from typing import Any

try:
    from telegram import Update
    from telegram.constants import ChatAction
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

from pocketpaw.bus import (
    BaseChannelAdapter,
    Channel,
    InboundMessage,
    OutboundMessage,
)
from pocketpaw.bus.format import convert_markdown

logger = logging.getLogger(__name__)

# Typing indicator refresh interval (Telegram clears typing after ~5s)
_TYPING_REFRESH_INTERVAL = 4.0
# Stream buffer update interval (rate limiting for message edits)
_BUFFER_UPDATE_INTERVAL = 1.5
# Voice media hint TTL (seconds) - hints older than this are evicted
_VOICE_MEDIA_HINT_TTL = 3600  # 1 hour


class TelegramAdapter(BaseChannelAdapter):
    """Adapter for Telegram Bot API."""

    def __init__(self, token: str, allowed_user_id: int | None = None):
        super().__init__()
        self.token = token
        self.allowed_user_id = allowed_user_id
        self.app: Application | None = None
        self._typing_tasks: dict[str, asyncio.Task] = {}  # chat_id -> typing refresh task
        self._buffers: dict[str, Any] = {}
        self._voice_media_hints: dict[tuple[str, str], bool] = {}
        self._voice_media_hints_ts: dict[
            tuple[str, str], float
        ] = {}  # (chat_id, path) -> timestamp

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
        _cmds = (
            "new",
            "sessions",
            "resume",
            "clear",
            "rename",
            "status",
            "delete",
            "backend",
            "backends",
            "model",
            "tools",
            "help",
            "kill",
        )
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
                    BotCommand("backend", "Show or switch agent backend"),
                    BotCommand("backends", "List available backends"),
                    BotCommand("model", "Show or switch model"),
                    BotCommand("tools", "Show or switch tool profile"),
                    BotCommand("help", "Show available commands"),
                    BotCommand("kill", "Cancel the current agent run"),
                ]
            )
        except Exception as e:
            logger.warning("Failed to set Telegram bot commands: %s", e)

        logger.info("📡 Telegram Adapter started")

    async def _on_stop(self) -> None:
        """Stop Telegram bot."""
        # Cancel all typing indicator tasks
        for task in self._typing_tasks.values():
            task.cancel()
        self._typing_tasks.clear()

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
            # Stream chunk handling with smart buffering:
            # 1. On first chunk, send a placeholder "..." message and start typing indicator
            # 2. Buffer chunks and update message every 1.5s (rate limiting)
            # 3. On stream_end, final edit and stop typing indicator
            if message.is_stream_chunk:
                await self._handle_stream_chunk(message)
                return

            if message.is_stream_end:
                # Stop typing indicator for this chat
                self._stop_typing_indicator(message.chat_id)
                # Flush buffer
                await self._flush_stream_buffer(message.chat_id)
                # Send any attached media files
                for path in message.media or []:
                    hint_key = (message.chat_id, path)
                    self._voice_media_hints[hint_key] = self._is_voice_media(path, message.metadata)
                    self._voice_media_hints_ts[hint_key] = time.time()
                    self._cleanup_voice_media_hints()
                    await self._send_media_file(message.chat_id, path)
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
            try:
                await self.app.bot.send_message(**send_kwargs)
            except Exception:
                # Markdown parse failed — retry without formatting
                send_kwargs["parse_mode"] = None
                send_kwargs["text"] = message.content
                await self.app.bot.send_message(**send_kwargs)

        except Exception as e:
            logger.error(f"Failed to send telegram message: {e}")

    # --- Typing indicator management ---

    async def _send_typing_indicator(self, chat_id: str) -> None:
        """Send a typing indicator to the chat."""
        if not self.app:
            return
        try:
            real_chat_id, _topic_id = self._parse_chat_id(chat_id)
            await self.app.bot.send_chat_action(chat_id=real_chat_id, action=ChatAction.TYPING)
        except Exception as e:
            logger.debug("Failed to send typing indicator: %s", e)

    def _start_typing_indicator(self, chat_id: str) -> None:
        """Start a background task that periodically refreshes the typing indicator."""
        if chat_id in self._typing_tasks:
            return  # Already running

        async def _typing_loop():
            try:
                while True:
                    await self._send_typing_indicator(chat_id)
                    await asyncio.sleep(_TYPING_REFRESH_INTERVAL)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug("Typing indicator loop error: %s", e)

        self._typing_tasks[chat_id] = asyncio.create_task(_typing_loop())

    def _stop_typing_indicator(self, chat_id: str) -> None:
        """Stop the typing indicator refresh task for a chat."""
        task = self._typing_tasks.pop(chat_id, None)
        if task:
            task.cancel()

    # --- Buffering logic ---

    async def _handle_stream_chunk(self, message: OutboundMessage) -> None:
        if not self.app:
            return
        chat_id = message.chat_id
        content = message.content

        if chat_id not in self._buffers:
            # Start periodic typing indicator
            self._start_typing_indicator(chat_id)
            # Send initial placeholder message (topic-aware)
            real_chat_id, topic_id = self._parse_chat_id(chat_id)
            send_kwargs: dict[str, Any] = {"chat_id": real_chat_id, "text": "🧠 ..."}
            if topic_id is not None:
                send_kwargs["message_thread_id"] = topic_id
            sent_msg = await self.app.bot.send_message(**send_kwargs)
            self._buffers[chat_id] = {
                "message_id": sent_msg.message_id,
                "text": content,
                "last_update": asyncio.get_running_loop().time(),
            }
        else:
            self._buffers[chat_id]["text"] += content

        # Rate-limited message update
        now = asyncio.get_running_loop().time()
        buf = self._buffers[chat_id]
        if now - buf["last_update"] > _BUFFER_UPDATE_INTERVAL:
            await self._update_message(chat_id, buf["message_id"], buf["text"])
            buf["last_update"] = now

    async def _flush_stream_buffer(self, chat_id: str) -> None:
        if not self.app:
            return
        if chat_id in self._buffers:
            buf = self._buffers[chat_id]
            text = convert_markdown(buf["text"], self.channel)
            await self._update_message(chat_id, buf["message_id"], text)
            del self._buffers[chat_id]

    async def _update_message(self, chat_id: str, message_id: int, text: str) -> None:
        if not self.app:
            return
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

    # --- Media sending ---

    def _cleanup_voice_media_hints(self) -> None:
        """Remove voice media hints that have exceeded TTL."""
        now = time.time()
        expired_keys = [
            key
            for key, ts in self._voice_media_hints_ts.items()
            if now - ts > _VOICE_MEDIA_HINT_TTL
        ]
        for key in expired_keys:
            self._voice_media_hints.pop(key, None)
            self._voice_media_hints_ts.pop(key, None)

    @staticmethod
    def _is_voice_media(file_path: str, metadata: dict[str, Any] | None) -> bool:
        """Determine whether an audio file should be sent as a Telegram voice note.

        Preferred metadata shape:
        - ``{"is_voice": true}`` to mark all audio files as voice notes
        - ``{"voice_media_paths": ["/abs/path/file.mp3", ...]}`` for per-file control
        """
        if not metadata:
            return False

        if bool(metadata.get("is_voice")):
            return True

        voice_paths = metadata.get("voice_media_paths")
        if isinstance(voice_paths, list):
            return file_path in voice_paths

        return False

    async def _send_media_file(self, chat_id: str, file_path: str) -> None:
        """Send a media file (audio/image/document) to a Telegram chat."""
        import os

        if not self.app or not os.path.isfile(file_path):
            return

        from pocketpaw.bus.adapters import guess_media_type

        media_type = guess_media_type(file_path)
        hint_key = (chat_id, file_path)
        is_voice = self._voice_media_hints.pop(hint_key, False)
        self._voice_media_hints_ts.pop(hint_key, None)  # Clean up timestamp
        real_chat_id, topic_id = self._parse_chat_id(chat_id)
        kwargs: dict[str, Any] = {"chat_id": real_chat_id}
        if topic_id is not None:
            kwargs["message_thread_id"] = topic_id

        try:
            with open(file_path, "rb") as f:
                if media_type == "audio":
                    # Voice-note behavior is driven by OutboundMessage metadata,
                    # not by adapter-local filesystem path assumptions.
                    if is_voice:
                        try:
                            await self.app.bot.send_voice(**kwargs, voice=f)
                        except Exception:
                            # Fallback: re-open and send as regular audio
                            with open(file_path, "rb") as f2:
                                await self.app.bot.send_audio(**kwargs, audio=f2)
                    else:
                        await self.app.bot.send_audio(**kwargs, audio=f)
                elif media_type == "image":
                    await self.app.bot.send_photo(**kwargs, photo=f)
                else:
                    await self.app.bot.send_document(**kwargs, document=f)
        except Exception as e:
            logger.warning("Failed to send Telegram media file: %s", e)

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

        # Send immediate typing indicator to show responsiveness
        await self._send_typing_indicator(chat_id)

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
                from pocketpaw.bus.media import build_media_hint, get_media_downloader

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

        # Send immediate typing indicator to show responsiveness
        await self._send_typing_indicator(chat_id)

        msg = InboundMessage(
            channel=Channel.TELEGRAM,
            sender_id=str(user_id),
            chat_id=chat_id,
            content=content,
            media=media_paths,
            metadata={"username": update.effective_user.username},
        )

        await self._publish_inbound(msg)
