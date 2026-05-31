# LiveKit Meeting Provider — Implementation Guide

> **Audience:** the engineer rebasing #1178 (LiveKit scheduling) and #1186
> (LiveKit recording) onto the new unified meetings platform.
>
> **You don't need the full platform plan to do this work.** This guide is
> self-contained. If you want the broader context, see
> `docs/plans/2026-05-23-unified-meetings-platform.md`, but it's not required.
>
> **What you are NOT changing:** the in-process LiveKit agent
> (`ee.cloud.livekit.agent.CallMeetingAgent`), the subprocess spawning, the
> Deepgram streaming in the agent, the LiveKit Cloud account setup, or any
> `livekit-api` SDK call. All of that stays. You're moving entry points
> and replacing 1178's stub `meetings/{domain,dto,service,router}.py` with
> a `MeetingProvider` implementation.
>
> **You have a working reference** — the Recall provider at
> `ee/pocketpaw_ee/cloud/meetings/providers/recall/` is fully wired into
> the platform. Read `providers/recall/provider.py` for the shape your
> `providers/livekit/provider.py` needs to match, and
> `providers/recall/__init__.py` for the registration pattern. The
> notifications bridge (`bridges/notifications.py`) already fans out
> `meeting.scheduled` / `meeting.cancelled` / `meeting.recording_ready` /
> `meeting.transcript_ready` to in-app notifications for every source —
> once you emit those events from your LiveKit code path, notifications
> light up automatically with no extra wiring.

---

## The 30-second summary

We're consolidating two parallel meeting modules (one for Recall-captured
Zoom/Meet calls, one for native LiveKit calls) into a single
`ee/cloud/meetings/` module. There is one `Meeting` document with a
`source: Literal["recall", "livekit"]` discriminator. Each source plugs in
via a `MeetingProvider` protocol.

Your job: implement `MeetingProvider` for `source="livekit"`, moving
1178's scheduling logic into the platform's `scheduling/` package and
1186's recording into your provider.

---

## What's already in place when you start

