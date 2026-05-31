"""Exceptions raised by the core agent runtime.

These carry no cloud/EE dependency. The cloud layer catches them broadly
(see ``agent_router`` / ``agent_bridge`` in ``pocketpaw_ee``) — they are not
HTTP-mapped, so a plain exception hierarchy is sufficient. Before the OSS-EE
split the pool raised ``pocketpaw_ee.cloud.shared.errors`` types directly;
that cross-import is what this module replaces.
"""

from __future__ import annotations


class AgentRuntimeError(Exception):
    """Base class for core agent-runtime failures."""


class AgentNotFound(AgentRuntimeError, LookupError):
    """No agent exists for the requested id."""

    def __init__(self, agent_id: str) -> None:
        super().__init__(f"agent not found: {agent_id}")
        self.agent_id = agent_id


class AgentBackendUnavailable(AgentRuntimeError):
    """The agent's configured backend is not registered/available."""

    def __init__(self, backend: str) -> None:
        super().__init__(f"agent backend not available: {backend}")
        self.backend = backend
