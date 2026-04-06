"""AGENTS.md support — standardized agent capability declaration.

Reads AGENTS.md files from target repositories to inject project-specific
constraints into the agent's system prompt, and publishes PocketPaw's own
AGENTS.md for other agents to consume.

Reference: https://github.com/anthropics/agents-md
"""

from __future__ import annotations

from pocketpaw.agents_md.loader import AgentsMd, AgentsMdLoader

__all__ = ["AgentsMd", "AgentsMdLoader"]
