<!--
docs/api-reference.md — Hand-maintained reference for cloud REST endpoints
that are not covered by the per-endpoint Mintlify pages under docs/api/.

Updated: 2026-08-18 (fix/sites-html-refine-names-the-edit-tool) — documented
`edit_html_file`, the html track's chat edit tool. It shipped in db083bfc without
reaching this file, so the section below still said html had no edit tool and was
"edited by uid splice via the leaf-edits route" — that route is the NATIVE
editor's path, not the agent's, and the two are different entry points. Recorded
with the same emphasis the react entry gets on the things a field list cannot
show: why the argument is `file_path` rather than `component_path` (html has no
component model, and its paths are root-relative), and why this tool does not
republish for a DIFFERENT reason than react's — html runs no build at all, so
there is no gate that could catch a bad edit before it went live.

Updated: 2026-08-11 (feat/sites-react-edit-lane, RX-4) — documented the build-lane
fields now on the `publish` tool response and the new read-only
`get_site_build_status` tool, in the same MCP section. Both are recorded here
because the *reason* they exist is not visible from their field lists: `url` and
`deployed` are individually insufficient to answer "is this site live", and on
react they actively mislead (a first publish returns `url: ""`, a re-publish
returns the previous deploy's url). Anyone reading only the field names would
reasonably use `url` directly, which is the defect.

Updated: 2026-08-11 (feat/sites-react-edit-lane, RX-3) — added the "Sites —
Agent Editing Tools (in-process MCP)" section documenting `edit_react_component`
and the per-engine split that decides which editing tool a site gets. This is
the first MCP tool documented in this file, which is otherwise REST-only, and it
belongs here for a specific reason: the react edit lane has no REST route at all
(it is chat-only), so a reader who checks the reference for "how do I change a
react site" would otherwise find the svelte native-editing endpoints above and
reasonably conclude nothing exists. The section also records WHY this tool does
not publish, because the missing republish looks like an omission next to
`edit_svelte_component` and is not one.

Created: 2026-05-21 (RFC 04 alpha) — documents the per-pocket backend
binding + read-only source-run endpoints. The rest of the cloud pockets
API is described in the auto-generated wiki article
`ee/docs/wiki/pockets-router-*.md`.

Updated: 2026-05-21 (PR #1177 security pass) — documented the new
DELETE /pockets/{id}/backend endpoint and the edit-access requirement on
GET /pockets/{id}/backend.

Updated: 2026-05-22 (RFC 05 M2a) — documented the write-action endpoints
(POST /pockets/{id}/actions/run, PUT /pockets/{id}/backend/write-policy)
and the per-pocket write allowlist now carried on the backend summary.

Updated: 2026-05-22 (feat/api-skills, Increment 2b) — documented
POST /skills/api-doc, the per-backend API-skill install endpoint that
turns a pocket backend's OpenAPI document into a loadable SKILL.md so
the authoring agent stops hallucinating endpoints.

Updated: 2026-05-22 (feat/catalog-allowlist, Increment 5) — documented
the catalog-as-allowlist ingest gate, the two escape-hatch widgets
(`model-viewer` + `embed`), and the `embed` URL/host policy.

Updated: 2026-06-11 (gap-3 outcome VALUE metering) — documented the
Outcome Metering section: the binding-level `outcome` / `outcome_value` /
`outcome_unit` declaration, the existing `GET /outcomes` count surface,
and the new `GET /outcomes/meter` aggregation surface that sums billable
value by unit per workspace over a since/until window. Invoicing /
payment / pricing-rules / clawback remain deferred.

Updated: 2026-06-15 (feat/invoke-tool-v1) — documented the now-live
POST /pockets/{id}/tools/run (was a fail-closed stub) and the new
owner-only PUT /pockets/{id}/backend/tool-policy. The backend summary now
carries `allowed_tools` (the per-pocket tool allowlist) alongside
`allowed_writes`.
Updated: 2026-06-15 (feat/invoke-tool-v1, v2) — the WRITE path is now live.
A connector READ tool still fires immediately; a WRITE tool is no longer
refused with `code: blocked` — it is PROPOSED for human approval through the
Instinct gate and returns `code: instinct_pending` with a `proposed_action_id`
(the write fires only when a human approves it in The Tray).

Updated: 2026-06-20 (feat/workspace-jobs, pp#1459) — documented the
workspace jobs primitive: the `kind: "job"` variant of
POST /pockets/{id}/actions/run that enqueues a server-side async job
instead of firing an HTTP write, and the new
GET /workspaces/{ws}/jobs/{job_id} status poll. Jobs run on the shared ARQ
worker under the synthetic `system:workspace_job` identity and merge their
results back into the pocket's `state` over the live update bridge.

Updated: 2026-06-26 (ART-1) — documented the Files — Versioned Writes
section: POST /files/write, PUT /files/{id}, and the two version-history
reads (GET /files/{id}/versions[/{vid}]). The write path archives each
prior blob as a FileVersionDoc and bumps a per-file content_version counter;
every read is workspace-scoped.
Updated: 2026-06-26 (ART-4) — documented the Agent Artifact Delivery
(deliver_artifact) in-process MCP tool: routes a built file/dir through the
workspace upload pipeline (file as-is, dir zipped) and returns a presigned
download URL; jail-scoped path safety; POCKETPAW_DELIVER_MAX_MB cap.
Updated: 2026-07-02 (NE-4b / NE-5b) — documented the Sites — Native Editing
section: POST /sites/by-pocket/{id}/leaf-edits (splice editor edits into the
svelte source via the apply-leaf-edit CLI and persist as a Branch draft, no
rebuild — dynamic-source split + input-keyspace confinement) and GET
/sites/by-pocket/{id}/native-artifact (serve the armed build's body_html + css
for shadow render — per-GET arm-build cost, path-traversal-guarded CSS reader).

Updated: 2026-07-27 (feat/growth-g1) — documented the Growth — Prospects
section: workspace-scoped prospect store under /growth/prospects (create /
get / list with tier|status|source filters / update), domain-deduped per
workspace, cross-tenant ids 404. First slice of the /growth outbound engine.

Updated: 2026-07-27 (feat/growth-g2) — added POST /growth/prospects/bulk to
the Growth — Prospects section: batch ingestion (max 500 rows) via the
upsert-by-domain seam, per-row errors, idempotent re-runs.

Updated: 2026-07-27 (feat/growth-g3) — added the Growth — Drafts section:
per-channel outreach drafts on a prospect (POST /growth/prospects/{id}/drafts,
GET /growth/drafts with prospect|channel|status filters, POST
/growth/drafts/{id}/status) with the enforced lifecycle
draft→proposed→approved→sent, sent→replied, non-terminal→rejected; illegal
moves 422 draft.illegal_transition.

Updated: 2026-07-27 (feat/growth-g5) — documented email dispatch: the
growth.dispatch job's email branch now sends through the per-workspace
Mailtrap connector, re-checks that the draft is still approved before any
provider call, writes a MessageLog audit row per attempt, and flips the draft
to sent through the existing gate seam. Also documented the retryable failure
path (failed row, draft stays approved, nothing raises) and the required
GROWTH_SENDING_DOMAIN config plus why outreach never rides the apex.

Updated: 2026-07-27 (feat/growth-g6) — documented the Growth — WhatsApp
dispatch section: the growth.dispatch job's channel="whatsapp" branch sends via
MSG91 behind a HARD prospect.opted_in guard (not opted in ⇒ no provider call at
all, typed error, blocked send-log row, draft left approved), the guard order,
the per-attempt WhatsAppSendLog compliance record, connector-state credential
resolution (no env fallback for the authkey), the fail-closed inbound webhook
POST /growth/webhooks/msg91, and the GROWTH_WHATSAPP_MAX_PER_HOUR /
GROWTH_MSG91_WEBHOOK_SECRET environment variables.

Updated: 2026-07-27 (feat/growth-g4) — documented the Instinct send gate:
POST /growth/drafts/{id}/propose files a gated _growth_send proposal and
flips the draft to proposed; the status route now refuses the gate-owned
approved/sent targets with 403 draft.gate_required. Approve (single or bulk)
flips the draft to approved and enqueues growth.dispatch on the growth arq
queue; reject flips it to rejected. Nothing sends without an approval.
Security review follow-up: documented per-route growth RBAC
(growth.read / growth.write MEMBER, growth.manage ADMIN on the propose verb)
and the fact that a _growth_send blob can only be minted by this route —
the generic POST /instinct/actions refuses reserved gated parameter keys.

Updated: 2026-07-28 (feat/growth-mcp) — added the Growth — the agent surface
section: the nine pocketpaw_growth in-process MCP tools the chat agent on the
/growth rail drives, the table of how that surface is narrower than the HTTP
one, and why the agent's reach ends at proposed (no send tool, no status
argument, no route to gate_transition). Also added PATCH /growth/drafts/{id}
— edit a draft's copy while it is still `draft`; anything past that is
403 draft.not_editable, because from proposed on the stored body is what the
Tray shows and what the worker sends.

Updated: 2026-07-28 (feat/growth-api-scale) — the prospect list grew a scale
surface. BREAKING: GET /growth/prospects now returns
{items, next_cursor, total} instead of a bare array. Added q search across
name/company/domain/research_brief, four sort modes (tier ordering is the
declared rank a-b-c-unqualified, not lexicographic), keyset cursor
pagination, GET /growth/prospects/facets (per-tier/status/source counts,
each block excluding its own filter), and POST /growth/drafts/propose-batch
(<=100 ids, each proposed through the existing Instinct gate, per-draft
error entries, growth.manage).

Updated: 2026-07-28 (feat/growth-projects) — a prospect can now be just a
domain: name and company are optional on create and on a bulk row, defaulting
to "" (not yet known), and nothing renders an empty value as "unknown".
domain stays required and still normalises. Added project_id — the client
container from cloud/projects — on create, on PATCH (three-valued: omit to
leave alone, an id to reassign, "" to clear) and as an optional filter on the
list, the facets and the search; a foreign project is 404 project.not_found.
The email dispatcher resolves a per-project sender identity (from-name /
from-address / reply-to, per-field fallback to the workspace default) via the
MAILTRAP_PROJECT_SENDERS and MAILTRAP_REPLY_TO connector keys, and the daily
follow-up sweep works one client's threads at a time so their nudges go out
under that identity.

Updated: 2026-07-27 (feat/growth-g8) — added the Growth — LinkedIn Queue
section: GET /growth/linkedin/queue (proposed/approved linkedin drafts joined
with prospect context, ?format=md for a paste-ready markdown export) and
POST /growth/linkedin/{draft_id}/mark-sent (record a manual send via the G-3
machine). Deliberately manual — no LinkedIn API. Integration note: mark-sent
rides the gate seam (sent is gate-owned since G-4) and takes growth.manage.

Updated: 2026-07-27 (feat/growth-g7) — added the Growth — Follow-ups section:
the daily `growth.followup_sweep` arq cron on the `growth` queue turns a send
that went quiet into a `variant: "follow_up"` draft filed back through the
same `_growth_send` gate (proposed, never approved or sent), capped at
GROWTH_FOLLOWUP_MAX per prospect+channel after which the prospect is retired
to `dead`. Documented both env knobs (GROWTH_FOLLOWUP_DELAY_DAYS,
GROWTH_FOLLOWUP_MAX).

Updated: 2026-07-11 (feat/real-pipeline-s1) — documented the Fabric — Transform
Mappings section: GET/POST/DELETE /fabric/ingest/mappings (author the
workspace's source→Fabric mappings, now with a "connector" source_kind that
dispatches through the OSS FABRIC_INGESTORS registry — gcalendar first) and
POST /fabric/ingest/run (run one mapping immediately; misconfiguration reports
status="error" in the body, never a 5xx).

Updated: 2026-08-01 (AM-6 desktop) — documented POST /auth/social/link/complete
and the desktop link handoff. Worth knowing before touching it: a Tauri webview
carries no cookie for our origin, so the callback cannot authenticate the
acting user and does NOT attach on flow=desktop. It parks the identity behind
a one-time code and the app redeems it under its bearer, where the account can
actually be proved. Also records the /oauth-callback contract that separates a
desktop LINK (link=) from a desktop SIGN-IN (xc=).

Updated: 2026-08-01 (AM-2..AM-6, feat/auth-social-providers) — documented the
Social Sign-In & Connected Accounts section: the four sign-in endpoints
(providers / login / callback / exchange) and the three connected-accounts ones
(GET identities, POST {provider}/link, DELETE identities/{provider}), the nine
refusal codes the frontend maps to copy, and the security model — why a
provider-VERIFIED email is the only join key on sign-in, and why the link path
deliberately does not use email as a join key at all. Also records two things
that are easy to get wrong and cost real time here: cloud routes authenticate
at the route level, because the global AuthMiddleware does not gate /api/v1/,
so a new cloud route needs its own guard; and localhost_auth_bypass defaults to
TRUE, so verifying an auth change with curl from your own machine cannot tell
you whether the guard is there.

Updated: 2026-07-22 (SHIP-4, feat/ship-4-agent-surface) — the two DELETE routes
now file REAL Instinct proposals (kind `_ship_action`), executed on approval by
``ship.executor`` with an execute-time `ship.manage` re-check; documented the
`pocketpaw_ship` MCP agent surface and how it is narrower than the HTTP one (a
prod deploy proposes rather than deploys).

Updated: 2026-07-22 (SHIP-3, feat/ship-3-cloud-entity) — documented the
Ship — Managed Deploys section: the workspace-scoped /ship surface for
provisioning a box, registering and deploying an app, routing a domain,
creating a linked database, and reading logs + box health. The two DELETE
routes PARK a teardown for human approval and never destroy anything.
Updated: 2026-08-04 (feat/knowledge-wiki-api) — documented the Knowledge —
Living Wiki API section: the enriched GET /knowledge/articles rows, the new
GET /knowledge/articles/{id}, GET /knowledge/stats, GET /knowledge/uploads,
and the two reingest routes (POST /knowledge/reingest,
POST /knowledge/reingest-upload) that re-run content through the hardened
KnowledgeService ingest funnel. Design doc:
docs/design/drafts/2026-08-04-knowledge-wiki-redesign.md (workspace repo).
-->

# Cloud REST API Reference

This file documents cloud (`pocketpaw-ee`) REST endpoints that do not yet
have a dedicated page under `docs/api/`. All cloud endpoints require a
valid enterprise license and an authenticated workspace context.

That second requirement is enforced **per route, not by the global
middleware**, which does not gate `/api/v1/`. If you are adding or reviewing a
cloud route, read "Cloud routes authenticate at the route level" in the Social
Sign-In section below — the route's own guard is what carries it.

## Pockets — Backend Binding & Live Data Sources

RFC 04 alpha. A pocket can be bound to **one** external backend (base URL +
auth credential). Its `rippleSpec.sources` declares read-only `GET`
bindings; a server-side executor runs them and returns the JSON results.

The credential is stored in a **separate, encrypted collection**
(`pocket_backend_credentials`) — never inside the `Pocket` document and
never inside `rippleSpec`, so the spec stays shareable and secret-free.

### `PUT /pockets/{pocket_id}/backend`

Bind a pocket to one backend. Requires pocket **edit** access.

Request body:

| Field | Type | Notes |
|-------|------|-------|
| `base_url` | string | Required. Must be `https://` and point to an external host (no loopback / RFC1918 / link-local). |
| `auth_type` | string | One of `bearer`, `api_key`, `basic`, `none`. |
| `auth_token` | string | The secret. Encrypted at rest; never returned. Required unless `auth_type` is `none`. |
| `auth_header` | string \| null | Custom header name for `api_key` auth. Defaults to `X-Api-Key`. |

Response `200`:

```json
{
  "base_url": "https://api.example.com",
  "auth_type": "bearer",
  "configured": true,
  "allowed_writes": [],
  "allowed_tools": []
}
```

The token is never echoed back. A non-https or internal `base_url` yields
a `400`. `allowed_writes` is the per-pocket write allowlist (RFC 05 M2a) —
empty by default, so no write action can fire until an owner sets a policy
via `PUT /pockets/{id}/backend/write-policy`. `allowed_tools` is the
per-pocket tool allowlist (feat/invoke-tool-v1) — also empty by default, so
no `invoke_tool` can fire until an owner sets a policy via
`PUT /pockets/{id}/backend/tool-policy`.

For `basic` auth, send `auth_token` as the raw `user:pass` credential —
the server base64-encodes it into the `Authorization: Basic` header. Do
not pre-encode it yourself.

### `GET /pockets/{pocket_id}/backend`

Read the pocket's backend binding summary. Requires pocket **edit** access
(owner or editor) — backend config metadata is owner/editor-facing,
consistent with the `PUT` route. Viewers receive a `403`.

Response `200`:

```json
{
  "base_url": "https://api.example.com",
  "auth_type": "bearer",
  "configured": true,
  "allowed_writes": [{ "method": "POST", "path_pattern": "/leases/*/renew" }],
  "allowed_tools": [{ "tool": "connector:github:list_issues" }]
}
```

Returns `404` when the pocket has no backend configured. The token is
never included in the response. `allowed_writes` carries the current
write allowlist (RFC 05 M2a); `allowed_tools` carries the current tool
allowlist (feat/invoke-tool-v1).

### `DELETE /pockets/{pocket_id}/backend`

Revoke the pocket's backend binding — deletes the stored (encrypted)
credential. Requires pocket **owner** access.

Returns `204 No Content`. Idempotent: deleting when no backend is
configured still returns `204`. The removal is written to the audit log.

### `POST /pockets/{pocket_id}/sources/run`

Run the pocket's read-only `rippleSpec.sources` (GET bindings) against its
configured backend. Read access mirrors `GET /pockets/{pocket_id}` —
deliberately **not** gated on edit access. Any pocket reader may run the
already-authored sources: a viewer of a shared live pocket triggering the
`pocket_open` refresh is the core shared-dashboard UX. A viewer cannot
change the backend or the source paths (both are edit-only), so the SSRF
hardening plus the immutable, edit-authored source list bound the risk.

Request body (all fields optional):

| Field | Type | Notes |
|-------|------|-------|
| `trigger` | `pocket_open` \| `manual` \| null | Run only sources whose `refresh` list contains this trigger. |
| `source` | string \| null | Run a single named source regardless of refresh policy. |

When both are omitted, every source in the spec runs.

Response `200`:

```json
{
  "ran": [
    { "source": "prs", "bind": "prs", "value": [ { "id": 1, "title": "PR one" } ] }
  ],
  "errors": [
    { "source": "issues", "error": "backend returned status 503", "code": "http_error" }
  ]
}
```

`bind` is the dotted state path the value should be written to, with a
leading `state.` stripped. The hydrated state is delivered **in this
response body** — there is no `pocket_mutation` SSE emit, because the run
endpoint is a standalone REST call outside any SSE-stream context. The
caller applies the results to the pocket's ripple state.

Returns `400` when the pocket has no backend configured.

**Security.** This endpoint is an SSRF boundary. The executor re-validates
the base URL, rejects absolute-URL paths / `..` traversal / cross-host
joins, runs a DNS check against internal IPs, disables redirects, applies
tight timeouts, caps response bodies at 512 KB, sanitizes error messages,
and rate-limits to 10 runs per `(pocket, user)` pair per minute. Every run
is written to the audit log (actor, pocket, status, query-stripped base
URL) — the credential token is never logged.

## Pockets — Write Actions

RFC 05 M2a. A pocket's `rippleSpec.actions` declares **write** bindings
(`POST` / `PUT` / `PATCH` / `DELETE`) — the write half of the data layer.
A write has blast radius a read does not, so two controls sit on top of the
SSRF guards the read executor already enforces:

- **The per-pocket write allowlist** (`allowed_writes` on the backend
  config). A write whose `(method, path)` does not match an allowlist entry
  is rejected server-side before any call leaves PocketPaw. The allowlist
  lives **outside** `rippleSpec`, in the same human-configured store as the
  credential — the agent authors bindings, a human authorizes the *class*
  of writes. The allowlist is **empty by default**: fail-closed, no write
  fires until an owner sets a policy.
- **Instinct-reject (fail-closed).** An action whose declaration carries a
  truthy `requires_instinct` is rejected with `code: instinct_required` and
  makes no call — M2a has no Instinct approval surface, so it refuses
  rather than silently honor-then-ignore the flag. M2b wires the approval
  routing.

### `PUT /pockets/{pocket_id}/backend/write-policy`

Set the pocket's write allowlist. Requires pocket **owner** access.

Request body:

| Field | Type | Notes |
|-------|------|-------|
| `allowed_writes` | array | List of `{method, path_pattern}` rules. Replaces the whole list. An empty list is valid — it revokes every write. |

Each rule: `method` is one of `POST` / `PUT` / `PATCH` / `DELETE`;
`path_pattern` is a glob (`/leases/*/renew` allows `POST /leases/42/renew`).
Omitting a verb means no action with that verb can ever fire.

Response `200`: the backend summary, including the updated `allowed_writes`.

Returns `400` when the pocket has no backend configured — a write policy
with no backend to apply it to is meaningless. The change is audit-logged.

### `POST /pockets/{pocket_id}/actions/run`

Run one declared `rippleSpec.actions` write action against the pocket's
configured backend. Access is **owner or explicit `shared_with` only** —
deliberately narrower than the source-run route: a write has blast radius,
so a workspace-visible pocket does **not** grant run access.

Request body:

| Field | Type | Notes |
|-------|------|-------|
| `action` | string | Required. The action's name (its key in `rippleSpec.actions`). |
| `path` | string | Required. The resolved path — Ripple's `{...}` expression resolver runs client-side at click time. |
| `params` | object | Optional. The resolved request body. |
| `idempotency_key` | string \| null | Optional. When omitted the server generates one so a write retried after a timeout cannot double-submit. |

The HTTP `method` is **read server-side** from the persisted action entry —
the client never picks the verb. The write fires only if the owner
allow-listed the `(method, path)`.

Response `200` (success):

```json
{
  "ok": true,
  "action": "mark_renewed",
  "status": 201,
  "response": { "id": 42, "status": "renewed" },
  "on_success": [{ "action": "run_source", "source": "leases" }],
  "on_error": []
}
```

Response `200` (rejected): `ok` is `false`, with an `error` message and a
`code`. Codes: `action_not_found`, `bad_binding`, `instinct_required`,
`rate_limited`, `bad_base_url`, `bad_path`, `bad_host`, `not_allowed`,
`redirect`, `http_error`, `too_large`, `timeout`, `request_failed`,
`error`. The result is delivered **in this response body** — there is no
`pocket_mutation` SSE emit; the client applies the `on_success` /
`on_error` reconcile handlers.

Returns `400` when the pocket has no backend configured; `403` when the
caller is neither the owner nor in `shared_with`.

**Security.** The write executor inherits every SSRF / timeout / size /
redirect guard from the shared `_http_guard` module (the same code the
read executor uses), then layers the write allowlist check, the
fail-closed instinct-reject, an `Idempotency-Key` header on every call,
and a write-specific rate limit — 20 writes per `(pocket, user)` per
minute, a **separate** counter from the read budget. Every run (including
every rejection) is written to the audit log; the credential token is
never logged.

## Pockets — Tool Invocations (`invoke_tool`)

feat/invoke-tool-v1. `invoke_tool` is the click-driven tool verb for pocket
FLOW-BUTTONs. A button fires `{action: "invoke_tool", tool, args}`; Ripple
resolves the `args` client-side and the host POSTs to the route below. Like
write actions, it carries a per-pocket allowlist that lives **outside**
`rippleSpec` — a human authorizes which tools a pocket may run, so a
compromised or hallucinated spec cannot grant itself a tool.

A grant's `tool` is one of:

- a connector action, `connector:<name>:<action>` (e.g.
  `connector:github:list_issues`), or
- a built-in tool name (e.g. `web_fetch`) — reserved; the built-in registry
  dispatch is a v1.x follow-up, so a built-in grant currently returns
  `code: unknown_tool`.

**Read/write split.** A connector grant is dispatched through the shared
connector executor (`connectors.service.execute`). A **read** action
(`trust=auto`) fires immediately and returns its data. A **write** action
(`trust=confirm`/`restricted`) **never** runs inline — it is **proposed for
human approval** through the Instinct gate. The route files a pending Instinct
Action (via `propose_external_action`) and returns `code: instinct_pending`
with a `proposed_action_id`; the connector write fires only when a human
approves the Action in The Tray, at which point the instinct router runs the
existing execute-on-approve path (`execute_approved_external_action` →
`connectors.service.execute`, re-validated for workspace + params + idempotency).
The client's `on_success` handler branches on `code == "instinct_pending"` to
show a "sent for approval" state and can watch the `proposed_action_id`.

### `PUT /pockets/{pocket_id}/backend/tool-policy`

Set the pocket's tool allowlist. Requires pocket **owner** access.

Request body:

| Field | Type | Notes |
|-------|------|-------|
| `allowed_tools` | array | List of `{tool}` grants. Replaces the whole list. An empty list is valid — it revokes every tool (fail-closed). |

Each grant: `tool` is a built-in tool name or a connector action
`connector:<name>:<action>` (`min_length=1`). Omitting a tool means that
tool can never fire.

Response `200`: the backend summary, including the updated `allowed_tools`.

Returns `400` when the pocket has no backend configured — a tool policy with
no backend to apply it to is meaningless. The change is audit-logged
(`pocket.backend.tool_policy`).

### `POST /pockets/{pocket_id}/tools/run`

Invoke a named tool with the resolved args. Access is **owner or explicit
`shared_with` only** — a tool invocation has the same blast radius as a write
binding, so a workspace-visible pocket does **not** grant run access.

Request body:

| Field | Type | Notes |
|-------|------|-------|
| `tool` | string | Required (`min_length=1`). The tool name — a built-in name or `connector:<name>:<action>`. |
| `args` | object | Optional. The resolved tool arguments (Ripple's `{...}` resolver runs client-side at click time). |

The allowlist is read server-side off the backend-credential row, never from
the spec. A pocket with no backend, no grants, or a tool not on the list
returns `code: not_allowed` — fail-closed.

Response `200` (connector read fired):

```json
{
  "ok": true,
  "tool": "connector:github:list_issues",
  "status": 200,
  "response": [{ "number": 1, "title": "first issue" }],
  "on_success": [],
  "on_error": []
}
```

Response `202` (write proposed for approval):

```json
{
  "ok": true,
  "tool": "connector:github:create_issue",
  "status": 202,
  "code": "instinct_pending",
  "proposed_action_id": "act-7f3c…",
  "response": {
    "action_id": "act-7f3c…",
    "proposed_action_id": "act-7f3c…",
    "status": "pending_approval",
    "connector": "github",
    "action": "create_issue"
  }
}
```

The write does **not** run at this point. The pending Action appears in The
Tray; on approve, the instinct router fires the connector write through the
existing execute-on-approve path. On reject, the write never runs.

Other rejection codes (`ok: false`): `not_allowed` (tool not on the
allowlist / no backend), `not_reachable` (connector not bound to this
pocket), `unknown_tool` (the connector has no such action, or a built-in
grant has no registry implementation yet), `bad_grant` (malformed
`connector:` grant), `propose_failed` (the write could not be filed for
approval — e.g. the Instinct store was unavailable; the write is **not** run
inline as a fallback), plus any connector-side `CloudError` code. The result
is delivered **in this response body** — there is no `pocket_mutation` SSE
emit; the client applies the `on_success` / `on_error` reconcile handlers.

Returns `403` when the caller is neither the owner nor in `shared_with`;
`404` when the pocket is not in the caller's scope.

**Security.** A connector grant re-checks the pocket/workspace bind
(`is_connector_bound_to_pocket`, the tenant boundary) and the action's trust
level before any call leaves PocketPaw; the connector path's outbound URL is
bounded by the connector definition, not by a spec-supplied URL. A
URL-taking built-in tool, when that path lands, must route through the same
`_http_guard` SSRF boundary the read/write executors use.

## Pockets — Jobs

pp#1459. A read (a source) fetches data into the canvas and a write (an
action) sends one HTTP call. A **job** is the third kind: a named,
server-side async unit of work that runs for minutes, computes a result,
and merges it back into the pocket's `state` so an open canvas updates
live. Because a job runs on the shared ARQ worker rather than in the
request, it survives the user closing the browser, and it emits its update
over the same cross-process bridge the resumable chat runs use.

A job is declared as an action with `kind: "job"` in `rippleSpec.actions`:

```json
{
  "actions": {
    "score_applications": {
      "kind": "job",
      "job": "score_applications",
      "params": { "batch_size": 20, "connector": "snctm-api" },
      "label": "Score Next Batch",
      "requires_instinct": false
    }
  }
}
```

The `job` value is the name of a callable registered in the workspace job
registry. An action with no `kind` keeps the existing write-action
behavior, so jobs are additive.

### Trigger: `POST /pockets/{pocket_id}/actions/run` with `kind: "job"`

The same endpoint and the same owner-or-`shared_with` access as a write
action. When the named action's `kind` is `"job"`, the route enqueues the
job instead of making an HTTP call.

Behavior:

- An unknown job name returns `400 job.unknown`.
- The server reads the params from the **persisted action declaration**, not
  from the request. A non-empty client `params` is rejected with
  `400 job.params_not_accepted` so a click can never widen a job's scope.
- A param key that looks credential-bearing (it contains `token`, `api_key`,
  `secret`, and the like, at any nesting depth) is rejected with
  `400 job.params_forbidden`. Jobs read workspace credentials server-side and
  never accept tokens through params.
- `requires_instinct: true` is rejected with
  `400 job.instinct_not_yet_supported`. The Instinct approval path for jobs
  lands in a later version; until then a job that asks to be gated refuses
  rather than run ungated.

Response `200` (enqueued):

```json
{ "ok": true, "code": "job_enqueued", "job_id": "665a1f2e9c3b4a0012ab34cd" }
```

The client polls the status endpoint below. The result arrives on the canvas
as a live `state` update when the job finishes; it is not in this response
body.

### `GET /api/v1/workspaces/{workspace_id}/jobs/{job_id}`

Poll a job's status. Requires workspace membership. The job document is
re-fetched by id and its workspace is re-checked, so a job id from another
workspace returns `404` rather than leaking its existence.

Response `200`:

| Field | Type | Notes |
|-------|------|-------|
| `job_id` | string | The job document id. |
| `status` | string | `queued`, `running`, `done`, or `failed`. |
| `error` | string \| null | The failure message when `status` is `failed`. |
| `created_at` / `started_at` / `ended_at` | string \| null | Lifecycle timestamps. |

The computed result is not returned here. On success the worker has already
merged it into the pocket's `state`; on failure it writes a
`{action}_status: "failed"` marker into `state` so the triggering button
stops spinning without a poll.

**Security.** Every job runs under the hardcoded synthetic identity
`system:workspace_job`, never the triggering user, and that identity is not
addressable from any request. A job result may write **only** `state`; a
result that touches `ui`, `actions`, `sources`, or `shape` is rejected and
the job is marked failed, so a job can never rewrite the template it runs
under. The writeback re-asserts that the target pocket belongs to the job's
workspace before it writes (fail-closed), and the worker enforces a timeout
(`POCKETPAW_JOB_TIMEOUT_SECONDS`, default `900`); a timed-out job writes the
same failed-state marker. Enqueue and failure are written to the audit log.

## Pockets — Template Reconcile

A pocket created from a template stores its `template_slug`. Re-running an
install/deploy script re-applies the template and **clobbers instance edits**.
Reconcile fixes that: it re-applies only the **template-owned** regions of the
source template while preserving the **instance-owned** regions.

| Region | Owner | Reconcile behavior |
|--------|-------|--------------------|
| `rippleSpec.ui` | template | overwritten from the template |
| `rippleSpec.actions` | template | overwritten from the template |
| `rippleSpec.sources` | template | overwritten from the template |
| `rippleSpec.shape` | template | overwritten from the template |
| `rippleSpec.state` (rows, `selected_id`, `pending_proposal`, …) | instance | never touched |
| pocket name / owner / team / visibility | instance | never touched |

Both endpoints accept standard cookie / bearer auth, or the loopback
internal-token bypass (the same one `GET /pockets/{id}` and `/spec/merge`
accept) so the `pocketpaw pocket reconcile` CLI can authenticate locally. The
service re-checks read (preview) / edit (apply) access on the resolved
identity.

### `POST /pockets/{pocket_id}/reconcile/preview`

Dry-run a reconcile — report what **would** change, write nothing. No
`PocketUpdated` event is emitted.

Response `200`:

```json
{
  "pocket_id": "663...",
  "template_slug": "applications-triage",
  "template_owned_regions": ["ui", "actions", "sources", "shape"],
  "changed_regions": ["ui"],
  "unchanged_regions": ["actions", "sources", "shape"],
  "preserved_regions": ["state"],
  "has_changes": true
}
```

Returns `422` (`reconcile.no_template`) when the pocket has no `template_slug`,
`422` (`reconcile.template_unresolved`) when the slug no longer resolves on
disk, `403` when the caller can't read the pocket, `404` for a missing /
cross-tenant pocket.

### `POST /pockets/{pocket_id}/reconcile/apply`

Apply the reconcile — re-write the template-owned regions, preserve the
instance-owned regions, persist through the same spec write path as a normal
edit (so the spec is normalized + validated and a `PocketUpdated` event fires).
**Edit access required.**

Response `200`:

```json
{
  "ok": true,
  "skipped": false,
  "diff": { "...": "the same diff shape as preview" },
  "pocket": { "...": "the updated pocket wire dict" }
}
```

When the pocket already matches its template the write is **skipped**
(`"skipped": true`, no `pocket` field, no event). Error codes mirror the
preview route, plus `403` when the caller lacks edit access — enforced even on
the skipped no-write path so a non-editor cannot probe sync state.

## Skills — Per-Backend API Skills

Increment 2b (the second half of pocket Increment 2, after the built-in
templates of 2a). When a pocket is bound to a backend, the
pocket-authoring agent does better work if it can see the backend's
**real** API instead of guessing endpoints. This endpoint installs a
backend's OpenAPI / Swagger document as a loadable skill: the agent then
authors `rippleSpec.sources` / `rippleSpec.actions` against real relative
paths and real response shapes rather than hallucinating them.

The skill is a `SKILL.md` file written under `~/.pocketpaw/skills/api-<domain-slug>/`
— one of the three roots PocketPaw's `SkillLoader` scans. The
pocket-specialist runtime loads it (keyed by the pocket's backend
hostname) and splices a `<backend-api>` endpoint reference into the
authoring prompt.

### `POST /skills/api-doc`

Install a backend's OpenAPI / Swagger spec as a per-backend API skill.
Requires the `skills.manage` role (**ADMIN**) — installing a skill
changes workspace-wide pocket-authoring behaviour.

Multipart form upload:

| Field | Type | Notes |
|-------|------|-------|
| `file` | file | Required. The OpenAPI 3.x or Swagger 2.x document — `.json`, `.yaml`, or `.yml`, max 2 MB. |
| `name` | string | Optional. The backend display name — used to derive the skill slug when the spec itself names no server. |

The slug is derived from the spec's server hostname (`servers[0].url`
for OpenAPI 3.x, `host` for Swagger 2.x), falling back to `name`. The
generated reference groups operations by tag (or first path segment),
caps at 200 endpoints, and records each operation's method, path,
summary, key request params, and key 200-response fields.

Response `200`:

```json
{ "ok": true, "slug": "api-example-com" }
```

Returns `422` when the file extension is unsupported, the file exceeds
the 2 MB cap, the document is unparseable, or it carries no `paths`
object. Every install is audit-logged with the workspace, the actor, and
the resulting slug — never the spec contents.

## Plugins — Install a `.claude-plugin`'s Skills and MCP Servers

PocketPaw adopts the `.claude-plugin` standard so a whole plugin's skills
and MCP servers install in one step. This endpoint clones a GitHub repo,
reads its `.claude-plugin/plugin.json`, copies each `skills/<name>/SKILL.md`
directory into the skill loader path, reloads the loader, registers and
starts any MCP servers the bundle declares, and records the install in a
registry at `~/.pocketpaw/plugins.json`.

### MCP servers

After the skills step, the installer reads the bundle's MCP config — a
`.mcp.json` file at the plugin root in the standard
`{"mcpServers": {name: spec}}` shape (a manifest `mcp_servers` path
override is honoured if present). When there is no MCP config the step is
recorded as `skipped`; it never fails the install.

Each declared server is mapped to a PocketPaw MCP server config:
`command`, `args`, and `env` carry over directly, and `transport` is
derived from the spec's `type` (`stdio` is the default; `http`, `sse`, and
`streamable-http` map through). To avoid cross-plugin collisions the
registered name is namespaced as `plugin:<plugin_name>:<server_name>`.

