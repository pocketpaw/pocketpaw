"""Unified Agent Loop.
Created: 2026-02-02
Part of Nanobot Pattern Adoption.
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
from typing import Any

from pocketclaw.config import get_settings, Settings
from pocketclaw.bus import get_message_bus, InboundMessage, OutboundMessage, Channel, SystemEvent
from pocketclaw.memory import get_memory_manager
from pocketclaw.bootstrap import AgentContextBuilder
from pocketclaw.agents.router import AgentRouter
from pocketclaw.stats.manager import get_stats_manager

logger = logging.getLogger(__name__)


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
            asyncio.create_task(self._process_message(message))

    async def _process_message(self, message: InboundMessage) -> None:
        """Process a single message flow using AgentRouter."""
        session_key = message.session_key
        logger.info(f"⚡ Processing message from {session_key}")

        # Initialize stats tracking
        stats_manager = get_stats_manager()
        settings = Settings.load()
        call_id = stats_manager.start_call(session_key, settings.agent_backend)
        
        # Variables to track token usage and model
        input_tokens = 0
        output_tokens = 0
        model_name = ""
        success = True
        error_msg = ""

        try:
            # 1. Store User Message
            await self.memory.add_to_session(
                session_key=session_key,
                role="user",
                content=message.content,
                metadata=message.metadata,
            )

            # 2. Emit thinking event
            await self.bus.publish_system(
                SystemEvent(event_type="thinking", data={"session_key": session_key})
            )

            # 3. Run through AgentRouter (handles all backends)
            router = self._get_router()
            full_response = ""

            async for chunk in router.run(message.content):
                chunk_type = chunk.get("type", "")
                content = chunk.get("content", "")
                metadata = chunk.get("metadata") or {}
                
                # Extract token usage from metadata if available
                if "usage" in metadata:
                    usage = metadata["usage"]
                    if "input_tokens" in usage:
                        input_tokens = usage["input_tokens"]
                    if "output_tokens" in usage:
                        output_tokens = usage["output_tokens"]
                
                # Extract model name if available
                if "model" in metadata and not model_name:
                    model_name = metadata["model"]

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
                            data={"name": f"run_{language}", "params": {"code": content[:100]}},
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

                elif chunk_type == "tool_use":
                    # Emit tool_start system event for Activity panel
                    tool_name = metadata.get("name") or metadata.get("tool", "unknown")
                    tool_input = metadata.get("input") or metadata
                    await self.bus.publish_system(
                        SystemEvent(
                            event_type="tool_start", data={"name": tool_name, "params": tool_input}
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
                                "result": content[:200],  # Truncate for display
                                "status": "success",
                            },
                        )
                    )

                elif chunk_type == "error":
                    # Emit error and send to user
                    success = False
                    error_msg = content
                    await self.bus.publish_system(
                        SystemEvent(
                            event_type="tool_result",
                            data={"name": "agent", "result": content, "status": "error"},
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

            # 4. Send stream end marker
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=message.channel, chat_id=message.chat_id, content="", is_stream_end=True
                )
            )

            # 5. Store assistant response in memory
            if full_response:
                await self.memory.add_to_session(
                    session_key=session_key, role="assistant", content=full_response
                )

        except Exception as e:
            logger.exception(f"❌ Error processing message: {e}")
            success = False
            error_msg = str(e)
            # Send error message
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=message.channel,
                    chat_id=message.chat_id,
                    content=f"An error occurred: {str(e)}",
                )
            )
            # Send stream end on error
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=message.channel, chat_id=message.chat_id, content="", is_stream_end=True
                )
            )
        
        finally:
            # End stats tracking and record
            stats = stats_manager.end_call(
                call_id=call_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                model=model_name,
                success=success,
                error=error_msg,
            )
            
            # Broadcast stats to dashboard via system event
            if stats:
                await self.bus.publish_system(
                    SystemEvent(
                        event_type="agent_stats",
                        data={
                            "response_time_ms": stats.response_time_ms,
                            "input_tokens": stats.input_tokens,
                            "output_tokens": stats.output_tokens,
                            "total_tokens": stats.total_tokens,
                            "model": stats.model,
                            "backend": stats.backend,
                            "success": stats.success,
                            "timestamp": stats.timestamp.isoformat(),
                        }
                    )
                )

    async def _send_response(self, original: InboundMessage, content: str) -> None:
        """Helper to send a simple text response."""
        await self.bus.publish_outbound(
            OutboundMessage(channel=original.channel, chat_id=original.chat_id, content=content)
        )

    def reset_router(self) -> None:
        """Reset the router to pick up new settings."""
        self._router = None
