"""Unified Agent Loop.

Core event loop that consumes from the message bus, feeds messages
through AgentRouter (which delegates to the configured backend),
and streams AgentEvent responses back to channels.

PII scanning before memory storage is opt-in via pii_scan_enabled + pii_scan_memory settings.

Updated: feat/pocketpaw-cognitive-engine
- start() now builds a PocketPawCognitiveEngine backed by the active AgentRouter
  and passes it to SoulManager.initialize() so the soul's cognition pipeline
  (sentiment, significance, fact extraction, reflection) uses the same LLM
  as the conversation rather than falling back to heuristics.
"""

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any

from pocketpaw.agents.router import AgentRouter
from pocketpaw.bootstrap import AgentContextBuilder
from pocketpaw.bus import InboundMessage, OutboundMessage, SystemEvent, get_message_bus
from pocketpaw.bus.commands import get_command_handler
from pocketpaw.bus.events import Channel
from pocketpaw.config import Settings, get_settings
from pocketpaw.memory import get_memory_manager
from pocketpaw.recent_files import get_recent_files_tracker
from pocketpaw.security.injection_scanner import ThreatLevel, get_injection_scanner
from pocketpaw.security.redact import redact_output

logger = logging.getLogger(__name__)

# Number of history messages (user + assistant turns) at which a compact
# identity reminder is appended to the system prompt.  The reminder nudges
# the model back toward the configured identity without a full re-injection.
_IDENTITY_REINFORCE_THRESHOLD = 20

# How long (seconds) a session lock must be idle before it is eligible for
# garbage collection.  1 hour is generous enough to cover any in-flight work
# while still bounding growth on long-running servers with many unique sessions.
_SESSION_LOCK_TTL = 3600  # seconds

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


# Strip markdown links to generated audio files that the LLM may insert after calling tts tool.
_AUDIO_LINK_RE = re.compile(
    r"\[(?:[^\]]*?)\]\([^)]*generated[/\\]audio[/\\][^)]*\)\n?",
    re.IGNORECASE,
)

# Audio file extensions for voice message detection
_AUDIO_EXTS = {".ogg", ".oga", ".mp3", ".wav", ".m4a", ".opus", ".aac", ".flac"}