Every server is registered and started through the MCP manager — one step
per server:

- **`succeeded`** — the server started, **or** it registered but couldn't
  start because it's missing required env. The latter is non-fatal and
  carries a `needs env: KEY` detail so the operator knows to supply the
  credential; the server is still recorded as installed.
- **`failed`** — the server failed to start for any other reason.

The namespaced server names appear in the registry entry under
`mcp_servers` and on the report's `installed_mcp_servers`.

### `POST /plugins/install`

Install a plugin's skills and MCP servers from a GitHub source. Requires
the **admin** scope — installing a plugin changes workspace-wide agent
behaviour.

Request body:

```json
{ "source": "owner/repo" }
```

`source` accepts `owner/repo`, `owner/repo/subdir` (when the plugin lives
in a subdirectory), or a full GitHub URL (a `/tree/<ref>/<subdir>` path is
honoured). The repo (or subdir) must contain a `.claude-plugin/plugin.json`
manifest and at least one `skills/<name>/SKILL.md`. An MCP `.mcp.json` is
optional.

Response `200` — a step-by-step install report:

```json
{
  "plugin": "my-plugin",
  "installed_at": "2026-06-07T12:00:00",
  "steps": [
    { "name": "read_manifest", "status": "succeeded", "detail": "my-plugin v1.2.3" },
    { "name": "skill:alpha", "status": "succeeded", "detail": "" },
    { "name": "reload_loader", "status": "succeeded", "detail": "" },
    { "name": "mcp:weather", "status": "succeeded", "detail": "" },
    { "name": "mcp:db", "status": "succeeded", "detail": "needs env: DB_URL" },
    { "name": "record_registry", "status": "succeeded", "detail": "" }
  ],
  "installed_skills": ["alpha"],
  "installed_mcp_servers": ["plugin:my-plugin:weather", "plugin:my-plugin:db"]
}
```

