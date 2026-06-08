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
  "allowed_writes": []
}
```

The token is never echoed back. A non-https or internal `base_url` yields
a `400`. `allowed_writes` is the per-pocket write allowlist (RFC 05 M2a) —
empty by default, so no write action can fire until an owner sets a policy
via `PUT /pockets/{id}/backend/write-policy`.

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
  "allowed_writes": [{ "method": "POST", "path_pattern": "/leases/*/renew" }]
}
```

Returns `404` when the pocket has no backend configured. The token is
never included in the response. `allowed_writes` carries the current
write allowlist (RFC 05 M2a).

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

Uninstall a plugin: delete each skill directory it installed, remove each
of its namespaced MCP servers from the MCP manager, reload the skill
loader, and drop its registry entry. Requires the **admin** scope.

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
server no longer registered) is `skipped` — the remove still completes and
the registry entry is **always** dropped, so a half-removed plugin never
lingers in the listing. The only up-front error is an unknown plugin:

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
