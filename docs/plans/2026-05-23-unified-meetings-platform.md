# Unified Meetings Platform — Implementation Plan

> **Status:** APPROVED 2026-05-23 — open questions resolved (see end of doc).
>
> **Branch:** `feat/meetings-platform` (both `pocketpaw` and `paw-enterprise`).
>
> **Ownership (revised 2026-05-23):**
> - **Entrypoint PR (Phase 1 + Recall provider + bridges):** us. ✅ landing on PR #1192.
>   This single PR ships:
>     * the unified platform (`domain`, `dto`, `service`, `router`, `events`,
>       `providers/base`, `scheduling/`, `bridges/`),
>     * the **Recall.ai provider** (full working implementation under
>       `providers/recall/`, registered at startup),
>     * the **notifications bridge** (meeting.* → in-app notifications),
>     * the **calendar bridge** (calendar.event.created → auto-create Meeting
>       from any Zoom/Meet URL in the event description).
>   `service.create_meeting` + `cancel_meeting` already dispatch through the
>   registry — Recall is fully integrated.
> - **LiveKit rebase (Phase 3 + Phase 4):** the LiveKit engineer. They write
>   their `providers/livekit/provider.py` against the same protocol Recall
>   implements; the bridges already work source-agnostically so LiveKit
>   gets notifications + (later) calendar auto-create for free.
>   Hand-off doc: `docs/plans/2026-05-23-livekit-provider-guide.md`.

**Goal:** One coherent `ee/cloud/meetings/` module that hosts *both* native
LiveKit calls and external Recall.ai-captured meetings, with a single
scheduling lifecycle, a single notification surface, and a single calendar
bridge. Land the architectural foundation as one **entrypoint PR**, then
rebase the four in-flight feature PRs onto it without losing work.

**Why now:** Two PRs targeting `dev` (#1140 Recall + #1178 LiveKit
scheduling) both create incompatible `meetings/{domain,dto,service,router}.py`
files. Whichever lands first forces the other into a painful rebase, and the
codebase ends up with one of two losing names baked in. We need to settle the
shape *once*, in the open, before either ships.

---

## In-flight PRs this plan unblocks

| # | Repo | Branch | What it ships | Status |
|---|---|---|---|---|
| **1140** | pocketpaw | `feat/meetings-integration` | Recall.ai bot integration (Zoom + Meet), connector page, async Deepgram nova-3 transcription, MCP tools | OPEN → dev |
| **1178** | pocketpaw | `feat/meetings-and-calls` | LiveKit meeting scheduling, reminders, auto-start, notifications, agent fixes | OPEN → dev |
| **1186** | pocketpaw | `feat/meeting_records` (stacked on 1178) | LiveKit composite recording → S3 → /files | OPEN → 1178 |
| **224** | paw-enterprise | `feat/meetings-integration` | Settings → Meetings panel (Zoom/Meet connector cards) | OPEN → dev |
| **235** | paw-enterprise | `feat/meetings-and-calls` | ScheduleMeetingModal, sidebar list, meeting.* notifications | OPEN → dev |
| **241** | paw-enterprise | `feat/meeting_records` (stacked on 240) | Call panel UI polish (filmstrip, fullscreen, auto-hide) | OPEN → 240 |

Both Workstream A (1140 + 224) and Workstream B (1178 + 235) target `dev`
and write to `ee/cloud/meetings/*`. Workstream C extends LiveKit only.

---

## Architectural decision: one domain, two transports

Before this plan, the two open meeting PRs treat "meeting" as two different
domain primitives:

- **A — external capture (Recall):** a third-party Zoom/Meet/Teams call we
  send a bot into. Bot lifecycle, transcript pipeline, provider credentials,
  asymmetric trust.
- **B — native calls (LiveKit):** a room we host on our own LiveKit Cloud.
  Tokens, participants, egress recording, in-room agent.

These are not the same thing — but the *user's* mental model treats them as
one: "the meetings I have." Scheduling, notifications, transcripts, recording
artefacts are concerns that apply to both. We split *transport* from
*meeting*: one `Meeting` domain document, two implementations of a single
`MeetingProvider` protocol.

