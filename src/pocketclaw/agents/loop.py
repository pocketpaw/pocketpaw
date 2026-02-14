"""Unified Agent Loop.
Created: 2026-02-02
Changes:
  - Added BrowserTool registration
  - 2026-02-05: Refactored to use AgentRouter for all backends.
                Now properly emits system_event for tool_use/tool_result.

This is the core "brain" of PocketPaw. It integrates:
1. MessageBus (Input/Output)
2. MemoryManager (Short-term & Long-term memory)
3. AgentRouter (Backend selection: claude_agent_sdk, pocketpaw_native, open_interpreter)
4. AgentContextBuilder (Identity & System Prompt)

It replaces the old highly-coupled bot loops.
"""

import asyncio
import logging

from pocketclaw.agents.router import AgentRouter
from pocketclaw.bootstrap import AgentContextBuilder
from pocketclaw.bus import InboundMessage, OutboundMessage, SystemEvent, get_message_bus
from pocketclaw.bus.commands import get_command_handler
from pocketclaw.bus.events import Channel
from pocketclaw.config import Settings, get_settings
from pocketclaw.memory import get_memory_manager
from pocketclaw.security.injection_scanner import ThreatLevel, get_injection_scanner

logger = logging.getLogger(__name__)


async def _iter_with_timeout(aiter, first_timeout=30, timeout=120):
    """Yield items from an async iterator with per-item timeouts.

    Uses a shorter timeout for the first item (to detect dead/hung backends
    quickly) and a longer timeout for subsequent items (to allow for tool
    execution, file operations, etc.).
    """
    ait = aiter.__aiter__()
    first = True
    while True:
        try:
            t = first_timeout if first else timeout
            yield await asyncio.wait_for(ait.__anext__(), timeout=t)
            first = False
        except StopAsyncIteration:
            break


