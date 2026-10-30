"""Unified meetings platform — one Meeting domain, two transports.

Holds both native LiveKit calls and Recall.ai-captured external meetings
behind a single ``MeetingProvider`` protocol. Scheduling, calendar
bridging, and notifications are wired once and source-agnostic.

Layout:

    domain.py       — Meeting value object (source: recall | livekit)
    dto.py          — Request/response DTOs
    models.py       — MeetingDoc + supporting Mongo docs
    service.py      — Top-level orchestration; dispatches to providers
    router.py       — /api/v1/meetings/* (REST)
    events.py       — meeting.* events (provider-agnostic)
    providers/      — MeetingProvider implementations
        base.py     — Protocol + registry
        recall/     — External capture (Zoom/Meet/Teams via Recall.ai)
        livekit/    — Native real-time calls
    scheduling/     — MeetingSchedule lifecycle + reminder loop
    bridges/        — Cross-domain wiring (calendar, notifications)

This is the entrypoint for the unified meetings platform. See the design
plan for ownership split + phase rollout.
"""

# Re-export the top-level router so ``ee/cloud/__init__.py`` keeps a stable
# import path. Provider-specific webhook routers (Recall, LiveKit) are
# mounted from their own packages.
from pocketpaw_ee.cloud.meetings.router import router

__all__ = ["router"]