```
ee/pocketpaw_ee/cloud/meetings/
    __init__.py
    domain.py              # Meeting (provider-agnostic value object)
    dto.py                 # request/response DTOs (CreateMeetingRequest, …)
    models.py              # MeetingDoc + supporting Mongo docs
    service.py             # Top-level orchestration; routes to providers
    router.py              # /api/v1/meetings/* (REST)
    events.py              # meeting.scheduled / .started / .recording_ready
                           # / .transcript_ready / .cancelled — emitted regardless
                           # of provider so notifications and KB don't fork

    providers/
        __init__.py
        base.py            # MeetingProvider protocol + registry
        recall/            # External capture (Workstream A)
            __init__.py
            provider.py    # implements MeetingProvider
            client.py      # Recall.ai REST wrapper (was recall_client.py)
            credentials.py # Mongo-stored Zoom/Meet OAuth creds
            settings.py    # transcription engine choice (deepgram/recall-native/…)
            webhooks.py    # mounted at /api/v1/meetings/webhooks/recall
            adapters/      # the Zoom/Meet connector-protocol adapters
            clients/       # low-level Zoom/Meet REST clients
        livekit/           # Native calls (Workstream B + C)
            __init__.py
            provider.py    # implements MeetingProvider
            service.py     # room lifecycle, token generation
            agent.py       # in-call subprocess agent (existing)
            recording.py   # egress / S3 composite (#1186)
            webhooks.py    # mounted at /api/v1/meetings/webhooks/livekit
            types.py       # MeetingAgentProtocol (existing)

    scheduling/
        __init__.py
        service.py         # MeetingSchedule lifecycle: schedule → start → end
        reminders.py       # background loop, 5-min reminders, exact-time auto-start

    bridges/
        __init__.py
        calendar.py        # ee.calendar event → Meeting auto-create
        notifications.py   # meeting.* events → notification fan-out
```

### The `MeetingProvider` protocol

```python
# providers/base.py
class MeetingProvider(Protocol):
    name: str  # "recall" | "livekit"

    async def create(self, ctx, body: CreateMeetingRequest) -> ProviderCreateResult: ...
    async def start(self, ctx, meeting: Meeting) -> ProviderStartResult: ...
    async def cancel(self, ctx, meeting: Meeting) -> None: ...
    async def end(self, ctx, meeting: Meeting) -> None: ...

    # Optional capability — declared via duck typing or a sub-protocol:
    async def request_recording(self, ctx, meeting: Meeting) -> RecordingRef: ...
    async def fetch_transcript(self, ctx, meeting: Meeting) -> TranscriptArtefact | None: ...
```

`service.py` resolves the provider for a `meeting.source` and dispatches.
Webhooks, agent MCP tools, and the calendar bridge all talk to the
`meetings.service` surface — they never reach into `providers/{recall,livekit}/`
directly.

### What stays out of scope for the entrypoint PR

- No Outlook calendar integration (only existing `ee.calendar` Google bridge).
- No transcription for LiveKit native calls (today the in-call agent already
  has Deepgram streaming wired; we keep that as-is, and treat *post-call
  transcript fetch* as a follow-up).
- No new UI components — the entrypoint is backend-only refactor. Frontend
  PRs (224, 235, 241) rebase onto the new API shape after.

---

## Timeline

The entrypoint PR (Phase 1 + Recall + bridges) is **shipped on PR #1192**.
Phases 3–6 are follow-up PRs each in-flight team rebases onto.

### Phase 1 — Entrypoint PR (this branch, `feat/meetings-platform`)

**Outcome:** `ee/cloud/meetings/` exists with the new shape, empty provider
shells, no behaviour change yet (no provider does anything until 1140/1178
rebase in).

1. Land the directory layout above with minimal stubs:
   - `domain.py`: `Meeting` value object (source: Literal["recall", "livekit"],
     scheduled_start, status, participants, recording_refs, transcript_refs,
     workspace_id).
   - `models.py`: `MeetingDoc` (replaces both 1140's and 1178's competing
     models). Unified shape with a `source` discriminator field.
   - `dto.py`: `CreateMeetingRequest`, `UpdateMeetingRequest`, `MeetingResponse`,
     `ListMeetingsRequest`. Generic across sources.
   - `service.py`: thin top-level — `create_meeting`, `list_meetings`,
     `get_meeting`, `cancel_meeting`, `start_meeting`, `end_meeting`. Each
     calls `providers.resolve(source).<op>`.
   - `router.py`: `/api/v1/meetings/*` routes calling the service. License
     gate stays. No provider-specific routes here.
   - `events.py`: `MeetingScheduled`, `MeetingStarted`, `MeetingEnded`,
     `MeetingCancelled`, `MeetingRecordingReady`, `MeetingTranscriptReady`.
   - `providers/base.py`: the `MeetingProvider` protocol + registry.
   - `providers/recall/__init__.py`, `providers/livekit/__init__.py`: empty
     placeholders that register `None` providers (so the registry exists but
     dispatch returns NotImplemented). Real implementations land in Phase 2/3.
   - `scheduling/{service,reminders}.py`: ports the 1178 reminder loop logic
     verbatim (it's already source-agnostic — just calls `meetings.service`
     `.start` instead of `livekit.service.create_room` directly).
   - `bridges/notifications.py`: subscribes to `MeetingScheduled`/`Started`/
     `Cancelled` and fans out via `notifications.service`. Notification
     templates are the ones from 1178 — they don't care about source.

