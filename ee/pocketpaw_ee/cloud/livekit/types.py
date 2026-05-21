"""Shared types for the LiveKit calling module.

Holds the ``MeetingAgentProtocol`` that both ``service.py`` and
``agent.py`` refer to, breaking the circular import between them.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class MeetingAgentProtocol(Protocol):
    """Minimal interface the LiveKit service needs from a meeting agent.

    ``service.py`` stores agents in ``_active_agents`` and calls
    ``start()`` / ``stop()`` on them.  The protocol avoids importing
    ``CallMeetingAgent`` directly from ``agent.py``.
    """

    group_id: str
    room_name: str

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
