"""LiveKit meeting provider — native real-time calls.

Wraps the existing ``ee.cloud.livekit`` service (room mgmt, in-call agent,
composite egress recording) behind the unified ``MeetingProvider`` contract.

Phase 1 ships the package shell. The provider implementation is owned by
a separate engineer rebasing #1178 + #1186; see the hand-off guide.
"""
