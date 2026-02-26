"""Unified Agent Loop.

Core event loop that consumes from the message bus, feeds messages
through AgentRouter (which delegates to the configured backend),
and streams AgentEvent responses back to channels.
"""

import asyncio
import logging
import re

from pocketpaw.agents.router import AgentRouter
from pocketpaw.bootstrap import AgentContextBuilder
from pocketpaw.bus import InboundMessage, OutboundMessage, SystemEvent, get_message_bus
from pocketpaw.bus.commands import get_command_handler
from pocketpaw.bus.events import Channel
from pocketpaw.config import Settings, get_settings
from pocketpaw.memory import get_memory_manager
from pocketpaw.security.injection_scanner import ThreatLevel, get_injection_scanner
from pocketpaw.security.redact import redact_output

logger = logging.getLogger(__name__)

_MEDIA_TAG_RE = re.compile(r"<!-- media:(.+?) -->")
# Fallback: detect file paths in ~/.pocketpaw/generated/ mentioned in agent text.
# The Claude SDK backend runs tools via Bash; the media tag stays inside the SDK
# and never surfaces. The agent echoes the path in its text response instead.
_GENERATED_PATH_RE = re.compile(
    r"[`\s(/]("  # preceded by backtick, space, paren, or slash
    r"(?:/[^\s`*]+/\.pocketpaw/generated/[^\s`*\)]+)"  # absolute path under generated/
    r")"
)


def _extract_media_paths(text: str) -> list[str]:
    """Extract media file paths from <!-- media:/path --> tags in text."""
    return _MEDIA_TAG_RE.findall(text)


def _extract_generated_paths(text: str) -> list[str]:
    """Fallback: extract file paths under ~/.pocketpaw/generated/ from agent text."""
    return _GENERATED_PATH_RE.findall(text)