class AgentLoop:
    """
    Main agent execution loop.

    Orchestrates the flow of data between Bus, Memory, and AgentRouter.
    Uses AgentRouter to delegate to the selected backend (claude_agent_sdk,
    pocketpaw_native, or open_interpreter).
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

    async def _loop(self) -> None:
        """Main processing loop."""
        while self._running:
            # 1. Consume message from Bus
            message = await self.bus.consume_inbound(timeout=1.0)
            if not message:
                continue

            # 2. Process message in background task (to not block loop)
            task = asyncio.create_task(self._process_message(message))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    async def _process_message(self, message: InboundMessage) -> None:
        """Process a single message flow using AgentRouter."""
        session_key = message.session_key
        logger.info(f"⚡ Processing message from {session_key}")

        # Resolve alias so two chats aliased to the same session serialize correctly
        resolved_key = await self.memory.resolve_session_key(session_key)

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

    _WELCOME_EXCLUDED = frozenset({Channel.WEBSOCKET, Channel.CLI, Channel.SYSTEM})

    async def _process_message_inner(self, message: InboundMessage, session_key: str) -> None:
        """Inner message processing (called under concurrency guards)."""
        # Keep context_builder in sync if memory manager was hot-reloaded
        if self.context_builder.memory is not self.memory:
            self.context_builder.memory = self.memory

        # Command interception — handle /new, /sessions, /resume, /help
        # before any agent processing or memory storage
        cmd_handler = get_command_handler()
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

            # 2. Build dynamic system prompt (identity + memory context + channel hint)
            sender_id = message.sender_id
            system_prompt = await self.context_builder.build_system_prompt(
                user_query=content,
                channel=message.channel,
                sender_id=sender_id,
                session_key=message.session_key,
            )

            # 2a. Retrieve session history with compaction
            history = await self.memory.get_compacted_history(
                session_key,
                recent_window=self.settings.compaction_recent_window,
                char_budget=self.settings.compaction_char_budget,
                summary_chars=self.settings.compaction_summary_chars,
                llm_summarize=self.settings.compaction_llm_summarize,
            )

            # 2b. Emit thinking event
            await self.bus.publish_system(
                SystemEvent(event_type="thinking", data={"session_key": session_key})
            )

            # 3. Run through AgentRouter (handles all backends)
            router = self._get_router()
            full_response = ""

            run_iter = router.run(content, system_prompt=system_prompt, history=history)
            try:
                async for chunk in _iter_with_timeout(run_iter):
                    chunk_type = chunk.get("type", "")
                    content = chunk.get("content", "")
                    metadata = chunk.get("metadata") or {}

                    if chunk_type == "message":
                        # Stream text to user
                        full_response += content
                        await self.bus.publish_outbound(
                            OutboundMessage(
                                channel=message.channel,
                                chat_id=message.chat_id,
                                content=content,
                                is_stream_chunk=True,
                            )
                        )

                    elif chunk_type == "code":
                        # Code block from Open Interpreter - emit as tool_use
                        language = metadata.get("language", "code")
                        await self.bus.publish_system(
                            SystemEvent(
                                event_type="tool_start",
                                data={
                                    "name": f"run_{language}",
                                    "params": {"code": content[:100]},
                                },
                            )
                        )
                        # Also stream to user
                        code_block = f"\n```{language}\n{content}\n```\n"
                        full_response += code_block
                        await self.bus.publish_outbound(
                            OutboundMessage(
                                channel=message.channel,
                                chat_id=message.chat_id,
                                content=code_block,
                                is_stream_chunk=True,
                            )
                        )

                    elif chunk_type == "output":
                        # Output from code execution - emit as tool_result
                        await self.bus.publish_system(
                            SystemEvent(
                                event_type="tool_result",
                                data={
                                    "name": "code_execution",
                                    "result": content[:200],
                                    "status": "success",
                                },
                            )
                        )
                        # Also stream to user
                        output_block = f"\n```output\n{content}\n```\n"
                        full_response += output_block
                        await self.bus.publish_outbound(
                            OutboundMessage(
                                channel=message.channel,
                                chat_id=message.chat_id,
                                content=output_block,
                                is_stream_chunk=True,
                            )
                        )

                    elif chunk_type == "thinking":
                        # Thinking goes to Activity panel only
                        await self.bus.publish_system(
                            SystemEvent(
                                event_type="thinking",
                                data={"content": content, "session_key": session_key},
                            )
                        )

                    elif chunk_type == "thinking_done":
                        await self.bus.publish_system(
                            SystemEvent(
                                event_type="thinking_done",
                                data={"session_key": session_key},
                            )
                        )

                    elif chunk_type == "tool_use":
                        # Emit tool_start system event for Activity panel
                        tool_name = metadata.get("name") or metadata.get("tool", "unknown")
                        tool_input = metadata.get("input") or metadata
                        await self.bus.publish_system(
                            SystemEvent(
                                event_type="tool_start",
                                data={"name": tool_name, "params": tool_input},
                            )
                        )

                    elif chunk_type == "tool_result":
                        # Emit tool_result system event for Activity panel
                        tool_name = metadata.get("name") or metadata.get("tool", "unknown")
                        await self.bus.publish_system(
                            SystemEvent(
                                event_type="tool_result",
                                data={
                                    "name": tool_name,
                                    "result": content[:200],
                                    "status": "success",
                                },
                            )
                        )

                    elif chunk_type == "error":
                        # Emit error and send to user
                        await self.bus.publish_system(
                            SystemEvent(
                                event_type="tool_result",
                                data={
                                    "name": "agent",
                                    "result": content,
                                    "status": "error",
                                },
                            )
                        )
                        await self.bus.publish_outbound(
                            OutboundMessage(
                                channel=message.channel,
                                chat_id=message.chat_id,
                                content=content,
                                is_stream_chunk=True,
                            )
                        )

                    elif chunk_type == "done":
                        # Agent finished - will send stream_end below
                        pass
            finally:
                # Always close the async generator to kill any subprocess
                await run_iter.aclose()

            # 4. Send stream end marker
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=message.channel,
                    chat_id=message.chat_id,
                    content="",
                    is_stream_end=True,
                )
            )

            # 5. Store assistant response in memory
            if full_response:
                await self.memory.add_to_session(
                    session_key=session_key, role="assistant", content=full_response
                )

                # 6. Auto-learn: extract facts from conversation (non-blocking)
                should_auto_learn = (
                    self.settings.memory_backend == "mem0" and self.settings.mem0_auto_learn
                ) or (self.settings.memory_backend == "file" and self.settings.file_auto_learn)
                if should_auto_learn:
                    asyncio.create_task(
                        self._auto_learn(
                            message.content,
                            full_response,
                            session_key,
                            sender_id=sender_id,
                        )
                    )
                    self._background_tasks.add(t)
                    t.add_done_callback(self._background_tasks.discard)

        except TimeoutError:
            logger.error("Agent backend timed out")
            # Kill the hung backend so it releases resources
            try:
                await router.stop()
            except Exception:
                pass
            # Force router re-init on next message
            self._router = None

            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=message.channel,
                    chat_id=message.chat_id,
                    content=(
                        "Request timed out — the agent backend didn't respond.\n\n"
                        "**Possible causes:**\n"
                        "- Claude Code CLI is not installed "
                        "(`npm install -g @anthropic-ai/claude-code`)\n"
                        "- API key is missing or invalid "
                        "(check Settings → API Keys)\n"
                        "- Try switching to **PocketPaw Native** backend "
                        "in Settings → General"
                    ),
                    is_stream_chunk=True,
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
        except Exception as e:
            logger.exception(f"❌ Error processing message: {e}")
            try:
                await router.stop()
            except Exception:
                pass

            error_str = str(e)
            error_lower = error_str.lower()

            if (
                "authentication" in error_lower
                or "api key" in error_lower
                or "unauthorized" in error_lower
            ):
                user_message = (
                    "🔐 **Authentication Error**\n\n"
                    "Failed to authenticate with the AI service.\n\n"
                    "**Check:**\n"
                    "- Your API key is correct in Settings → API Keys\n"
                    "- The API key hasn't expired"
                )
            elif (
                "connection" in error_lower
                or "refused" in error_lower
                or "network" in error_lower
            ):
                user_message = (
                    "🌐 **Connection Error**\n\n"
                    "Couldn't connect to the AI service.\n\n"
                    "**Check:**\n"
                    "- Your internet connection\n"
                    "- The service is running\n"
                    "- Try again in a moment"
                )
            elif "timeout" in error_lower or "timed out" in error_lower:
                user_message = (
                    "⏱️ **Request Timed Out**\n\n"
                    "The request took too long.\n\n"
                    "**Try:**\n"
                    "- A smaller model in Settings → General\n"
                    "- A simpler request\n"
                    "- Try again"
                )
            elif "rate limit" in error_lower or "too many requests" in error_lower:
                user_message = (
                    "⏳ **Rate Limit Exceeded**\n\n"
                    "Too many requests. Please wait a moment and try again."
                )
            else:
                user_message = (
                    "⚠️ **Something went wrong**\n\n"
                    f"An unexpected error occurred: {error_str[:100]}\n\n"
                    "Try again, or check Settings → General to switch to a different backend."
                )

            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=message.channel,
                    chat_id=message.chat_id,
                    content=user_message,
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
