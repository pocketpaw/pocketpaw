"""
Bootstrap protocol for agent identity and context.
Created: 2026-02-02
"""

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class BootstrapContext:
    """The core identity and context for the agent."""

    name: str
    identity: str  # The main system prompt / personality
    soul: str  # Deeper philosophical core
    style: str  # Communication style guidelines
    knowledge: list[str] = field(default_factory=list)  # Key background info
    user_profile: str = ""  # USER.md content

    def to_system_prompt(self) -> str:
        """Combine fields into a coherent system prompt."""
        parts = [
            f"# Identity: {self.name}",
            self.identity,
            "\n# Core Philosophy (Soul)",
            self.soul,
            "\n# Communication Style",
            self.style,
        ]

        if self.knowledge:
            parts.append("\n# Key Knowledge")
            for item in self.knowledge:
                parts.append(f"- {item}")

        if self.user_profile:
            parts.append("\n# User Profile")
            parts.append(self.user_profile)

        return "\n".join(parts)


class BootstrapProviderProtocol(Protocol):
    """Protocol for loading agent bootstrap context."""

    async def get_context(self) -> BootstrapContext:
        """Load and return the bootstrap context."""
        ...
