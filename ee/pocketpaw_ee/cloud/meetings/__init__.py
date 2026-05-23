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

Note: this commit folds in #1140's Recall.ai work via merge. The Recall
code currently lives at the top level (router.py, service.py, webhooks.py,
recall_client.py, etc.) — follow-up commits move it under
``providers/recall/`` to match the platform layout.
"""

# Re-export the legacy top-level router + webhooks until the providers/recall/
# move lands. ``ee/cloud/__init__.py`` imports both names.
from pocketpaw_ee.cloud.meetings.router import router
from pocketpaw_ee.cloud.meetings.webhooks import router as webhooks_router

__all__ = ["router", "webhooks_router"]