class AgentLoop:
    """
    Main agent execution loop.

    Orchestrates the flow of data between Bus, Memory, and AgentRouter.
    Uses AgentRouter to delegate to the selected backend (claude_agent_sdk,
    openai_agents, google_adk, codex_cli, opencode, or copilot_sdk).
    """

    def __init__(self):
        self.settings = get_settings()
        self.bus = get_message_bus()
        self.memory = get_memory_manager()
        self.context_builder = AgentContextBuilder(memory_manager=self.memory)

        # Agent Router handles backend selection
        self._router: AgentRouter | None = None

        # Concurrency controls
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._global_semaphore = asyncio.Semaphore(self.settings.max_concurrent_conversations)
        self._background_tasks: set[asyncio.Task] = set()
        self._active_tasks: dict[str, asyncio.Task] = {}  # session_key -> processing task

        self._running = False

    def _get_router(self) -> AgentRouter:
        """Get or create the agent router (lazy initialization)."""
        if self._router is None:
            # Reload settings to pick up any changes
            settings = Settings.load()
            self._router = AgentRouter(settings)
        return self._router

    async def start(self) -> None:
        """Start the agent loop."""
        self._running = True
        settings = Settings.load()
        logger.info(f"🤖 Agent Loop started (Backend: {settings.agent_backend})")
        await self._loop()

    async def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("🛑 Agent Loop stopped")

    async def cancel_session(self, session_key: str) -> bool:
        """Cancel in-flight processing for a session. Returns True if cancelled."""
        router = self._router
        if router is not None:
            await router.stop()

        task = self._active_tasks.get(session_key)
        if task is not None and not task.done():
            task.cancel()
            logger.info("Cancelled processing task for session %s", session_key)
            return True
        return False

    async def _loop(self) -> None:
        """Main processing loop."""
        while self._running:
            # 1. Consume message from Bus
            message = await self.bus.consume_inbound(timeout=1.0)
            if not message:
                continue

            # 2. Process message in background task (to not block loop)
            session_key = message.session_key
            task = asyncio.create_task(self._process_message(message))
            self._background_tasks.add(task)
            self._active_tasks[session_key] = task

            def _on_done(t: asyncio.Task, key: str = session_key) -> None:
                self._background_tasks.discard(t)
                self._active_tasks.pop(key, None)

            task.add_done_callback(_on_done)

    async def _process_message(self, message: InboundMessage) -> None:
        """Process a single message flow using AgentRouter."""
        session_key = message.session_key
        logger.info(f"⚡ Processing message from {session_key}")

        # Resolve alias so two chats aliased to the same session serialize correctly
        resolved_key = await self.memory.resolve_session_key(session_key)

        try:
            # Global concurrency limit — blocks until a slot is available
            async with self._global_semaphore:
                # Per-session lock — serializes messages within the same session
                if resolved_key not in self._session_locks:
                    self._session_locks[resolved_key] = asyncio.Lock()
                lock = self._session_locks[resolved_key]
                async with lock:
                    await self._process_message_inner(message, resolved_key)

                # Clean up lock if no one else is waiting on it
                if not lock.locked():
                    self._session_locks.pop(resolved_key, None)
        except asyncio.CancelledError:
            logger.info("Processing cancelled for session %s", session_key)
            raise

    _WELCOME_EXCLUDED = frozenset({Channel.WEBSOCKET, Channel.CLI, Channel.SYSTEM})

    async def _process_message_inner(self, message: InboundMessage, session_key: str) -> None:
        """Inner message processing (called under concurrency guards)."""
        # Keep context_builder in sync if memory manager was hot-reloaded
        if self.context_builder.memory is not self.memory:
            self.context_builder.memory = self.memory

        # Command interception — handle /new, /sessions, /resume, /help
        # before any agent processing or memory storage
        cmd_handler = get_command_handler()
        if cmd_handler._on_settings_changed is None:
            cmd_handler.set_on_settings_changed(self.reset_router)
        if cmd_handler.is_command(message.content):
            response = await cmd_handler.handle(message)
            if response is not None:
                await self.bus.publish_outbound(response)
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=message.channel,
                        chat_id=message.chat_id,
                        content="",
                        is_stream_end=True,
                    )
                )
                return

        # Welcome hint — one-time message on first interaction in a channel
        if self.settings.welcome_hint_enabled and message.channel not in self._WELCOME_EXCLUDED:
            existing = await self.memory.get_session_history(session_key, limit=1)
            if not existing:
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=message.channel,
                        chat_id=message.chat_id,
                        content=(
                            "Welcome to PocketPaw! Type /help (or !help) to see available commands."
                        ),
                    )
                )

        router = None
        try:
            # 0. Injection scan for non-owner sources
            content = message.content
            if self.settings.injection_scan_enabled:
                scanner = get_injection_scanner()
                source = message.metadata.get("source", message.channel.value)
                scan_result = scanner.scan(content, source=source)

                if scan_result.threat_level == ThreatLevel.HIGH:
                    if self.settings.injection_scan_llm:
                        scan_result = await scanner.deep_scan(content, source=source)

                    if scan_result.threat_level == ThreatLevel.HIGH:
                        logger.warning(
                            "Blocked HIGH threat injection from %s: %s",
                            source,
                            scan_result.matched_patterns,
                        )
                        await self.bus.publish_system(
                            SystemEvent(
                                event_type="error",
                                data={
                                    "message": "Message blocked by injection scanner",
                                    "patterns": scan_result.matched_patterns,
                                },
                            )
                        )
                        await self.bus.publish_outbound(
                            OutboundMessage(
                                channel=message.channel,
                                chat_id=message.chat_id,
                                content=(
                                    "Your message was flagged by the security scanner and blocked."
                                ),
                            )
                        )
                        return

                # Wrap suspicious (non-blocked) content with sanitization markers
                if scan_result.threat_level != ThreatLevel.NONE:
                    content = scan_result.sanitized_content

            # 1. Store User Message
            await self.memory.add_to_session(
                session_key=session_key,
                role="user",
                content=content,
                metadata=message.metadata,
            )

            # 1b. Inject inbound media file paths so the agent can use them
            if message.media:
                paths_info = ", ".join(message.media)
                content += f"\n[Media files on disk: {paths_info}]"

            # 2. Build system prompt + session history concurrently (independent I/O)
            sender_id = message.sender_id
            system_prompt, history = await asyncio.gather(
                self.context_builder.build_system_prompt(
                    user_query=content,
                    channel=message.channel,
                    sender_id=sender_id,
                    session_key=message.session_key,
                ),
                self.memory.get_compacted_history(
                    session_key,
                    recent_window=self.settings.compaction_recent_window,
                    char_budget=self.settings.compaction_char_budget,
                    summary_chars=self.settings.compaction_summary_chars,
                    llm_summarize=self.settings.compaction_llm_summarize,
                ),
            )

            # 2b. Emit thinking event
            await self.bus.publish_system(
                SystemEvent(event_type="thinking", data={"session_key": session_key})
            )

            # 3. Run through AgentRouter (handles all backends)
            router = self._get_router()
            full_response = ""
            media_paths: list[str] = []
            cancelled = False

            run_iter = router.run(
                content, system_prompt=system_prompt, history=history, session_key=session_key
            )
            try:
                async for event in run_iter:
                    etype = event.type
                    econtent = event.content
                    meta = event.metadata or {}

                    if etype == "message":
                        full_response += econtent
                        # Apply output redaction before sending to user
                        redacted_content = redact_output(econtent)
                        await self.bus.publish_outbound(
                            OutboundMessage(
                                channel=message.channel,
                                chat_id=message.chat_id,
                                content=redacted_content,
                                is_stream_chunk=True,
                            )
                        )

                    elif etype == "thinking":
                        await self.bus.publish_system(
                            SystemEvent(
                                event_type="thinking",
                                data={"content": econtent, "session_key": session_key},
                            )
                        )

                    elif etype == "thinking_done":
                        await self.bus.publish_system(
                            SystemEvent(
                                event_type="thinking_done",
                                data={"session_key": session_key},
                            )
                        )

                    elif etype == "token_usage":
                        await self.bus.publish_system(
                            SystemEvent(event_type="token_usage", data=meta)
                        )

                    elif etype == "tool_use":
                        tool_name = meta.get("name") or meta.get("tool", "unknown")
                        tool_input = meta.get("input") or meta
                        await self.bus.publish_system(
                            SystemEvent(
                                event_type="tool_start",
                                data={"name": tool_name, "params": tool_input},
                            )
                        )

                    elif etype == "tool_result":
                        tool_name = meta.get("name") or meta.get("tool", "unknown")
                        await self.bus.publish_system(
                            SystemEvent(
                                event_type="tool_result",
                                data={
                                    "name": tool_name,
                                    "result": econtent[:200],
                                    "status": "success",
                                },
                            )
                        )
                        media_paths.extend(_extract_media_paths(econtent))

                    elif etype == "error":
                        await self.bus.publish_system(
                            SystemEvent(
                                event_type="tool_result",
                                data={
                                    "name": "agent",
                                    "result": econtent,
                                    "status": "error",
                                },
                            )
                        )
                        # Apply output redaction to error messages too
                        redacted_content = redact_output(econtent)
                        await self.bus.publish_outbound(
                            OutboundMessage(
                                channel=message.channel,
                                chat_id=message.chat_id,
                                content=redacted_content,
                                is_stream_chunk=True,
                            )
                        )

                    elif etype == "done":
                        pass
            except asyncio.CancelledError:
                cancelled = True
                logger.info("Stream cancelled for session %s", session_key)
            finally:
                await run_iter.aclose()

            # 4. Send stream end marker (with any media files detected)
            # Fallback: if no media tags found in tool_result chunks,
            # check full_response for generated file paths (Claude SDK backend
            # runs tools via Bash — media tags stay inside the SDK and the
            # agent echoes the path in its text response instead).
            if not media_paths and full_response:
                media_paths.extend(_extract_generated_paths(full_response))

            # Deduplicate while preserving order
            seen: set[str] = set()
            media_paths = [p for p in media_paths if not (p in seen or seen.add(p))]
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=message.channel,
                    chat_id=message.chat_id,
                    content="",
                    is_stream_end=True,
                    media=media_paths,
                )
            )

            # 5. Store assistant response in memory
            if cancelled and full_response:
                full_response += "\n\n[Response interrupted]"
            if full_response:
                await self.memory.add_to_session(
                    session_key=session_key, role="assistant", content=full_response
                )

                # 6. Auto-learn: extract facts from conversation (non-blocking)
                # Skip auto-learn on cancelled responses — partial data is unreliable
                should_auto_learn = not cancelled and (
                    (self.settings.memory_backend == "mem0" and self.settings.mem0_auto_learn)
                    or (self.settings.memory_backend == "file" and self.settings.file_auto_learn)
                )
                if should_auto_learn:
                    t = asyncio.create_task(
                        self._auto_learn(
                            message.content,
                            full_response,
                            session_key,
                            sender_id=sender_id,
                        )
                    )
                    self._background_tasks.add(t)
                    t.add_done_callback(self._background_tasks.discard)

        except Exception as e:
            logger.exception(f"❌ Error processing message: {e}")
            # Record to persistent health error log
            try:
                import traceback

                from pocketpaw.health import get_health_engine

                get_health_engine().record_error(
                    message=str(e),
                    source="agents.loop",
                    severity="error",
                    traceback=traceback.format_exc(),
                    context={"session_key": session_key},
                )
            except Exception:
                pass
            # Kill the backend on error
            if router is not None:
                try:
                    await router.stop()
                except Exception:
                    pass

            # Apply output redaction to exception messages
            error_msg = redact_output(f"An error occurred: {str(e)}")
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=message.channel,
                    chat_id=message.chat_id,
                    content=error_msg,
                )
            )
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=message.channel,
                    chat_id=message.chat_id,
                    content="",
                    is_stream_end=True,
                )
            )

    async def _send_response(self, original: InboundMessage, content: str) -> None:
        """Helper to send a simple text response."""
        await self.bus.publish_outbound(
            OutboundMessage(channel=original.channel, chat_id=original.chat_id, content=content)
        )

    async def _auto_learn(
        self,
        user_msg: str,
        assistant_msg: str,
        session_key: str,
        sender_id: str | None = None,
    ) -> None:
        """Background task: feed conversation turn for fact extraction."""
        try:
            messages = [
                {"role": "user", "content": user_msg},
                {"role": "assistant", "content": assistant_msg},
            ]
            result = await self.memory.auto_learn(
                messages,
                file_auto_learn=self.settings.file_auto_learn,
                sender_id=sender_id,
            )
            extracted = len(result.get("results", []))
            if extracted:
                logger.debug("Auto-learned %d facts from %s", extracted, session_key)
        except Exception:
            logger.debug("Auto-learn background task failed", exc_info=True)

    def reset_router(self) -> None:
        """Reset the router to pick up new settings."""
        self._router = None