def _strip_tts_links(text: str) -> str:
    """Remove generated-audio markdown links and bare media tags from LLM text output."""
    text = _AUDIO_LINK_RE.sub("", text)
    text = re.sub(r"<!-- media:[^>]+-->", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


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
        # Tracks the last time each session lock was touched (time.monotonic).
        # Used by the GC task to identify and discard idle locks so that
        # _session_locks does not grow without bound on long-running servers.
        self._session_lock_last_used: dict[str, float] = {}
        # Background task that periodically prunes stale locks (see _gc_session_locks).
        self._lock_gc_task: asyncio.Task | None = None
        self._global_semaphore = asyncio.Semaphore(self.settings.max_concurrent_conversations)
        self._background_tasks: set[asyncio.Task] = set()
        self._active_tasks: dict[str, asyncio.Task] = {}  # session_key -> processing task

        # Soul Protocol (optional)
        self._soul_manager: Any = None  # SoulManager | None

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

        # Initialize Soul if enabled
        if settings.soul_enabled:
            try:
                from pocketpaw.soul.cognitive import PocketPawCognitiveEngine
                from pocketpaw.soul.manager import SoulManager

                # Build a lazy engine: the backend_provider lambda captures `self`
                # so it resolves the router (and therefore the backend) on every
                # think() call.  By the time any cognitive call fires the router
                # will already be initialised (first in-bound message precedes any
                # memory/reflect pipeline call).
                engine = PocketPawCognitiveEngine(
                    backend_provider=lambda: (
                        self._get_router()._backend if self._router is not None else None
                    )
                )

                self._soul_manager = SoulManager(settings)
                await self._soul_manager.initialize(engine=engine)
                if self._soul_manager.bootstrap_provider:
                    self.context_builder.bootstrap = self._soul_manager.bootstrap_provider
                self._soul_manager.start_auto_save()
            except Exception:
                logger.exception("Soul initialization failed, continuing without soul")
                self._soul_manager = None

        # Spawn the session-lock GC before entering the main loop so it begins
        # pruning stale locks as soon as the server is live.
        self._lock_gc_task = asyncio.create_task(self._gc_session_locks(), name="session-lock-gc")
        await self._loop()

    async def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        # Cancel the GC task so it does not linger after shutdown.
        if self._lock_gc_task is not None and not self._lock_gc_task.done():
            self._lock_gc_task.cancel()
            try:
                await self._lock_gc_task
            except asyncio.CancelledError:
                pass  # expected on clean shutdown
            self._lock_gc_task = None
        # Persist soul state and stop auto-save
        if self._soul_manager is not None:
            try:
                await self._soul_manager.shutdown()
            except Exception:
                logger.exception("Failed to shut down soul")
        logger.info("🛑 Agent Loop stopped")

    async def _gc_session_locks(self) -> None:
        """
        Periodically garbage-collect idle session locks to prevent unbounded
        memory growth.

        **Problem being solved**
        ``_session_locks`` is a dict keyed by ``session_key``.  Entries are
        created on-demand in ``_process_message`` but the existing eager-cleanup
        (``pop`` after the lock is released) can be bypassed when:

        * An unhandled exception propagates before the ``pop`` line is reached.
        * A task is cancelled while another coroutine is already *waiting* for
          the same lock — the entry cannot be safely removed until all waiters
          are gone, so the last waiter may miss cleanup under certain race
          conditions.
        * A session that produced an error leaves its lock entry permanently.

        Over weeks of operation with thousands of unique sessions this causes
        unbounded memory growth (one ``asyncio.Lock`` object + two dict entries
        per dead session).

        **Algorithm**
        Every 5 minutes inspect ``_session_lock_last_used``; any lock that has
        not been touched for longer than ``_SESSION_LOCK_TTL`` seconds *and* is
        currently unlocked is safe to discard.  Locked entries are always
        skipped — they are either actively held or have at least one waiter.

        Because asyncio is single-threaded for coroutine scheduling, the dict
        mutations here are safe without additional locking.
        """
        while True:
            # Sleep first so we don't run immediately on a cold start.
            await asyncio.sleep(300)  # check every 5 minutes
            now = time.monotonic()
            stale_keys = [
                key
                for key, last_used in list(self._session_lock_last_used.items())
                if now - last_used > _SESSION_LOCK_TTL
                and key in self._session_locks
                and not self._session_locks[key].locked()
            ]
            for key in stale_keys:
                self._session_locks.pop(key, None)
                self._session_lock_last_used.pop(key, None)
            if stale_keys:
                logger.debug("session-lock GC removed %d stale lock(s)", len(stale_keys))

    async def cancel_session(self, session_key: str) -> bool:
        """Cancel in-flight processing for a session. Returns True if cancelled."""
        task = self._active_tasks.get(session_key)
        if task is not None and not task.done():
            task.cancel()
            logger.info("Cancelled processing task for session %s", session_key)
            return True
        return False

    def cancel_task(self, session_key: str) -> bool:
        """Cancel just the processing task without stopping the router.

        Lighter-weight than cancel_session() — used by the SSE bridge when
        a new stream starts for the same session so the stale task stops
        publishing events, but the persistent client subprocess stays alive.
        """
        task = self._active_tasks.get(session_key)
        if task is not None and not task.done():
            task.cancel()
            logger.info("Cancelled stale task for session %s", session_key)
            return True
        return False

    async def _loop(self) -> None:
        """Main processing loop."""
        while self._running:
            # 1. Consume message from Bus
            message = await self.bus.consume_inbound(timeout=1.0)
            if not message:
                continue

            # Intercept /kill before entering session-locked pipeline so it
            # can cancel an in-flight task without being blocked by the lock.
            # Uses the same regex as CommandHandler to avoid false positives
            # on normal sentences containing "kill".
            content = message.content.strip()
            _kill_match = re.match(r"^[/!]kill(?:@\S+)?(?:\s.*)?$", content, re.IGNORECASE)
            if _kill_match:
                cancelled = await self.cancel_session(message.session_key)
                reply = (
                    "Agent run cancelled for this session."
                    if cancelled
                    else "No active agent run for this session."
                )

                # Audit log: /kill is security-relevant
                try:
                    from pocketpaw.security.audit import AuditEvent, AuditSeverity, get_audit_logger

                    get_audit_logger().log(
                        AuditEvent.create(
                            severity=AuditSeverity.WARNING,
                            actor=message.sender_id or message.channel.value,
                            action="kill_session",
                            target=message.session_key,
                            status="cancelled" if cancelled else "no_active_run",
                            channel=message.channel.value,
                        )
                    )
                except Exception:
                    logger.exception(
                        "Failed to write audit log for /kill action",
                    )

                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=message.channel,
                        chat_id=message.chat_id,
                        content=reply,
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
                continue

            # 2. Process message in background task (to not block loop)
            session_key = message.session_key
            task = asyncio.create_task(self._process_message(message))
            self._background_tasks.add(task)
            self._active_tasks[session_key] = task

            def _on_done(t: asyncio.Task, key: str = session_key) -> None:
                self._background_tasks.discard(t)
                # Only remove from _active_tasks if this task is still the
                # registered one — a newer task for the same session may have
                # overwritten the entry already.
                if self._active_tasks.get(key) is t:
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
                # Record access time so the GC task can identify idle locks.
                self._session_lock_last_used[resolved_key] = time.monotonic()
                lock_contended = lock.locked()
                if lock_contended:
                    logger.info("Session lock contended for %s — waiting", resolved_key)
                async with lock:
                    if lock_contended:
                        logger.info("Session lock acquired for %s", resolved_key)
                    await self._process_message_inner(message, resolved_key)

                # Eager cleanup: remove the lock immediately when no further
                # coroutines are waiting on it.  The GC task is a safety net
                # for the cases where this eager path is skipped (e.g. after
                # an exception propagates past this block).
                if not lock.locked():
                    self._session_locks.pop(resolved_key, None)
                    self._session_lock_last_used.pop(resolved_key, None)
                logger.info("Message processing complete for %s", session_key)
        except asyncio.CancelledError:
            logger.info("Processing cancelled for session %s", session_key)
            raise

    _WELCOME_EXCLUDED = frozenset({Channel.WEBSOCKET, Channel.CLI, Channel.SYSTEM, Channel.DISCORD})

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
        agent_started = False
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
                                    "session_key": session_key,
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

            # PII scan before memory storage (opt-in)
            if self.settings.pii_scan_enabled and self.settings.pii_scan_memory:
                from pocketpaw.security.pii import get_pii_scanner

                pii_result = get_pii_scanner().scan(content, source=session_key)
                if pii_result.has_pii:
                    logger.info(
                        "PII detected in %s: %s",
                        session_key,
                        [t.value for t in pii_result.pii_types_found],
                    )
                    content = pii_result.sanitized_text

            # 1. Store User Message
            await self.memory.add_to_session(
                session_key=session_key,
                role="user",
                content=content,
                metadata=message.metadata,
            )

            # 1b. Inject inbound media file paths so the agent can use them
            # Also detect whether this is a voice message so we can auto-TTS the reply.
            is_voice_message = any(
                Path(p).suffix.lower() in _AUDIO_EXTS for p in (message.media or [])
            )
            if message.media:
                paths_info = ", ".join(message.media)
                content += f"\n[Media files on disk: {paths_info}]"

            # 2. Build system prompt + session history concurrently (independent I/O)
            sender_id = message.sender_id
            file_context = (message.metadata or {}).get("file_context")
            # Resolve working directory for AGENTS.md discovery:
            # prefer explicit file_context path, then fall back to jail root.
            agents_md_dir: str | None = None
            if file_context and file_context.get("current_dir"):
                agents_md_dir = file_context["current_dir"]
            else:
                agents_md_dir = str(self.settings.file_jail_path)

            system_prompt, history = await asyncio.gather(
                self.context_builder.build_system_prompt(
                    user_query=content,
                    channel=message.channel,
                    sender_id=sender_id,
                    session_key=message.session_key,
                    file_context=file_context,
                    agents_md_dir=agents_md_dir,
                    metadata=message.metadata,
                ),
                self.memory.get_compacted_history(
                    session_key,
                    recent_window=self.settings.compaction_recent_window,
                    char_budget=self.settings.compaction_char_budget,
                    summary_chars=self.settings.compaction_summary_chars,
                    llm_summarize=self.settings.compaction_llm_summarize,
                ),
            )

            # 2a. Emit AGENTS.md event for the dashboard Activity panel
            try:
                from pocketpaw.agents_md import AgentsMdLoader

                agents_md = AgentsMdLoader().find_and_load(agents_md_dir)
                if agents_md:
                    await self.bus.publish_system(
                        SystemEvent(
                            event_type="agents_md_loaded",
                            data={
                                "path": str(agents_md.path),
                                "preview": agents_md.preview,
                                "session_key": session_key,
                            },
                        )
                    )
            except Exception:
                logger.debug(
                    "AGENTS.md discovery failed (continuing)",
                    exc_info=True,
                )

            # 2b. Emit thinking event
            # 2b. Periodic identity reinforcement for long conversations.
            # When the session has accumulated many turns the model may start
            # drifting from the identity defined in <identity> block.
            # Appending a compact reminder keeps the agent on-character without
            # a full re-injection (which would waste context window).
            if len(history) >= _IDENTITY_REINFORCE_THRESHOLD:
                system_prompt += (
                    "\n\n<identity-reminder>\n"
                    "Regardless of conversation length, you remain the agent described in the "
                    "<identity> block above. Maintain your defined personality, tone, and "
                    "communication style consistently throughout this conversation.\n"
                    "</identity-reminder>"
                )

            # 2c. Emit agent_start + thinking events
            agent_started = True
            await self.bus.publish_system(
                SystemEvent(event_type="agent_start", data={"session_key": session_key})
            )
            await self.bus.publish_system(
                SystemEvent(event_type="thinking", data={"session_key": session_key})
            )

            # 3. Run through AgentRouter (handles all backends)
            router = self._get_router()
            full_response = ""
            media_paths: list[str] = []
            cancelled = False
            # Streaming redaction: accumulate raw content and track what has
            # already been sent (redacted) so secrets split across chunk
            # boundaries are still caught.
            stream_buffer = ""
            safe_sent = ""

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
                        # Accumulate raw content and redact the full buffer so
                        # secrets that span chunk boundaries are fully redacted.
                        stream_buffer += econtent
                        safe_buffer = redact_output(stream_buffer)
                        # Send only the newly safe portion (delta from last publish).
                        safe_chunk = safe_buffer[len(safe_sent) :]
                        safe_sent = safe_buffer
                        await self.bus.publish_outbound(
                            OutboundMessage(
                                channel=message.channel,
                                chat_id=message.chat_id,
                                content=safe_chunk,
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
                            SystemEvent(
                                event_type="token_usage",
                                data={**meta, "session_key": session_key},
                            )
                        )
                        # Persist to usage tracker
                        try:
                            from pocketpaw.usage_tracker import get_usage_tracker

                            get_usage_tracker().record(
                                backend=meta.get("backend", "unknown"),
                                model=meta.get("model", ""),
                                input_tokens=meta.get("input_tokens", 0),
                                output_tokens=meta.get("output_tokens", 0),
                                cached_input_tokens=meta.get("cached_input_tokens", 0),
                                session_id=session_key or "",
                                total_cost_usd=meta.get("total_cost_usd"),
                            )
                        except Exception:
                            logger.debug(
                                "Failed to persist token usage metrics",
                                exc_info=True,
                            )

                    elif etype == "tool_use":
                        tool_name = meta.get("name") or meta.get("tool", "unknown")
                        tool_input = meta.get("input") or meta
                        await self.bus.publish_system(
                            SystemEvent(
                                event_type="tool_start",
                                data={
                                    "name": tool_name,
                                    "params": tool_input,
                                    "session_key": session_key,
                                },
                            )
                        )

                        # Track file paths for recent files
                        try:
                            get_recent_files_tracker().record_tool_use(
                                tool_name, tool_input if isinstance(tool_input, dict) else {}
                            )
                        except Exception:
                            logger.debug(
                                "Failed to record recent file tracker event for tool '%s'",
                                tool_name,
                                exc_info=True,
                            )

                        # AskUserQuestion — forward the question to the
                        # client so the user can see and answer it.
                        if tool_name == "AskUserQuestion":
                            question = tool_input.get("question", "")
                            options = tool_input.get("options", [])
                            await self.bus.publish_system(
                                SystemEvent(
                                    event_type="ask_user_question",
                                    data={
                                        "question": question,
                                        "options": options,
                                        "session_key": session_key,
                                    },
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
                                    "session_key": session_key,
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
                                    "session_key": session_key,
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

            # 4b. Auto-TTS: if the inbound message was a voice note and the agent
            # didn't already call text_to_speech (no audio in media_paths), synthesize
            # the full response as a voice reply now.
            already_has_audio = any(Path(p).suffix.lower() in _AUDIO_EXTS for p in media_paths)
            voice_media_paths: list[str] = []
            if (
                is_voice_message
                and not already_has_audio
                and not cancelled
                and full_response
                and self.settings.voice_reply_enabled
            ):
                try:
                    from pocketpaw.tools.builtin.voice import synthesize_speech

                    tts_path = await synthesize_speech(full_response)
                    if tts_path:
                        logger.info("Auto-TTS voice reply: %s", tts_path)
                        media_paths.append(tts_path)
                        voice_media_paths.append(tts_path)
                except Exception as _tts_err:
                    logger.warning("Auto-TTS failed: %s", _tts_err)

            # Deduplicate while preserving order
            seen: set[str] = set()
            media_paths = [p for p in media_paths if not (p in seen or seen.add(p))]
            metadata_out: dict[str, Any] = {}
            if voice_media_paths:
                metadata_out["voice_media_paths"] = voice_media_paths
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=message.channel,
                    chat_id=message.chat_id,
                    content="",
                    is_stream_end=True,
                    media=media_paths,
                    metadata=metadata_out,
                )
            )

            # 5. Store assistant response in memory
            # Strip TTS links from full_response before storing (keep memory clean)
            if full_response:
                full_response = _strip_tts_links(full_response)
            if cancelled and full_response:
                full_response += "\n\n[Response interrupted]"
            if full_response:
                stored_response = full_response
                if self.settings.pii_scan_enabled and self.settings.pii_scan_memory:
                    from pocketpaw.security.pii import get_pii_scanner

                    pii_result = get_pii_scanner().scan(full_response, source="assistant_response")
                    if pii_result.has_pii:
                        stored_response = pii_result.sanitized_text
                await self.memory.add_to_session(
                    session_key=session_key, role="assistant", content=stored_response
                )

                # 6. Auto-learn: extract facts from conversation (non-blocking)
                # Skip auto-learn on cancelled responses — partial data is unreliable.
                # Also skip when soul is active — soul.observe() + reflect() handles
                # fact extraction and memory consolidation, so auto_learn would duplicate.
                should_auto_learn = (
                    not cancelled
                    and self._soul_manager is None
                    and (
                        (self.settings.memory_backend == "mem0" and self.settings.mem0_auto_learn)
                        or (
                            self.settings.memory_backend == "file" and self.settings.file_auto_learn
                        )
                    )
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

                # Soul observation: feed turn for personality/memory evolution
                if self._soul_manager is not None and not cancelled:
                    t = asyncio.create_task(
                        self._soul_observe_and_emit(message.content, full_response, session_key)
                    )
                    self._background_tasks.add(t)
                    t.add_done_callback(self._background_tasks.discard)

            # Signal agent processing complete
            if agent_started:
                await self.bus.publish_system(
                    SystemEvent(event_type="agent_end", data={"session_key": session_key})
                )

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
                logger.warning(
                    "Failed to persist processing error in health engine",
                    exc_info=True,
                )
            # Kill the backend on error
            if router is not None:
                try:
                    await router.stop()
                except Exception:
                    logger.warning(
                        "Failed to stop router cleanly after processing error",
                        exc_info=True,
                    )

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
            # Signal agent processing complete even on error
            if agent_started:
                await self.bus.publish_system(
                    SystemEvent(event_type="agent_end", data={"session_key": session_key})
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

    async def _soul_observe_and_emit(
        self, user_input: str, agent_output: str, session_key: str
    ) -> None:
        """Observe interaction, run self-evaluation, and emit soul state event."""
        if self._soul_manager is None or not self._soul_manager._initialized:
            return
        try:
            await self._soul_manager.observe(user_input, agent_output)
            soul = self._soul_manager.soul
            if soul is not None:
                state = soul.state
                event_data: dict[str, Any] = {
                    "mood": getattr(state, "mood", None),
                    "energy": getattr(state, "energy", None),
                    "social_battery": getattr(state, "social_battery", None),
                    "focus": getattr(state, "focus", None),
                    "memory_count": soul.memory_count if hasattr(soul, "memory_count") else None,
                    "session_key": session_key,
                }

                # v0.2.8+: Include bond state if available
                if hasattr(soul, "bond") and soul.bond:
                    try:
                        event_data["bond"] = (
                            soul.bond.model_dump() if hasattr(soul.bond, "model_dump") else str(soul.bond)
                        )
                    except Exception:
                        pass

                # v0.2.4+: Run rubric self-evaluation (non-blocking)
                eval_result = await self._soul_manager.evaluate(user_input, agent_output)
                if eval_result is not None:
                    event_data["evaluation"] = eval_result

                await self.bus.publish_system(
                    SystemEvent(
                        event_type="soul_state",
                        data=event_data,
                    )
                )
        except Exception:
            logger.debug("Soul observation failed (non-fatal)", exc_info=True)

    def reset_router(self) -> None:
        """Reset the router to pick up new settings."""
        self._router = None

        # Handle soul_enabled toggle at runtime
        settings = Settings.load()
        if settings.soul_enabled and self._soul_manager is None:
            try:
                from pocketpaw.soul.manager import SoulManager

                self._soul_manager = SoulManager(settings)
                asyncio.create_task(self._initialize_soul_runtime())
            except Exception:
                logger.debug("Soul runtime init failed", exc_info=True)
        elif not settings.soul_enabled and self._soul_manager is not None:
            if self._soul_manager._initialized:
                asyncio.create_task(self._teardown_soul_runtime())
            else:
                # Not yet initialized, just discard the reference
                self._soul_manager = None

    def _build_cognitive_engine(self) -> Any:
        """Build a CognitiveEngine for soul, backed by the active agent backend."""
        try:
            from pocketpaw.soul.cognitive import PocketPawCognitiveEngine
            return PocketPawCognitiveEngine(
                backend_provider=lambda: (
                    self._get_router()._backend if self._router is not None else None
                )
            )
        except ImportError:
            return None

    async def _initialize_soul_runtime(self) -> None:
        """Initialize soul when enabled at runtime."""
        if self._soul_manager is None:
            return
        try:
            engine = self._build_cognitive_engine()
            await self._soul_manager.initialize(engine=engine)
            if self._soul_manager.bootstrap_provider:
                self.context_builder.bootstrap = self._soul_manager.bootstrap_provider
            self._soul_manager.start_auto_save()
        except Exception:
            logger.exception("Soul runtime initialization failed")
            self._soul_manager = None

    async def _teardown_soul_runtime(self) -> None:
        """Tear down soul when disabled at runtime."""
        if self._soul_manager is None:
            return
        try:
            await self._soul_manager.shutdown()
        except Exception:
            logger.debug("Soul runtime teardown failed", exc_info=True)
        self._soul_manager = None
        from pocketpaw.bootstrap.default_provider import DefaultBootstrapProvider

        self.context_builder.bootstrap = DefaultBootstrapProvider()
