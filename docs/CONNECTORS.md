<!--
  Connectors documentation.
  Updated: 2026-07-02 (AW-3 egress default-close) — clarified the "Development
  against localhost" section: the egress guard rejects internal/metadata IPs BY
  DEFAULT; POCKETPAW_ALLOW_INTERNAL_URLS opens the escape ONLY when set to an
  explicit truthy value; unset ⇒ reject (safe production posture).
  Updated: 2026-06-28 (AW-1/AW-2 connector egress guard) — added the "Egress
  allow-list (SSRF protection)" section: the POCKETPAW_CONNECTOR_EGRESS_GUARD
  flag (default-deny posture, off by default for safe rollout), the optional
  top-level `allowed_hosts:` YAML key + the per-workspace WorkspaceConnector
  `allowed_hosts` field, how the effective allow-list is auto-seeded (declared
  base-URL host + auth-endpoint host, templated hosts resolved at call time)
  and how to add hosts, the dev escape POCKETPAW_ALLOW_INTERNAL_URLS, and the
  fail-closed-on-config-error / cookie-jar-preserving guarantees.
  Updated: 2026-06-12 (connector-store-unification CS-6) — added the
  "Lifecycle: definitions, state, cache" section: the three layers a connector
  lives in (YAML definitions with two scan dirs + CWD precedence, the durable
  state store at ~/.pocketpaw/connectors/state, and the in-memory adapter
  cache), restart semantics, and the presence-based "connected" status.
  Updated: 2026-06-12 (workspace-scope reach) — the agent tool surface now
  reaches workspace-scoped connectors: list_connector_actions returns the
  current pocket's bound connectors PLUS the workspace-enabled ones (deduped
  by name), unanchored chats (no pocket) reach exactly the workspace-scoped
  set, and connector_execute passes for pocket-bound OR workspace-scoped
  rows (executing with workspace-scope credentials when unanchored). The
  read-first / write-blocked trust gate is unchanged.
  Updated: 2026-06-11 (connector cookie/session auth) — documented two new
  auth methods on the DirectREST engine: `cookie` (emits a Cookie: header from
  a declared credential, name set via auth.credential) and `header` (emits an
  arbitrary header named by auth.header — the escape hatch for keys that are
  not Bearer tokens). Both are additive; api_key/bearer/basic are unchanged.
  Updated: 2026-06-11 (firestore-fabric-ingest) — added the "Firestore → Fabric
  ingestion worker" section: the cloud background worker that mirrors selected
  Firestore collections into Fabric objects per a per-workspace mapping config,
  with a real high-water cursor, upsert-by-source, and tenant-stamped writes.
  Updated: 2026-06-08 (sense-mcp / Sense tier chunk 4) — added the Senses
  section: the "Sense" glossary entry, the sense-vs-connector distinction, and
  the two new agent tools (list_senses / sense_execute on the same
  pocketpaw_connectors MCP server) that address a capability instead of a
  provider, with the resolver binding it to the tenant's enabled connector.
  Updated: 2026-06-08 (connector-mcp-execution / keystone) — documented the
  agent-callable connector tool surface (list_connector_actions /
  connector_execute on the pocketpaw_connectors MCP server), the v1
  read-first / write-blocked policy, how a connector becomes usable in a room
  (bind scope=pocket + token in config + the derived skill), and the GitHub +
  Gmail examples.
  Updated: 2026-06-07 (M3 connector→skill auto-authoring) — documented the
  optional ``surface_profile`` YAML block and the connector→skill/tool
  auto-authoring path (derivation at bind/unbind from the full enabled set, the
  Gmail reference, and the coexistence rule with hand-set ripple_mode /
  system_message_override).
-->

# Connectors — Data Source Integration

Connectors bring external data into PocketPaw Pockets. Each service is defined in a YAML file — the engine reads the definition and handles auth, execution, and sync.

## Quick Start

```bash
# List available connectors
paw connectors list

# Connect Stripe to a pocket
paw connect stripe --pocket "My Business"

# Check connection status
paw connectors status
```

## How It Works

```
Your Service (Stripe, Shopify, CSV, etc.)
    ↓
Connector YAML (defines endpoints, auth, sync)
    ↓
DirectREST Engine (reads YAML, makes API calls)
    ↓
pocket.db (data lands in SQLite tables)
    ↓
Pocket widgets auto-update with fresh data
```