Each unit of work is a step with status `succeeded` / `skipped` /
`failed`, so a per-skill copy failure or a single MCP server start failure
surfaces in the report rather than aborting the whole install. When the
bundle declares no MCP servers, a single `mcp` step is recorded as
`skipped`. Up-front failures return clear status codes instead of `500`:

| Status | When |
|--------|------|
| `400` | Missing or malformed `source`, or an invalid `plugin.json`. |
| `404` | No `.claude-plugin/plugin.json`, or no skills found in the plugin. |
| `502` | The git clone failed. |
| `504` | The git clone timed out. |

A malformed `.mcp.json` (parse error, or a wrong `mcpServers` shape) does
**not** fail the request — it surfaces as a single `failed` `mcp` step in
the report, so already-installed skills and the registry entry are
preserved.

Every install is audit-logged with the source, plugin name, version, and
the installed skill names.

### `GET /plugins`

List every installed plugin from the registry
(`~/.pocketpaw/plugins.json`). Requires the **admin** scope.

Response `200` — an array of installed plugins:

```json
[
  {
    "name": "my-plugin",
    "version": "1.2.3",
    "source": "acme/widgets",
    "skills": ["alpha", "beta"],
    "mcp_servers": ["plugin:my-plugin:weather"],
    "installed_at": "2026-06-08T12:00:00"
  }
]
```

### `POST /plugins/remove`

Uninstall a plugin: delete each skill directory it installed, **stop** each
of its namespaced MCP servers and remove their configs from the MCP manager,
reload the skill loader, and drop its registry entry. Stopping the live
server (not just deleting its config) mirrors install, which both registers
the config and starts the server — so remove tears down the running
connection too, rather than leaving it up until the next restart. Requires
the **admin** scope.

Request body:

```json
{ "name": "my-plugin" }
```

Response `200` — a step-by-step remove report (mirrors the install report):

```json
{
  "plugin": "my-plugin",
  "removed_at": "2026-06-08T12:05:00",
  "steps": [
    { "name": "skill:alpha", "status": "succeeded" },
    { "name": "mcp:plugin:my-plugin:weather", "status": "succeeded" },
    { "name": "reload_loader", "status": "succeeded" },
    { "name": "drop_registry", "status": "succeeded" }
  ],
  "removed_skills": ["alpha", "beta"],
  "removed_mcp_servers": ["plugin:my-plugin:weather"]
}
```

Like install, each component is a step with status `succeeded` / `skipped`
/ `failed`. A component that's already gone (a missing skill dir, an MCP
server that's neither running nor registered) is `skipped` — the remove
still completes and the registry entry is **always** dropped, so a
half-removed plugin never lingers in the listing. The only up-front error
is an unknown plugin:

| Status | When |
|--------|------|
| `400` | Missing or invalid `name`. |
| `404` | The named plugin is not installed. |

Every removal is audit-logged (`action="plugin_remove"`) with the plugin
name and the removed skill / MCP server names.

The registry read-modify-write (shared by install and remove) is
serialised by a process-level lock and written via a temp file + atomic
`os.replace`, so concurrent operations can't corrupt `plugins.json` or
clobber each other's entries.

### Per-agent skills (`skill_refs` + `plugins`)

An agent's `config` carries two skill-bearing fields, set on
`POST /agents` (create) or `PATCH /agents/{id}` (update — via either the
nested `config` object or the flat top-level fields):

| Field | Type | Meaning |
|-------|------|---------|
| `skill_refs` | `string[]` | Skill names this agent always materializes. |
| `plugins` | `string[]` | Installed plugin names whose bundled skills this agent always materializes. |

Both default to `[]`. Unlike a surface / entity-room `skill_names` subset
— which only applies inside that room — an agent's `skill_refs` plus the
skills of its enabled `plugins` fold into the per-run skill set on **every**
run the agent does, regardless of surface. At run time the plugin names are
resolved to their skills via the installed-plugin registry
(`~/.pocketpaw/plugins.json`); an unknown plugin name is ignored and a
missing / unreadable registry degrades to no plugin skills (it never fails
the run). The agent set is UNIONed with any surface/entity skill subset, so
both apply together. Per-agent MCP servers are **not** part of this — that
is a separate, deferred slice.

## Pockets — Catalog-as-Allowlist Ingest Gate

Increment 5. The Ripple renderer has a **closed widget registry**: a
node whose `type` is not a known widget renders as a red "Unknown widget
type" box. The catalog gate catches that at ingest time, before the spec
is persisted.

On every pocket write that carries a `rippleSpec`, the service walks the
node tree and flags any node whose `type` is not in the widget manifest
(plus the control-flow types `if` and `each`). The gate runs in one of
two modes:

- **Strict** — the agent-generation path (`create_from_ripple_spec`, the
  pocket-specialist `agent_create` / `agent_update` ops). A violation
  blocks the write; the specialist edit tools return the corrective
  message so the LLM can retry with a real widget type.
- **Logged** — the human / import path (`POST /pockets`,
  `PUT /pockets/{id}`). A violation is recorded as a structured warning
  for triage but does **not** block — an older imported spec may use a
  widget that has since left the catalog.

Each flagged node reports `{path, type, suggestion}`, where `suggestion`
is the nearest catalog widget by edit distance. The gate is best-effort:
when the widget manifest can't be fetched it is skipped.

### Required-prop gate

The widget manifest marks the props a widget cannot render without as
`required: true` (a `chart` with no `data`, a `stat` with no `value`, a
`table` with no `columns`/`rows`). That flag used to live only in the
system prompt — a node like `{"type": "chart", "props": {}}` passed the
catalog gate (its `type` is known) and rendered an empty box. The
required-prop gate runs as a sibling to the catalog walk and closes that
hole: it flags any node missing a manifest-required prop for its `type`.

It is the rippleSpec expression of the constraint-zone model's 🔒 **HARD**
`required_fields` zone — the agent is free to choose which widgets to use
and how to fill the creative props, but the manifest-declared structural
minimum is locked and checked, not merely asked for. Same **strict**
(agent path, blocks + returns a corrective message naming the missing
prop) / **logged** (human / import path, structured warning, never blocks)
posture as the catalog gate, and the same best-effort skip when the
manifest can't be fetched. A prop counts as present when its key exists
with a non-null value — a literal, an empty list / `0` / `false`, or a
bound `{...}` expression all satisfy it; only a missing key or explicit
`null` is a violation. A node-level `bind` satisfies a single-required-prop
input widget (the bound value populates the prop at render time).

Each flagged node reports `{path, type, missing, required}`.

### Escape-hatch widgets

Two catalog widgets cover content the rest of the catalog can't express:

- `model-viewer` — an interactive 3D model (`.glb` / `.gltf`) with
  orbit / zoom / pan controls.
- `embed` — the **sanctioned escape hatch**: a renderer-sandboxed
  iframe for a CodePen, a Figma frame, an Observable notebook, or a
  self-contained visualization. `mode` is required (`url` or `srcdoc`).
  The iframe `sandbox` attribute is renderer-controlled — it is **not**
  author-settable.

### `embed` URL / host policy

An `embed` node in `mode: "url"` points an iframe at a third-party page,
so its `url` is an SSRF / clickjacking boundary. The ingest gate
enforces:

- `url` must be **https** — plain `http` is rejected.
- the host must be on the embed allow-list (`POCKETPAW_RIPPLE_EMBED_ALLOWED_HOSTS`,
  a JSON array — default: `youtube-nocookie.com`, `player.vimeo.com`,
  `codepen.io`, `codesandbox.io`, `observablehq.com`, `www.figma.com`).
- loopback / RFC1918 / link-local / carrier-grade-NAT / cloud-metadata
  hosts are **hard-blocked unconditionally** — this holds even if the
  allow-list is widened to `["*"]`.

Every ingested spec that contains an `embed` node is audit-logged
(category `pocket_embed`) with the embed count and URLs — never the
iframe contents.

## Outcome Metering

RFC 05 M2b.2 + gap-3. When a governed write action succeeds, the pocket's
binding can declare a named `outcome` and an optional billable value/unit.
Each one appends a row to a workspace-scoped, append-only JSONL ledger.
Two read surfaces sit over the ledger; both take tenancy from the auth
context and **reject** a `workspace_id` query param (a caller cannot read
another workspace's ledger).

### Declaring an outcome on a binding

A write binding in `rippleSpec.actions` declares the metering fields:

| Field | Type | Notes |
|-------|------|-------|
| `outcome` | string \| null | The named business event (`meeting_booked`, `ticket_resolved`, …). `null` → the write is not metered. |
| `outcome_value` | number \| null | The billable value the operator assigns this outcome. Requires `outcome_unit` AND a non-null `outcome` — a half-declared pair is rejected at parse time. |
| `outcome_unit` | string \| null | The unit the value is denominated in (`usd`, `ticket_resolved`, …). Requires `outcome_value`. |

Declaring `outcome` with no value/unit is the **count-only** binding (the
prior behaviour). Declaring all three turns the count into a billable
figure.

### `GET /outcomes`

Count this workspace's recorded outcomes, grouped by name and pocket.
Requires the `outcomes.read` action.

Query params: `pocket_id` (narrow to one pocket), `since` (inclusive
ISO-8601 lower bound on `occurred_at`). Both optional.

Response `200`:

```json
{ "total": 12, "by_outcome": { "ticket_resolved": 9, "meeting_booked": 3 },
  "by_pocket": { "p_support": 9, "p_sales": 3 } }
```

### `GET /outcomes/meter`

Aggregate this workspace's **billable** outcomes into a queryable figure —
the "pay for governed outcomes" read primitive. Requires the
`outcomes.read` action.

Query params: `pocket_id`, `since` (inclusive lower bound), `until`
(**exclusive** upper bound, so adjacent periods never double-count a
boundary outcome). All optional.

Response `200`:

```json
{ "total_outcomes": 12, "metered_count": 9,
  "by_unit": {
    "usd": { "unit": "usd", "count": 6, "total_value": 7200.0 },
    "ticket_resolved": { "unit": "ticket_resolved", "count": 3, "total_value": 3.0 }
  } }
```

`total_outcomes` counts every matching row (including count-only ones);
`metered_count` counts only the rows carrying a whole value/unit pair;
`by_unit` sums `outcome_value` per unit over the window.

**Deferred (not in this surface):** invoicing, payment, currency
conversion, a pricing-rules engine, and disputes / clawback. This endpoint
returns a raw sum of declared values — the queryable figure those layers
will build on later (see `outcome-spec.md`).

## Agent Activity

HR-12a. The workspace-scoped answer to "which of my agents are working
right now". Distinct from the herdr cockpit (`GET /cockpit/*`), which reads
terminal panes on one operator box, is ADMIN-only, and never shows a `/chat`
agent — that agent runs as an in-process SDK client, not a pane.

The board is built from `ChatRunDoc`, the durable per-turn record, so it is
correct whether runs execute in the web process or an arq worker, complete
across multiple workers, and intact after a restart.

### `GET /agent-activity`

One entry per agent in the caller's workspace with at least one run in the
last **24 hours**. Requires the `agent_activity.read` action (MEMBER). Takes
no query params; tenancy comes from the auth context and a `workspace_id`
query param is **rejected** (`400`), not ignored.

This is a **team board**: it covers every member's runs, not just the
caller's. An Agent is a workspace resource, so its aggregate state is shared.
The individual turn is not — the response carries no `user_id`, no run id and
no message content, and `GET /cloud/chat/runs/{run_id}/stream` still returns
`404` for a run belonging to another member.

`agent_id` is the agent's ObjectId hex (`Agent._id`), the same key
`GET /agents` returns — not a display name.

Response `200`:

```json
{ "agents": [
    { "agent_id": "66f1a2b3c4d5e6f708192a3b", "status": "active",
      "active_runs": 2, "last_active": "2026-07-28T11:58:04+00:00" },
    { "agent_id": "66f1a2b3c4d5e6f708192a3c", "status": "blocked",
      "active_runs": 0, "last_active": "2026-07-28T10:12:44+00:00" }
  ],
  "ts": "2026-07-28T12:00:00+00:00" }
```

`status` uses the Mission Control `AgentStatus` vocabulary, the same one
the cockpit's pane dots use:

| Status | Meaning |
|--------|---------|
| `active` | The agent has at least one `queued` or `running` run. Wins over any earlier failure. |
| `blocked` | No live run, and the agent's newest run `failed` or was `interrupted`. |
| `idle` | No live run, and the newest run `completed` or was `cancelled` (a user stopping their own turn does not block the agent). |

Agents with **no run in the window are omitted** rather than returned as
`offline`: this surface reads runs, not the agent roster. A client that
wants every configured agent joins this board against `GET /agents` and
treats the absent ones as offline.

`active_runs` is how many of that agent's runs are live now; `last_active`
is the newest run's end, else its start, else its creation; `last_run_id`
identifies that run. Entries are ordered working-first, then most recently
active. `ts` stamps when the board was built.

v1 is a plain GET for the client to poll. A push stream is the upgrade path
if polling stops being enough — event-driven off the existing run-status
transitions, not a faster poll.

## Files — Versioned Writes

The `file_versions` entity layers a versioned write path over the uploads
storage adapter. A file's live (current) content lives in the
StorageAdapter; each edit archives the prior blob as a `FileVersionDoc` row
and bumps a per-file `content_version` counter on the `FileUpload` record.
All four routes share the `/files` prefix with the listing router (`GET
/files`, `/files/tree`, `/files/browse`) without collision, require a valid
license, and are workspace-scoped — every read is filtered to the caller's
workspace.

### `POST /files/write`

Create a file, or overwrite an existing one in place (versioned). Used for
first-save and programmatic writes.

Request body:

```json
{ "path": "<file id or path>", "content": "<full content>", "filename": "<optional display name>" }
```

Response `201`:

```json
{ "fileId": "<id>", "version": 1, "sizeBytes": 42 }
```

When a file already exists for `path`, the content is updated through the
PUT path instead and `version` reflects the bumped counter.

### `PUT /files/{file_id}`

Replace a text file's content inline with optimistic concurrency. Body:

```json
{ "content": "<new text>", "expectedVersion": 3 }
```

`expectedVersion` (or the `If-Match: <version>` header, which takes
precedence) guards against lost updates. Response `200`:

```json
{ "fileId": "<id>", "newVersion": 4, "sizeBytes": 57, "contentHash": "<sha256>" }
```

Returns `404` if the file is missing, `409` on a version conflict, and
`422` if the file's mime type is not editable inline.

### `GET /files/{file_id}/versions`

List archived versions for a file (oldest first, content omitted). Returns
only versions in the caller's workspace:

```json
[ { "id": "<oid>", "fileId": "<id>", "versionNumber": 2, "sizeBytes": 42,
    "editorKind": "human", "editorId": "<user id>", "createdAt": "<iso>" } ]
```

### `GET /files/{file_id}/versions/{version_id}`

Fetch a single archived version with its full content (for revert preview /
diff). Workspace-scoped — a version id from another workspace returns `404`.

## Agent Artifact Delivery (`deliver_artifact`)

`deliver_artifact` is a cloud-only in-process MCP tool the chat agent calls to
hand the user a **downloadable** result. The cloud agent works inside a
per-tenant jail (ART-2) the user can't reach, so a file the agent "wrote to
`./out.pdf`" — or a preview server it started on `127.0.0.1` — is invisible to
them. This tool lands the artifact in the tenant's blob storage and returns a
real, short-lived download URL.

It is registered cloud-gated via the `pocketpaw.mcp_servers` entry point
(`pocketpaw_deliver` → `mcp__pocketpaw_deliver__deliver_artifact`), ambient on
the default chat surface (OSS never sees it). Source:
`ee/pocketpaw_ee/agent/mcp_servers/deliver.py`.

**Input:** `{ "path": "<file or directory inside the agent's workspace>" }`.

**Routing:** a single file is uploaded as-is (mime guessed from the filename); a
directory is zipped (`application/zip`) and the zip is uploaded. Both go through
`EEUploadService.upload` — the same workspace-scoped pipeline as `POST /uploads`
— so a delivered artifact emits `FileReady` (→ KB) and appears in the tenant's
`GET /files` listing, and the returned URL is the storage adapter's presigned
download (S3) or the authenticated `/api/v1/uploads/{id}` path (local adapter).

**Result (JSON in the MCP text payload):**

```json
{ "ok": true, "filename": "report.pdf", "url": "<download URL>",
  "file_id": "<id>", "size": 12345, "mime": "application/pdf",
  "expires_in_seconds": 300 }
```

On failure the tool returns `is_error` with a plain reason (missing identity,
path escapes the jail, file missing / over the size cap, or the upload failed) —
the agent surfaces the reason rather than fabricating a link.

**Security:** the path must resolve to inside the caller's own jail
(`~/.pocketpaw/workspaces/<workspace_id>/...`); `..` traversal, absolute paths
out, symlinks pointing out (including symlinks nested inside a delivered
directory), and another tenant's jail are all rejected (reusing ART-2's
path-segment guard). Size is capped by `POCKETPAW_DELIVER_MAX_MB` (default
`100`). Because the upload relaxes the mime allowlist, a delivered artifact
whose mime is not in `INLINE_MIMES` (HTML, SVG, JS, …) is served with
`Content-Disposition: attachment` on the presigned download — it downloads, it
does not render inline on the storage origin. Inline-safe types (images, pdf,
plain text) still embed as before. The whole tool is gated on
`is_multi_tenant_cloud()`.

