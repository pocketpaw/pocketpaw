<!--
docs/api-reference.md — Hand-maintained reference for cloud REST endpoints
that are not covered by the per-endpoint Mintlify pages under docs/api/.

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
-->

# Cloud REST API Reference

This file documents cloud (`pocketpaw-ee`) REST endpoints that do not yet
have a dedicated page under `docs/api/`. All cloud endpoints require a
valid enterprise license and an authenticated workspace context.

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
out, symlinks pointing out, and another tenant's jail are all rejected (reusing
ART-2's path-segment guard). Size is capped by `POCKETPAW_DELIVER_MAX_MB`
(default `100`).
