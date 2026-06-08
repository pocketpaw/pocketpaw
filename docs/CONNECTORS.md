<!--
  Connectors documentation.
  Updated: 2026-06-07 (M3 connector→skill auto-authoring) — documented the
  optional ``surface_profile`` YAML block and the connector→skill/tool
  auto-authoring path (derivation at bind/unbind from the full enabled set, the
  Gmail reference, and the coexistence rule with hand-set ripple_mode /
  system_message_override).
  Updated: 2026-06-08 (M3 v2 — create-time derivation) — documented the second
  surface_profile derivation trigger: deriving a conservative default at pocket
  CREATE from type / pattern, the (intentionally empty) mapping table, and that
  explicit caller profiles win + connector-bind re-derivation composes on top.
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

## Writing a Connector YAML

Each connector is a YAML file in `connectors/`. Here's the structure:

```yaml
# connectors/my_service.yaml
name: my_service
display_name: My Service
type: payment                     # category for grouping
icon: credit-card                 # lucide icon name

auth:
  method: api_key                 # api_key | oauth | basic | bearer | none
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

surface_profile:                  # OPTIONAL — connector→skill/tool auto-authoring
  skill: my_service               # a skill to load in rooms with this connector
  allow_tools: []                 # tool-id patterns to add to the SDK allowlist
  deny_tools: []                  # tool-id patterns to deny
```

The `surface_profile` block is optional. Connectors without it parse and behave
exactly as before. See [Connector → Skill / Tool auto-authoring](#connector--skill--tool-auto-authoring) below.

## Auth Methods

| Method | When to Use | Example |
|--------|-------------|---------|
| `api_key` | Service provides a static API key | Stripe, Tavily |
| `oauth` | Service uses OAuth 2.0 flow | Google, Spotify |
| `bearer` | Token-based auth (API key in Authorization header) | Generic REST APIs |
| `basic` | Username + password auth | Legacy APIs |
| `none` | Public API, no auth needed | Reddit (read-only) |

## Trust Levels

Each action has a trust level that controls how much human oversight the agent needs:

| Level | Behavior | Use For |
|-------|----------|---------|
| `auto` | Agent executes without asking | Read-only operations (list, search) |
| `confirm` | Agent asks user before executing | Write operations (create, update, delete) |
| `restricted` | Requires admin approval | Destructive or financial operations |

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

### Create-time derivation (the second trigger)

Connector bind/unbind is one of **two** triggers that can author a pocket's
`surface_profile`. The other fires at **pocket create**: when a pocket is created
*without* an explicit `surface_profile`, PocketPaw derives a conservative default
from the pocket's `type` / `pattern` before persisting it.

The mapping lives in a small, documented table
(`pockets/create_profile_defaults.py`, `derive_create_time_profile(type, pattern)`).
It is **intentionally conservative — empty today** — so every create currently
returns no override and inherits the surface-kind default. That is a deliberate
zero-regression stance, not an oversight:

- **`type="site"` / `pattern="landing"` (marketing landing pages)** get **no
  create-time override**. The surface a site is chatted on already resolves the
  right profile: the `/sites` ripple-create and refine modes, and the
  `/pockets/[id]` view, all default to `ripple_mode="on"`. Only the *svelte*
  create mode turns ripple off, and that is keyed on a per-turn `engine="svelte"`
  signal — a property of the chat turn, not of the pocket. Stamping
  `ripple_mode="on"` here would be a redundant no-op; stamping `"off"` would be
  wrong (ripple-track sites genuinely author a ripple spec). So the surface
  default fully covers sites and the table does **not** duplicate it.
- **Anything with no clear, safe default** returns `None` and inherits the
  surface-kind default.

The mechanism is fully wired and tested so a future product policy can add a row
to the table without re-plumbing. To add one, append a
`((type, pattern), factory)` rule — a `None` on either side of the key is a
wildcard, and the first matching rule wins (list most-specific first).

**Precedence.** An **explicit caller `surface_profile` always wins** — create-time
derivation only runs when the caller didn't supply one, and never overrides an
explicit value. The **connector-bind re-derivation (v1) composes on top**: it owns
the connector dimensions (`skill_names` / `allowed_sdk_tools` / `deny_mcp_tool_ids`)
and preserves the user-owned `ripple_mode` / `system_message_override`, so whatever
create-time derivation stamps for those preserved dims survives a later bind/unbind.

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