2. Write 4 small unit tests covering: registry resolves by source, service
   raises `NotImplemented` for unregistered source, scheduling.reminders
   loop ticks correctly under freezegun, events are emitted with the right
   audience.

3. Update `ee/cloud/__init__.py` `mount_cloud()` to mount the new
   `meetings.router` and `meetings.providers.recall.webhooks.router` /
   `meetings.providers.livekit.webhooks.router` (latter is empty in Phase 1).

4. **Import-linter contract** — add a contract so nothing outside
   `meetings/providers/recall/` and `meetings/providers/livekit/` may import
   `recall_client` / `livekit-api`. Keeps the abstraction honest.

5. Open as a draft PR titled `feat(meetings): unified provider platform —
   entrypoint`. Body cites this plan, lists the four follow-up PR rebases,
   and is marked "blocks #1140, #1178; required before either merges."

6. Get review + sign-off from whoever owns #1140 + #1178 before merge.

### Phase 2 — Rebase #1140 (Recall provider) onto the new platform

Owner: 1140's branch.

1. Move `recall_client.py` → `meetings/providers/recall/client.py`. No diff
   beyond the import path.
2. Move `credentials.py`, `settings.py`, `webhooks.py`, `adapters/`,
   `clients/` under `providers/recall/`.
3. Delete 1140's `meetings/{domain,dto,service,router,models}.py` — replaced
   by Phase 1's unified versions.
4. Implement `providers/recall/provider.py` as a real `MeetingProvider`:
   - `create()` → wraps the existing Zoom/Meet adapter `create` action,
     returns a Meeting with `source="recall"`.
   - `request_recording()` → wraps `recall_client.request_bot_for_meeting`.
   - `fetch_transcript()` → wraps the existing transcript pipeline.
5. Recall webhooks emit the unified `MeetingRecordingReady` /
   `MeetingTranscriptReady` events (not the Recall-specific ones it used
   internally before).
6. MCP tools (`schedule_meeting`, `send_bot_to_meeting`,
   `find_meeting_transcript`, etc.) all switch to calling
   `meetings.service`, not `recall_client` or per-provider service.
7. The "transcript not ready" tool enrichment (commit `f28ba3c6`) carries
   through unchanged — the structured state response is at the service
   layer, not provider layer.
8. All 157 Recall tests stay green; integration tests against
   `meetings.service.create_meeting(..., source="recall")` get added.

### Phase 3 — Rebase #1178 (LiveKit scheduling) onto the new platform

Owner: 1178's branch.

1. Delete 1178's `meetings/{domain,dto,service,router,models}.py` — replaced
   by Phase 1.
2. Implement `providers/livekit/provider.py` as a `MeetingProvider`:
   - `create()` → returns a Meeting with `source="livekit"` and
     `provider_payload.room_name` set, but does NOT create the room yet
     (rooms are created on `start()` per 1178's existing behavior).
   - `start()` → calls `livekit.service.create_room` and the agent dispatch.
   - `end()` → calls `livekit.service.delete_room`.
3. The reminder loop / auto-start logic from 1178 is *already* in
   `meetings/scheduling/reminders.py` after Phase 1. The 1178 PR's job is
   just to land the LiveKit provider — no scheduling code remains in 1178.
4. LiveKit agent fixes (`afa22e8`, `84ce071`) are independent of the
   platform refactor and land alongside.
5. Notification templates already moved into `meetings/bridges/notifications.py`
   in Phase 1 — 1178 drops its inline notification code.

### Phase 4 — Rebase #1186 (LiveKit recording) onto the new platform

Owner: 1186's branch (currently stacked on 1178; restack on Phase 3 once
that merges).

1. Move recording start/stop endpoints from `livekit/router.py` to
   `providers/livekit/recording.py`, expose via `MeetingProvider.request_recording`.
2. The composite egress → S3 → /files flow stays unchanged; only entry
   points move.
