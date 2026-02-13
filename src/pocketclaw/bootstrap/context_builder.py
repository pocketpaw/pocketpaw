"""
Builder for assembling the full agent context.
Created: 2026-02-02
Updated: 2026-02-07 - Semantic context injection for mem0 backend
Updated: 2026-02-10 - Channel-aware format hints
"""

from __future__ import annotations

from pocketclaw.bootstrap.default_provider import DefaultBootstrapProvider
from pocketclaw.bootstrap.protocol import BootstrapProviderProtocol
from pocketclaw.bus.events import Channel
from pocketclaw.bus.format import CHANNEL_FORMAT_HINTS
from pocketclaw.memory.manager import MemoryManager, get_memory_manager


class AgentContextBuilder:
    """
    Assembles the final system prompt by combining:
    1. Static Identity (Bootstrap)
    2. Dynamic Memory (MemoryManager)
    3. Current State (e.g., date/time, active tasks)
    """

    def __init__(
        self,
        bootstrap_provider: BootstrapProviderProtocol | None = None,
        memory_manager: MemoryManager | None = None,
    ):
        self.bootstrap = bootstrap_provider or DefaultBootstrapProvider()
        self.memory = memory_manager or get_memory_manager()

    async def build_system_prompt(
        self,
        include_memory: bool = True,
        user_query: str | None = None,
        channel: Channel | None = None,
        sender_id: str | None = None,
        session_key: str | None = None,
    ) -> str:
        """Build the complete system prompt.

        Args:
            include_memory: Whether to include memory context.
            user_query: Current user message for semantic memory search (mem0).
            channel: Target channel for format-aware hints.
            sender_id: Sender identifier for memory scoping and identity injection.
            session_key: Current session key for session management tools.
        """
        # 1. Load static identity
        context = await self.bootstrap.get_context()
        base_prompt = context.to_system_prompt()

        parts = [base_prompt]

        # 2. Inject memory context (scoped to sender)
        if include_memory:
            if user_query:
                memory_context = await self.memory.get_semantic_context(
                    user_query, sender_id=sender_id
                )
            else:
                memory_context = await self.memory.get_context_for_agent(sender_id=sender_id)
            if memory_context:
                parts.append(
                    "\n# Memory Context (already loaded — use this directly, "
                    "do NOT call recall unless you need something not listed here)\n"
                    + memory_context
                )

        # 3. Inject sender identity block
        if sender_id:
            from pocketclaw.config import get_settings

            settings = get_settings()
            if settings.owner_id:
                is_owner = sender_id == settings.owner_id
                role = "owner" if is_owner else "external user"
                identity_block = (
                    f"\n# Current Conversation\n"
                    f"You are speaking with sender_id={sender_id} (role: {role})."
                )
                if is_owner:
                    identity_block += "\nThis is your owner."
                else:
                    identity_block += (
                        "\nThis is NOT your owner. Be helpful but do not share "
                        "owner-private information."
                    )
                parts.append(identity_block)

        # 4. Inject channel format hint
        if channel:
            hint = CHANNEL_FORMAT_HINTS.get(channel, "")
            if hint:
                parts.append(f"\n# Response Format\n{hint}")

        # 5. Inject session key for session management tools
        if session_key:
            parts.append(
                f"\n# Session Management\n"
                f"Current session_key: {session_key}\n"
                f"Pass this value to any session tool (new_session, list_sessions, "
                f"switch_session, clear_session, rename_session, delete_session)."
            )

        return "\n\n".join(parts)
