<!--
2026-05-19 — Created RFC for native ee/calendar/ module.
Engineering spec covering module shape, domain model, DTOs, service signatures,
bus events, external sync architecture, recurrence, permissions, UI surface,
test plan, and open questions.
-->

# RFC: Native Calendar Module (`ee/calendar/`)

*Date: 2026-05-19*
*Status: DRAFT*
*Owner: Prakash. Reviewers: anyone touching `ee/cloud/instinct/`, `ee/cloud/pockets/`, or the `connectors/gcalendar.yaml` adapter.*

---

## Problem

PocketPaw has no workspace-level calendar primitive. The pieces that exist are partial:

- `connectors/gcalendar.yaml` plus `src/pocketpaw/connectors/adapters/gcalendar.py` handle Google Calendar API auth and raw read/write.
- `ripple/src/lib/widgets/data/Calendar.svelte` renders calendar UIs inside Ripple specs.

There is no service layer between them. No internal `Event` model, no workspace-scoped storage, no recurrence, no free/busy, no conflict detection, no cross-tenant calendar entity. Pockets that need scheduling (lease renewal, patient intake, discovery review, statute deadlines) have no anchor for scheduling logic.

This RFC proposes a native `ee/calendar/` module conforming to the 4-file ee/cloud convention (see `pocketpaw/CLAUDE.md` ee/cloud rules).

## Why native, not a pocket

A pocket is a scoped workspace for one job. A calendar is cross-cutting; every pocket that schedules consumes it. Eight integration seams force the calendar to be a primitive:

1. **Bus.** Reminder-due, event-start, event-update, conflict-detected events fan out to other subsystems.
2. **Fabric.** Events link to typed objects via `fabric_object_id` (a lease, a patient, a task) so the calendar is queryable through the fabric graph.
3. **Soul.** Per-agent preferences (working hours, preferred meeting length) live on the agent's soul.
4. **Instinct.** Calendar mutations (deletes, reschedules of attendee-visible events) pass through Instinct's approval gate.
5. **Connectors.** Bi-directional sync with Google, Outlook, iCal. The connector layer handles transport; calendar owns reconciliation.
6. **KB.** Meeting notes file into a scoped KB (`workspace:{id}` or `pocket:{id}`).
7. **Memory.** Recent and upcoming events feed agent session context.
8. **Outcomes.** `meeting_held`, `appointment_completed`, `reminder_acknowledged` hit the metering pipeline.

A pocket cannot own those seams; they are workspace- and agent-wide. The primitive must.

## Module shape

`ee/calendar/` follows the canonical 4-file shape plus supporting modules.

Canonical four:

- `__init__.py` — public API exports.
- `domain.py` — frozen Pydantic value objects: `Event`, `Calendar`, `Attendee`, `Recurrence`, `FreeBusy`. All carry `workspace_id` as a required field (rule 3). Constructing without tenancy is a type error.
- `dto.py` — distinct request/response classes (rule 4): `CreateEventRequest`, `UpdateEventRequest`, `ListEventsRequest`, `EventResponse`, `FreeBusyRequest`, `FreeBusyResponse`, `ConflictReport`.
- `models.py` — Beanie `_EventDoc` with indexes on `(workspace_id, calendar_id, starts_at)`. Imported only by `service.py` (rule 2).
- `service.py` — module-level async functions: `create_event`, `update_event`, `delete_event`, `get_event`, `list_events`, `get_freebusy`, `detect_conflicts`. Signature per rule 5. Validates at entry (rule 6). Tenant filter on every read (rule 7). Emits on every write (rule 9). Errors via `CloudError` (rule 10).
- `router.py` — thin FastAPI router: `POST /api/v1/calendar/events`, `GET /api/v1/calendar/events`, `PATCH /events/{event_id}`, `DELETE /events/{event_id}`, `POST /api/v1/calendar/freebusy`.

Supporting modules:

- `recurrence.py` — RRULE parsing/expansion via `python-dateutil`.
- `freebusy.py` — availability across N attendees.
- `conflicts.py` — overlap detection plus suggested-resolution helpers.
- `events.py` — bus events (Pydantic): `EventCreated`, `EventUpdated`, `EventDeleted`, `EventStarted`, `ReminderDue`, `ConflictDetected`.
- `sync.py` — bi-directional sync; gcalendar adapter first, Outlook + iCal placeholders.
- `policy.py` — workspace permissions plus per-calendar helpers.

## Domain model detail

All frozen Pydantic value objects with `workspace_id` required (rule 3).

- `Event`: `id`, `workspace_id`, `calendar_id`, `title`, `description`, `starts_at`, `ends_at`, `timezone` (IANA), `location`, `attendees: list[Attendee]`, `recurrence: Recurrence | None`, `fabric_object_id: str | None`, `created_at`, `updated_at`.
- `Recurrence`: `rrule_string` (RFC 5545 RRULE), `until: datetime | None`, `count: int | None`, `exceptions: list[datetime]` (EXDATE entries).
- `FreeBusy`: `attendee_id`, `busy_periods: list[tuple[datetime, datetime]]`.
- `Calendar` and `Attendee` follow the same frozen pattern with `workspace_id` required.

