"""
Bootstrap protocol for agent identity and context.
Created: 2026-02-02
Updated: 2026-08-02 (PA-3b, feat/prompt-assembler-seam) — ``BootstrapContext``
  carries ``identity_cache_key``: the provider's own claim about what its
  ``identity`` + ``knowledge`` content is stable for. Defaulted to ``None`` (no
  claim) so every existing provider and every test constructor is unchanged.
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
    instructions: str = ""  # Behavioral instructions & tool usage guides
    knowledge: list[str] = field(default_factory=list)  # Key background info
    user_profile: str = ""  # USER.md content

    # What the PROVIDER says its ``identity`` + ``knowledge`` content is stable
    # for. ``None`` — the default, and what every non-soul provider returns —
    # means "I make no claim", and a consumer keying on this must fall back to
    # whatever it keyed on before.
    #
    # It travels WITH the text rather than being derived from it downstream for
    # the same reason ``PromptContext.surface_cache_key`` does: only the
    # provider knows which of the bytes it just rendered are meaningful and
    # which are counters. ``SoulBootstrapProvider`` renders a soul whose mood,
    # energy, focus, self-image confidences, bond level and memory count all
    # move on ordinary interaction; it is the only module that can say so
    # without parsing its own output from two packages away.
    identity_cache_key: str | None = None

    def to_system_prompt(self) -> str:
        """Combine fields into a coherent system prompt.

        Layout: tool instructions first (background context), then the identity
        block last — closest to the live conversation so the model pays more
        attention to it and drifts less over long exchanges.
        """
        parts: list[str] = []

        # 1. Tool docs / behavioural instructions go FIRST — they are long
        #    and act as background reference material.
        if self.instructions:
            parts.append("# Instructions")
            parts.append(self.instructions)

        if self.knowledge:
            parts.append("\n# Key Knowledge")
            for item in self.knowledge:
                parts.append(f"- {item}")

        # 2. Identity block goes LAST — wrapped in <identity> XML tags so the
        #    model treats it as a high-priority structural directive and it sits
        #    as close as possible to the actual conversation turns.
        identity_lines: list[str] = [
            "<identity>",
            f"# Identity: {self.name}",
            self.identity,
            "\n# Core Philosophy (Soul)",
            self.soul,
            "\n# Communication Style",
            self.style,
        ]
        if self.user_profile:
            identity_lines.append("\n# User Profile")
            identity_lines.append(self.user_profile)
        identity_lines.append("</identity>")
        parts.append("\n".join(identity_lines))
        return "\n\n".join(parts)

    def to_identity_block(self) -> str:
        """Get just the <identity> block for periodic reinforcement."""
        identity_lines: list[str] = [
            "<identity>",
            f"# Identity: {self.name}",
            self.identity,
            "\n# Core Philosophy (Soul)",
            self.soul,
            "\n# Communication Style",
            self.style,
        ]
        if self.user_profile:
            identity_lines.append("\n# User Profile")
            identity_lines.append(self.user_profile)
        identity_lines.append("</identity>")
        return "\n".join(identity_lines)


class BootstrapProviderProtocol(Protocol):
    """Protocol for loading agent bootstrap context."""

    async def get_context(self) -> "BootstrapContext":
        """Load and return the bootstrap context."""
        ...