The entrypoint PR (#1192) will have landed before you start, so on `dev`
you'll find a fully populated platform — including a working Recall
provider you can pattern against. The layout:

```
ee/pocketpaw_ee/cloud/meetings/
    domain.py              # Meeting value object (read-only for you)
    dto.py                 # Request/response DTOs (read-only for you)
    models.py              # MeetingDoc Mongo document (read-only for you)
    service.py             # Top-level dispatch (read-only for you)
    router.py              # /api/v1/meetings/* (read-only for you)
    events.py              # The 5 unified events (read-only for you)

    providers/
        base.py            # MeetingProvider protocol (read-only for you)
        recall/            # ← FULL WORKING REFERENCE — pattern against this.
            __init__.py    # Side-effect registration: base.register(RecallProvider())
            provider.py    # Implements MeetingProvider + SupportsRecording
                           #   + SupportsTranscript. Copy the shape; replace
                           #   the recall_client calls with livekit calls.
            client.py      # Recall.ai REST wrapper (folded in from #1140)
            webhooks.py    # Recall webhook handler (Svix-signed)
            credentials.py # Encrypted-at-rest Zoom/Meet creds
            settings.py    # Transcription engine choice
            adapters/      # Zoom + Meet ConnectorProtocol adapters
            clients/       # Low-level Zoom + Meet REST clients
        livekit/           # ← Your house. Phase 1 leaves it as a docstring stub.
            __init__.py    # Will register LiveKitProvider on import (mirror recall/)
            # provider.py  ← You create.
            # recording.py ← You create (or fold into provider.py).
            # webhooks.py  ← You create.

    scheduling/
        service.py         # MeetingSchedule lifecycle (Phase 1 stub — your PR
                           # fills it from #1178's existing reminder logic)
        reminders.py       # 60s reminder loop (Phase 1 stub — same)

    bridges/
        notifications.py   # meeting.* → in-app notifications (WIRED, working).
                           # When you emit meeting.scheduled etc. from your
                           # provider, notifications fire — no extra wiring.
        calendar.py        # calendar.event.created → auto-create Recall Meeting.
                           # Lives in the platform; doesn't touch your code.
```

**You will not need to edit `domain.py`, `service.py`, `router.py`, or
`scheduling/`.** Those are the platform layer. If you find yourself wanting
to, stop and flag it — that's a signal we picked the wrong abstraction and
we should talk before you ship.

---

## The `MeetingProvider` protocol

This is the contract you're implementing. Lives in
`ee/cloud/meetings/providers/base.py`:

```python
from typing import Protocol, runtime_checkable

from pocketpaw_ee.cloud.meetings.domain import Meeting
from pocketpaw_ee.cloud.meetings.dto import CreateMeetingRequest
from pocketpaw_ee.cloud.shared.deps import RequestContext


@runtime_checkable
class MeetingProvider(Protocol):
    """Source-specific implementation behind the unified meetings module.

    Lifecycle: create → (scheduled) → start → ... → end.
    Cancel can fire from any state before end.
    """

    name: str  # "livekit" — used by the registry

    async def create(
        self, ctx: RequestContext, body: CreateMeetingRequest
    ) -> "ProviderCreateResult":
        """Reserve resources for the meeting. Called by service.create_meeting
        BEFORE the MeetingDoc is persisted. Return the provider-payload bits
        we should store on the doc (room_name, etc.) plus any join URL.

        For LiveKit: do NOT create the room here. Rooms are created lazily on
        start() so they don't accumulate when meetings are scheduled days
        ahead and never happen. Just compute the deterministic room_name and
        return it in provider_payload.
        """

    async def start(self, ctx: RequestContext, meeting: Meeting) -> "ProviderStartResult":
        """Transition the meeting to active. For LiveKit: create the room,
        spawn the in-call agent subprocess, return the join URL/token URL.
        Idempotent — start() may be called twice (reminders.py auto-start +
        user-clicked Join).
        """

    async def cancel(self, ctx: RequestContext, meeting: Meeting) -> None:
        """Cancel a scheduled-but-not-yet-started meeting. For LiveKit: no-op
        (nothing reserved server-side until start). Just return."""

    async def end(self, ctx: RequestContext, meeting: Meeting) -> None:
        """Tear down server-side resources for an active meeting. For LiveKit:
        stop the agent subprocess + delete the room. Idempotent.
        """

    # Optional capabilities — declared via additional protocols. Don't
    # implement these on MeetingProvider directly; implement on the
    # sub-protocol and the registry will check isinstance() at dispatch.


@runtime_checkable
class SupportsRecording(Protocol):
    async def request_recording(
        self, ctx: RequestContext, meeting: Meeting
    ) -> "RecordingRef": ...

    async def stop_recording(
        self, ctx: RequestContext, meeting: Meeting
    ) -> None: ...


@runtime_checkable
class SupportsTranscript(Protocol):
    async def fetch_transcript(
        self, ctx: RequestContext, meeting: Meeting
    ) -> "TranscriptArtefact | None": ...
```

You'll implement `MeetingProvider` + `SupportsRecording`. You won't
implement `SupportsTranscript` in this PR — LiveKit transcription happens
in-call via the existing Deepgram streaming in `agent.py`, not as a post-call
fetch. Post-call LiveKit transcripts are a follow-up.

---

## The `Meeting` and `MeetingDoc` shape

Already defined by Phase 1. You read these; you don't change them.

```python
# domain.py
class Meeting(BaseModel, frozen=True):
    id: str
    workspace_id: str
    source: Literal["recall", "livekit"]
    title: str
    scheduled_start: datetime | None
    scheduled_end: datetime | None
    actual_start: datetime | None
    actual_end: datetime | None
    status: Literal["scheduled", "active", "ended", "cancelled"]
    organizer_user_id: str
    participants: list[ParticipantRef]
    join_url: str | None
    recording_refs: list[RecordingRef]   # FileUpload ids, populated as recordings finish
    transcript_refs: list[TranscriptRef] # File ids of completed transcripts
    provider_payload: dict[str, Any]     # Source-specific bag — your home
```

For LiveKit, `provider_payload` will hold:
```python
{
    "room_name": "group-call-{group_id}",
    "group_id": "...",              # if the meeting is tied to a chat group
    "agent_pid": 12345,              # if active; cleared on end
    "active_egress_id": "EG_...",    # if recording; cleared on stop_recording
}
```

Keep it flat. Don't nest. Other code shouldn't need to know the schema
— it's opaque to everything outside `providers/livekit/`.

---

## Files to create

```
providers/livekit/
    __init__.py        # registers your provider with the registry
    provider.py        # implements MeetingProvider + SupportsRecording
    recording.py       # composite egress / S3 logic (from #1186)
    webhooks.py        # mounted at /api/v1/meetings/webhooks/livekit
```

### `providers/livekit/__init__.py`

```python
"""LiveKit meeting provider — native real-time calls hosted on our LiveKit Cloud."""

from pocketpaw_ee.cloud.meetings.providers import registry
from pocketpaw_ee.cloud.meetings.providers.livekit.provider import LiveKitProvider

registry.register(LiveKitProvider())

__all__ = ["LiveKitProvider"]
```

The Phase 1 entrypoint will eager-import `meetings.providers.livekit` from
`mount_cloud()`, so this side-effect registration runs at startup.

### `providers/livekit/provider.py`

This is the meat. Skeleton:

```python
"""LiveKit MeetingProvider — wraps the existing livekit.service for the
unified meetings platform."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pocketpaw_ee.cloud.livekit import service as livekit_service
from pocketpaw_ee.cloud.meetings.domain import Meeting
from pocketpaw_ee.cloud.meetings.dto import (
    CreateMeetingRequest,
    ProviderCreateResult,
    ProviderStartResult,
    RecordingRef,
)
from pocketpaw_ee.cloud.shared.deps import RequestContext


class LiveKitProvider:
    """Implements MeetingProvider + SupportsRecording for source='livekit'."""

    name = "livekit"

    async def create(
        self, ctx: RequestContext, body: CreateMeetingRequest
    ) -> ProviderCreateResult:
        # Resolve a deterministic room name. If the meeting is tied to a chat
        # group, reuse the group's call room; otherwise mint a fresh one off
        # the meeting id (which the service will generate before calling us).
        group_id = (body.provider_options or {}).get("group_id")
        if group_id:
            room_name = livekit_service.room_name_for_group(group_id)
            provider_payload = {"group_id": group_id, "room_name": room_name}
        else:
            room_name = f"meeting-{body.client_meeting_id}"
            provider_payload = {"room_name": room_name}

        # Deep-link join URL. The app handles ?join=<meeting_id> by fetching
        # the LiveKit access token + connecting.
        join_url = f"pocketpaw://meetings/{body.client_meeting_id}?join"

        return ProviderCreateResult(
            provider_payload=provider_payload,
            join_url=join_url,
        )

    async def start(self, ctx: RequestContext, meeting: Meeting) -> ProviderStartResult:
        room_name = meeting.provider_payload["room_name"]

        # Idempotent: create_room is a no-op if the room exists.
        await livekit_service.create_room(room_name)

        # Spawn the in-call agent. The existing service does this — we just
        # call into it. Track the subprocess by group_id (existing registry).
        group_id = meeting.provider_payload.get("group_id")
        if group_id:
            await livekit_service.start_meeting_agent(group_id, room_name)

        return ProviderStartResult(
            provider_payload_updates={
                "started_at": datetime.now(UTC).isoformat(),
            },
            join_url=meeting.join_url,  # unchanged from create()
        )

    async def cancel(self, ctx: RequestContext, meeting: Meeting) -> None:
        # Nothing reserved server-side until start() — pure no-op.
        return None

    async def end(self, ctx: RequestContext, meeting: Meeting) -> None:
        group_id = meeting.provider_payload.get("group_id")
        if group_id:
            await livekit_service.stop_meeting_agent(group_id)

        room_name = meeting.provider_payload["room_name"]
        await livekit_service.delete_room(room_name)

    # ----- SupportsRecording -----

    async def request_recording(self, ctx: RequestContext, meeting: Meeting) -> RecordingRef:
        from pocketpaw_ee.cloud.meetings.providers.livekit import recording

        room_name = meeting.provider_payload["room_name"]
        egress = await recording.start_composite_egress(
            workspace_id=ctx.workspace_id,
            meeting_id=meeting.id,
            room_name=room_name,
        )
        # Returning the ref also signals the service layer to update
        # provider_payload["active_egress_id"]. The actual FileUpload id
        # lands later, via the recording webhook (see webhooks.py below).
        return RecordingRef(
            provider="livekit",
            external_id=egress.egress_id,
            status="recording",
            started_at=datetime.now(UTC),
            file_id=None,  # filled on egress.done
        )

    async def stop_recording(self, ctx: RequestContext, meeting: Meeting) -> None:
        from pocketpaw_ee.cloud.meetings.providers.livekit import recording

        egress_id = meeting.provider_payload.get("active_egress_id")
        if not egress_id:
            return
        await recording.stop_egress(egress_id)
```

### `providers/livekit/recording.py`

Take 1186's existing recording code (today in `livekit/router.py` +
`livekit/service.py`) and move it here verbatim. Strip the HTTP layer
(no FastAPI route definitions) — the wrapping route lives in the unified
`meetings/router.py` already. You're exposing:

