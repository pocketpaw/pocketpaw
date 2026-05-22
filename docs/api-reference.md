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