## Sites — Native Editing

NE-4b / NE-5b. The **native site editor** renders a svelte Paw Site directly in
the dashboard — the built page's markup is injected into a shadow root rather
than framed in an iframe — and persists in-place text / prop edits by splicing
them back into the pocket's component source as a **reviewable Branch draft**.
Two endpoints back it: one serves the render, one persists the edits. Source:
`ee/pocketpaw_ee/sites/router.py`.

Both require an authenticated workspace context, the `fabric.write` action, and
the `sites` plan feature (the whole sites router is gated on it). Both operate
on a **svelte** Paw Site — a pocket with `engine: "svelte"` and a `source`
component map; a ripple-engine or non-site pocket is a `422`
(`pocket.not_svelte_site`). A missing or access-denied pocket surfaces as `404`
(`pocket.not_found`) / `403` (`pocket.access_denied`) from the pockets service,
exactly like every other `by-pocket` route. All work is tenant-scoped on the
request context (`workspace_id`, `user_id`).

### `POST /sites/by-pocket/{pocket_id}/leaf-edits`

Persist a batch of native-editor leaf edits as a Branch draft. The editor has
already rendered each edit optimistically; this splices them into the pocket's
svelte source and writes the draft — **there is no rebuild**. Skipping the
per-edit iframe rebuild is the UX win over the older `edit_svelte_component`
path; an approved review is what later takes the draft live.

Request body:

| Field | Type | Notes |
|-------|------|-------|
| `edits` | array | Required, non-empty. Each entry is one leaf edit. An empty list is a `422` (`site_leaf_edit.empty_edits`). |

Each edit:

| Field | Type | Notes |
|-------|------|-------|
| `uid` | string | The stable id of the edited leaf, e.g. `"Hero:headline:0"`. |
| `op` | object | The change. One of `{ "kind": "setText", "html": "<new inner HTML>" }` or `{ "kind": "setProp", "name": "<prop>", "value": <any> }`. The `op` shape is validated downstream by the `apply-leaf-edit` CLI, not at the request boundary. |

Response `200`:

```json
{
  "pocket_id": "p_abc123",
  "results": [
    { "uid": "Hero:headline:0", "applied": true, "reason": null },
    { "uid": "Pricing:cta:2", "applied": false, "reason": "uid not found in current source" }
  ]
}
```

One verdict per submitted edit, in submission order. `applied` is whether the
splice landed; `reason` (CLI-produced) explains a rejection and is `null` on
success. The caller keeps the whole-file re-author path for any leaf that comes
back `applied: false`.

**How it persists.** Edits apply **in order** through the paw-sites
`apply-leaf-edit` CLI — a pure source transform, no build or workerd. Only the
files whose contents actually changed are persisted (each write auto-writes the
Branch-draft snapshot), so a rejected edit churns no draft. A **dynamic** svelte
site carries its live-data bindings (`objects` / `sources` / `actions` / `auth`)
as sibling keys of the `{path: contents}` file map; the service splits those out
before the splice (the CLI treats every source key as a file) and **confines the
persist loop to the original file keyspace** — a binding key, or a brand-new
path the CLI might echo, is never written back as a component file.

Errors:

| HTTP | Code | When |
|------|------|------|
| 422 | `site_leaf_edit.empty_edits` | The `edits` list is empty. |
| 422 | `pocket.not_svelte_site` | The pocket is not a svelte Paw Site (no component source map). |
| 404 | `pocket.not_found` | Unknown pocket id. |
| 403 | `pocket.access_denied` | The caller lacks access to the pocket. |
| 500 | `sites.leaf_edit_failed` | The `apply-leaf-edit` CLI exited non-zero, timed out, or returned unparseable output. |

### `GET /sites/by-pocket/{pocket_id}/native-artifact`

Serve the armed svelte build's body markup and CSS so the native editor can
shadow-render the site.

Response `200`:

```json
{
  "pocket_id": "p_abc123",
  "body_html": "<div data-uid=\"Hero:root:0\">…<script id=\"paw-edit-manifest\">…</script></div>",
  "css": "/* concatenated stylesheets */"
}
```

- `body_html` is the built page's `<body>` **inner** HTML — the `data-uid`-stamped
  editable leaves plus the embedded `<script id="paw-edit-manifest">`. The
  frontend injects it into a shadow root.
- `css` is the built stylesheet(s) — inline `<style>` blocks plus every linked
  stylesheet — concatenated into one string, injected as a single `<style>`.

**Read-through cache — a plain view no longer rebuilds.** The endpoint hashes the
pocket's render inputs (svelte source map + theme + builder origin + generator
version) and serves a prior render from the on-disk artifact store
(`~/.pocketpaw/site-artifacts/<pocket_id>/<hash>.json`) on a **cache hit** — zero
subprocess builds. It builds once only on a **cold miss** (then stores the result),
and publishing a site plus every source-changing edit **pre-warm** the store in the
background, so a live/clean site is a hit and a view never triggers a build. A cold
miss's build is keyed on `pocket_id` in a stable directory, so `node_modules` /
`bun install` stay cached; it skips the SSR smoke fail-gate (`smoke=False`) but still
emits the static output that is read.

**Auth is `fabric.write`, not a read scope,** because a cold miss still **arms a
build**: it builds the pocket with a builder origin set so the generator stamps
`data-uid` on the editable leaves and embeds the edit manifest — the endpoint can
still trigger on-disk work, so it is not a pure read. The builder origin is resolved
from the request's `Origin` header, falling back to the configured
`PAW_SITES_BUILDER_ORIGIN` when absent (the same precedence as `/editable` and
`/dev-preview`), so the call works with no header.

**Origin stability — the pre-warm must match the view.** The builder origin is part
of the content hash, so a pre-warm only saves a view a build when it builds with the
**same** origin that view resolves. A browser view resolves its origin from the
request `Origin` header (the dashboard origin), so `POST /sites/publish` and
`POST /sites/by-pocket/{id}/leaf-edits` thread their request `Origin` into the
background pre-warm — otherwise the pre-warm would fall back to
`PAW_SITES_BUILDER_ORIGIN` while the view uses the dashboard origin, the two hashes
would differ, and every view would stay a cold miss. Chat-agent / MCP publishes and
edits have no request origin, so their pre-warm keeps the env fallback: **set
`PAW_SITES_BUILDER_ORIGIN` to the dashboard origin** (e.g.
`https://paw.example.com`, not the `http://localhost:8888` default) in every
deployment so that fallback matches the origin views ask for — a belt-and-braces
default even where the request `Origin` is threaded.

**CSS reader is path-traversal-guarded.** Stylesheet `href`s from the built
`index.html` are resolved against the build tree (relative `./_app/…` and
absolute `/_app/…` hrefs both) and each resolved path is checked to be contained
inside the tree before it is read — a `../` traversal in a hand-authored
component's `<link>` is refused.

Errors:

| HTTP | Code | When |
|------|------|------|
| 422 | `pocket.not_svelte_site` | The pocket is not a svelte Paw Site (no component build to render). |
| 404 | `pocket.not_found` | Unknown pocket id. |
| 403 | `pocket.access_denied` | The caller lacks access to the pocket. |
| 500 | `sites.generator_failed` | The arm build failed (missing toolchain, non-zero build, or smoke-gate failure). |

## Ship — Managed Deploys

SHIP-3. The `/api/v1/ship` surface behind the /ship console: a workspace
provisions a **box** (a VPS running Dokku), registers **apps** on it, and
deploys them. Every route is license-gated and scoped to the caller's active
workspace — the workspace never travels in a body or query param, and an id
belonging to another tenant reads as `404`, never `403` (existence does not
leak).

Long work never blocks the request. `POST /ship/boxes` and
`POST /ship/apps/{id}/deploy` enqueue an ARQ job and return immediately with a
pollable record; the engine-backed routes (domains, database, scale, checks,
resources, volumes, restart, rebuild, logs, metrics) run inline over SSH and
answer `409` with `code: ship.*_failed` when the deploy engine refuses.
pollable record; the engine-backed routes (domains, database, logs, metrics)
run inline over SSH and answer `409` with `code: ship.*_failed` when the deploy
engine refuses.

**Secrets never cross this surface.** A box's SSH key is decrypted only inside
the engine session and shredded with it. App env **names** are accepted and
stored (`env_refs`); env **values** are not. A database's connection string
stays on the box — `POST /ship/apps/{id}/db` returns the NAME of the variable
holding it.

### `POST /ship/boxes`

Provision a box. Body: `{"provider": "hcloud"}`. `server_type` and `region` are
optional; they default to `cx22` / `fsn1` (Hetzner: 2 vCPU / 4 GB / 40 GB in
Falkenstein — the cheapest shape that comfortably runs Dokku plus a couple of
app containers), overridable per deployment via `POCKETPAW_SHIP_SERVER_TYPE` /
`POCKETPAW_SHIP_REGION`.

Returns the box in `provisioning`; poll `GET /ship/boxes` until it is `ready`:

```json
{"id": "…", "provider": "hcloud", "ip": "", "status": "provisioning", "price_monthly": null}
```

`status` is one of `provisioning` | `ready` | `degraded` | `destroyed`.

### `GET /ship/boxes`

The workspace's boxes, newest first — a list of the object above.

### `GET /ship/boxes/{box_id}/metrics`

Live box health, read over SSH. Three percentages, `0.0`–`100.0`:

```json
{"cpu": 21.0, "mem": 37.5, "disk": 23.0}
```

`cpu` is derived from the 1-minute load average over the core count and capped
at 100. A box that is not `ready` answers `409 ship.box_not_ready`.

### `DELETE /ship/boxes/{box_id}`

**Parks** a teardown for human approval. Nothing is destroyed, no engine command
runs, and the box keeps its current `status`:

```json
{"status": "pending_approval", "proposal_id": "<instinct-action-id>"}
```

The `proposal_id` is a real Instinct Action id: the teardown lands in The Tray
for a human to approve or reject. Only on approval does
`ship.executor.execute_approved_ship_action` touch the box — the request path
never calls the engine's destroy verb. Repeating the call returns the same
`proposal_id` rather than filing a duplicate.

The executor re-checks `ship.manage` against the proposer's **current** role
before it runs, so an approval for a since-demoted proposer fails closed, and
re-approving an already-executed action never fires twice.

### `POST /ship/apps`

Register an app on a box. Body: `{"name": "demo", "box_id": "…"}`. Optional:
`image` (the container image reference the deploy ships), `git_ref`,
`build_path` (`dockerfile` | `nixpacks`), `prod`, and `env_refs` (variable
NAMES only). Returns:

```json
{"id": "…", "name": "demo", "box_id": "…", "status": "created", "urls": []}
```

`status` walks `created` → `deploying` → `live` | `failed`. A duplicate name on
the same box is `409 ship.app_exists`; a `box_id` from another workspace is
`404`.

### `GET /ship/apps?box_id=<id>`

The workspace's apps, newest first, optionally narrowed to one box.

### `POST /ship/apps/{app_id}/deploy`

Enqueue a deploy. Takes **no body** — the app already carries its image, and the
attempt pins that image so a later app edit cannot rewrite what is in flight. An
app with no image is `422 ship.app_no_image`; a box that is not `ready` is
`409 ship.box_not_ready`. Returns the attempt immediately:

```json
{"id": "…", "app_id": "…", "status": "queued", "started_at": "2026-07-22T…Z", "finished_at": null}
```

### `GET /ship/apps/{app_id}/deploys`

The app's deploy attempts, newest first. `status` walks
`queued` → `building` → `releasing` → `live`, or lands on `failed`;
`finished_at` is set on a terminal state. Poll this to follow a deploy.

### `POST /ship/apps/{app_id}/domains`

Route a domain to the app and (by default) issue a certificate for it. Body:
`{"domain": "demo.example.com", "enable_tls": true}`. Returns
`{"domain": "…", "tls_enabled": true}` and adds the resulting URL to the app's
`urls`.

### `GET /ship/apps/{app_id}/domains`

`{"domains": [{"domain": "…", "tls_enabled": true}]}` — the domains recorded at
add time.

### `POST /ship/apps/{app_id}/db`

Create a database service and link it to the app. Body is optional; `service`
defaults to `<app-name>-db`. `db_type` picks the engine — `postgres`, `redis`,
or `mongo` (default `mongo`); the box installs all three plugins at provision
time. The injected variable name follows the engine (`DATABASE_URL` for
postgres, `REDIS_URL` for redis, `MONGO_URL` for mongo). Returns:

```json
{"service": "demo-db", "linked_app": "demo", "env_var": "DATABASE_URL"}
defaults to `<app-name>-db`. Returns:

```json
{"service": "demo-db", "linked_app": "demo", "env_var": "MONGO_URL"}
```

`env_var` is the NAME of the variable the link injected. The connection string
is a secret and never crosses the wire.

### `PUT /ship/apps/{app_id}/scale`

Set how many containers run per process type. The body is a `scale` map of
process name → count; a count of `0` stops that process. Process names use the
Procfile grammar (`^[a-z][a-z0-9_-]*$`). Applies on the next deploy. Returns the
app with its new `scale`:

```json
{"scale": {"web": 2, "worker": 1}}
```

### `PUT /ship/apps/{app_id}/checks`

Configure zero-downtime deploys. `zero_downtime` (default `true`) toggles Dokku's
settle-and-drain deploy — the new container must pass its checks before the old
one is retired; `healthcheck_path` is the optional HTTP path the check hits.
Both apply on the next deploy. Returns the app's current settings:

```json
{"zero_downtime": true, "healthcheck_path": "/healthz"}
```

### `PUT /ship/apps/{app_id}/resources`

Set the app's CPU and/or memory ceilings (the cost-control lever, `resource:limit`).
`cpu` is in Dokku's CPU units, `memory_mb` in megabytes; a `0` leaves that
dimension unlimited, but at least one must be non-zero. Applies on the next
container start. Returns the app with its new `cpu_limit` / `memory_limit_mb`:

```json
{"cpu_limit": 1000, "memory_limit_mb": 512}
```

### `POST /ship/apps/{app_id}/volumes`

Create a persistent volume and mount it into the app (`storage:create` +
`storage:mount`). `mount_path` is the absolute container path; `name` is optional
and defaults to `<app-name>-data`. The data survives redeploys (a host bind
mount). Returns the app with its `volumes` list:

```json
{"volumes": [{"name": "demo-data", "mount_path": "/data",
              "host_path": "/var/lib/dokku/data/storage/demo-data"}]}
```

### `POST /ship/apps/{app_id}/restart` · `POST /ship/apps/{app_id}/rebuild`

Restart (`ps:restart`) or rebuild-from-source (`ps:rebuild`) the app. Both are
reversible bounces — the app comes back — so they run inline, not through the
Instinct gate, and they change no persisted config. Each answers a confirmation:

```json
{"app_id": "665…", "action": "restart"}
```

### `GET /ship/apps/{app_id}/logs?num=<n>`

Recent app log lines, newest last (`num` defaults to 100, max 1000). The engine
redacts them before they leave the box:

```json
{"lines": ["2026-07-22T…Z app[web.1]: GET /health 200"]}
```

### `DELETE /ship/apps/{app_id}`

**Parks** an app teardown for human approval, exactly like the box DELETE above.
Nothing is destroyed.

### `GET /ship/apps/{app_id}/metrics`

One app's live health: process state (from Dokku) plus **real per-container
CPU/memory** (from `docker stats` — Dokku's own `ps:report` gives only process
state, not resource usage). `cpu`/`mem`/`disk` are percentages or `null` when the
box could not report them (an old Docker, a down container) — render "—" for a
null, never a misleading 0. Process state always comes back.

```json
{"deployed": true, "running": true, "processes": 1,
 "cpu": 12.3, "mem": 5.6, "disk": 38.0}
```

### Environment variables (SHIP-9)

An app's env vars are stored **Fernet-encrypted at rest** (the same envelope as
the box SSH key) and are **never returned in plaintext** — every response masks
the value to a short hint. Values are decrypted only at deploy time, merged into
the engine's `config:set`, and redacted from every log line. `scope` is one of
`both` (default), `prod`, or `preview`; at deploy only the vars matching the
app's kind (its `prod` flag) plus every `both` var are applied.

#### `GET /ship/apps/{app_id}/env`

Lists the app's env vars, values masked:

```json
{"vars": [{"key": "API_KEY", "masked_value": "sk-…3f9", "scope": "both"}]}
```

#### `PUT /ship/apps/{app_id}/env`

Upserts a batch. Each key is added or overwritten; keys absent from the body are
left untouched. Keys use the POSIX env-name grammar; values are opaque (any
string up to 64 KiB). Returns the full masked list.

```json
{"vars": [{"key": "API_KEY", "value": "sk-live-…", "scope": "prod"}]}
```

#### `POST /ship/apps/{app_id}/env/import`

Bulk-imports a `.env` blob. Blank lines and `#` comments are ignored; each
remaining line is split on the first `=` with surrounding quotes stripped; a line
whose key is not a valid POSIX name is **skipped** (a paste never 422s on one
stray line). Returns the full masked list.

```json
{"dotenv": "API_KEY=sk-live-abc\n# comment\nDEBUG=false"}
```