```python
async def start_composite_egress(workspace_id: str, meeting_id: str, room_name: str) -> EgressRef:
    """Start a RoomCompositeEgress to the workspace S3 bucket. Returns
    egress_id + the eventual S3 path. Tracks egress_id in the in-memory
    registry that webhooks.py reads on egress completion."""

async def stop_egress(egress_id: str) -> None:
    """Stop an active egress. The egress.done webhook fires once it lands
    in S3, and webhooks.py creates the FileUpload doc + emits
    MeetingRecordingReady."""
```

The S3 path scheme (`recordings/{workspace_id}/{meeting_id}/{timestamp}.mp4`)
stays the same. The workspace-owner permission check moves UP to the
service layer in `meetings/router.py` — you don't enforce it here.

### `providers/livekit/webhooks.py`

LiveKit Cloud fires webhooks for egress lifecycle. Mount your own router
at `/api/v1/meetings/webhooks/livekit`. The Phase 1 entrypoint includes
the mount line for you; you fill in the handler. Skeleton:

```python
"""LiveKit Cloud webhook — egress lifecycle. Mounted at
/api/v1/meetings/webhooks/livekit. Like the Recall webhook, this has no
auth dependency — trust is established by signature verification (LiveKit
uses its own JWT-signed webhook scheme; see livekit-api docs).
"""

from fastapi import APIRouter, Request
from pocketpaw_ee.cloud.meetings import service as meetings_service
from pocketpaw_ee.cloud.meetings.events import MeetingRecordingReady
from pocketpaw_ee.cloud.realtime.bus import emit

router = APIRouter(prefix="/meetings/webhooks", tags=["Meetings"])


@router.post("/livekit")
async def livekit_webhook(request: Request) -> dict:
    body = await request.json()
    # ... verify signature (LiveKit SDK has a helper) ...

    event_type = body.get("event")
    if event_type == "egress_ended":
        egress_id = body["egress_info"]["egress_id"]
        s3_url = body["egress_info"]["file"]["location"]

        meeting = await meetings_service.find_by_active_egress(egress_id)
        if not meeting:
            return {"ok": True, "ignored": "unknown_egress"}

        # Upload to /files: write a FileUpload row pointing at the S3 path.
        file_id = await meetings_service.attach_recording(
            meeting_id=meeting.id,
            s3_url=s3_url,
        )
        await emit(MeetingRecordingReady(
            workspace_id=meeting.workspace_id,
            meeting_id=meeting.id,
            file_id=file_id,
            source="livekit",
        ))
        return {"ok": True, "recording_attached": file_id}

    return {"ok": True, "ignored": event_type}
```

