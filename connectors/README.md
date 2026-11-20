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
```

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
