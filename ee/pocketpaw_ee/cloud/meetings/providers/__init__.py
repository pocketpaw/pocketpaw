"""MeetingProvider implementations + registry.

Each provider implements ``base.MeetingProvider`` (plus optional
capability sub-protocols like ``SupportsRecording``). Providers register
themselves at import time; ``service.py`` dispatches to the right one
based on ``Meeting.source``.

Phase 1 ships the registry empty — concrete providers land in follow-up
commits / PRs:
    * recall — folded in from #1140
    * livekit — owned by a separate engineer, see hand-off guide
"""