Note: `meetings_service.find_by_active_egress` and `attach_recording` are
platform helpers exposed by Phase 1 specifically because both providers
need them. If they don't exist when you start, flag it and we'll add them
— don't reach into `MeetingDoc` directly from your provider.

---

## What you delete from #1178 and #1186

### From #1178

```
ee/pocketpaw_ee/cloud/meetings/__init__.py     # replaced by Phase 1
ee/pocketpaw_ee/cloud/meetings/domain.py       # replaced by Phase 1
ee/pocketpaw_ee/cloud/meetings/dto.py          # replaced by Phase 1
ee/pocketpaw_ee/cloud/meetings/models.py       # replaced by Phase 1
ee/pocketpaw_ee/cloud/meetings/router.py       # replaced by Phase 1
ee/pocketpaw_ee/cloud/meetings/service.py      # replaced by Phase 1
```

You keep:
- The LiveKit agent fixes in `livekit/agent.py` (afa22e8 + 84ce071) — those
  are bug fixes independent of the platform refactor.
- The realtime event additions in `_core/realtime/audience.py` +
  `_core/realtime/events.py` — those become the building blocks of the
  unified `meetings/events.py` in Phase 1.
- The notification.py touch — confirm the notification types
  (`meeting_scheduled`, `meeting_started`, `meeting_reminder`,
  `meeting_cancelled`) match what `meetings/bridges/notifications.py`
  produces; reconcile if not.

