# Native Meetings Integration — Design Proposal

**Status:** Draft — design session 2026-05-19 with @prakash. Phases 1–2 designed in full; phase 3 (live-tier) deferred.
**Date:** 2026-05-19

> **Update 2026-05-21 — meeting-bot pivoted to Recall.ai.** The in-meeting
> recording/transcription tier no longer uses the short-lived Vexa
> integration (or the earlier in-tree `meeting-bot/` service, both now
> removed). It uses [Recall.ai](https://recall.ai), a hosted bot API, via
> `ee/cloud/meetings/recall_client.py`. Transcripts are pushed back through
> the Svix-signed webhook at `ee/cloud/meetings/webhooks.py` and remain
> fetchable on demand. This supersedes the "Recall.ai rejected" row in
> *Decisions locked* and the webhook-rejection note in Section 5; the rest
> of the design (BYO Zoom/Meet creds for meeting *lifecycle*, the adapters,
> the data model) is unchanged. The Recall API key is a single operator
> credential, separate from the per-tenant BYO provider creds.
**Branch:** `feat/meetings-integration` (off `ee`)
**Scope:** `backend/` only. Adds Google Meet + Zoom as native integrations under `ee/cloud/meetings/` plus two connector adapters in `src/pocketpaw/connectors/adapters/`. Paw-enterprise gets a new Integrations → Meetings settings page.

---

## TL;DR

Native, BYO-credential meetings integration for Google Meet and Zoom. Each enterprise workspace creates its own Zoom Server-to-Server OAuth app and its own Google Cloud OAuth client; PocketPaw never holds shared provider credentials. Code splits across two layers — provider adapters (stateless REST clients) in `src/pocketpaw/connectors/adapters/`, and a first-class meetings domain (`ee/cloud/meetings/`) that owns Mongo state, webhook ingestion, and transcript pipeline. Three connectors register: `google_meet` (write+read), `zoom` (write+read), `meetings` (read-only cross-provider aggregator). Phase 1 ships meeting lifecycle; phase 2 ships post-call transcript ingestion → uploads → KB. Phase 3 (live bot / RTMS / Meeting SDK) explicitly deferred.

---

## Decisions locked

| Dimension | Decision |
|---|---|
| **Providers v1** | Google Meet + Zoom. Teams/Webex deferred. |
| **Build vs buy** | Native end-to-end. Recall.ai rejected — vendor lock-in risk and the BYO model dodges Zoom's marketplace review entirely. |
| **Credentials model** | Per-tenant BYO. Each workspace creates its own Zoom S2S OAuth app + its own Google Cloud OAuth client + Pub/Sub topic. PocketPaw never ships shared provider creds. Confirmed appropriate because PocketPaw is workspace-first; all customers have a Google Workspace org (Internal app constraint is satisfied). |
| **Code split** | Provider connector adapters + first-class meetings module (Approach B from session). Adapters do REST; meetings module owns domain. |
| **Connector registry shape** | Three connectors: `google_meet`, `zoom`, `meetings`. The first two need creds; `meetings` is a read-only aggregator that auto-enables when ≥1 provider is configured. |
| **Phases** | Phase 1 = lifecycle (create/list/cancel). Phase 2 = post-call transcripts → uploads → KB. Phase 3 (live) deferred entirely — no scaffolding now. |
| **Storage** | Transcripts stored as files via existing `EEUploadService`. Meet/Zoom recordings opt-in per workspace (link stored, MP4 not auto-downloaded). |
| **Webhook model** | Webhooks fire `MeetingEvent` onto the existing in-process bus; a listener fetches transcript async. 15-min polling fallback for missed events. |
| **Compliance with `ee/cloud` code rules** | 4-file shape, service-is-repo, frozen domain with required `workspace_id`, validate-at-entry, tenant filter on every read, emit on every write, `CloudError` over `HTTPException`. Per `backend/CLAUDE.md` `ee/cloud` Code Rules. |

---

## Why now

PocketPaw Enterprise's near-term customers consistently ask for meeting integration — both "agent schedules and tracks meetings" and "agent ingests transcripts into KB so I can ask about past conversations." Today there is zero meeting code in the codebase (grep confirmed). The recent Composio shipment demonstrated the pattern for third-party integrations, but Composio doesn't meaningfully cover Meet/Zoom, and the team already flagged Composio as strategic-only ("tactical yes, strategic yes-but"). Meetings is the wedge for the native-integration exit door.

Two timing factors:
- The connectors registry pattern (charter at `ee/cloud/connectors/CHARTER.md`) is stable enough to build against — adapters drop in cleanly.
- Google Meet REST API v2 (`conferenceRecords`, `transcripts`, `transcripts.entries`) is GA. Zoom S2S OAuth is mature. There's no API maturity blocker.

---

## Section 1 — Module layout

Two layers. Stateless runtime mirrors how Composio + Gmail/Calendar tools live today; workspace state mirrors `ee/cloud/connectors/`.

```
backend/src/pocketpaw/connectors/adapters/
  google_meet/
    __init__.py
    adapter.py            # GoogleMeetAdapter(Connector) — schema() + execute()
    actions.py            # action handlers (one func per action)
    client.py             # Google Meet REST v2 client (google-apps-meet SDK + httpx hybrid)
  zoom/
    __init__.py
    adapter.py            # ZoomAdapter(Connector)
    actions.py
    client.py             # Zoom REST client (raw httpx + S2S OAuth token cache)
  meetings/
    __init__.py
    adapter.py            # MeetingsAggregatorAdapter(Connector) — read-only, no creds
    actions.py            # search, list_recent, get_transcript_by_id
  _shared/
    retrying_http.py      # NEW: shared httpx wrapper, exp backoff on 429/5xx
    paginated_fetch.py    # NEW: pagination iterator helper

backend/ee/cloud/meetings/
  __init__.py
  domain.py               # frozen value objects: Meeting, TranscriptEntry, ParticipantSnapshot
  dto.py                  # Pydantic Request/Response classes
  service.py              # MeetingsService — IS the repository (per ee/cloud rule 1)
  router.py               # FastAPI routes
  webhooks.py             # POST /webhooks/zoom, /webhooks/google-meet
  listeners.py            # bus subscribers: MeetingEvent → sync_meeting → upload → FileReady
  credentials.py          # MeetingCredentialsService — store/retrieve per-workspace BYO creds
  oauth_flow.py           # Google OAuth 3-leg callback handlers

backend/ee/cloud/models/
  meeting.py              # Beanie docs (see Section 2)

backend/ee/cloud/_core/security/
  webhook_hmac.py         # NEW shared helper — verify_hmac_sha256(body, signature, secret)
```

**Why this split:**
- Adapters know nothing about Mongo, FastAPI, or workspaces. They take credentials in, return DTOs out. Keeps them testable and keeps `src/pocketpaw/` deployable without `ee/`.
- The meetings module owns persistence, webhooks, listeners, and agent-facing queries. It calls into adapters via the existing `ConnectorRegistry`.
- `webhook_hmac.py` is a new tiny shared primitive — codebase exploration confirmed no inbound webhook verification helper exists yet. Putting it in `_core/security/` makes it reusable for future inbound integrations (Slack, GitHub, Notion) without re-inventing.
- No `bot/` subdirectory — phase 3 explicitly deferred.
- No new OAuth helper module — we extend the existing `OAuthManager.PROVIDERS` dict with `zoom` and `google_meet` entries. The Zoom `account_credentials` grant is a small new branch (~20 lines) in `OAuthManager`.

---

## Section 2 — Data model

Three Beanie documents in `ee/cloud/models/meeting.py`. All scoped to a workspace; all enforce tenancy at construction (ee/cloud rule 3).

### `MeetingProviderCredentials`

Per-workspace per-provider BYO credentials.

```python
class MeetingProviderCredentials(TimestampedDocument):
    workspace_id: PydanticObjectId
    provider: Literal["google_meet", "zoom"]
    credentials_ref: str             # ~/.pocketpaw/oauth/workspace-{id}-{provider}.json
    webhook_secret: str              # we generate this; admin pastes into provider config
    pubsub_subscription: str | None  # Google Meet only — Pub/Sub subscription resource name
    enabled: bool = True
    last_validated_at: datetime | None
    last_error: str | None
    # Indexes: unique (workspace_id, provider)
```

Why a separate doc (not stuffed into `WorkspaceConnector.config`): webhook secrets, Pub/Sub subscription names, and validation timestamps need lifecycle of their own. `WorkspaceConnector` still gets a row per provider so the connectors registry sees them, but credential bytes never live in Mongo.

### `Meeting`

```python
class Meeting(TimestampedDocument):
    workspace_id: PydanticObjectId
    provider: Literal["google_meet", "zoom"]
    provider_meeting_id: str         # Zoom meeting ID or Meet conference_record name
    provider_space_id: str | None    # Meet space name (spaces/{space}); null for Zoom
    title: str | None
    join_url: str
    organizer_email: str | None
    scheduled_start: datetime | None
    scheduled_end: datetime | None
    actual_start: datetime | None
    actual_end: datetime | None
    status: Literal["scheduled", "in_progress", "ended", "transcript_ready", "failed", "cancelled"]
    participants: list[dict]         # [{name, email, joined_at, left_at}] — best-effort
    recording_file_ids: list[PydanticObjectId]  # FK → FileUpload (opt-in per workspace)
    raw_provider_payload: dict       # last-seen provider response, for debugging
    created_by_user_id: PydanticObjectId | None  # null = ingested from webhook, not created by us
    # Indexes:
    #   (workspace_id, status)
    #   (workspace_id, scheduled_start desc)
    #   (provider, provider_meeting_id) unique
```

### `MeetingTranscript`

```python
class MeetingTranscript(TimestampedDocument):
    workspace_id: PydanticObjectId
    meeting_id: PydanticObjectId     # FK → Meeting
    provider_transcript_id: str
    file_id: PydanticObjectId | None # FK → FileUpload (the stored .vtt/.txt blob)
    entry_count: int
    speaker_count: int
    language: str | None
    fetched_at: datetime | None
    indexed_in_kb: bool = False
    # Indexes: (meeting_id), (workspace_id, indexed_in_kb)
```

### Deliberately not modeled

- **Recordings** as their own collection — they're files. Reference `FileUpload._id` from `Meeting.recording_file_ids`. Mime: `video/mp4`.
- **Transcript entries** as a Mongo collection — entries live in the `.vtt`/`.txt` file. Reading a 10k-line transcript is a file read, not a `find()` over a 10k-document collection. Avoids "Mongo collection with 10M rows" worst case.
- **Calendar events** — out of scope. If users want "schedule via Outlook/Google Calendar," that's a follow-up Calendar connector.
- **Bot session state** — phase 3 deferred.

### Retention invariant

Google Meet deletes transcript entries from their API **30 days after the conference ends**. Phase 2's sync path must run within this window; the polling fallback (Section 5) catches anything missed by webhooks. Documented invariant — surfaced in the design doc + operational runbook.

---

## Section 3 — Auth flow & BYO credentials UX

### Zoom (Server-to-Server OAuth)

No browser handshake — pure account-credentials grant.

1. Workspace admin → Zoom App Marketplace → Develop → Build App → **Server-to-Server OAuth**. App is private to their Zoom account; no Zoom review required.
2. Scopes: `meeting:write:admin`, `recording:read:admin`, `cloud_recording:read:admin`, `report:read:admin`.
3. Admin pastes `account_id`, `client_id`, `client_secret` into desktop client → Settings → Integrations → Meetings → Zoom.
4. Backend immediately validates: request OAuth token (account_credentials grant), call `GET /users/me`. On 200, save creds + mark `last_validated_at`. On 4xx, surface the exact Zoom error.
5. We generate a `webhook_secret` and show it to the admin. Admin pastes it into the Zoom app's "Event Subscriptions" config alongside `https://{host}/api/v1/meetings/webhooks/zoom`. Subscribes to `recording.completed`, `meeting.started`, `meeting.ended` at minimum.

### Google Meet (OAuth 2.0 + Pub/Sub)

Standard 3-leg flow plus Pub/Sub provisioning.

1. Workspace admin creates a Google Cloud project (or reuses one), enables **Google Meet API** + **Cloud Pub/Sub API**, creates an OAuth 2.0 Client ID (Web application). Sets redirect URI to backend callback. Marks the app as **Internal** (no Google verification needed since all customers are Workspace orgs).
2. Admin pastes `client_id` + `client_secret` into the Meet panel.
3. User clicks **Connect Google Meet** → redirect to Google consent with scopes `meetings.space.created`, `meetings.space.readonly`, `meetings.conference_records.readonly`, plus `drive.readonly` if recordings opt-in is checked → consent → callback exchanges code for refresh token → saved.
4. For events, Meet uses Cloud Pub/Sub push. We provide a setup script that runs against the admin's project using their creds, provisioning the topic + push subscription pointing at `https://{host}/api/v1/meetings/webhooks/google-meet`. Subscription is tagged with `workspace_id` so the webhook can identify the tenant.

### Credentials storage

Reuse existing `~/.pocketpaw/oauth/` directory + `TokenStore` (chmod 0600). Naming scheme:

```
~/.pocketpaw/oauth/workspace-{workspace_id}-zoom.json
~/.pocketpaw/oauth/workspace-{workspace_id}-google_meet.json
```

`MeetingProviderCredentials.credentials_ref` is just this path. Mongo never sees secret bytes — backups stay clean, consistent with Composio + existing OAuth providers.

### Why not a shared Google Cloud project

Considered and rejected:

| | BYO (chosen) | Shared (rejected) |
|---|---|---|
| Customer setup | 15–30 min guided wizard | 30 sec click |
| Google verification | None (Internal app) | Required (multi-week, CASA $15–75k/yr) |
| Blast radius if creds leak | Customer's project only | All customers |
| Quotas | Per customer | Pooled — noisy-neighbor risk |
| Regulated customer pushback | None | Substantial |
| Works for Gmail individuals | No (needs Workspace org) | Yes |

Decisive factor: PocketPaw Enterprise is workspace-first. Gmail-individual users are not the target. Consistency with Zoom's BYO model breaks ties.

### Friction mitigation (paw-enterprise)

The 15–30 min Google setup is real. Three mitigations ship alongside phase 1:

- **In-app setup wizard** — step-by-step page with screenshots, "open this link" buttons, and paste fields per value. Not a docs-page bounce.
- **Validate-as-you-go** — after each creds paste, immediately attempt token exchange; show the exact provider error inline. Most "it's not working" cases are missing scopes or wrong redirect URI, both detectable.
- **CLI helper** — `pocketpaw setup meet --workspace XYZ` that uses the admin's existing `gcloud` install to provision project + OAuth + Pub/Sub in one command. **Deferred to phase 1.5** — not in initial PR.

### OAuthManager extension

```python
# src/pocketpaw/clients/oauth.py — extend PROVIDERS dict
PROVIDERS["zoom"] = {
    "auth_url": None,                       # S2S — no browser
    "token_url": "https://zoom.us/oauth/token",
    "grant_type": "account_credentials",    # new code path (~20 LOC)
}
PROVIDERS["google_meet"] = {
    "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
    "token_url": "https://oauth2.googleapis.com/token",
    "scopes": [...],                        # see above
}
```

The `account_credentials` grant is a new branch in `OAuthManager.get_token()` — keeps S2S as a first-class flow alongside the existing 3-leg pattern. Only change to existing OAuth infra.

---

## Section 4 — Provider adapters & agent tool surface

Adapters implement the existing `Connector` protocol from `src/pocketpaw/connectors/protocol.py`. The connectors registry + `connector_tools_for(c)` pipeline picks them up for free — no changes to tool generation.

### Action set

Both provider adapters expose the same action names where semantically equivalent:

| Action | Trust | Execution | Notes |
|---|---|---|---|
| `create_meeting(title, start, duration_minutes)` | write | CLOUD | Persists `Meeting` row |
| `list_meetings(since, until, status?)` | read | CLOUD | Queries our `Meeting` collection (cheap, indexed) |
| `get_meeting(meeting_id)` | read | CLOUD | Reads `Meeting`; refreshes from provider if just-ended and missing `actual_end` |
| `cancel_meeting(meeting_id)` | write | CLOUD | Zoom: real cancel. Meet: marks `cancelled`, join URL still works (documented limitation) |
| `list_recordings(meeting_id)` | read | CLOUD | Phase 2. Returns file refs / Drive IDs |
| `get_transcript(meeting_id)` | read | CLOUD | Phase 2. Returns signed URL to stored transcript file |

The `meetings` meta-connector exposes only cross-provider read actions:

| Action | Trust | Execution | Notes |
|---|---|---|---|
| `search(query, since?, until?)` | read | CLOUD | KB-backed if transcript indexed; falls back to title/participant match |
| `list_recent(limit?)` | read | CLOUD | Across providers, scheduled_start desc |
| `get_transcript_by_id(meeting_id)` | read | CLOUD | Same as provider action but provider-agnostic |

### Adapter implementation pattern

```python
# src/pocketpaw/connectors/adapters/zoom/adapter.py
class ZoomAdapter:
    def __init__(self, creds: ZoomCredentials, persistence: MeetingsPersistencePort):
        self._client = ZoomClient(creds)
        self._persist = persistence  # Injected callback into MeetingsService

    def schema(self) -> ConnectorSchema:
        return ConnectorSchema(name="zoom", actions=[...])

    async def execute(self, action: str, params: dict, scope: ConnectorScope) -> dict:
        handler = ACTION_HANDLERS[action]
        return await handler(self._client, self._persist, scope, params)
```

`MeetingsPersistencePort` is a tiny Protocol the adapter accepts at construction. The cloud-side wiring injects a `MeetingsService` instance that satisfies it. This keeps the adapter free of Mongo imports while still being able to persist `Meeting` rows — matches the ee/cloud rule that only `<entity>/service.py` writes to its own Beanie models.

### SDK choice

- **Google Meet**: hybrid. `google-apps-meet` SDK for `conferenceRecords` + `transcripts` + `transcripts.entries` (nested pagination is annoying to hand-roll). Raw httpx for `spaces` (SDK lags on the create-meeting path; it's `v2beta`).
- **Zoom**: raw httpx + S2S OAuth token caching. No third-party SDK — Zoom's official Python wrappers are unmaintained and the REST surface is small. We own the client.

### Why no shared base class

Meet and Zoom diverge enough (Meet has `spaces` + `conferenceRecords` separation, Zoom has flat meetings + cloud recordings, transcript shapes are fundamentally different) that a base class would be a leaky abstraction. Inheritance kills more code than it saves here. Shared logic lives in `src/pocketpaw/connectors/adapters/_shared/` (retrying HTTP, pagination iterator).

### Agent tool surface (what the LLM sees)

```
google_meet.create_meeting(title: str, start: str, duration_minutes: int) -> Meeting
google_meet.list_meetings(since?: date, until?: date, status?: str) -> list[Meeting]
google_meet.get_transcript(meeting_id: str) -> TranscriptFile
zoom.create_meeting(title: str, start: str, duration_minutes: int) -> Meeting
zoom.list_meetings(since?: date, until?: date, status?: str) -> list[Meeting]
zoom.get_transcript(meeting_id: str) -> TranscriptFile
meetings.search(query: str, since?: date, until?: date) -> list[Meeting]
meetings.list_recent(limit?: int) -> list[Meeting]
meetings.get_transcript_by_id(meeting_id: str) -> TranscriptFile
```

The meta-connector lets the agent answer "what did we discuss with Acme last week" without picking a provider.

---

## Section 5 — Webhook ingestion & transcript pipeline

The phase 2 flow: provider event → verify → enqueue → fetch → store → emit `FileReady` → KB indexes.

### Inbound routes

```
POST /api/v1/meetings/webhooks/zoom
POST /api/v1/meetings/webhooks/google-meet
```

Both endpoints follow the same shape: verify, identify workspace, dispatch to bus, return 200 in <500ms. Zero business logic in the handler.

### Zoom verification (HMAC-SHA256)

1. Pull `X-Zm-Signature` (`v0=...`) and `X-Zm-Request-Timestamp` from headers.
2. Reject if timestamp >5 min old (replay protection).
3. Identify workspace by Zoom `account_id` in the body → look up `MeetingProviderCredentials` → use its `webhook_secret`.
4. Compute `hmac_sha256(f"v0:{ts}:{raw_body}", webhook_secret)`, compare constant-time.
5. Special case: Zoom's `endpoint.url_validation` event posts `payload.plainToken`. Respond `{plainToken, encryptedToken: hmac_sha256(plainToken, webhook_secret)}`. Same endpoint, short branch.

### Meet verification (Pub/Sub OIDC)

1. Pull `Authorization: Bearer <jwt>` from headers.
2. Verify JWT with Google's public keys via `google.auth.jwt`; check `aud` matches our endpoint URL, `iss` is `https://accounts.google.com`, `email` is the Pub/Sub service account.
3. Decode Pub/Sub envelope → body has `resource_name` (e.g. `conferenceRecords/abc`).
4. Identify workspace via Pub/Sub subscription tag (we set `workspace_id` as a message attribute at provisioning time).

The two shapes are deliberately not unified — Zoom is classic HMAC, Meet is Google OIDC. Shared util (`_core/security/webhook_hmac.py`) covers HMAC; Pub/Sub JWT verification lives inline in the Meet handler.

### Event flow after verification

```
webhook handler
  → emit MeetingEvent(provider, event_type, resource_id, workspace_id, raw_payload)
    via _core.realtime.bus.InProcessBus
  → return 200 OK immediately

listener (ee/cloud/meetings/listeners.py)
  → on MeetingEvent(event_type ∈ {"meeting.ended", "recording.completed"})
    → MeetingsService.sync_meeting(ctx, provider, resource_id)
      → adapter.get_meeting + adapter.list_recordings + adapter.get_transcript
      → upload transcript via EEUploadService.write_text_file(...)
      → MeetingTranscript created with file_id
      → emit FileReady (existing event) → KB indexer auto-ingests
      → emit MeetingTranscriptReady (new event) → desktop client refreshes
```

### Why async, not synchronous-in-handler

- Zoom requires <3s webhook ack or they retry. Transcript fetch can take 5–30s.
- Listener can retry on failure without making the provider retry the webhook (which would duplicate events).
- Decouples ingestion from availability — meetings that ended at 3am don't block webhook workers.

### Polling fallback

`ee/cloud/cycles/scheduler.py` registers a 15-minute job per workspace with `MeetingProviderCredentials.enabled = True`:
- Find `Meeting` rows where `status = "ended"` and `MeetingTranscript.fetched_at` is null, meeting age <30 days (Meet retention invariant).
- Trigger the same `sync_meeting` listener path.

Catches: missed webhooks, webhooks during deploys, meetings that ended before webhooks were enabled, Meet's transcript-arrives-asynchronously case. 15 min is fast enough to feel real-time; configurable per workspace via env (`POCKETPAW_MEETINGS_POLL_INTERVAL_MIN`).

### Retry & dead-letter

Listener uses the existing event-bus retry semantics (3 retries, 5s/30s/2min backoff per `connectors/CHARTER.md`). After final failure, write to a small `MeetingSyncFailure` collection (`{meeting_id, error, attempted_at, attempts}`) so the dashboard can surface it. Not a new feature surface — just durable error visibility.

### Outbound events

| Event | Trigger | Consumers |
|---|---|---|
| `MeetingScheduled` | After `create_meeting` adapter call succeeds | Activity log, desktop toast |
| `MeetingEnded` | Webhook `meeting.ended` | Desktop client meeting list |
| `MeetingTranscriptReady` | After listener stores transcript file | Desktop "Transcript available" badge |
| `FileReady` (existing) | After transcript file upload | KB indexer |

All piggyback on the existing event bus; no new infra.

### Explicitly out of scope for phase 2

- Real-time streaming (RTMS / Zoom Meeting SDK / Meet Pub/Sub conference-level events). Phase 3.
- Participant-level analytics — we store the snapshot but don't model it as queries.
- Auto-summarization on ingest — that's the agent's job at query time. If we want this later, it's a separate listener on `MeetingTranscript`; doesn't require pipeline changes.
- Drive recording auto-downloads for Meet — link stored, file not pulled. Opt-in per workspace.

---

## Section 6 — Desktop client (paw-enterprise) surface

New surface lives under **Integrations → Meetings** in workspace settings.

### Meetings settings sub-page

Two cards, one per provider. Each card has three states:

- **Not configured** — paste-credentials form + link to setup wizard.
- **Configured, not connected** (Meet only) — "Connect" button to start OAuth.
- **Connected** — green checkmark, `last_validated_at` timestamp, "Disconnect" button, webhook URL + secret shown for copy (Zoom) or Pub/Sub setup script for download (Meet).

### Setup wizard

A new multi-step page (`paw-enterprise/src/routes/settings/integrations/meetings/setup/[provider]/+page.svelte`) that walks the admin through their provider's setup with screenshots. Critical for adoption — without this, BYO friction is the dominant failure mode.

### Meetings list panel (phase 1.5, not initial PR)

A dedicated panel showing recent meetings with transcript availability badges. Deferred to follow-up — phase 1 ships with meetings only visible via the agent + via the existing files panel (transcripts are files).

### Why NOT surface meetings as a generic connector card

The connectors UI is for plug-and-play integrations. Meetings has multi-step BYO + webhook config that genuinely warrants its own surface. We still write a `WorkspaceConnector` row so tool generation sees `google_meet`/`zoom` in the registry — UI is decoupled from registry.

---

## Section 7 — Phase plan

| Phase | Scope | Surface area | Ships in |
|---|---|---|---|
| **1** | Lifecycle: create/list/cancel meetings for both providers. BYO creds flow + setup wizard. Three connectors registered. | Adapters + `meetings/{domain,dto,service,router,credentials,oauth_flow}.py` + paw-enterprise settings page + setup wizard | PR #1 |
| **2** | Post-call transcripts: webhook routes, listener, polling fallback, transcript upload + KB indexing | `meetings/webhooks.py` + `meetings/listeners.py` + `_core/security/webhook_hmac.py` + scheduler job + recordings opt-in | PR #2 (immediately after #1) |
| **1.5** (parallel-ish) | CLI helper (`pocketpaw setup meet`) + meetings list panel in desktop client | `pocketpaw/cli/setup.py` + paw-enterprise meetings panel | PR #3 (post-launch) |
| **3** (deferred) | Live tier — RTMS for Zoom listen-only, or Meeting SDK bot. Meet has no native live path. | TBD | Not scoped — revisit when a customer asks |

PR #1 and PR #2 are designed to stack cleanly. Phase 1 lands fully usable on its own (you can create meetings via agent, just no transcripts yet); phase 2 adds the transcript pipeline without changing phase 1 contracts.

---

## Risks & open items

1. **Setup friction kills adoption.** BYO is correct; if the wizard isn't excellent, customers will bounce. **Mitigation:** invest real design time in the wizard before phase 1 PR opens. Validate with 2–3 admins in a usability test.
2. **Meet's 30-day transcript retention.** Customer-facing surprise if a meeting from >30 days ago has no transcript. **Mitigation:** document prominently; surface a "transcript expired" state in the desktop client.
3. **Zoom S2S OAuth scope changes require admin re-paste.** If we later add a scope, every customer's Zoom app needs reconfig. **Mitigation:** version the required scopes; surface "scope drift detected" in settings page.
4. **Pub/Sub subscription costs.** Customer pays Google for Pub/Sub usage — typically negligible but worth documenting.
5. **Webhook secret rotation.** No mechanism in v1 — rotation requires disconnect + reconnect. Acceptable for v1; revisit if compliance pushes.
6. **Recording file sizes.** If we ever enable auto-download, 1-hour video at 1080p ≈ 1GB. Storage budget needs review before that flag flips.

---

## Compliance with `ee/cloud` code rules

Verified against `backend/CLAUDE.md` `ee/cloud Code Rules`:

- ✅ Rule 1: 4-file shape (`domain.py`, `dto.py`, `service.py`, `router.py`). Plus `webhooks.py`, `listeners.py`, `credentials.py`, `oauth_flow.py` as additional sibling modules (allowed — they're not repositories).
- ✅ Rule 2: Only `meetings/service.py` imports `ee.cloud.models.meeting`. Adapters access persistence via injected `MeetingsPersistencePort`.
- ✅ Rule 3: `Meeting`, `TranscriptEntry`, `ParticipantSnapshot` in `domain.py` are frozen with required `workspace_id`.
- ✅ Rule 4: `dto.py` separates `<Op>Request` and `<Entity>Response` classes.
- ✅ Rule 5: Service signatures are `async def op(ctx: RequestContext, body: <Request>) -> <Response>`.
- ✅ Rule 6: First line of each service function is `body = <Request>.model_validate(body)`.
- ✅ Rule 7: Every Beanie `find` filters by `workspace=ctx.workspace_id`.
- ✅ Rule 8: Mapping via `model_validate(..., from_attributes=True)`; rename helpers stay in `service.py`.
- ✅ Rule 9: State-mutating service functions emit `MeetingScheduled`/`MeetingEnded`/`MeetingTranscriptReady` events.
- ✅ Rule 10: Errors via `CloudError` subclasses (`NotFound`, `Forbidden`, `Conflict`, `ProviderError`).
- ✅ Rule 11: No transactions used — pure event-driven coordination.

---

## What writing-plans will produce next

Per the brainstorming skill, this design hands off to writing-plans for the actual implementation plan. The implementation plan should break phase 1 into ordered, individually-shippable steps:

1. Extend `OAuthManager` with `zoom` (`account_credentials` grant) and `google_meet` providers.
2. Create `ee/cloud/models/meeting.py` with the three documents + indexes.
3. Stand up `ee/cloud/meetings/` with stub `service.py` + `router.py` (no provider calls yet) — verify routes register, tenancy filter works, CloudError mapping fires.
4. Build `src/pocketpaw/connectors/adapters/zoom/` end-to-end (smaller surface, no OAuth-callback complexity).
5. Wire Zoom adapter into `ee/cloud/meetings/service.py` via `MeetingsPersistencePort`. Test `create_meeting` agent action end-to-end.
6. Build `src/pocketpaw/connectors/adapters/google_meet/` including OAuth callback handler.
7. Build the `meetings` meta-connector aggregator.
8. Paw-enterprise: Integrations → Meetings settings page + setup wizard.
9. Phase 1 PR.

Phase 2 is a separate plan once phase 1 ships.

---

**Approval needed before commit:** Per standing instruction, this file stays untracked until @prakash explicitly approves it for commit. After approval, commits to `feat/meetings-integration` and hands off to writing-plans for the phase 1 implementation plan.
