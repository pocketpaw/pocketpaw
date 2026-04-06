# Soul bridge — connects soul-protocol into PocketPaw's bootstrap and agent loop.
# Created: 2026-03-02
# SoulBootstrapProvider implements BootstrapProviderProtocol.
# SoulBridge provides high-level observe/recall for the agent loop.

from __future__ import annotations

from typing import TYPE_CHECKING

from pocketpaw.bootstrap.protocol import BootstrapContext

if TYPE_CHECKING:
    from soul_protocol import Soul


class SoulBootstrapProvider:
    """Wraps a Soul into PocketPaw's BootstrapProviderProtocol.

    Maps the soul's system prompt, personality, and memories into
    the BootstrapContext fields that AgentContextBuilder consumes.
    Preserves instructions (tool docs) and user profile from the
    default provider so the agent retains all its capabilities.
    """

    def __init__(self, soul: Soul) -> None:
        self._soul = soul
        # Load instructions and user profile from default provider once
        from pocketpaw.bootstrap.default_provider import DefaultBootstrapProvider

        self._default = DefaultBootstrapProvider()

    async def get_context(self) -> BootstrapContext:
        """Build BootstrapContext from the soul's current state.

        Identity, soul, and style come from the Soul instance.
        Instructions and user_profile come from the default provider
        (INSTRUCTIONS.md, USER.md) so tool docs and user context are preserved.
        """
        soul = self._soul

        # Load default context for instructions + user_profile
        default_ctx = await self._default.get_context()

        system_prompt = soul.to_system_prompt()

        # Extract personality, mood, and biorhythm for style hints
        state = soul.state
        mood_hint = f"Current mood: {state.mood}" if hasattr(state, "mood") else ""
        energy_hint = f"Energy: {state.energy}" if hasattr(state, "energy") else ""
        tired_hint = ""
        if hasattr(state, "energy") and hasattr(state, "tired_threshold"):
            if state.energy <= state.tired_threshold:
                tired_hint = "Status: fatigued (low energy)"
        style_parts = [s for s in [mood_hint, energy_hint, tired_hint] if s]

        # Pull active self-images for knowledge context
        knowledge: list[str] = []
        if hasattr(soul, "self_model") and soul.self_model:
            try:
                images = soul.self_model.get_active_self_images(limit=5)
                for img in images:
                    knowledge.append(f"[{img.domain}] confidence={img.confidence}")
            except Exception:
                pass

        return BootstrapContext(
            name=soul.name if hasattr(soul, "name") else "Paw",
            identity=system_prompt,
            soul="I am a persistent AI companion powered by soul-protocol.",
            style="; ".join(style_parts) if style_parts else "Helpful and attentive.",
            instructions=default_ctx.instructions,
            knowledge=knowledge,
            user_profile=default_ctx.user_profile,
        )


class SoulBridge:
    """High-level bridge for observe/recall in the agent loop."""

    def __init__(self, soul: Soul) -> None:
        self._soul = soul

    async def observe(self, user_input: str, agent_output: str) -> None:
        """Record an interaction for the soul to learn from."""
        try:
            from soul_protocol import Interaction

            await self._soul.observe(Interaction(user_input=user_input, agent_output=agent_output))
        except Exception:
            pass  # Observation failure should never break the agent loop

    async def recall(self, query: str, limit: int = 5) -> list[str]:
        """Search soul memories and return content strings."""
        try:
            memories = await self._soul.recall(query, limit=limit)
            return [m.content for m in memories]
        except Exception:
            return []