#### `DELETE /ship/apps/{app_id}/env/{key}`

Removes one variable. Returns the remaining masked list.

### The agent surface (`pocketpaw_ship` MCP)

A chat agent in a room whose pocket has the **Ship connector** bound reaches the
same service layer through sixteen in-process MCP tools — `ship_list_boxes`,
`ship_provision_box`, `ship_list_apps`, `ship_create_app`, `ship_deploy_app`,
`ship_add_domain`, `ship_create_db`, `ship_set_scale`, `ship_set_checks`,
`ship_set_resources`, `ship_create_volume`, `ship_restart`, `ship_rebuild`,
`ship_logs`, `ship_metrics`, and `ship_request_destroy`. Binding the connector
also auto-surfaces the bundled `ship` skill into that room.
### The agent surface (`pocketpaw_ship` MCP)

A chat agent in a room whose pocket has the **Ship connector** bound reaches the
same service layer through ten in-process MCP tools — `ship_list_boxes`,
`ship_provision_box`, `ship_list_apps`, `ship_create_app`, `ship_deploy_app`,
`ship_add_domain`, `ship_create_db`, `ship_logs`, `ship_metrics`, and
`ship_request_destroy`. Binding the connector also auto-surfaces the bundled
`ship` skill into that room.

The agent's surface is deliberately **narrower than the HTTP one**:

| Verb | Operator over HTTP | Agent over MCP |
|------|--------------------|----------------|
| reads, provision, create app, domain, db | runs | runs |
| deploy to a non-prod app | runs | runs |
| deploy to a **prod-flagged** app | runs | **proposes** |
| destroy a box or an app | proposes | proposes |

An operator calling the API with their own credentials is a different actor from
an agent acting on their behalf, which is why the prod deploy splits. Both paths
converge on the same Instinct gate for teardowns: the tool returns
`{"status": "proposed", "proposal_id": "…"}` and the agent is instructed never to
report a destroy as done.
## Sites — Agent Editing Tools (in-process MCP)

Editing a Paw Site from chat does not go over HTTP. The chat agent reaches it
through the in-process MCP server `pocketpaw_sites_manager`
(`ee/pocketpaw_ee/agent/mcp_servers/sites.py`), whose tools are namespaced
`mcp__pocketpaw_sites_manager__<tool>`. Three editing tools live there, one per
hand-authored engine, and they are **not interchangeable** — each rejects the
other's pockets.

| Tool | Engine | Publishes? |
|------|--------|-----------|
| `edit_svelte_component` | `engine: "svelte"` | Builds a draft **preview** (workerd smoke gate; rolls the source back if it fails) |
| `edit_react_component` | `engine: "react"` | **No.** Persists the draft and stops — no build, no deploy |
| `edit_html_file` | `engine: "html"` | **No.** Persists the draft and stops — html runs no build, so there is nothing to gate a deploy on |

A ripple or dynamic site is edited through the pocket specialist's rippleSpec
merge instead. The leaf-edits REST route above is the *native editor's* html path
(uid splice) and is a different entry point from `edit_html_file`, which is the
chat agent's.

### `edit_react_component`

Write ONE file of a react site's `source` map as a reviewable draft.

| Arg | Type | Notes |
|-----|------|-------|
| `pocket_id` | string | Required. The react site pocket. |
| `component_path` | string | Required. Project-relative, e.g. `src/components/Hero.tsx`. Must already exist unless `create` is true. |
| `edits` | array | A list of `{old_string, new_string}` blocks applied to the file's current contents. Each `old_string` must match **exactly once**. Exactly one of `edits` / `new_source`. |
| `new_source` | string | The full new file contents (replaces the whole file). Required with `create`. |
| `create` | boolean | Default `false`. Create a NEW file at `component_path`; the path must **not** already exist. |

Returns `{ok: true, status: "draft", is_live: false, pocket_id, component_path,
created, message}`. To **add a section**, call it twice: once with `create: true`
for `src/components/<Name>.tsx`, then again with `edits` on `src/App.tsx` to
import and render it.

**It does not publish and does not enqueue a build**, and that is a deliberate
divergence from the svelte tool rather than an omission. `build_runs_async("react")`
is true: a react publish enqueues a Daytona build and returns before any build
outcome exists, so there is no synchronous result to gate on and nothing to roll
back from — a rollback fired on enqueue-success would revert a good edit.
Persisting the draft is the whole job (the same shape the leaf-edits route
documents). Publishing stays an explicit `publish` call the user asks for.

**Write scope is enforced, not advisory.** The generator owns the build shell, so
`index.html`, `package.json`, `vite.config.ts`, `paw-prerender.mjs` and everything
under `src/paw/` are rejected, and the resolved path must land under `src/` or
`public/`. Paths are normalized (backslashes, `.`/`..`) before the check, so
`./package.json` and `src/paw/../paw/entry.tsx` are rejected too. This is the same
policy `create_react_site` applies, shared through
`ee/pocketpaw_ee/sites/react_paths.py` — an edit that could write `package.json`
would be writing the dependency manifest, which is where the supply-chain
release-age floor is enforced.

Errors (relayed to the agent as `is_error` with the code, so it can fix and retry):

| Code | When |
|------|------|
| `site_edit.invalid_args` | Not exactly one of `edits` / `new_source`. |
| `site_edit.create_needs_source` | `create` without `new_source`. |
| `site_edit.reserved_path` | The resolved path is generator-owned. |
| `site_edit.path_outside_source` | The resolved path is outside `src/` and `public/`. |
| `site_edit.no_match` / `site_edit.ambiguous_match` | An `old_string` matched 0 or >1 times. Make it more specific and retry. |
| `pocket.not_react_site` | The pocket is not a react Paw Site. |
| `pocket.react_component_exists` | `create` on a path that already exists. |
| `site_component.not_found` | `create` is false and the path is not in the source map. |
| `plan.feature_denied` | The workspace's plan lacks the `sites` feature. |

Every write goes through `pockets_service.set_react_source_file`, which emits
`PocketUpdated` and records a draft `ArtifactVersion` snapshotting the full edited
source map — so an edit is a reviewable Branch draft a later publish promotes.

### `edit_html_file`

Write ONE file of an html site's `source` map as a reviewable draft.

| Arg | Type | Notes |
|-----|------|-------|
| `pocket_id` | string | Required. The html site pocket. |
| `file_path` | string | Required. Project-relative and usually at the site **root** — `index.html`, `styles.css`, `about.html`, `img/logo.svg`. Must already exist unless `create` is true. |
| `edits` | array | A list of `{old_string, new_string}` blocks applied to the file's current contents. Each `old_string` must match **exactly once**. Exactly one of `edits` / `new_source`. |
| `new_source` | string | The full new file contents (replaces the whole file). Required with `create`. |
| `create` | boolean | Default `false`. Create a NEW file at `file_path`; the path must **not** already exist. |

Returns `{ok: true, status: "draft", is_live: false, pocket_id, file_path,
created, message}`. To **add a page**, call it twice: once with `create: true` for
e.g. `about.html`, then again with `edits` on `index.html` to link to it.

**The argument is `file_path`, not `component_path`, and the difference is not
cosmetic.** svelte and react have a component model; html does not — the scaffold
writes the author's map verbatim into the directory the edge serves, so what
exists is files. Paths are **root-relative with no `src/` prefix**; passing react's
`src/components/Hero.tsx` shape here creates a file nothing serves.

**It does not publish**, for a different reason than react's. React defers because
its build is async and there is no synchronous outcome to roll back from. Html has
no build *at all* (`needs_node_build` is false), so there is no smoke render and
nothing that could reject a bad edit before it deployed — a republish here would
push unvalidated markup straight to a live customer site. Draft-only is the safer
contract, not merely the convenient one.

**Write scope**: only two rejections, because an html site's files legitimately
live at the project root and react's `src/`-or-`public/` rule would reject the
whole track. Paths are normalized (backslashes, `.`/`..`) before the check.

| Rejected | Why |
|----------|-----|
| the `_paw/` namespace | Generator-owned. `_paw/edit-manifest.json` maps each editable element to a byte range; shadowing it makes the next **native** editor edit splice at wrong offsets and land mid-tag — silently. |
| anything escaping the site directory | `..` and absolute paths. |

Errors (relayed to the agent as `is_error` with the code, so it can fix and retry):

| Code | When |
|------|------|
| `site_edit.invalid_args` | Not exactly one of `edits` / `new_source`. |
| `site_edit.create_needs_source` | `create` without `new_source`. |
| `site_edit.reserved_path` | The resolved path is in `_paw/`. |
| `site_edit.path_outside_source` | The resolved path escapes the site directory. |
| `site_edit.no_match` / `site_edit.ambiguous_match` | An `old_string` matched 0 or >1 times. Make it more specific and retry. |
| `pocket.not_html_site` | The pocket is not an html Paw Site. |
| `pocket.html_file_exists` | `create` on a path that already exists. |
| `site_component.not_found` | `create` is false and the path is not in the source map. |
| `plan.feature_denied` | The workspace's plan lacks the `sites` feature. |

Every write goes through `pockets_service.set_html_source_file`, which emits
`PocketUpdated` and records a draft `ArtifactVersion` — the same chokepoint
contract the react tool uses.

**Keep the form plumbing.** If a file contains a `<form>` posting to
`/capture/form`, its `action` and the hidden `paw_site_id` / `paw_key` /
`paw_redirect` inputs are what deliver leads. A rewrite that drops them leaves a
form that still looks right and captures nothing, with no visible change to the
page.

### Build state on the `publish` response

`publish` returns `{ok, message, site: {...}}`. The `site` object carries the five
original keys (`id`, `pocket_id`, `name`, `url`, `deployed`) plus the build lane's
state:

| Key | Notes |
|-----|-------|
| `build_status` | `none` \| `queued` \| `building` \| `built` \| `failed`. Passed through **verbatim** — an unrecognised value is never normalised. |
| `build_reason` | `"<rung>:<cause>"` explaining how the build settled. `null` until one does. A `failed` status without this is unactionable. |
| `build_job_id` | Handle for the queued build. Persisted, so it survives a reload. |
| `build_in_progress` | Derived. `true` while a build is running, **and for any unrecognised `build_status`**. |
| `is_live` | Derived. The only field to gate "show the user the url" on. |

`is_live` requires a non-empty `url` **and** `deployed` **and** no build in flight,
because each is individually insufficient. This matters on **react**, the only engine
where `build_runs_async(engine)` is true:

- On a **first** publish, `_enqueue_static_build` creates the Site doc with `url: ""`
  and `deployed: false`. That is honest — nothing is serving yet, and the worker flips
  both when the deploy succeeds — but it means `url` alone is an empty string.
- On a **re-publish**, `url` and `deployed` deliberately keep the *previous* deploy's
  values so a rebuild never reports a working site as down. Both say "live" while the
  url serves the pre-change page.
- `build_status` alone cannot tell a never-built pocket (`none`) from a finished one.

`build_in_progress` reads an unknown status as in-progress, which is the wire contract
and the deliberate **opposite** of `build_state.should_enqueue`, which treats an
unknown status as terminal. Both are correct on their own axis: a redundant build costs
one sandbox, while a spurious "your site is live" costs the user's trust.

The derivation lives in `sites.service.build_wire_state` and is shared with the status
tool below, so the two surfaces cannot disagree about whether a site is live.

### `get_site_build_status`

Read-only. Takes `pocket_id` and returns `{ok, message, pocket_id, site_id, name,
published, url, deployed, build_status, build_reason, build_job_id, build_in_progress,
is_live}`.

This exists because a react publish is **asynchronous**: the `publish` call returns
before the build starts, so its response can never report how the build ended. Without
a later read, `queued` is a dead end — the agent learns a build was enqueued and has no
way to discover it finished.

A pocket with no Site doc returns `published: false` rather than an error; from the
caller's side "this was never published" is the useful answer, and it is correct whether
the pocket has no site or does not exist. The read resolves the canonical Site doc
through `canonical_site_for_pocket`, which is tenant-scoped on the workspace — that
filter is the access check, and there is no plan gate because nothing is mutated.

## Fabric — Transform Mappings (source→Fabric ingest)

The transform surface over the per-workspace `FabricIngestConfig`: which
sources land as typed Fabric objects, and how. A mapping's `source_kind`
picks the pipeline:

- `"firestore"` (default) — the original reader path: mirror a Firestore
  collection, keyed on the doc path, with a real high-water cursor.
- `"connector"` — pull records through the OSS connector→Fabric ingestor
  registry (`pocketpaw.connectors.fabric_ingest.FABRIC_INGESTORS`;
  `gcalendar` is the first registered adopter). By convention `collection`
  holds the connector name (it stays the routing key everywhere);
  `connector_id` overrides it when they differ. The run resolves the
  workspace's **enabled** `WorkspaceConnector` row and calls the ingestor
  with that row's `user_id`, so a user-scoped connector reads with that
  member's OAuth token bucket (`null` = the shared/workspace bucket).

All routes are license + plan-feature `fabric` gated (business tier and up).
Reads require `fabric.read`, mutations (author, delete, run-now) require
`fabric.write`; the workspace is always the caller's active workspace — it
never travels in a request body.

### `GET /fabric/ingest/mappings`

Returns `{"mappings": [...]}` — the caller's workspace's authored mappings
(empty list when nothing is configured yet). Shown regardless of the config's
`enabled` flag so a paused pipeline is still visible.

### `POST /fabric/ingest/mappings`

Author one mapping — create-or-replace, keyed on `collection` (201). Body:

```json
{
  "collection": "gcalendar",
  "object_type_id": "ot-calendar-event",
  "source_kind": "connector",
  "connector_id": null,
  "field_map": {},
  "cursor_field": "",
  "link_rules": []
}
```

A malformed mapping (blank `collection` / `object_type_id`, blank field-map
entries) is rejected with 422 before anything is stored.

### `DELETE /fabric/ingest/mappings?collection=<key>`