## Service signatures

Module-level async functions per ee/cloud rule 5:

```python
async def create_event(ctx: RequestContext, body: CreateEventRequest) -> EventResponse: ...
async def update_event(ctx: RequestContext, body: UpdateEventRequest) -> EventResponse: ...
async def list_events(ctx: RequestContext, body: ListEventsRequest) -> list[EventResponse]: ...
async def get_freebusy(ctx: RequestContext, body: FreeBusyRequest) -> FreeBusyResponse: ...
async def detect_conflicts(ctx: RequestContext, body: ConflictCheckRequest) -> ConflictReport: ...
```

(`delete_event` and `get_event` follow the same shape.) Every function: `body = <RequestSchema>.model_validate(body)` first. Every read scopes `_EventDoc.find(workspace=ctx.workspace_id, ...)`. Every write ends with `await emit(<Event>(data=...))`. Errors via `NotFound`, `Forbidden`, `Conflict` from `_core.errors`.

## Bus events

All Pydantic. `EventCreated(workspace_id, event_id, calendar_id, starts_at)`. `EventUpdated(workspace_id, event_id, changes: dict[str, Any])`. `EventDeleted(workspace_id, event_id)`. `EventStarted(workspace_id, event_id, started_at)`. `ReminderDue(workspace_id, event_id, minutes_before)`. `ConflictDetected(workspace_id, candidate_event_id, conflicting_event_ids)`. The first three come from `service.py`; `EventStarted` and `ReminderDue` from the scheduler tick loop; `ConflictDetected` from either path.

## External sync architecture

`sync.py` wraps the existing `connectors/gcalendar.yaml` connector and `src/pocketpaw/connectors/adapters/gcalendar.py` adapter. Two flows:

- **Pull external → reconcile → update local.** Periodic poll per linked calendar; fetch remote changes since last sync token; for each remote event, look up by `external_id`; insert if missing, otherwise compare `updated_at` and apply conflict-resolution policy.
- **Push local → external.** On every successful create / update / delete, enqueue a sync job. Idempotency keyed on `(workspace_id, event_id, version)`.

Conflict policy is configurable per linked calendar. Default: events originally created externally have their external version win; events originally created locally have their local version win. Outlook and iCal stubs live in `sync.py` from day one but raise `NotImplementedError` until adapters land.

## Recurrence

RRULE handling via `python-dateutil.rrule`. Storage: master event with its `Recurrence` value object; compute expansions on read within a requested window. Avoids exploding storage for long-running recurrences and keeps mutations cheap. Tradeoff: read-time CPU on every `list_events`. Mitigation: per-window cache with short TTL, invalidated on any write to the master. Eager materialization rejected — a single RRULE edit would require rewriting an unbounded number of rows.

## Permissions

Workspace-scoped (multi-tenant via `ctx.workspace_id`). Per-user calendar visibility filters at `list_events` time. Per-calendar roles: `owner`, `write`, `read`, `freebusy-only`. `freebusy-only` callers resolve availability without seeing event titles or attendees (cross-org scheduling). `policy.py` is the single check surface; bypass is a lint failure.

## Mission Control UI

A new operator-surface Calendar page in `paw-enterprise` renders workspace events through the existing `ripple/src/lib/widgets/data/Calendar.svelte` widget. The widget already accepts event lists and emits selection events; wiring is endpoint-side. UI ships in a separate PR.

## Test plan

`pytest-asyncio` plus `mongomock-motor` for the document layer, `freezegun` for time. Coverage targets: service happy paths for all six operations; tenant-filter enforcement via cross-workspace fixture pairs; RRULE expansion (daily, weekly, monthly, `until`-bounded, `count`-bounded, with `exceptions`); free/busy across N attendees including the no-events edge case; conflict detection (full overlap, partial overlap, touch-boundary which is not a conflict).

## Open questions

1. Recurrence storage: master-plus-expand-on-read is proposed; cache materialized expansions per window, or recompute every read?
2. Default conflict-resolution policy: external-wins works for externally created events, but what about the case where both sides edit the same event between syncs?
3. All-day vs timezoned events: store separately, or unify with an `is_all_day` flag?
4. Recurring-event modification semantics: this-event vs this-and-future vs all — match Google's three-way prompt, or pick one default?
5. OAuth refresh handling: sync failures during a token refresh need a queue, not a hard fail.
6. Reminder delivery channel: `ReminderDue` is a bus event; which subsystem owns delivery — channel adapters, a new reminder service, or both?
7. Pocket-scoped calendars: do we add a `pocket_id` field on `Calendar`, or rely on a generic `scope` field?
8. External-write tombstones: when sync overwrites local changes per policy, do we keep the lost version for audit?

## Out of scope (this RFC)

- Mission Control UI implementation.
- Outlook and iCal sync (gcalendar lands first).
- Multi-domain calendar federation across separate workspaces.
- Meeting transcription / summary generation.
- Calendar-driven pocket triggers (cron-like activation on events) — separate RFC.
