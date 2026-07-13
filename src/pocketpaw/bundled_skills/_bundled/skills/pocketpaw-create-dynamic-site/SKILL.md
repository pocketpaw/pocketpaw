---
name: pocketpaw-create-dynamic-site
description: |
  Build a DYNAMIC Paw Site — a published website backed by the customer's
  OWN LIVE DATA (a per-tenant Cloudflare D1), with reads AND writes, not a
  static brochure. Invoke when the user wants a site that LISTS live records
  and/or has a form that SAVES records: "a guestbook site", "a public
  booking list people can add to", "a submissions board", "an order tracker
  page", "a site where visitors sign up and it remembers them". This is the
  live-data authoring brain — YOU author a rippleSpec that carries both the
  UI and the dynamic data bindings (objects = the D1 tables, sources = read
  bindings, actions = write bindings, optional auth), and a deterministic
  tool persists the pocket stamped type="site" + pattern="dynamic" so publish
  scaffolds the D1 migration + read/write remote functions. For a STATIC
  marketing page use pocketpaw-create-paw-site (ripple) or
  pocketpaw-create-svelte-site (svelte); for publishing an EXISTING pocket
  use pocketpaw-create-site. Loading this skill keeps the chat agent's
  always-on system prompt small while still delivering the full dynamic-site
  authoring brain when a live-data site is actually requested.
---

# Build a Paw Site — the dynamic (live-data) authoring brain

You're building a **dynamic Paw Site**: a real, standalone website backed by
the customer's **own live Cloudflare D1**. Unlike a marketing page, it **reads
records from a database and lists them**, and/or has a **form that writes
records**. A guestbook, a public booking list, a submissions board, an order
tracker — anything where the page shows live data and visitors can add to it.

This is the sibling of the marketing-site skills. The difference is the
**payload and what gets generated**:

| | `create-paw-site` / `create-svelte-site` | **`create-dynamic-site` (this skill)** |
|---|---|---|
| Backs the page with | nothing (static brochure) | **the customer's own live D1** |
| What you provide | copy / Svelte components | **a rippleSpec with data bindings** |
| What publish generates | prerendered static HTML | **SSR routes + D1 migration + read/write remote functions** |
| Persisted as | `pattern="landing"` | `pattern="dynamic"` |

A dynamic site is a **ripple-engine** site whose spec carries extra **dynamic
blocks** as top-level keys. You author the `ui` (the page) **plus** those blocks;
the tool persists the whole spec, and publish carries it to the paw-sites
generator, which scaffolds the D1 and compiles the bindings into SvelteKit
remote functions. You do **NOT** call `pocket_specialist`, and you do **NOT**
provision Cloudflare yourself — the generator handles the D1.

## The dynamic blocks (this is the whole skill)

A spec is **dynamic** when it declares any `sources`, any `actions`, or
`auth: true`. Add these four optional top-level keys alongside `ui`:

### `objects` — the data schema (the D1 tables)

An array of table definitions. The generator derives the D1 migration from
this. Every `source`/`action` references an object by `name`.

```jsonc
"objects": [
  {
    "name": "entry",                          // table name
    "fields": {                               // column -> field type
      "id": "text",
      "name": "text",
      "message": "text"
    },
    "primaryKey": "id"                        // the column used as PRIMARY KEY
  }
]
```

Field types (spec → D1): `text`→TEXT, `integer`→INTEGER, `real`→REAL,
`boolean`→INTEGER (0/1), `timestamp`→TEXT (ISO 8601).

### `sources` — READ bindings (D1 → page)

Each compiles to a `query` remote function over the D1. The UI references a
source by `"{<source name>}"` (e.g. a table bound to `"{entries}"`).

```jsonc
"sources": [
  {
    "name": "entries",        // export name; the UI binds a table to "{entries}"
    "kind": "data",           // only "data" is supported
    "object": "entry",        // must be a declared object
    "where": "id = ?",        // optional SQL WHERE
    "orderBy": "name",        // optional SQL ORDER BY column
    "limit": 50,              // optional LIMIT
    "refresh": "pocket_open"  // pocket_open | interval | manual | live
  }
]
```

`refresh` modes: `pocket_open` (query on page load — the default you want),
`interval`/`manual` (same query, client re-fetches), `live` (compiles to a
`query.live` generator that re-queries every ~3s and streams updates with no
reload — use for an order/status tracker).

### `actions` — WRITE bindings (page → D1)

Each compiles to a validated `form` remote function that INSERTs into D1, and
the generated page renders a **native `<form>`** for it (works with JS off). A
write that touches an object single-flight-refreshes any source reading it, so
the list updates after a submit.

```jsonc
"actions": [
  {
    "name": "sign",                  // export name; rendered as a form
    "object": "entry",               // must be a declared object
    "op": "insert",                  // "insert" is the path built today
    "confirm": "Sign the guestbook?",   // optional client confirm before submit
    "requiresOwnerReview": false        // optional; flags the row for owner review
  }
]
```