Remove the mapping keyed on `collection` (204; 404 when it doesn't exist).
The key rides a query param, not a path segment — Firestore collection paths
can contain `/`.

### `POST /fabric/ingest/run`

Run one mapping's ingest immediately. Body: `{"collection": "<key>"}`.
Returns the ingest result envelope:

```json
{
  "workspace_id": "…", "source_id": "gcalendar", "status": "ok",
  "mode": "backfill", "objects": 3, "cursor": "", "errors": []
}
```

Misconfiguration — no mapping for the key, connector not connected or
disabled, no ingestor registered under the connector id — reports
`status: "error"` with the reason in `errors` (HTTP 200, matching the
background sweep's never-raise, per-source isolation contract). Re-runs are
idempotent: objects upsert by `(source_connector, source_id)`.

---

## Social Sign-In & Connected Accounts

Google and GitHub sign-in, plus the Settings surface where a signed-in user
connects and disconnects those identities. Seven endpoints in two groups, and
the groups differ in what authorises them — which is the thing to get right
before changing any of this.

### Cloud routes authenticate at the route level

**Every cloud route needs its own guard.** The global `AuthMiddleware` does not
gate `/api/v1/`: it builds `is_auth_optional` from
`auth_optional_prefixes = ("/api/v1/",)` and skips its final 401 for every
match, so that ee routes resolve identity through fastapi-users instead. The
cascade still runs and still populates `request.state` — session cookies, API
keys, `full_access` — so routes mounted at the shared prefix can read it; it
simply is not the thing that rejects.

So when you add a cloud route, a session dependency (or an explicit in-handler
check) is **required**, not belt-and-braces. `tests/cloud/auth/test_route_auth_audit.py`
asserts this across every mounted router and keeps an allowlist of the routes
that are public by design, each with its reason.

### Verifying an auth change locally proves nothing by default

`POCKETPAW_LOCALHOST_AUTH_BYPASS` **defaults to true** and grants
`request.state.full_access` to any caller whose address is loopback. On a dev
box you therefore cannot tell "this endpoint requires auth" from "this endpoint
let me in because I am on localhost" — a `curl` from your own machine succeeds
either way.

Set it to false before testing an auth change by hand:

```bash
export POCKETPAW_LOCALHOST_AUTH_BYPASS=false
```

Better, assert it in a test against the ASGI app with no session, the way
`tests/cloud/sessions/test_runtime_route_auth.py` does. The bypass refuses a
spoofed `X-Forwarded-For`, so a remote caller cannot claim loopback — the trap
here is local verification, not a production hole.

### Sign-in endpoints (no session — that is the point)

| Endpoint | Notes |
|---|---|
| `GET /auth/social/providers` | `{providers: [...]}` — only providers whose credentials are set. An unconfigured provider is **absent**, not present-and-broken. |
| `GET /auth/social/{provider}/login` | Begins consent, 302s to the provider. Takes `flow=web` or `flow=desktop`, and `next=<relative path>`. |
| `GET /auth/social/callback` | The provider's redirect. Redeems the code, applies the policy, then signs in **or** links. |
| `POST /auth/social/exchange` | `{xc}` traded for a bearer token. How a desktop client gets its FIRST token. Rate-limited per IP. |

Unauthenticated by necessity: the caller has no session yet. The control is the
single-use `state` from `auth/_oauth_state.py` — server-side, 32 bytes,
GET-then-DEL, 600s TTL, namespaced per flow so an SSO state cannot be spent on
the social callback. Server-side rather than a signed token deliberately: a
self-verifying state token verifies for *anyone* who presents it, which is
CVE-2025-68481 against fastapi-users.

Failures **redirect rather than return JSON**, because these are reached by a
full-page browser navigation and a JSON body would render as raw text in the
address bar. A refusal goes to `<frontend>/?auth=signin&auth_error=<code>` —
the dialog reopened with an explanation, because a refusal is a UI state and
not an error page.

The desktop branch redirects to `<frontend>/oauth-callback?xc=<code>` carrying
a **one-time reference, never a token**: 60-second TTL, single-use. A token in
a URL leaks through browser history, `Referer`, window titles and every proxy
log on the path; a spent reference is worthless.

`flow` and `next` are read from the state payload, never from the callback's
query string — a callback URL is attacker-influenced by definition. `next` is
re-validated server-side to a same-origin relative path (one leading slash, no
backslash), so `//evil.com` and absolute URLs degrade to `/`.

### Connected-accounts endpoints (session required)

| Endpoint | Notes |
|---|---|
| `GET /auth/social/identities` | `{identities: [{provider, account_email, linked_at}]}`. `linked_at` is null for rows linked before that field existed. |
| `POST /auth/social/{provider}/link` | Returns `{authorize_url}` — a URL, **not** a 302. Takes `flow=web` (default) or `flow=desktop`, and `next=<relative path>`. |
| `POST /auth/social/link/complete` | Desktop only. `{code}` → `{provider, identities}`. See below. |
| `DELETE /auth/social/identities/{provider}` | 204 on success. |

All three take `current_active_user`, and the acting account comes from that
dependency only. The provider name is the sole caller-chosen value, so no
request shape acts on somebody else's credentials. **A link endpoint that took
its target user from a body or path parameter would be an account-takeover
primitive, not a settings page** — do not add one.

`POST .../link` returns a URL rather than redirecting because Settings calls it
with `fetch`, which follows a 302 opaquely: the request would succeed against
the provider's HTML and the page would never move. The client assigns the URL
to `window.location`. These three return JSON errors in the shared `CloudError`
envelope, unlike the sign-in routes above, because they are XHR with a caller
waiting on a response.

The link flow reuses the sign-in callback. The two are told apart by a
`link_user_id` pinned into the state at authorize time, and **the callback
re-checks that id against the session cookie**. That check is load-bearing:
state is a bearer secret, so without it a stolen link state lets an attacker
complete the flow with their OWN provider account, attach it to the victim, and
sign in as them afterwards.

Web link outcomes redirect to `<frontend><next>?social_linked=<provider>` on
success and `?social_error=<code>` on refusal — never to the sign-in dialog,
which would prompt an already-signed-in user to sign in.

### Linking on desktop finishes somewhere else entirely

A desktop client is not "the web client in a window", and this is the one place
that difference is load-bearing. It authenticates with a bearer held in
localStorage, and the Tauri webview that completes consent carries **no cookie
for this origin**. So the callback cannot authenticate anyone at all — and
attaching on the strength of the state alone is exactly the theft the web
branch's cookie check exists to prevent.

The proof therefore moves to a request the app can actually authenticate. Pass
`flow=desktop` when starting the link, and the callback attaches nothing:

```
1. POST /auth/social/{provider}/link?flow=desktop     (Authorization: Bearer …)
   -> {"authorize_url": "https://github.com/login/oauth/authorize?…"}

2. app opens a webview at authorize_url; user consents

3. callback parks the identity and redirects the webview to:
      <frontend>/oauth-callback?link=<code>&provider=<provider>
   or on failure:
      <frontend>/oauth-callback?link_error=<code>

4. webview closes; app redeems the code:
   POST /auth/social/link/complete   {"code": "<code>"}   (Authorization: Bearer …)
   -> 200 {"provider": "github", "identities": [...]}
   -> 4xx CloudError envelope, same auth.* codes as everywhere else
```

`link=` is what distinguishes this from a desktop **sign-in**, which uses `xc=`
on the same `/oauth-callback` route. They are not interchangeable: one attaches
an identity, the other mints a bearer, and they live in separate single-use
namespaces so a code from one is refused by the other.

Step 4 is where authorisation happens. The parked record names the account the
link was started for, and `complete` compares it against `current_active_user`.
**A stolen link code is worth nothing without that account's bearer**, which
makes the desktop path stronger than the cookie check rather than a concession
to it. The code is single-use with a 60-second TTL.

The response carries the refreshed identity list because the window that
started the flow has already closed; a follow-up refetch that failed would
leave the panel stale with no way to explain itself.

Policy refusals (`auth.identity_claimed`, `auth.sso_enforced`,
`auth.unverified_link`) surface as JSON from step 4, which the panel renders
inline. Only a provider or network failure still refuses at the callback, and
it redirects to `/oauth-callback?link_error=…` so the webview closes rather
than sitting on a page it cannot use.

The desktop redirect ignores `next` — the webview's job is to close, and the
Settings panel that opened it is still mounted in the main window. Not building
a `next`-derived URL there also means the hostile-value problem cannot reach
that redirect at all.

An unknown `flow` is **refused** (`social.unknown_flow`, 422) rather than
defaulted to `web`. A desktop client that silently got the web branch would
consent successfully and then attach nothing, which reads as a frontend bug for
as long as it takes someone to find this paragraph.

### Refusal codes

The frontend maps each of these to its own copy in
`core/auth/social-errors.ts`, so renaming one silently degrades a specific
message to a generic fallback.

| Code | Means | Path |
|---|---|---|
| `auth.unverified_link` | The provider would not vouch for any email address. | Both |
| `auth.sso_enforced` | The user's workspace mandates SSO. | Both |
| `auth.identity_claimed` | That identity is already attached to a **different** account. | Link |
| `auth.link_session_mismatch` | The callback's session is not the account that started the link. | Link |
| `auth.last_credential` | Unlinking would leave the account with no way to sign in. 409. | Unlink |
| `auth.not_linked` | No identity from that provider is attached. 404. | Unlink |
| `social.invalid_state` | State unknown, already spent, expired, or from another flow. | Both |
| `social.provider_not_configured` | No credentials for that provider on this server. 503. | Both |
| `social.unknown_provider` | Not `google` or `github`. 422. | Both |
| `social.unknown_flow` | `flow` was neither `web` nor `desktop`. 422. | Both |
| `social.invalid_link_code` | A parked desktop link record could not be rebuilt. | Link |

### The security model — read this before adding provider #3

**On sign-in, a provider-verified email is the only join key.** The policy, in
order:

1. This `(provider, account_id)` is already linked → sign in.
2. No verified email from the provider → **REFUSE**.
3. Verified email matches an existing account → link, then sign in.
4. Verified email, no existing account → create, link, sign in.

Step 2 before step 3 is the whole defence. Matching an **unverified** address
against an existing account is how an attacker attaches `victim@corp.com` to
their own provider profile and walks into the victim's account. Not
hypothetical — this is nOAuth (Entra's mutable, unverified `email` claim) and
GHSA-6g38-8j4p-j3pr. So the rule every adapter must hold to: **compute
`email_verified` from the provider's authoritative source, and never infer it
from the mere presence of an address.** GitHub's `/user` payload carries an
`email` field that is *not* proof of verification; the flag comes from
`GET /user/emails`, which is why the `user:email` scope is required.

Where a provider gives no verified address the adapter reports `email=None`
rather than guessing, and the service turns that into a refusal, not a link.

Step 1 sitting *before* step 2 is also deliberate. A returning user whose
provider has since stopped vouching for their address — they removed it, or
declined the scope on a re-consent — is still the same person, because that
match was on the provider's immutable id. Verification only gates the step that
BINDS an identity to an account it was not already bound to.

**On linking, email is deliberately NOT a join key at all.** The session
already establishes who the user is, so the identity's address is not needed to
resolve an account and must not be used to. The link path looks up only
`(provider, account_id)`:

- already attached to the caller → no-op, because clicking "Connect" twice is
  not an error and reporting one would put the panel in a failure state over a
  state it already has;
- attached to a **different** account → refuse `auth.identity_claimed`. Never
  re-point it. That would hand over this account *and* silently strip a
  credential from the account that legitimately holds it;
- attached to nobody → attach.

Unverified identities are still refused on the link path, but for a different
reason than on sign-in: it preserves the invariant that **every row in
`oauth_accounts` was established from a provider-verified identity**, which is
exactly what makes step 1 above safe when it signs a returning user in on a
link alone. Break the invariant here and step 1 loses its foundation.

**Unlinking refuses to remove the last credential**, and the check is
`_has_usable_password`, not `bool(hashed_password)`. Accounts created by the
social path and by SSO JIT provisioning store an *unusable sentinel*
(`!social-only-...`, `!sso-only-...`) rather than an empty string, because an
empty hash can compare-equal in some verifiers. A truthiness check therefore
reports "has a password" for precisely the users who have none, and would let
them delete their only way in. The test is positive — pwdlib and passlib hashes
are Modular Crypt Format and begin with `$` — so a sentinel added later needs
no change here. It over-refuses an SSO member who could still reach their IdP;
that is the intended direction, because a false refusal costs one password
reset and a false allow is a permanent lockout support cannot undo.

**Enforced SSO refuses both sign-in and linking.** A workspace paying for SSO
is buying the guarantee that its members authenticate through the IdP, and
consumer Google must not become the documented way around it. Linking is
guarded even though a link is not itself a bypass — sign-in re-checks every
time, so an identity attached under enforced SSO could not be spent — because
it would be a bypass lying in wait if that check ever regressed, and it stores
exactly the credential the org enabled the control to exclude. The check also
runs at `begin_link`, so Settings shows an explainable error instead of a round
trip through Google that ends in a redirect.

Provider access tokens are never stored. Sign-in needs identity, not ongoing
API access, and a token we never use is avoidable breach surface. Repository
access is codeconnect's job.

### Configuration

| Variable | Purpose |
|---|---|
| `POCKETPAW_GOOGLE_OAUTH_CLIENT_ID` / `_SECRET` | Google sign-in. Unset hides the button. |
| `POCKETPAW_GITHUB_OAUTH_CLIENT_ID` / `_SECRET` | GitHub sign-in. Unset hides the button. |
| `POCKETPAW_PUBLIC_BASE_URL` | Backend origin; the callback URL is derived from it. Default `http://localhost:8888`. |
| `POCKETPAW_SOCIAL_REDIRECT_URI` | Overrides the derived callback outright. Set it when the backend sits behind a proxy whose public origin it cannot infer. |
| `POCKETPAW_FRONTEND_BASE_URL` | Where the SPA lives. Default `http://localhost:1420`. |

Callback URL, registered with both providers:

```
<backend-origin>/api/v1/auth/social/callback
```

**GitHub needs an OAuth App, not a GitHub App.** They are different products
with different consent screens and different token models, and picking the
wrong one costs an hour before anything works. Create it under Settings →
Developer settings → **OAuth Apps**. These credentials are also distinct from
two other Google/GitHub credentials already in this codebase, and reusing
either will not work:

- `POCKETPAW_GITHUB_APP_*` — codeconnect's **GitHub App**, for repository
  access via installation tokens. Sign-in uses its own OAuth App so the
  account-creation consent screen asks for identity only, never repository
  permissions.
- `GOOGLE_OAUTH_CLIENT_ID` / `_SECRET` (no `POCKETPAW_` prefix, OSS core) — the
  Drive connector's per-install data integration.

Scopes are requested at runtime and are identity-only: Google gets
`openid email profile`, GitHub gets `read:user user:email`. `user:email` is not
optional — without it `GET /user/emails` returns 403, no address can be treated
as verified, and every GitHub sign-in refuses with `auth.unverified_link`.

`POCKETPAW_FRONTEND_BASE_URL` matters more than it looks. Every redirect out of
these routes is **absolute** against that origin, because a relative redirect
resolves against the API origin — the same host only when both are served from
one domain. In production they usually are; in local dev they are not, and a
successfully signed-in user lands on the API root and sees nothing.
## Paw Bar — the site concierge and its owner inbox

Every published Paw Site can carry a concierge: a per-site agent that answers
visitors on the page. These are its endpoints. They split cleanly in two, and
the split is the security model:

- **Public** routes are called by the widget on the customer's site. The caller
  is an anonymous visitor holding a world-visible embed key, so every one of
  them runs the same fail-closed chain — unknown widget 404, rate limit 429,
  bad/revoked key 401, disallowed origin or a key that doesn't own the widget
  403 — and none of them expose owner-private data.
- **Admin** routes are called by the site's owner from the dashboard. They are
  workspace-scoped and gated on `paw_bar.read` (reads) or `paw_bar.manage`
  (writes).

### Public — the visitor surface

| Route | What it does |
|---|---|
| `GET /paw-bar/widget.js` | The embed loader a published page includes. |
| `GET /paw-bar/frame` | The concierge iframe document. Gated by a CSP `frame-ancestors` header built from the Site's `allowed_origins`; a disabled concierge returns a blank self-removing shell rather than an error page, because this body renders inside a visible iframe. |
| `GET /paw-bar/spec/{widget_id}` | The widget's render spec. |
| `POST /paw-bar/events/{widget_id}` | Ingest a widget event. |
| `GET /paw-bar/events/{widget_id}/decision/{customer_ref}` | Poll the outcome of a gated action the visitor requested. |
| `POST /paw-bar/chat` | Stream a concierge reply (SSE). When the owner has taken the conversation over this emits a single `human_replying` frame and dispatches no agent run at all. Takes an optional `conversation_id`; omit it and the turn lands on the visitor's conversation in progress, which is what widget bundles built before that field send. |
| `GET /paw-bar/conversations` | The visitor's own conversations on this bar, newest first, with a preview and which one is in progress. Scoped to the `customer_ref` the embed key already bound, so there is nothing to enumerate. |
| `POST /paw-bar/conversations` | Start a fresh conversation. The current one is retired rather than deleted — it stays in the visitor's list and in the owner's inbox — and the next turn starts the agent cold instead of replaying the thread the visitor walked away from. |
| `POST /paw-bar/action` | Run a verb the widget spec declares. `auto` verbs touch only the visitor's own cart or a checkout link; `gated` verbs execute nothing and raise an Instinct proposal for a human. |
| `GET /paw-bar/cart` | The visitor's own cart. |
| `POST /paw-bar/decision-contact` | Leave an email so a decision reaches the visitor after they close the page. The address is stored on the decision row only — never in agent context, the KB, or transcripts. |
| `GET /paw-bar/messages/{widget_id}/{customer_ref}` | Poll for owner and system messages once a human has joined. Returns `role`, `content`, `at` and `bot_paused` — never notes, tags, assignee or contact address. |
| `GET /paw-bar/articles` | The site's own synced pages, for a self-serve reading list. |

### Admin — the owner surface

| Route | What it does |
|---|---|
| `GET /paw-bar/admin/site/{site_id}/overview` | Counts and the bound widget. |
| `GET/PATCH /paw-bar/admin/site/{site_id}/settings` | The kill switch, greeting, transcript-retention toggle, and `concierge_appearance` — the white-label block (accent, surface mode, radius, blur, font, launcher, hero, motion preset, agent identity) that renders into the widget's `--pawbar-*` custom properties. Sent whole rather than per-field; every value validates into a safe CSS literal, since these become the right-hand side of a custom property in a document the widget serves. |
| `GET /paw-bar/admin/site/{site_id}/conversations` | The inbox. Supports `?state=open\|needs_human\|snoozed\|closed`, carries per-state `counts`, and each row joins its lifecycle state, unread count, tags and whether an action is pending. |
| `GET /paw-bar/admin/site/{site_id}/conversations/{customer_ref}` | One conversation's transcript, interleaving visitor, assistant, owner and system turns by timestamp. |
| `PATCH /paw-bar/admin/site/{site_id}/conversations/{customer_ref}` | Move state, snooze, tag, or append a private note. |
| `POST /paw-bar/admin/site/{site_id}/conversations/{customer_ref}/reply` | Reply as the owner. Persists the turn, mutes the bot, clears unread, and reopens a closed or snoozed conversation. |
| `GET /paw-bar/admin/agent/{agent_id}/conversations` | The same inbox scoped to an agent rather than a site — the union across every site that agent serves. |
| `GET /paw-bar/admin/site/{site_id}/decisions` | Gated actions awaiting a human. |
| `GET /paw-bar/admin/site/{site_id}/handoffs` | Conversations a visitor asked to escalate. |
| `GET/POST /paw-bar/admin/site/{site_id}/knowledge` | What the concierge can answer from, and a resync. |
| `GET /paw-bar/admin/site/{site_id}/preview-frame` | An owner-authed preview of the live bar. |

Owner replies are stored in their own table rather than as chat runs, because
the metering sweeper bills every terminal run and would otherwise charge the
owner credits for typing their own sentence.

---

## Growth — Prospects

First slice of the `/growth` outbound engine (G-1): a workspace-scoped
prospect store. All routes are license-gated and carry the canonical
`request_context`; every read is workspace-scoped inside the service, so a
prospect id from another workspace returns an identical 404 (existence never
leaks). The company website `domain` is the dedupe key — normalised to a bare
lowercase hostname (scheme, `www.`, path, and port stripped) — unique per
workspace. G-2 adds the bulk-ingestion route below; later slices add drafts
and Instinct-gated sends on the dedicated `growth` arq queue
(`pocketpaw_ee.cloud.growth.worker.WorkerSettings`).

**RBAC (G-4).** Every `/growth` route carries a workspace-role guard on top of
the license gate. Reads (`GET /growth/prospects`, `GET /growth/drafts`, …)
require `growth.read` (MEMBER); authoring writes — create/update a prospect,
bulk ingest, create a draft, non-gated lifecycle moves — require `growth.write`
(MEMBER); and the outbound verbs — `POST /growth/drafts/{id}/propose` and
`POST /growth/drafts/propose-batch` — require `growth.manage` (ADMIN). The propose route sits at the ADMIN tier deliberately:
`growth.executor` re-checks that same action against the proposer's *current*
role at dispatch time, so a member-filed proposal would always fail closed at
approve. A caller below the required tier gets
`403 workspace.insufficient_role`.

### `POST /api/v1/growth/prospects`

Create a prospect. Body:

```json
{
  "name": "Sam Founder",
  "company": "Acme Dental",
  "domain": "acme-dental.com",
  "source": "manual",
  "tier": "unqualified",
  "research_brief": "",
  "emails": [],
  "linkedin_url": null,
  "whatsapp_number": null,
  "opted_in": false,
  "status": "new"
}
```

Only `domain` and `source` are required. `name` and `company` default to
`""` — **not yet known**, which is the honest shape an import arrives in: a
pasted list of bare domains, enriched by research later on the same
`(workspace, domain)` identity. Nothing renders an empty value as the word
"unknown". The agent surface is stricter: `growth_upsert_prospect` refuses to
CREATE a row without a name and a company.

`project_id` (optional) assigns the prospect to a client project
(`cloud/projects`). It is validated against the caller's workspace — another
tenant's project is `404 project.not_found`. Nullable throughout: a workspace
not using projects is unaffected.

Enums: `source` is
`clay | directory | manual`; `tier` is `a | b | c | unqualified` (default
`unqualified`); `status` is
`new | qualified | drafted | in_sequence | replied | dead` (default `new`).
A duplicate `(workspace, domain)` returns `409 prospect.domain_taken` —
create-or-update callers use the service's `upsert_by_domain` seam instead.

Returns the prospect envelope: all fields above plus `id`, `workspace_id`,
and ISO `created_at` / `updated_at`.

### `POST /api/v1/growth/prospects/bulk`

Batch create-or-update — the ingestion endpoint for Clay exports and
partner-directory scrapes (G-2). Body:

```json
{
  "rows": [
    {
      "name": "Sam Founder",
      "company": "Acme Dental",
      "domain": "acme-dental.com",
      "source": "clay",
      "emails": ["sam@acme-dental.com"]
    }
  ]
}
```

Each row is `CreateProspectRequest`-shaped (same fields, defaults, and enums
as the single-create route above). Max **500 rows** — an oversized payload is
a 422 before any row is processed. Rows are processed individually through
the upsert-by-domain seam:

- a **new** `(workspace, domain)` inserts → counted in `created`;
- an **existing** one updates in place (all fields overwritten except
  `source`, which keeps first-capture provenance) → counted in `updated`;
- an **invalid** row (bad enum, missing field, empty domain) is skipped and
  recorded — the rest of the batch proceeds. No all-or-nothing abort;
  upserts are idempotent, so re-posting the same payload is safe and reports
  every row as updated.
## Knowledge — Living Wiki API

The workspace knowledge browser (`/api/v1/knowledge/*`) is the read/reingest
surface the living-wiki frontend renders. It aggregates the workspace kb-go
scope (`workspace:{wid}`) with every agent scope in the workspace
(`agent:{aid}`). All routes require a valid license plus `kb.read`
(`kb.write` for the reingest POSTs) on the active workspace; routes that
accept a `scope` bind it to the caller through the same allowlist the `/kb`
router uses (own workspace + visible pockets + workspace agents + the
caller's own `user:` scope) and answer `403 kb.scope_forbidden` otherwise.
Errors use the standard envelope `{"error": {"code", "message"}}`.

### `GET /knowledge/articles`

Query params: `workspace_id` (optional, must match the active workspace),
`agent_id` (optional filter; `"workspace"` = workspace-only).

Response:

```json
{
  "created": 18,
  "updated": 1,
  "errors": [
    {"index": 4, "code": "prospect.invalid_row", "message": "source: Input should be 'clay', 'directory' or 'manual'"}
  ]
}
```

`index` is the row's position in the submitted `rows` array. Rows land only
in the caller's workspace — the same domains ingested by another workspace
create independent rows.

### `GET /api/v1/growth/prospects`

One page of the workspace's prospects. **The response is an envelope, not a
bare array** — it changed shape in G-10a:

```json
{
  "items": [ { ...prospect envelope }, ... ],
  "next_cursor": "newest:2026-07-28T09:14:02+00:00|66a1...f3",
  "total": 3182
}
```

`total` counts every row matching the current filters (not the page), so the
UI can say "showing 40 of 3,182". `next_cursor` is `null` on the last page.

Query parameters:

| Param | Default | Notes |
|---|---|---|
| `tier`, `status`, `source` | — | Validated against the enums above; an unknown value is a 422, not an empty list. |
| `project_id` | — | Scope to one client's pipeline. Omitted means every project (the whole view for a workspace not using them); an empty string means the rows with no client assigned. |
| `q` | — | Case-insensitive substring search across `name`, `company`, `domain` and `research_brief`. Regex metacharacters are escaped, so `.*` matches nothing rather than everything. Max 200 chars. |
| `sort` | `newest` | `newest` \| `oldest` \| `company` \| `tier`. |
| `cursor` | — | The previous page's `next_cursor`, passed back unchanged. |
| `limit` | 100 | Max 500. |

**Tier sort order is the declared rank `a → b → c → unqualified`**, not a
lexicographic comparison. Today's tier names happen to sort the same way
lexicographically; that is an accident, and renaming a tier would break it
silently. The rank lives in `growth/domain.py` as `TIER_SORT_ORDER` and the
query walks those buckets in order.

**Pagination is keyset**, so a page never skips or repeats a row when the
collection is written to mid-scroll. The cursor is opaque — do not parse or
construct it — and carries the sort mode it was issued under: reusing a
cursor after changing `sort` is a `422 prospect.bad_cursor` rather than a
silently wrong page. Any malformed cursor is the same 422.

**Search scale ceiling.** `q` is an unanchored regex `$or` across four
fields, which Mongo cannot serve from an index — it is a collection scan
bounded by the workspace filter. Fine at the scale this surface targets (tens
of thousands of rows per workspace); past ~100k it needs a real text index or
an external search index. No text index was added here: `models/prospect.py`
carries a unique `(workspace, domain)` index plus a `(workspace, createdAt)`
list cursor, and a Mongo text index is a per-collection singleton that has to
be designed against those rather than bolted on.

### `GET /api/v1/growth/prospects/facets`

Counts behind the filter chips. Takes the same `tier` / `status` / `source` /
`project_id` / `q` filters as the list route and returns:

```json
{
  "tier":   { "a": 12, "b": 40, "c": 8, "unqualified": 300 },
  "status": { "new": 210, "qualified": 90, "drafted": 40, "in_sequence": 12, "replied": 6, "dead": 2 },
  "source": { "clay": 180, "directory": 140, "manual": 40 }
}
```

Each block respects every active filter **except its own**. With
`status=new` on, the tier counts describe the new rows rather than
collapsing to whichever tier is selected — otherwise the selected chip reads
`n` and every sibling reads `0`, which tells the user nothing about where to
go next. `q` constrains all three blocks (it is not a facet of its own), and so does
`project_id` — it is not a chip the user toggles inside the list, it is
*which client's list* they are looking at, so the other three counts have to
be scoped to it.

Every legal value appears, zeros included, so the chip row keeps a stable
shape as the user filters. Served by one workspace-scoped `$facet`
aggregation — three separate queries would be three chances for the counts to
disagree with each other.

### `GET /api/v1/growth/prospects/{prospect_id}`

Fetch one prospect. Cross-tenant or unknown ids: `404 prospect.not_found`.

### `PATCH /api/v1/growth/prospects/{prospect_id}`

Partial update — send only the fields to change. `domain` (the dedupe
identity) and `source` (capture-time provenance) are immutable; the other
fields (`name`, `company`, `tier`, `research_brief`, `emails`,
`linkedin_url`, `whatsapp_number`, `opted_in`, `status`) patch in place.
Returns the updated envelope.

`project_id` is three-valued here: omitting it leaves the assignment alone,
an id reassigns the prospect to that client (validated against the workspace
— a foreign project is `404 project.not_found`), and `""` clears it.
Un-assigning is deliberately explicit: a bulk upsert only ever *sets* the
project, so an enrichment pass that carries no project can never orphan a
client's prospect.

## Growth — Drafts

Third slice of the `/growth` outbound engine (G-3): per-channel outreach
drafts attached to a prospect, with an enforced status lifecycle. Same gates
as prospects — license + `request_context`, every read workspace-scoped
(cross-tenant ids 404). The lifecycle is the object the send-gate slice
(G-4) proposes and dispatches on top of:

```
draft → proposed → approved → sent → replied
  └────────┴──────────┴─────────┴──→ rejected   (any non-terminal)
```

`replied` and `rejected` are terminal. Any other move — skipping ahead,
going backwards, leaving a terminal state — is a
`422 draft.illegal_transition`. Transitions are mechanism-only: no side
effects, no sending.

**G-4 — the Instinct send gate owns the `approved` and `sent` edges.** The
public status route refuses those targets with `403 draft.gate_required`
even though they are legal per the table: `approved` is only ever set after
a human approves the draft's `_growth_send` Instinct proposal (which also
enqueues the `growth.dispatch` arq job on the dedicated `growth` queue),
and `sent` only by the dispatch worker (G-5/G-6 — a logging stub in G-4).
Structural, like /ship's destroy gate: nothing sends without an approval.

### `POST /api/v1/growth/prospects/{prospect_id}/drafts`

Attach one channel's copy to a prospect. Body:

```json
{
  "channel": "email",
  "subject": "Quick idea for Acme Dental's booking flow",
  "body": "Saw your online booking stops at a contact form — here's a live demo.",
  "variant": "first_touch",
  "demo_url": null
}
```

`channel` (`email | linkedin | whatsapp`) and `body` (non-empty, max 10 000
chars) are required. `subject` is **email-only** — sending it on another
channel is a 422. `variant` is `first_touch | follow_up` (default
`first_touch`). Drafts are always born in `status: "draft"` — there is no
status field here; lifecycle moves go through the transition route.

The prospect must exist in the caller's workspace (`404 prospect.not_found`
otherwise). A prospect still in `new` / `qualified` flips to `drafted` on
its first draft; later prospect statuses are never regressed.

Returns the draft envelope: the fields above plus `id`, `workspace_id`,
`prospect_id`, `status`, and ISO `created_at` / `updated_at`.

### `GET /api/v1/growth/drafts`

List the workspace's drafts, newest first. Optional query filters:
`prospect_id`, `channel`, `status` (enum-validated — an unknown value is a
422) and `limit` (default 100, max 500).

### `PATCH /api/v1/growth/drafts/{draft_id}`

Edit a draft's copy. Body: any subset of `subject`, `body`, `demo_url`
(an empty body object is a 422 — there is nothing to change). No `status`
field exists on this request: a lifecycle move dressed as an edit would be a
second, unreviewed road to `approved`.

**Only while the draft is still `draft`.** From `proposed` on, the stored body
is what a human is reading in the Tray and what the dispatch worker puts on
the wire, so an edit there would send copy nobody approved — refused with
`403 draft.not_editable`. Revise by rejecting the draft and writing a new one.
`subject` stays email-only (`422 draft.subject_not_allowed` on a linkedin /
whatsapp draft). Cross-tenant or unknown ids: `404 draft.not_found`. Requires
`growth.write` (MEMBER) — editing copy is authoring, not an outbound verb.

### `POST /api/v1/growth/drafts/{draft_id}/status`

Move a draft along the lifecycle. Body: `{"status": "proposed"}` (any
`DraftStatus`). Legal moves per the machine above; anything else is a
`422 draft.illegal_transition` and the draft is unchanged. Cross-tenant or
unknown ids: `404 draft.not_found`. Returns the updated envelope.

The gate-owned targets `approved` and `sent` are refused here with
`403 draft.gate_required` (see the G-4 note above) — approval happens only
in the Instinct Tray, and only the approved dispatch path may send.

### `POST /api/v1/growth/drafts/{draft_id}/propose`

File a gated `_growth_send` Instinct proposal for a draft (G-4). Requires
`growth.manage` (ADMIN). No body.

This route is the **only** way a `_growth_send` proposal comes into existence:
the generic `POST /instinct/actions` (open to any member holding
`instinct.propose`) refuses reserved gated parameter keys with
`422 instinct.reserved_parameter_key`, so nobody can hand-craft a Tray card
that dispatches a send on approval. Approving with edits cannot re-point one
either — the blob's tenancy, proposer, target draft and channel are pinned back
from the stored proposal.

The draft must be able to legally move to `proposed`
(`422 draft.illegal_transition` otherwise — so re-proposing an already
proposed draft is refused and no duplicate proposal is filed); cross-tenant
or unknown ids `404 draft.not_found`.

Flips the draft to `proposed` and files an Instinct `Action` whose
`_growth_send` blob carries the draft/prospect ids, the channel, the
prospect's name + company, and the **rendered preview** (subject + body) —
the human approves the exact copy that was staged. Returns:

```json
{ "proposal_id": "<instinct action id>", "draft": { ...draft envelope, "status": "proposed" } }
```

NOTHING is sent by this route. On **approve** (single or bulk, in the
Instinct Tray) the growth executor flips the draft to `approved` and
enqueues the `growth.dispatch` job `{draft_id, channel}` on the dedicated
`growth` arq queue — with an execute-time re-check that the proposer STILL
holds `growth.manage` (a since-demoted proposer's approved send fails
closed), and `mark_failed` on the Action if the enqueue fails. On
**reject** the draft flips to `rejected` and nothing is enqueued. The
`email` branch is live (below) and the `whatsapp` branch is live (*Growth —
WhatsApp dispatch*); `linkedin` keeps the logging stub on purpose — it is
sent by hand from the LinkedIn queue.

### `POST /api/v1/growth/drafts/propose-batch`

Propose a selection of drafts in one call. Requires `growth.manage` (ADMIN)
— the same tier as the single propose, so batching is not a cheaper route to
the outbound verb.

```json
{ "draft_ids": ["66a1...f3", "66a1...f4", "66a1...f5"] }
```

Max 100 ids; an oversized payload is a `422` at the boundary, before a single
proposal is filed. The cap is 100 rather than bulk ingest's 500 because each
id costs a proposal a human then has to triage in the Tray.

Each id goes through the **same** `propose_send` path as the single-draft
route: one gated `_growth_send` Instinct proposal per draft, each approved or
rejected individually. There is no batch proposal object, no batch approval,
and no shortcut into the gate — a "batch" here is a UI convenience over N
gated proposals. Nothing is sent by this route.

Partial success, like bulk ingest — a draft that cannot be proposed (missing,
cross-tenant, already proposed, terminal) records an indexed error entry and
the remaining ids still go:

```json
{
  "proposed": 2,
  "failed": [
    { "index": 1, "draft_id": "not-an-object-id", "code": "draft.not_found", "message": "..." },
    { "index": 2, "draft_id": "66a1...f5", "code": "draft.illegal_transition", "message": "..." }
  ]
}
```

`index` is the id's position in the submitted `draft_ids` array. Nothing is
rolled back on partial failure — the proposals already filed are legitimate
and a human can reject them in the Tray.

### Dispatch — how an approved email actually sends (G-5)

The `growth.dispatch` job's `email` branch is live. It is not an HTTP route —
there is no "send this now" endpoint, by design — but its behaviour is part of
the contract the propose/approve routes above promise.

1. **Load and re-check.** The job re-reads the draft and refuses anything that
   is not `approved`. A job whose draft was rejected while queued, or a
   redelivered job for a draft already `sent`, logs a warning and makes **no**
   provider call. This is the dispatcher's half of the send gate.
2. **Send.** Delivery goes through the workspace's **Mailtrap** connector
   (`connectors/mailtrap.yaml`) over the Email Sending API. The token is a
   per-workspace credential held in that workspace's connector row and read
   through the connector state store — never an inlined process credential,
   and never logged, returned, or put on a DTO. Disabling the connector
   revokes sending immediately (the state store only resolves enabled rows).
   The connector declares **no actions**, so no agent or connector-execute
   call can reach a send: the approved-draft path is the only one.
3. **Record, then flip.** A `MessageLog` row is written first (the audit row
   proves a message physically left even if the following write fails), then
   the draft moves `approved → sent` through the same gate seam the executor
   uses. No second status path is introduced.

**Failure is retryable, not fatal.** A provider rejection, a transport error,
an unconfigured connector, a prospect with no email address, or a
subject-less draft all produce `MessageLog(outcome="failed", error=...)` and
leave the draft `approved` — the human approval still stands, only delivery
failed, so a re-run needs no second approval. Nothing raises out of the job:
the growth worker runs `max_tries=1` precisely so outbound work is never
auto-retried into a double-send, and the `MessageLog` row is the durable
failure record.

**`MessageLog`** (collection `growth_message_logs`, one row per delivery
**attempt**): `workspace`, `draft_id`, `prospect_id`, `channel`, `provider`
(`"mailtrap"`), `provider_message_id`, `to_address`, `sent_at`, `outcome`
(`sent | failed`), `error`. Written only by the growth service.

**Config — `GROWTH_SENDING_DOMAIN` (required to send).** The secondary
sending domain outreach rides. Unset means nothing goes out; the dispatcher
fails closed rather than guessing. The from-address (default
`outreach@<GROWTH_SENDING_DOMAIN>`, overridable per workspace via the
connector's `MAILTRAP_FROM_EMAIL`) is validated against it at send time, and
the value may not equal the deployment's own host (`POCKETPAW_PUBLIC_BASE_URL`).
Cold outreach draws spam complaints at rates transactional mail never sees,
and every complaint lands on the sending domain's reputation — a burnt
secondary domain costs a DNS record and a warm-up, while a burnt apex takes
password resets, invoices, and receipts down with it.

## Growth — LinkedIn Queue

The manual send surface for LinkedIn outreach (G-8). **Deliberately manual**:
there is no LinkedIn API integration and no automation — the captain
copy-pastes each note by hand (account-ban avoidance is the feature). Same
gates as the rest of `/growth` — license + `request_context`, every read
workspace-scoped.

### `GET /api/v1/growth/linkedin/queue`

The workspace's linkedin-channel drafts in `proposed` / `approved`, newest
first, each joined with its prospect's targeting context. Query:
`limit` (default 100, max 500) and `format` (`json` default, `md`).

JSON items:

```json
{
  "draft": { "id": "…", "body": "…", "variant": "first_touch", "status": "approved", "…": "…" },
  "prospect_name": "Sam Founder",
  "prospect_company": "Acme Dental",
  "linkedin_url": "https://linkedin.com/in/sam-founder",
  "research_brief": "Books via a contact form; no online scheduling.",
  "tier": "a"
}
```

`?format=md` returns `text/markdown` instead — a paste-ready export, one
section per prospect (no tables, no HTML): name + company heading, the
profile URL as a link, tier + the brief's first line, the connect note
(`first_touch` body, with a char count against LinkedIn's 300-char connect
limit), the after-accept message (`follow_up` body, when queued), and each
draft's id for the mark-sent call.

### `POST /api/v1/growth/linkedin/{draft_id}/mark-sent`

Record that a queued LinkedIn draft was manually sent. The draft must be
linkedin-channel (`422 draft.wrong_channel` otherwise) and `approved` — the
move rides the G-3 machine, so anything but approved→sent is a
`422 draft.illegal_transition`. Cross-tenant or unknown ids:
`404 draft.not_found`. Returns the updated draft envelope (`status: "sent"`);
the draft leaves the queue and continues the normal lifecycle
(sent→replied / rejected).

Requires `growth.manage` (ADMIN) — it is an OUTBOUND verb, the same tier as
propose. Because G-4 made `sent` a gate-owned target, this route walks the
gate seam rather than the public status route; the structural guarantee is
unchanged (only an `approved` draft can move, and `approved` is reachable
only through an approved `_growth_send` proposal). The queue read requires
`growth.read` (MEMBER).

## Growth — Follow-ups

Final slice of the `/growth` v1 outbound engine (G-7): the loop that closes
the cycle. A draft that went out and got no reply produces a **second draft**
— a short nudge — which is filed straight back into the Instinct Tray through
the same `_growth_send` gate. There is **no new API surface** here and no new
authority: the sweep's terminal state is a `proposed` draft plus a pending
Action a human decides on, exactly like a first touch typed by hand. Nothing
auto-approves and nothing auto-sends.

**Where it runs.** `growth.followup_sweep`, a daily arq **cron** at 13:00 UTC
on the dedicated `growth` queue
(`pocketpaw_ee.cloud.growth.worker.WorkerSettings.cron_jobs`, `unique=True`
so a horizontally-scaled worker fleet runs one tick, not N). Deploy it with
the same process that already serves `growth.dispatch`:

```bash
arq pocketpaw_ee.cloud.growth.worker.WorkerSettings
```

**What it does**, per (workspace, prospect, channel) thread:

1. Finds the thread's most recent `sent` draft and checks it is older than
   `GROWTH_FOLLOWUP_DELAY_DAYS`. The clock starts at the LAST touch, not the
   first — a thread that already had a nudge waits the full delay again.
2. Skips the thread when the prospect is `replied` (they answered) or `dead`
   (already retired), or when any draft in the thread is `replied`.
3. Skips the thread when a follow-up is already **open** in it (`draft`,
   `proposed` or `approved`) — that one is the human's move. This is also
   what makes the sweep idempotent: the follow-up it filed on the last pass
   blocks the next one, so re-running a pass creates nothing.
4. Counts the thread's non-rejected follow-ups. At `GROWTH_FOLLOWUP_MAX` the
   prospect is retired to `status: "dead"` and nothing further is created —
   the sweep never touches them again. (A follow-up a human **rejected**
   doesn't burn a cap slot.)
5. Otherwise: creates a `variant: "follow_up"` draft (copy templated in code
   from the thread's first touch — a placeholder the `/growth` crew skill
   replaces; on email the subject is the original's, `Re:`-prefixed, so the
   nudge threads under it) and immediately runs it through the existing
   propose path — filing the `_growth_send` Action and flipping the draft to
   `proposed`.

**Who proposes.** A cron has no user, but the gate re-checks the proposer's
*current* `growth.manage` role at execute time, so a "system"-proposed
follow-up would be approvable and then fail closed at dispatch. The sweep
therefore **inherits the human** who proposed the thread's last send, read off
that draft's own `_growth_send` Action — they become the follow-up's trigger
source, its Tray assignee, and the identity the execute-time re-check runs
against. When no proposer can be resolved (a draft that reached `sent` with no
Tray record) the thread is skipped: a proposal nobody can execute is worse
than none.

**Config.** Both read from the environment at sweep time, so a change takes
effect on the next tick without a redeploy. An unparseable or out-of-range
value logs a warning and falls back to the default — a typo must not take the
outbound loop down.

| Env var | Default | Meaning |
|---|---|---|
| `GROWTH_FOLLOWUP_DELAY_DAYS` | `4` | Days of silence after a send before its follow-up comes due. Minimum `1`. |
| `GROWTH_FOLLOWUP_MAX` | `2` | Follow-ups allowed per (prospect, channel). On the pass where a capped thread comes due again, the prospect is set to `dead` instead. |

**Send timestamp.** The age check reads the draft's `sent_at` when the
dispatch worker's send record supplies one, and otherwise falls back to
`updated_at` — which, for a draft sitting in `sent`, is the moment of the
`sent` transition, since the status flip is the last write to that row.

---

## Growth — WhatsApp dispatch (MSG91)

Sixth slice of the `/growth` outbound engine (G-6): the `channel="whatsapp"`
branch of the `growth.dispatch` arq job actually sends, through MSG91 (an
official Meta WhatsApp Business Solution Provider).

**The opt-in guard is a service-level invariant, not a UI convention.** Meta
bans WhatsApp Business Accounts that send business-initiated template messages
to numbers that never consented — the quality rating collapses, then the number
gets restricted, then banned, for the whole tenant. So dispatch refuses any
draft whose prospect has `opted_in = false`: it makes **no provider call at
all**, raises `growth.whatsapp_opt_in_required`, records a `blocked` row, and
leaves the draft in `approved` so the refusal is visible instead of silent.

Because business-initiated messages must be pre-approved templates, the draft
`body` is sent as the template's first body variable, never as free-form text.

**Guard order** (each refusal writes its own send-log row and raises):

| # | Guard | Error code | Blocked reason |
|---|---|---|---|
| 1 | Draft still `approved` (the G-4 gate owns that status) | `growth.draft_not_approved` | `draft_not_approved` |
| 2 | Prospect still exists in the draft's workspace | `growth.prospect_unavailable` | `prospect_missing` |
| 3 | **`prospect.opted_in`** | `growth.whatsapp_opt_in_required` | `not_opted_in` |
| 4 | Prospect has a WhatsApp number | `growth.prospect_unavailable` | `no_number` |
| 5 | Hourly rate cap | `growth.whatsapp_rate_capped` | `rate_capped` |
| 6 | Resolvable MSG91 credentials | `growth.whatsapp_not_configured` | `not_configured` |

On success the job writes a `sending` row, calls MSG91, finalises the row to
`sent` (or `failed`), and flips the draft `approved → sent` through the gate's
own `service.gate_transition` seam. The growth worker runs with `max_tries = 1`,
so a refusal lands as a failed arq job for operator review — an outbound message
is never retried automatically.

Every attempt — including refused ones — writes a row to
`growth_whatsapp_send_logs` (`WhatsAppSendLog`): workspace, draft, prospect,
recipient number, status, blocked reason, provider message id, and
`opted_in_at_attempt` (the consent fact *as of* the send, which a later prospect
edit cannot rewrite).

### Credentials

The MSG91 authkey is resolved per workspace through the **connector state
pattern** — the workspace's `msg91` `WorkspaceConnector` row, read via
`CloudConnectorStateStore` with the `ws:<workspace_id>` scope key. There is
deliberately **no env-var fallback for the authkey**: a deployment-global
provider key would let one tenant's outbound traffic burn another tenant's WABA
quality rating. No row, no send.

Config keys on that row:

| Key | Required | Notes |
|---|---|---|
| `authkey_enc` | yes* | The authkey as a `_core.crypto` Fernet ciphertext (needs `CLOUD_ENCRYPTION_KEY`). Preferred — keeps the plaintext out of Mongo and out of the connectors entity's own `config` echo. |
| `authkey` | yes* | Plaintext fallback for installs with no encryption key. Warns on every resolve. |
| `integrated_number` | yes | The WABA business number messages are sent from. |
| `template_name` | yes | The pre-approved Meta template. |
| `language_code` | no | Defaults to `en`. |
| `namespace` | no | WABA template namespace, when the account requires one. |
| `base_url` | no | Defaults to `https://api.msg91.com`. Per-workspace override for regional mirrors. |

\* one of `authkey_enc` / `authkey`.

The authkey is never logged, never returned by any DTO, and never persisted into
the send log. `Msg91Credentials.__repr__` redacts it, so a traceback or a `%r`
format cannot spill it either.

### `POST /api/v1/growth/webhooks/msg91`

Inbound MSG91 WhatsApp events. **Unauthenticated by nature** (MSG91 is the
caller) — mounted separately from the licensed `/growth` router, with no license
gate, no RBAC and no `RequestContext`.

**Fails closed.** Trust rests entirely on the signature: HMAC-SHA256 over the
raw request body, keyed by `GROWTH_MSG91_WEBHOOK_SECRET`, hex-encoded, in
`X-Msg91-Signature` (an optional `sha256=` prefix is tolerated). A bad
signature, a missing header, **and an unset secret** all return
`403 growth.webhook_signature_invalid` / `growth.webhook_unsigned` /
`growth.webhook_unverifiable`. Unlike the Recall webhook there is no
accept-while-you-wire-it-up mode — a forged inbound reply would flip
`opted_in` and thereby unlock business-initiated sends to a number that never
consented.

On a verified inbound reply the handler sets `prospect.opted_in = true`, moves
the prospect to `replied`, and walks any `sent` WhatsApp draft for that prospect
to `replied` (through the gate seam). Under Meta's rules a user-initiated
message both opens the 24-hour service window and *is* the opt-in signal for
that number.

Delivery-status callbacks (`status` / `delivered` / `read` / …) are accepted and
ignored — a receipt is not consent. A number no workspace holds is a 200 no-op.

The response body is a constant `{"ok": true}` for every accepted request —
processed, ignored, or unknown number — so the endpoint cannot be used as a
membership oracle over phone numbers.

Tenancy: the payload carries no workspace, so the lookup starts from the number
and is immediately re-narrowed — when any workspace has actually WhatsApp'd that
number, only those workspaces' rows are touched, so a tenant that merely holds
the same prospect never learns someone else's outreach got a reply.

### Environment

| Variable | Default | Purpose |
|---|---|---|
| `GROWTH_WHATSAPP_MAX_PER_HOUR` | `20` | Per-workspace outbound WhatsApp ceiling per rolling hour. WhatsApp quality rating is computed over a rolling window of recent business-initiated messages, and a burst (bulk approval, retry storm, mis-scoped follow-up cron) is exactly the shape that trips it — with the damage landing on the WABA, not the individual send. The cap bounds the blast radius of a bug. Attempts that reached the provider (`sending` / `sent` / `failed`) consume the window; refused attempts do not. There is no "disabled" value — `0` refuses every send rather than meaning unlimited, and a non-numeric or negative value falls back to the default, so a fat-fingered setting fails closed. |
| `GROWTH_MSG91_WEBHOOK_SECRET` | *(unset)* | Shared secret for the inbound webhook HMAC. **Required** — while unset, `POST /growth/webhooks/msg91` rejects every request with 403. |
| `CLOUD_ENCRYPTION_KEY` | *(unset)* | Existing deployment-wide Fernet key. Needed to store the MSG91 authkey as `authkey_enc` rather than plaintext. |

## Growth — the agent surface (`pocketpaw_growth` MCP)

The chat agent on the `/growth` rail reaches the same service layer through
nine in-process MCP tools. It is the operator's assistant on that page: it can
research and file a prospect, write and revise the copy, and put a send in
front of a human. It cannot send.

| Tool | RBAC | What it does |
|---|---|---|
| `growth_list_prospects` | `growth.read` | Compact rows + the filter-scoped `total`; `tier` / `status` / `source` / `q` / `sort` / `cursor` / `limit` |
| `growth_get_prospect` | `growth.read` | One prospect in full, with every draft written for it |
| `growth_list_drafts` | `growth.read` | Drafts with truncated body previews; filters by prospect / channel / status |
| `growth_linkedin_queue` | `growth.read` | The manual LinkedIn queue with its prospect context |
| `growth_upsert_prospect` | `growth.write` | Create-or-enrich keyed on `domain`; omitted fields keep their stored values |
| `growth_create_draft` | `growth.write` | Write one channel's copy — born `draft` |
| `growth_update_draft` | `growth.write` | Revise copy, only while the draft is still `draft` |
| `growth_propose_send` | `growth.manage` | Files one `_growth_send` Instinct proposal; returns `{status: "proposed", proposal_id}` |
| `growth_propose_send_batch` | `growth.manage` | The same, over up to 100 draft ids — one proposal each |

The agent's surface is deliberately **narrower than the HTTP one**:

| Verb | Operator over HTTP | Agent over MCP |
|------|--------------------|----------------|
| reads, upsert a prospect, write a draft | runs | runs |
| edit a draft's copy (while `draft`) | runs | runs |
| propose a send | runs | runs |
| move a draft to `approved` or `sent` | **refused** (gate-owned) | **no tool exists** |
| mark a LinkedIn draft sent | runs | **no tool exists** |

**The agent's reach ends at `proposed`.** No tool sends; no tool takes a
`status` argument (the legal move is exposed as the named verb
`growth_propose_send`, so there is no shape of argument that could ask for
`approved`); and `service.gate_transition` — the seam the executor and the
dispatch worker walk — is not reachable from the MCP module at all. The two
`status` fields that do appear are read filters on the list tools.
`growth_update_draft` stops at `draft` for the same reason: from `proposed`
on, the stored body is what the Tray shows and what goes on the wire, so an
edit past that point would be a send bypass wearing an edit's clothes. Tests
assert all of this against the tool list and schemas, so a tool added later
trips them before it ships.

Tenancy comes from the chat stream's identity — no tool accepts a
`workspace_id`, and every schema sets `additionalProperties: false`. The RBAC
tiers mirror the HTTP routes, and the ADMIN tier on the propose verbs is
load-bearing: `growth.executor` re-checks `growth.manage` against the
proposer's **current** role at approve time, so a proposal filed below that
tier could only ever clog the Tray.
  "articles": [
    {
      "id": "deploy-runbook",
      "title": "Deploy runbook",
      "source": "",
      "scope": "workspace:w1",
      "agent_id": null,
      "updated_at": "2026-08-01T12:00:00Z",
      "summary": "How we deploy.",
      "word_count": 250,
      "compiled_with": "claude-haiku-4-5",
      "version": 3,
      "categories": ["Ops"],
      "concepts": ["deploys", "rollbacks"],
      "compiled_at": "2026-08-01T12:00:00Z"
    }
  ],
  "total": 1,
  "agent_ids": ["agent-1"]
}
```

The first six keys are the pre-2026-08-04 row shape, unchanged. The wiki
metadata after them comes from `kb list --json` plus the article's wiki
frontmatter (kb list doesn't emit categories/concepts/compiled_at);
`updated_at` falls back to `compiled_at`. Orphan raw docs — ingested files
whose compile never completed — still appear as synthetic rows with
`compiled_with: null` and `version: null`.

### `GET /knowledge/articles/{article_id}?scope=`

Full article for the reader view. `scope` defaults to the active workspace.
Response is the row shape above plus `content` (markdown), `backlinks`
(list of article ids), `source_docs` (raw-doc ids), `scope`, and `orphan`.
An orphan raw-doc id returns `orphan: true` with the raw text as `content`
and `compiled_with: null`. Unknown id or an id outside the scope →
`404 article.not_found`. A kb failure that is NOT a genuine miss (timeout,
missing binary, transient error) → `500 knowledge.kb_unavailable` — a kb
outage never reads as "the article vanished".

### `GET /knowledge/stats`

Per-scope `kb stats` rollup across the workspace scope and every agent
scope. A scope whose stats call fails is skipped, never a 500.

```json
{
  "stats": [
    {
      "scope": "workspace:w1",
      "agent_id": null,
      "articles": 4,
      "words": 1000,
      "raw_docs": 5,
      "concepts": 12,
      "categories": 3
    }
  ],
  "agent_ids": ["agent-1"]
}
```

### `POST /knowledge/reingest`

Body: `{"article_id": "<id>", "scope": "<scope>" | null}`.

Re-runs an article's linked raw doc through the hardened
`KnowledgeService.ingest_text_to_scope` funnel (agent-backend compile on
keyless boxes, verbatim-fallback rejection). `article_id` may be a compiled
article (its frontmatter's first `source_docs` entry names the raw doc) or
an orphan raw-doc id. Response:

```json
{
  "scope": "workspace:w1",
  "article_id": "deploy-runbook",
  "new_article_id": "deploy-runbook-v2",
  "raw_doc_id": "raw-1",
  "source": "notes.txt",
  "result": { "article": "deploy-runbook-v2", "title": "...", "words": 250, "compiled_with": "llm" }
}
```

`result` is kb-go's ingest receipt passed through verbatim — note the id key
is `article` (finishIngest's shape), not `id`. `new_article_id` is the
server-extracted id of the article the recompile produced; when it differs
from `article_id` (the compile landed under a new slug) the FL-11b tracking
on any upload row pointing at the old id is re-pointed automatically.

Errors: `404 article.not_found` / `404 raw_doc.not_found`,
`422 knowledge.empty_raw_doc`, `500 knowledge.reingest_failed`.

### `POST /knowledge/reingest-upload`

Body: `{"upload_id": "<file id>", "scope": "<scope>" | null}`.

Synchronous counterpart of the FileReady auto-index listener, one upload per
call: resolves the uploaded blob (local or S3 via a temp file), extracts
text through the configured extraction chain, funnels it through
`ingest_text_to_scope` with the original filename as source, and stamps the
FL-11b `kb_article_id`/`kb_scope` tracking on the upload row. Response:

```json
{
  "scope": "workspace:w1",
  "upload_id": "up-1",
  "filename": "report.pdf",
  "article_id": "report-pdf",
  "result": { "article": "report-pdf", "title": "...", "words": 300, "compiled_with": "llm" }
}
```

`article_id` is the server-extracted id from kb-go's receipt (whose own id
key is `article`, not `id`) — clients should read the top-level field.
Pocket-scoped uploads are refused on this workspace surface: ingesting them
into workspace KB would lift pocket-private content across the pocket ACL
boundary; reingest those from the pocket surface instead.

Errors: `404 upload.not_found`, `403 knowledge.upload_hidden`
(`hide_from_ai` files are not ingestable),
`403 knowledge.upload_pocket_scoped` (pocket files belong to the pocket
surface), `422 knowledge.extraction_empty`,
`500 knowledge.extraction_failed` / `knowledge.upload_unreadable` /
`knowledge.reingest_failed`.

### `GET /knowledge/uploads?scope=`

The WORKSPACE's uploaded files eligible for ingest. Excluded: soft-deleted
rows, `hide_from_ai` rows, and any pocket-scoped upload (pocket files are
ACL-gated on the pocket surface and never list here). `has_article` is
derived cheaply: primarily the FL-11b tracking column matched against the
resolved scope; untracked rows fall back to a filename-vs-article-sources
match that only counts when the upload predates the matching article's
`compiled_at` — a fresh re-upload of a same-named file reads as pending,
not compiled.

```json
{
  "uploads": [
    {
      "id": "up-1",
      "filename": "report.pdf",
      "mime": "application/pdf",
      "size": 12345,
      "uploaded_at": "2026-08-01T10:00:00+00:00",
      "has_article": true
    }
  ],
  "total": 1,
  "scope": "workspace:w1"
}
```