3. Recording-ready event becomes the unified `MeetingRecordingReady` so
   notifications + KB indexer don't need a LiveKit-specific path.

### Phase 5 — Frontend rebases

Owner: paw-enterprise side.

| PR | What changes |
|---|---|
| #224 | API client paths likely unchanged (router prefix kept). MeetingsPanel may pick up provider tabs if Phase 6 lands a provider picker. |
| #235 | ScheduleMeetingModal already source-agnostic UI. Wire it to the unified `POST /api/v1/meetings` with a `source` field. Today it hardcodes a LiveKit call; add a provider-picker step. |
| #241 | Independent of the refactor — call panel UI is LiveKit-only and stays under the LiveKit call route. |

### Phase 6 — Calendar bridge

Cleanest follow-up after 1178/1140 land. Subscribes the
`ee.calendar.events` to `meetings/bridges/calendar.py`, which on certain
event types (e.g. a calendar event with a Zoom/Meet join URL) auto-creates
a `Meeting(source="recall")` and queues a Recall bot. For LiveKit-hosted
meetings (events created from inside our UI), it writes the LiveKit
join-deep-link back to the calendar event.

Specifically out of Phase 1's scope; designed so that nothing in the
entrypoint forecloses on it.

---

## Risks + mitigations

- **Bigger blast radius on rebase.** Both 1140 and 1178 are large diffs. If
  Phase 1 doesn't land cleanly first, the rebases compound. Mitigation:
  keep Phase 1 minimal (stubs + scaffolding only — no behaviour change), so
  it's a fast review with low conflict surface.
- **Provider protocol leakage.** If `MeetingProvider` ends up needing 20
  methods to cover every quirk, the abstraction loses value. Mitigation:
  start with 4 required methods (`create`, `start`, `cancel`, `end`) and
  let `request_recording` / `fetch_transcript` be optional capabilities
  declared via sub-protocols. Don't add a method until both providers need it.
- **Two source enums in flight.** Until Phase 2+3 land, the `source` field
  on `MeetingDoc` will only have placeholder values from the registry.
  Mitigation: Phase 1 ships with an explicit `# providers register here in
  Phase 2/3` comment in `providers/__init__.py` and a startup log that
  warns if no providers are registered.
- **MCP tool consumers expect today's response shapes.** The "transcript
  not ready" enrichment (commit `f28ba3c6`) is in the agent MCP layer, not
  the service. Phase 1 leaves that path untouched; Phase 2 just changes
  what the service layer returns underneath. Mitigation: integration tests
  pin the MCP response shape across the refactor.

---

## What this plan does *not* do

- Doesn't unify `ee.calendar` with `ee.cloud.meetings` — calendar stays its
  own module; the bridge is a thin subscriber.
- Doesn't reshape `livekit/` outside of moving recording bits — the
  in-process agent + room mgmt stays.
- Doesn't touch the OSS core. Everything here is enterprise (`ee/`).
- Doesn't try to retro-fit pre-existing MeetingDoc rows in production — at
  this point both #1140 and #1178 are pre-merge so there's no migration debt.

---

## Decisions (resolved 2026-05-23)

1. ✅ **One `Meeting` doc** with a `source: Literal["recall", "livekit"]`
   discriminator and a `provider_payload: dict` bucket for source-specific
   fields (bot_id, room_name, recording_id, transcript_id, …). Keeps cross-
   provider queries trivial ("list all meetings this week", "what's still
   live") and avoids forking the notifications/KB consumers across two
   sibling docs.

2. ✅ **Phase 1 ships with empty providers.** The registry exists and
   `service.create_meeting` dispatches by `source`, but each provider's
   `create`/`start`/`cancel`/`end` raises `NotImplemented` until its owner
   ships their rebase PR. Lets the entrypoint merge fast, lets #1140 and
   #1178 land independently in parallel.

3. ✅ **Separate events** — `MeetingRecordingReady` and
   `MeetingTranscriptReady` are distinct. Notifications and the KB indexer
   subscribe to whichever one they care about without matching on a `kind`
   field.

4. ✅ **LiveKit ownership split.** The LiveKit-side rebase work
   (Phase 3 + Phase 4) is owned by a different engineer. They get a
   standalone implementation guide at
   `docs/plans/2026-05-23-livekit-provider-guide.md` that they can read
   without context from this plan — what the protocol expects, where
   the `MeetingDoc` shape comes from, how to wire webhooks, how to test.
   The guide is the single source of truth for them; this plan is the
   single source of truth for the platform overall.
