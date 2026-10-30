"""LiveKit — voice/video calling for groups.

Provides:
- ``LiveKitService`` — room management + token generation for LiveKit Cloud
- ``CallMeetingAgent`` — AI agent that listens to calls, transcribes, and posts meeting notes
- ``MeetingAgentProtocol`` — protocol to break circular imports between service and agent
- REST router at ``/api/v1/livekit/*``
"""

from __future__ import annotations

from pocketpaw_ee.cloud.livekit.router import router
from pocketpaw_ee.cloud.livekit.types import MeetingAgentProtocol

__all__ = ["router", "MeetingAgentProtocol"]
