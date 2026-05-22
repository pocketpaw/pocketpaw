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
{ "base_url": "https://api.example.com", "auth_type": "bearer", "configured": true }
```

The token is never echoed back. A non-https or internal `base_url` yields
a `400`.

For `basic` auth, send `auth_token` as the raw `user:pass` credential —
the server base64-encodes it into the `Authorization: Basic` header. Do
not pre-encode it yourself.

### `GET /pockets/{pocket_id}/backend`

Read the pocket's backend binding summary. Requires pocket **edit** access
(owner or editor) — backend config metadata is owner/editor-facing,
consistent with the `PUT` route. Viewers receive a `403`.

Response `200`:

```json
{ "base_url": "https://api.example.com", "auth_type": "bearer", "configured": true }
```

Returns `404` when the pocket has no backend configured. The token is
never included in the response.

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