## Lifecycle: definitions, state, cache

A connector lives in three layers with different lifetimes:

| Layer | What it holds | Where | Lifetime |
|-------|--------------|-------|----------|
| **Definition** | What the connector *is* — endpoints, auth schema, actions | `~/.pocketpaw/connectors/*.yaml`, then `connectors/*.yaml` (CWD) | As long as the file exists |
| **State** | That a connector *is configured* — the config passed to `/connect`, keyed by (name, pocket) | `~/.pocketpaw/connectors/state/*.json` (the durable state store) | Until `/disconnect` |
| **Cache** | Live adapter instances (HTTP clients, DB pools, OAuth sessions) | In-memory, per process | Until the process exits |

**Definition scan.** The registry scans the home dir
(`~/.pocketpaw/connectors/`) first, then the CWD `connectors/` dir. On a name
collision the CWD definition wins — deploys override user-installed
definitions. A definition dropped in after startup is picked up on the next
lookup miss (the registry rescans cheaply instead of requiring a restart).

**State.** `/connect` is write-through: the config is persisted to the state
store before the adapter connects, and rolled back if the connect fails.
`/disconnect` deletes the row. State files are chmod 0600 and live under a
0700 dir — the config can carry credentials, same posture as the OAuth token
store.

**Restart semantics.** The cache dies with the process; definitions and state
do not. After a restart the list/detail/status endpoints report a configured
connector as `connected` (derived from definition-present + config-persisted,
never from the in-memory adapter map), and `/execute` lazily reconnects the
adapter from the persisted config via `ensure_connected` — no manual
re-`/connect` step.

**What "connected" means.** Status is a *presence* semantic: a definition
exists and config is persisted. It does not probe the remote service per
request — a revoked API key still shows `connected` until an execute fails.
Use a connector's `health()` for a live check.

**Orphaned state.** A state row whose definition is gone (YAML deleted, or a
deploy dropped it) surfaces in list/status as `definition_missing` instead of
disappearing or crashing. It heals automatically once the definition is back,
or can be cleared with `/disconnect`.

## Writing a Connector YAML

Each connector is a YAML file in `connectors/`. Here's the structure:

```yaml
# connectors/my_service.yaml
name: my_service
display_name: My Service
type: payment                     # category for grouping
icon: credit-card                 # lucide icon name

auth:
  method: api_key                 # api_key | bearer | basic | header | cookie | oauth | none
  credentials:
    - name: MY_API_KEY
      description: API key from My Service dashboard
      required: true

actions:
  - name: list_items
    description: Get all items
    method: GET
    url: https://api.myservice.com/v1/items
    params:
      limit: { type: integer, default: 10 }
      status: { type: string, enum: [active, archived] }
    trust_level: auto             # auto | confirm | restricted

  - name: create_item
    description: Create a new item
    method: POST
    url: https://api.myservice.com/v1/items
    body:
      name: { type: string, required: true }
      price: { type: number }
    trust_level: confirm          # requires user approval

sync:
  table: my_service_items         # target table in pocket.db
  schedule: every_15m             # polling interval
  mapping:                        # field mapping
    id: id
    name: name
    price: price
    created: created_at

allowed_hosts:                    # OPTIONAL — extra egress allow-list hosts
  - cdn.myservice.com             # added ON TOP of the auto-seeded hosts

surface_profile:                  # OPTIONAL — connector→skill/tool auto-authoring
  skill: my_service               # a skill to load in rooms with this connector
  allow_tools: []                 # tool-id patterns to add to the SDK allowlist
  deny_tools: []                  # tool-id patterns to deny
```

