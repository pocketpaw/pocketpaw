"""Exceptions raised by the core agent runtime.

These carry no cloud/EE dependency. The cloud layer catches them broadly
(see ``agent_router`` / ``agent_bridge`` in ``pocketpaw_ee``) — they are not
HTTP-mapped, so a plain exception hierarchy is sufficient. Before the OSS-EE
split the pool raised ``pocketpaw_ee.cloud.shared.errors`` types directly;
that cross-import is what this module replaces.

Updated: 2026-06-28 (feat/aiam-agent-revoke, AW-4) — added ``AgentDisabled``,
raised by ``AgentPool.get`` when an admin has soft-disabled an agent (the
``disabled`` flag on the cloud Agent doc). Distinct from ``AgentNotFound`` so
dispatch sites can tell "no such agent" apart from "revoked, finish in-flight
runs only" and surface a clean "agent unavailable" instead of a 500.
"""

from __future__ import annotations


class AgentRuntimeError(Exception):
    """Base class for core agent-runtime failures."""


class AgentNotFound(AgentRuntimeError, LookupError):
    """No agent exists for the requested id."""

    def __init__(self, agent_id: str) -> None:
        super().__init__(f"agent not found: {agent_id}")
        self.agent_id = agent_id


class AgentDisabled(AgentRuntimeError):
    """The agent exists but has been soft-disabled (revoked) by an admin.

    Raised by ``AgentPool.get`` so a NEW resolve fails closed everywhere at
    once. In-flight runs are unaffected — only new resolutions are blocked.
    Dispatch sites should skip (group/DM) or surface a clean "agent
    unavailable" error (direct chat) rather than 500.
    """

    def __init__(self, agent_id: str) -> None:
        super().__init__(f"agent disabled: {agent_id}")
        self.agent_id = agent_id


class AgentBackendUnavailable(AgentRuntimeError):
    """The agent's configured backend is not registered/available."""

    def __init__(self, backend: str) -> None:
        super().__init__(f"agent backend not available: {backend}")
        self.backend = backend
