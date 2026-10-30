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

Updated: 2026-07-18 (feat/herdr-runtime-adapter, HR-1) — added
``HerdrUnavailable``, raised by ``HerdrRuntime`` (the flagged, fail-open
adapter for the ``herdr`` terminal multiplexer) whenever herdr cannot service
a call: the ``herdr_runtime_enabled`` flag is off, the ``herdr`` binary is
missing, or a herdr command fails at runtime (server down, socket error, or a
JSON error-envelope). Callers MUST catch it and degrade to today's non-herdr
behaviour — the adapter never crashes PocketPaw when herdr is absent.
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


class HerdrUnavailable(AgentRuntimeError):
    """The herdr terminal-multiplexer runtime cannot service this call.

    Raised by ``HerdrRuntime`` (``pocketpaw.agents.herdr_runtime``) when herdr
    is not usable: the ``herdr_runtime_enabled`` flag is off, the ``herdr``
    binary is not installed, or a herdr command fails at runtime (server not
    running, socket error, timeout, or a JSON error-envelope such as
    ``{"error": {"code": ..., "message": ...}}``).

    This is the adapter's single fail-open signal — callers MUST catch it and
    degrade to today's non-herdr behaviour rather than let it propagate. The
    adapter never crashes PocketPaw when herdr is absent (same discipline as
    the Fable advisor). Guard cheaply with ``HerdrRuntime.available`` before a
    call to avoid the exception path entirely.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"herdr runtime unavailable: {reason}")
        self.reason = reason