The `surface_profile` block is optional. Connectors without it parse and behave
exactly as before. See [Connector → Skill / Tool auto-authoring](#connector--skill--tool-auto-authoring) below.

The `allowed_hosts` block is optional and only matters when the egress guard is
on — see [Egress allow-list (SSRF protection)](#egress-allow-list-ssrf-protection) below.

## Auth Methods

| Method | When to Use | Example |
|--------|-------------|---------|
| `api_key` | Service provides a static API key sent as `Authorization: Bearer …` | Stripe, Tavily |
| `oauth` | Service uses OAuth 2.0 flow | Google, Spotify |
| `bearer` | Token-based auth (token in the `Authorization` header) | Generic REST APIs |
| `basic` | Username + password auth | Legacy APIs |
| `header` | Key goes in a custom header (not a Bearer token) | APIs using `X-API-Key`, `Api-Token`, … |
| `cookie` | Session/cookie auth — a stored value sent as the `Cookie` header | Login-session APIs, internal tools |
| `none` | Public API, no auth needed | Reddit (read-only) |

### `header` — custom-header auth

Use when the credential is sent in a named header that is **not** an
`Authorization: Bearer` token. Set `header` to the header name and `credential`
to the credential the value comes from. The value is sent verbatim — no `Bearer`
prefix — so this is the escape hatch for the `api_key` method's
always-`Bearer` behavior.

```yaml
auth:
  method: header
  header: X-API-Key             # the header to emit
  credential: SERVICE_KEY       # which credential holds the value
  credentials:
    - name: SERVICE_KEY
      description: API key sent in the X-API-Key header
      required: true
```

### `cookie` — session / cookie auth

Use for services authenticated by a session cookie (or any value that belongs in
the `Cookie` header). `credential` names the credential whose value is emitted
as the `Cookie` header; it defaults to the first declared credential when
omitted. The value is sent as-is (e.g. `sessionid=abc123` or a raw token), so
store the full cookie string in the credential.

```yaml
auth:
  method: cookie
  credential: SESSION_COOKIE    # which credential holds the cookie value
  credentials:
    - name: SESSION_COOKIE
      description: Session cookie string, e.g. "sessionid=abc123"
      required: true
```

The DirectREST engine keeps one HTTP client per connected adapter, so any
`Set-Cookie` the service returns is retained in the client's cookie jar and sent
on the next call within the same connection — and connections are pooled across
actions.

## Trust Levels

Each action has a trust level that controls how much human oversight the agent needs:

| Level | Behavior | Use For |
|-------|----------|---------|
| `auto` | Agent executes without asking | Read-only operations (list, search) |
| `confirm` | Agent asks user before executing | Write operations (create, update, delete) |
| `restricted` | Requires admin approval | Destructive or financial operations |

## Egress allow-list (SSRF protection)

A connector executes HTTP with stored credentials attached. Without a guard,
a connector whose URL is influenced by a template, a credential, or a call-time
param could be steered at an internal address (cloud metadata endpoint, an
internal service) — a classic SSRF. The egress guard closes that.

### Turning it on

The guard is a per-deployment switch, **off by default** for a safe rollout:

```bash
POCKETPAW_CONNECTOR_EGRESS_GUARD=true
```

When on, every connector request is checked before it leaves: the URL must be
`https://`, carry no userinfo (`user:pass@host`) and no fragment, its host must
be on the connector's **allow-list**, and the host is DNS-resolved and rejected
if it lands on an internal/loopback/private/metadata IP. The vetted IP is then
*pinned* for the connection, so the name can't be re-resolved to an internal
address between the check and the connect (DNS-rebinding).

### Default-deny — the allow-list is the gate

With the guard on, a request to any host **not** on the allow-list is blocked
with a clean error (`Blocked by egress guard: host '…' is not in the egress
allow-list`). The allow-list is auto-seeded from the connector's own declared
topology, so the common case needs **no configuration**:

1. **Every action's base-URL host** — taken from each action's `url:`. A
   templated host (`https://{REGION}.freshdesk.com/...`, `{CONFLUENCE_BASE_URL}`)
   is resolved with the connector's credentials at call time, so the *real*
   runtime host is allow-listed, never the template string. The `BASE_URL`
   credential's host (the build-from-base path) is included too.
2. **The auth-endpoint host** — `auth.auth_url` / `auth.token_url`. Some
   connectors authenticate on a different host than the API; both are seeded so
   the auth call and the API call each pass.

### Adding hosts

When a connector legitimately reaches a host outside its declared topology (a
CDN, a regional mirror, a webhook host), add it explicitly. Additions are
layered **on top** of the auto-seeded hosts — they never narrow the list.

* **Per connector (all workspaces)** — the top-level `allowed_hosts:` key in the
  connector YAML:

  ```yaml
  allowed_hosts:
    - cdn.myservice.com
    - eu.myservice.com
  ```

* **Per workspace** — the `allowed_hosts` field on the workspace's connector
  binding (`WorkspaceConnector.allowed_hosts`). Use this to permit one extra
  host for a single workspace without editing the shared YAML.

Host matching is case-insensitive; IPv6-literal and IP-literal base URLs are
supported (the resolved-IP internal check still applies to them).

### Development against localhost

The egress guard **rejects internal/loopback/private/metadata IPs by default**.
When `POCKETPAW_ALLOW_INTERNAL_URLS` is unset (or set to anything other than an
explicit truthy value), a resolved internal IP is blocked — this is the safe
production posture and needs no configuration.

For local development against internal hosts (Ollama, a dev API on `127.0.0.1`),
set the dev escape **explicitly**:

```bash
POCKETPAW_ALLOW_INTERNAL_URLS=true
```

Only an explicit `true` / `1` opens the escape. It permits resolved internal
IPs **while the guard still enforces the allow-list** — so a localhost connector
keeps working in development without disabling the guard entirely. This is a
dev-only flag; leave it unset in production so internal IPs stay rejected.

### Guarantees

* **Fail closed on config error.** If `POCKETPAW_CONNECTOR_EGRESS_GUARD` is set
  but the settings load fails, the guard does **not** silently turn off — it logs
  the error and fails closed (the request is routed through the guard), so a
  malformed config can never silently re-open the SSRF bypass.
* **Session/cookie auth keeps working.** The pinned HTTP client is cached per
  resolved host, so a `Set-Cookie` from one call is replayed on the next —
  cookie/session-auth connectors are unaffected by the guard.

## Connector → Skill / Tool auto-authoring

When a connector is **bound to a pocket** (`scope=pocket`), PocketPaw can
auto-derive that pocket's behavioral profile — the skill the agent loads and the
tool allow/deny lists — straight from the connector. No hand-setting. Bind Gmail
to a room and the room's agent gets the Gmail skill automatically; unbind it and
the skill drops back off.

### The `surface_profile` YAML block

The mapping source is a per-connector field, not a hard-coded table. Add an
optional `surface_profile` block to the connector YAML:

```yaml
surface_profile:
  skill: gmail                    # skill name loaded for rooms with this connector
  allow_tools: ["mcp__*gmail*"]   # tool-id glob patterns to ALLOW (optional)
  deny_tools: []                  # tool-id glob patterns to DENY (optional)
```

All three keys are optional; a block with none of them is treated as no block.
Connectors with no block contribute nothing.

### How it derives at bind / unbind

A pocket's profile is **derived from ALL of its enabled pocket-scoped
connectors**, not just the one being toggled:

- **`skill_names`** — the union of every bound connector's `skill`.
- **`allowed_sdk_tools`** — the union of every connector's `allow_tools`
  (stays unrestricted/`None` when no connector contributes one).
- **`deny_mcp_tool_ids`** — the union of every connector's `deny_tools`.

Because it re-derives from the full enabled set every time, **enable and disable
both converge correctly**: enabling adds a connector's contribution to the union;
disabling removes the connector from the set, so its contribution drops on the
next re-derive. The derivation is deterministic and idempotent — re-deriving from
the same set yields the same profile.

The derived profile flows end-to-end immediately: the entity-aware resolver
unions it over the surface base, and the run forwards `skill_names` /
`allowed_sdk_tools` / `deny_mcp_tool_ids` to the agent.

### Coexistence with hand-set profiles

The derivation **owns the connector-contributed dimensions** (`skill_names`,
`allowed_sdk_tools`, `deny_mcp_tool_ids`) — it sets them to the connector union,
overwriting any prior values on those dimensions. It **preserves the user-owned
dimensions** (`ripple_mode`, `system_message_override`) already on the pocket.

Practical consequence: a skill you hand-set on a pocket via the surface_profile
override may be overwritten the next time a connector is bound or unbound. That's
intentional — auto-authoring is the point. Keep durable, hand-set behavior in
`ripple_mode` / `system_message_override`, which the derivation never touches.

### Gmail — the reference

`connectors/gmail.yaml` ships the block (`skill: gmail`). The bundled
[`gmail` skill](../src/pocketpaw/bundled_skills/_bundled/skills/gmail/SKILL.md)
teaches the agent the Gmail action surface (search → read → act) and the
safe-by-default workflow (read before you act; confirm before you send or
destroy). Bind Gmail to a pocket and the room gets that skill with zero
configuration.

`allow_tools` is left empty on Gmail today: its agent tools are hand-written
classes (`gmail_search`, `gmail_send`, …) that are not yet wrapped as stable
`mcp__<server>__<tool>` SDK tool ids. An empty allow means "no SDK-tool
restriction" (the union only adds to the allowlist, never narrows it), so the
skill is the load-bearing contribution until those ids exist.

## Calling connectors from chat — the agent tool surface

Loading a skill teaches the cloud chat agent *how* to use a connector; the
**`pocketpaw_connectors` MCP server** is what lets it actually *call* one. The
server exposes two tools to the agent, namespaced
`mcp__pocketpaw_connectors__*`:

| Tool | What it does |
|------|--------------|
| `list_connector_actions()` | Lists the connectors **reachable from the current chat** — the current pocket's bound connectors plus the workspace-enabled ones, deduped by name — and, per connector, its READ actions (runnable) and WRITE actions (listed, blocked). No arguments — the identity comes from the active chat. |
| `connector_execute(connector_name, action, params)` | Runs ONE action. Read (auto-trust) actions execute; write actions are refused (see below). |

The agent reads the pocket it is in from the per-run identity (the same
mechanism that scopes pocket reads/writes), so the tools always act on the room
the user is chatting in. A chat not anchored to a pocket (a plain DM or group
thread) still reaches the **workspace-scoped** connectors — the workspace is
the tenant boundary, so anything enabled workspace-wide is available from any
chat in it. Pocket-scoped connectors stay private to their room. Outside a
chat stream entirely, the tools return a clear error instead of mis-scoping.

### v1 policy: read-first, writes blocked

v1 is deliberately **read-only**:

- **Read actions** (`trust_level: auto`) execute. They run through the existing
  cloud execution path (`connectors.service.execute`) in-process via the
  `DirectRESTAdapter` / native adapter, using the connector's stored token.
- **Write actions** (`trust_level: confirm` or `restricted`) are **listed but
  blocked**. `connector_execute` refuses them with
  *"This action modifies &lt;connector&gt; and needs approval (coming in v2).
  Not executed."* and never calls the API. The agent surfaces that to the user
  rather than pretending the write happened.

The trust level on each action's YAML (`auto` / `confirm` / `restricted`) is the
gate — the same trust level documented in [Trust Levels](#trust-levels). Mark
reads `auto` and writes `confirm` and the tool surface does the rest.

### Making a connector usable in a room

Three things make a connector callable from a pocket's chat:

1. **Bind it** — either at `scope=pocket` (enable with the pocket's id; private
   to that room — a connector bound to pocket A is not reachable from pocket B)
   or at `scope=workspace` (enable workspace-wide; reachable from every chat in
   the workspace, anchored or not). When the same connector is enabled at both
   scopes, the listing dedupes it by name.
2. **Put a token in the connector's config** — v1 auth is the PAT / API token
   already stored in the connector config (no OAuth flow). For GitHub that's a
   `GITHUB_TOKEN`; for a bearer/`api_key` connector it's the credential named in
   the YAML's `auth.credentials`. Without it, read actions hit the API
   unauthenticated and return an honest auth error.
3. **The derived skill** — binding the connector auto-derives the pocket's
   `surface_profile`, which loads the connector's bundled skill (see
   [auto-authoring](#connector--skill--tool-auto-authoring)). The skill tells the
   agent to call `list_connector_actions` first, then `connector_execute`.

### GitHub example

`connectors/github.yaml` ships a `surface_profile` (`skill: github`) and 9 read
actions plus one write (`create_issue`, `confirm`). Bind it to a pocket with a
`GITHUB_TOKEN` and the room can read issues, PRs, repos, releases, CI runs, and
search code/issues:

```
list_connector_actions()
  → github: read [list_issues, list_pull_requests, get_repo, search_code, …],
    write-blocked [create_issue]

connector_execute("github", "list_issues",
                  {"owner": "acme", "repo": "api", "state": "open"})
  → the repo's open issues

connector_execute("github", "create_issue", {...})
  → blocked: "needs approval (coming in v2). Not executed."
```

### Gmail example

`connectors/gmail.yaml` ships `skill: gmail`. Bound to a pocket, the room can
search and read mail; sending / labeling / trashing are confirm-trust writes and
are blocked in v1:

```
connector_execute("gmail", "gmail_search",
                  {"query": "from:acme.com subject:invoice", "max_results": 5})
  → matching message stubs

connector_execute("gmail", "gmail_send", {...})
  → blocked: "needs approval (coming in v2). Not executed."
```

## Firestore → Fabric ingestion worker

The connectors above pull data on demand into a pocket's SQLite tables. A
separate cloud background worker mirrors a different shape of source — a
Firestore database — into **Fabric**, PocketPaw's ontology layer of typed
objects and links. Use it when a deployment already runs on Firestore and wants
those records to show up as Fabric objects that agents and pockets can query.

The worker is **fully generic**. There are no collection names, field names, or
object types in the code. A deployment describes its own mapping in a
per-workspace `FabricIngestConfig`, and the worker walks it.

### What it does

On a schedule (every 5 minutes by default), for each workspace that has a config:

1. Read each mapped Firestore collection. The first run is a full **backfill**;
   later runs are **incremental** and read only documents newer than the stored
   cursor.
2. For each document, create or update a Fabric object of the mapped type. The
   field map decides which Firestore fields become which object properties.
3. Stamp every object with the workspace, `source_connector="firestore"`, and
   `source_id` set to the full Firestore document path.
4. Apply any link rules, wiring objects together by source path.

### Upsert, not duplicate

Each document maps to one object, keyed on its Firestore path. The worker rides
the same connector→Fabric mapper the Google Calendar ingestion uses
(`pocketpaw.connectors.fabric_ingest.ingest_records`): on a re-run it looks the
object up by `(source_connector, source_id)` and updates it in place, so
re-ingesting the same collection never piles up duplicates.

### The cursor is a real high-water mark

The incremental cursor is taken from the **document data** — the value of the
mapping's `cursor_field` (typically an `updated_at` timestamp) on the
newest-updated document seen, falling back to the Firestore snapshot's
`update_time` when a document has no value for that field. It is **not** the
run's wall clock. A document that arrives late but carries an older timestamp is
still picked up on its own merits, and a re-run never re-scans a time window.

### Configuring a mapping

One `FabricIngestConfig` row per workspace holds a list of mappings:

| Field | Meaning |
|-------|---------|
| `collection` | The Firestore collection path to mirror. |
| `object_type_id` | The Fabric object type mirrored documents become. |
| `field_map` | Firestore field name → Fabric property name. Unmapped fields are dropped. |
| `cursor_field` | The document field used as the incremental high-water mark. |
| `link_rules` | Optional. Each rule reads `via_field` off the document and links the new object to another mirrored object (`to_type`) found at that path, with `link_type`. |

The mapping is validated at entry — a blank collection, a blank object type, or
a link rule missing a field is rejected before any Firestore read, so a bad
config fails loudly instead of mirroring nothing.

### Operational notes

- Gated by `POCKETPAW_CLOUD_SCHEDULER_ENABLED=true`, the same gate the other
  cloud sweeps use, so tests never spawn a background loop. Override the cadence
  with `POCKETPAW_FABRIC_INGEST_INTERVAL_SECONDS`.
- The Firestore client is an **optional** dependency
  (`pocketpaw-ee[firestore]`). A deployment that doesn't mirror Firestore never
  installs it; the worker raises a clear install error if a config references
  Firestore without the extra present.
- Credentials resolve through Google Application Default Credentials — the
  worker holds no secrets of its own.
- Writes commit one object per row in v1. Batching is a known follow-up.

## Senses — provider-agnostic capabilities above connectors

> **Glossary — Sense.** A provider-agnostic capability that sits one layer above
> connectors. Templates and agents address a Sense (e.g. `paw.email.v1`,
> `paw.code.v1`) instead of a specific connector; the resolver binds the Sense
> to whichever connector the tenant enabled for that capability in the current
> workspace. A Sense is the *what* (email, calendar, code); a connector is the
> *how* (Gmail, Google Calendar, GitHub). This is the anti-fragmentation rule:
> the core Sense vocabulary is closed and curated, so one template works across
> every tenant regardless of which providers they connected.

A **Sense** is a capability addressed by what it *does*, not by which vendor
provides it. `paw.email.v1` means "email" regardless of whether the tenant
enabled Gmail, Outlook, or anything else; `paw.code.v1` means
"repos/issues/PRs" whether that's GitHub or GitLab. Templates and agents
address a Sense, and the **resolver** binds it to whichever connector the
tenant actually enabled in this workspace. This keeps templates portable: a
"weekly digest" template asks for `paw.email.v1` and works for every tenant
without hard-coding a provider.

Two MCP tools on the same `pocketpaw_connectors` server expose Senses to the
chat agent:

| Tool | What it does |
|------|--------------|
| `list_senses()` | Lists the capabilities (Senses) that resolve to a connector in the **current pocket** — each with the bound `connector`, whether the choice was `ambiguous` (more than one provider, no preference set), and the `candidates`. No arguments; the pocket comes from the active chat. |
| `sense_execute(sense, action, params)` | Runs ONE READ action against a Sense **without naming the connector** — the resolver picks the provider. Read (auto-trust) actions execute; writes are refused. |

**Sense vs connector.** Use `connector_execute` when you already know the
provider (the user said "GitHub" or "Gmail"); use `sense_execute` when the
user asks by capability ("check my email", "list my open PRs") and you want
the resolver to pick the enabled provider. `sense_execute` enforces the same
read-first policy as `connector_execute` — the resolver only runs `auto`-trust
actions; `confirm` / `restricted` (write-shaped) actions are refused with a
"needs approval" message and never executed. When no enabled connector fills
the sense, `sense_execute` returns a clear "no provider" error so the agent
can prompt the user to connect one.

```
list_senses()
  → [{"sense": "paw.email.v1", "connector": "gmail", "ambiguous": false, ...},
     {"sense": "paw.code.v1",  "connector": "github", ...}]

sense_execute("paw.email.v1", "gmail_search",
             {"query": "from:acme.com subject:invoice"})
  → runs against the resolved connector (gmail), returns matching stubs

sense_execute("paw.email.v1", "gmail_send", {...})
  → refused: "needs approval … not executed in v1 (read-first)."
```

## Using with Existing Integrations

PocketPaw already has built-in integrations for Google Workspace, Spotify, and Reddit. These work as **agent tools** (one-off actions via chat). Connectors add **continuous data sync** on top:

| Integration | As Tool (built-in) | As Connector (YAML) |
|-------------|-------------------|---------------------|
| Gmail | "Search my emails for invoices" → one-off result | Sync inbox every 15m → `gmail_messages` table → Pocket widget |
| Google Calendar | "Create a meeting tomorrow" → done | Sync events daily → `calendar_events` table → schedule widget |
| Stripe | (not built-in yet) | Sync invoices → `stripe_invoices` table → revenue dashboard |
| CSV | (not built-in yet) | Import file → custom table → data visualization |

Tools and connectors complement each other. Tools are for actions. Connectors are for data.

## Built-in Connectors

| Connector | File | Auth | Syncs |
|-----------|------|------|-------|
| **Stripe** | `connectors/stripe.yaml` | API key | Invoices, customers |
| **CSV Import** | `connectors/csv.yaml` | None | Any CSV/Excel file |
| **REST API** | `connectors/rest_generic.yaml` | Bearer token | Any REST endpoint |

## Architecture

```
ConnectorProtocol (Python async interface)
│
├── DirectRESTAdapter     ← YAML-defined REST APIs (primary)
├── ComposioAdapter       ← 250+ apps with managed OAuth (planned)
└── CuratedMCPAdapter     ← Whitelisted MCP servers (planned)
```

The `ConnectorRegistry` auto-discovers YAML files from the `connectors/` directory and manages adapter instances per pocket.

## Adding a New Connector

1. Create `connectors/your_service.yaml` following the schema above
2. Test it: `paw connect your_service --pocket "Test"`
3. The agent can now use it: "Connect my Shopify to this pocket"

That's it. No Python code needed — just YAML.

## Security

- Credentials are never stored in YAML files or pocket.db
- Auth tokens flow through the credential store (Infisical planned)
- Each pocket has isolated connector access
- Trust levels enforce human oversight for write operations
- All connector actions are logged to the audit trail