### From #1186

```
ee/pocketpaw_ee/cloud/livekit/router.py        # recording endpoints move OUT
ee/pocketpaw_ee/cloud/livekit/service.py       # recording helpers move to providers/livekit/recording.py
```

You keep the recording behaviour exactly — just the entry point moves
from `POST /api/v1/livekit/rooms/{group_id}/recording/start` to
`POST /api/v1/meetings/{meeting_id}/recording/start` (the unified route is
in `meetings/router.py`, written for you, and calls
`provider.request_recording(ctx, meeting)`).

The workspace-owner permission check moves to the platform's
`meetings/router.py`. You can drop the inline `_require_workspace_owner`
helper.

---

## Wiring + tests checklist

When you ship, verify:

- [ ] `from pocketpaw_ee.cloud.meetings.providers.livekit import LiveKitProvider`
      registers without ImportError on dashboard boot.
- [ ] `POST /api/v1/meetings` with `source="livekit"` creates a `MeetingDoc`
      and returns a join URL.
- [ ] `POST /api/v1/meetings/{id}/start` lazily creates the LiveKit room.
- [ ] The reminder loop (already in `scheduling/reminders.py`) auto-starts
      `livekit` meetings the same way it does today for #1178 — no special
      casing needed.
- [ ] `POST /api/v1/meetings/{id}/recording/start` starts an egress.
- [ ] `egress_ended` webhook lands a FileUpload + emits
      `MeetingRecordingReady`.
- [ ] `MeetingScheduled`/`Started`/`Cancelled` events route through
      `bridges/notifications.py` and produce the same `meeting_*`
      notification types #1178 + #235 expect.
- [ ] All existing LiveKit agent + recording integration tests pass with
      the new entry points. Old tests for the deleted
      `meetings/{domain,dto,...}.py` files get removed.
- [ ] One end-to-end test: create → schedule for 60s out → reminder fires
      at 55s mark → auto-start fires at 60s → recording starts → end →
      egress webhook → MeetingRecordingReady event observed.

---

## What you don't need to do

- **Do NOT add UI.** Frontend rebases for #235 are owned by the frontend
  team — they'll wire `ScheduleMeetingModal` to `POST /api/v1/meetings`
  with `source="livekit"` after you ship.
- **Do NOT touch Recall code.** It rebases in a parallel PR.
- **Do NOT add `SupportsTranscript`** for LiveKit. Post-call LiveKit
  transcripts are a follow-up — the in-call Deepgram streaming agent
  already covers the live case.
- **Do NOT change the LiveKit Cloud configuration** (`LIVEKIT_URL`,
  `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`). Env-var contract is unchanged.

---

## When you need us

Ping us if:

- Phase 1 isn't on `dev` yet. Don't start the rebase until it lands —
  you'll just have to redo work.
- `meetings_service.find_by_active_egress` / `attach_recording` /
  `RequestContext` / any helper this guide names doesn't exist or has a
  different signature. The platform layer is supposed to give you these;
  if it doesn't, that's our bug to fix in Phase 1.
- The `provider_payload` shape feels too restrictive. We picked a flat dict
  on purpose, but if you legitimately need nested structure, push back —
  it's better to evolve the contract now than after both providers ship.
- A LiveKit fix you need to land changes anything outside `providers/livekit/`
  or `livekit/agent.py`. The whole point of the abstraction is that LiveKit-
  specific changes stay LiveKit-local; if you're reaching into the
  platform, talk to us first.