The form's inputs are derived from the object's **non-primary-key fields**
(here: `name`, `message`), so the writer fills those. Keep the object lean —
every non-PK field becomes a required form input. Auto-set columns (a created
timestamp, a status) are friction on a write form; leave them out of the object
unless the writer should provide them.

### `auth` — end-customer accounts (optional)

A top-level boolean. `auth: true` makes the generator add `users`/`sessions`
tables, signup/login/logout remote functions, and a protected route group.
**Auth wiring is a thin slice today** — emit `auth: true` only if the user
explicitly asks for accounts; otherwise leave it off.

## STEP 1 — Author the spec (UI + dynamic blocks)

Write the `ui` tree the way you would for any pocket page: a container with the
content. For the dynamic part:

- To **show a list**, bind a `table` (or list widget) to a source:
  `{ "type": "table", "props": { "data": "{entries}" } }`. The source seeds its
  rows into the page at render.
- The **write form** is generated automatically from each `action` — you do
  **not** hand-author the form widget in `ui`. Just declare the `action`.

Use **real, concrete copy** (a real heading, real field intent) — never "TBD".

### Worked example — a guestbook (read + write)

```json
{
  "ui": {
    "type": "container",
    "children": [
      { "type": "heading", "props": { "text": "Guestbook" } },
      { "type": "table", "props": { "data": "{entries}" } }
    ]
  },
  "objects": [
    {
      "name": "entry",
      "fields": { "id": "text", "name": "text", "message": "text" },
      "primaryKey": "id"
    }
  ],
  "sources": [
    { "name": "entries", "kind": "data", "object": "entry", "orderBy": "name", "refresh": "pocket_open" }
  ],
  "actions": [
    { "name": "sign", "object": "entry", "op": "insert", "confirm": "Sign the guestbook?" }
  ]
}
```

This generates a site that **lists** the `entry` rows (the `entries` source)
and renders a **form** with `name` + `message` inputs (the `sign` action) that
writes a new row and refreshes the list.

## STEP 2 — Call `create_dynamic_site`

Hand the spec to the tool. It validates the dynamic contract (a `ui`, an
`objects` block, at least one source/action/auth, every binding referencing a
declared object), then persists the pocket stamped `type="site"` +
`pattern="dynamic"` with your spec as the rippleSpec — directly, no specialist.

```
mcp__pocketpaw_sites_manager__create_dynamic_site(
  spec = <the spec from STEP 1>,
  name = "Guestbook"           // optional; defaults to "Dynamic site"
)
```

It returns `{ ok, pocket_id, pocket }`. Keep `pocket_id` for STEP 3. If `ok` is
false, **relay the error** — do **not** claim a phantom create. The tool fails
closed and tells you what's wrong (e.g. "needs at least one live binding", or a
source referencing an undeclared object); fix the spec and retry. If the request
is actually a static marketing page (no live data), the tool steers you to
`create_landing_site` — follow that.

## STEP 3 — Publish

Publish the new pocket as a site:

```
mcp__pocketpaw_sites_manager__publish(pocket_id = <the id from STEP 2>)
```

Publish carries the spec's dynamic blocks to the generator, which scaffolds the
D1 migration + the read/write remote functions, runs the smoke gate, and
deploys. Show the user the returned `url` plus a pointer to **/sites**. Relay any
`ok: false` error — never claim a phantom publish.

> Note on data: real per-tenant Cloudflare D1 provisioning is a deploy-time
> concern handled by the control plane. Locally (no Cloudflare creds) the site
> serves against a local D1. Either way you don't provision anything — you only
> author the spec.

## Quality bar — done right when

1. **The spec is genuinely dynamic.** It declares `objects` AND at least one
   `source` / `action` / `auth` — not a static page pushed through the dynamic
   tool. Every source/action references a declared object.
2. **Read and write are both wired** (when the user wants both): a `source` the
   UI lists via `"{name}"`, and an `action` whose object's non-PK fields are the
   form inputs.
3. **The object is lean.** Only the columns the writer provides plus the
   primary key — no auto-set columns forced into the write form.
4. **You showed the live URL.** The user got the `url` from publish and a
   pointer to /sites — not just "done". Errors were relayed, never masked.

## Related tools (via MCP)

- `mcp__pocketpaw_sites_manager__create_dynamic_site` — **the create step.**
  Pass the dynamic `spec` you authored; the tool persists the pocket stamped
  `type="site"` + `pattern="dynamic"`. Returns `{ok, pocket_id, pocket}`.
- `mcp__pocketpaw_sites_manager__publish` — publish the pocket as a live site
  (generates the D1 + remote functions); show the user the `url`.
- `mcp__pocketpaw_sites_manager__create_landing_site` — for a STATIC marketing
  page instead (no live data).
