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
    """

    def __init__(self, soul: Soul) -> None:
        self._soul = soul

    async def get_context(self) -> BootstrapContext:
        """Build BootstrapContext from the soul's current state."""
        soul = self._soul
        system_prompt = soul.to_system_prompt()

        # Extract personality and mood for style hints
        state = soul.state
        mood_hint = f"Current mood: {state.mood}" if hasattr(state, "mood") else ""
        energy_hint = f"Energy: {state.energy}" if hasattr(state, "energy") else ""
        style_parts = [s for s in [mood_hint, energy_hint] if s]

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
            knowledge=knowledge,
        )


class SoulBridge:
    """High-level bridge for observe/recall in the agent loop."""

    def __init__(self, soul: Soul) -> None:
        self._soul = soul

    async def observe(self, user_input: str, agent_output: str) -> None:
        """Record an interaction for the soul to learn from."""
        try:
            from soul_protocol import Interaction

            await self._soul.observe(
                Interaction(user_input=user_input, agent_output=agent_output)
            )
        except Exception:
            pass  # Observation failure should never break the agent loop

    async def recall(self, query: str, limit: int = 5) -> list[str]:
        """Search soul memories and return content strings."""
        try:
            memories = await self._soul.recall(query, limit=limit)
            return [m.content for m in memories]
        except Exception:
            return []
