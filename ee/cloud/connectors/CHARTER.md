# Connectors — Phase 1 Charter

**Status:** draft, 2026-05-03
**Owner:** crew
**Captain sign-off pending**

This charter locks the boundaries for the connector layer **before** any
extraction. It exists so we don't lock the abstraction wrong on the way to
spinning out `paw-connectors/` as a workspace sibling later.

Phase 1 = consolidate the four scattered layers behind one protocol, land
the tenanted-state entity at `ee/cloud/connectors/`, ship without changing
the public agent-tool surface. **No package extraction in Phase 1.** That's
Phase 2, planned for ~3–4 weeks after Phase 1 ships.

---

## 1 — The problem

Connectors live in four places today, and three of them duplicate work:

| Layer | Path | Purpose |
|---|---|---|
| YAML specs | `pocketpaw/connectors/*.yaml` | Declarative — 30 connectors, drives KB ingestion |
| YAML runtime + native adapters | `src/pocketpaw/connectors/{protocol,registry,yaml_engine,db_adapter,firebase_adapter,gcp_adapter,mongo_adapter,drive/}.py` | `ConnectorProtocol`, registry, `DirectRESTAdapter` |
| Direct integrations | `src/pocketpaw/clients/{gmail,gcalendar,gdocs,gdrive,oauth,token_store,reddit,spotify}.py` | HTTP clients for Google Workspace + Reddit + Spotify |
| Agent tools | `src/pocketpaw/tools/builtin/{gmail,calendar,gdocs,gdrive}.py` | LLM-facing wrappers over the direct integrations |

Drive lives in **all four** layers. Gmail lives in three (no YAML).
Stripe lives in only YAML. The four layers don't share an interface, so
every consumer (KB / agent tools / home widgets / automations) reaches
into a different one.

Five consumers exist today or are imminent:

- KB ingestion (Files-as-Knowledge)
- Agent tools (LLM tool calls)
- Home widgets (`paw-enterprise` AddWidgetPicker, generated 2026-05-02)
- Automations / scheduled routines
- Soul memory (e.g. "remember inbox this morning")

Each one currently couples to a different layer. That's the smell.

## 2 — Phase 1 goal

**Every connector — YAML-driven or native Python — implements one protocol.
Every consumer reads from one registry.**

After Phase 1:

- Gmail / Calendar / Docs / Drive / Reddit / Spotify register as
  `Connector` instances alongside the YAML-defined ones.
- The home widget picker, the agent tool generator, the KB ingest router,
  and the automations engine all consume the same registry.
- Per-workspace state (which connectors are enabled, OAuth tokens, sync
  status) lives in a tenanted `ee/cloud/connectors/` entity following the
  4-file shape.
- The legacy `tools/builtin/{gmail,calendar,gdocs,gdrive}.py` tool classes
  remain as thin generators on top of the new registry — no breaking
  changes to the agent's tool surface.

## 3 — Boundary

### In scope (Phase 1)

1. **Unified `Connector` protocol** in `src/pocketpaw/connectors/protocol.py`.
   Builds on the existing `ConnectorProtocol` + `IngestAdapter` already
   defined there.
2. **Registry expansion** so the existing YAML registry also discovers
   native Python connectors (Gmail, Calendar, Docs, Drive, etc.).
3. **`Connector.widgets()`** — new capability. Each connector exposes a
   list of default home-page widget recipes (display type + title + data
   action). Drives AddWidgetPicker's "From your pockets" + "From
   connectors" sections.
4. **Tenanted state at `ee/cloud/connectors/`** — 4-file shape
   (`domain.py / dto.py / service.py / router.py`) tracking which
   connectors a workspace has enabled, per-workspace OAuth tokens
   (referenced not stored — token bytes stay in `clients/token_store.py`),
   sync status, and per-connector config.
5. **Consumer adapters** — thin functions that map registry entries to
   each consumer's interface:
   - `tools.builtin.connector_tools_for(c) -> list[BaseTool]`
   - `kb.sources.connector_source_for(c) -> SourceAdapter`
   - `home.widget_recipes_for(c) -> list[WidgetRecipe]`
   - `automations.connector_step_for(c) -> RoutineStep`
6. **OAuth consolidation** — the existing `clients/oauth.py` +
   `token_store.py` become the single OAuth surface for all connectors,
   regardless of layer of origin.
7. **`/api/v1/cloud/connectors` REST router** in `ee/cloud/connectors/router.py`.
   Lives under `/cloud/` to coexist with the legacy pocket-scoped routes
   at `/api/v1/connectors` (`connect / disconnect / execute / status`,
   used by `PocketDataPanel`). Frontend home-widget integration calls
   the new `/cloud/connectors` path; `PocketDataPanel` keeps using the
   legacy path. Long-term, when PR-2 lands `Connector.widgets()` and
   we migrate `PocketDataPanel` to consume the cloud entity directly,
   the legacy path retires.

### Out of scope (defer to Phase 2 / 3)

- Extracting connectors to a sibling package. **Phase 2.**
- `Connector` schema versioning / breaking-change policy. **Phase 2.**
- Public OSS contribution model + governance. **Phase 3.**
- ComposioAdapter, CuratedMCPAdapter (mentioned in the existing protocol
  docstring) — protocol contract should accommodate them, but
  implementations come later.
- Connector-level rate limiting, circuit breaking, retries. The
  `core/runtime` retry/circuit breaker layer (per Linear backlog notes
  on infra PR review patterns) handles that.
- **`ExecutionMode.SANDBOX`** — ephemeral container per invocation with
  workspace-scoped service-account creds. Needed for prod database
  CLIs (`psql`, `mongosh`) and untrusted code execution. Defer until a
  Nerve client genuinely needs DB-CLI widgets running 24/7. Phase 1
  ships only `cloud` and `local` modes. The protocol enum carries
  `SANDBOX` from day one so a future PR is purely runtime, not a
  schema change.
- New connectors. Phase 1 ships zero net-new connectors; we only unify
  what exists.

### Outside the connector layer (always)

- Tenanted Mongo entities outside `ee/cloud/connectors/`. Connectors
  themselves are stateless.
- KB ingestion logic, agent reasoning, widget rendering. The connector
  layer exposes capabilities; consumers own their behaviour.
- Soul memory writes. Connectors expose data; the soul layer decides
  what to remember.

## 4 — Protocol shape (locked for Phase 1)

The existing `ConnectorProtocol` in
`src/pocketpaw/connectors/protocol.py` is the starting point and is
mostly correct. Phase 1 adds `widgets()`, `health()`, `ConnectorScope`,
and **`ExecutionMode`** on the action schema. The `ExecutionMode` axis
exists because CLI-based connectors (`firebase`, `gcp`, `gh`, `kubectl`,
…) cannot execute in the cloud's FastAPI process — there's no clean way
to multi-tenant per-workspace `gcloud` configs on a shared host.

```python
class Connector(Protocol):
    name: str                            # "gmail"
    display_name: str                    # "Gmail"
    auth: AuthSpec                       # bearer | oauth2 | api_key | none
    actions: list[ActionSchema]          # search, read, send, list, …
    widgets: list[WidgetRecipe]          # NEW — default home widgets

    async def connect(self, scope: ConnectorScope, config: dict) -> ConnectionResult: ...
    async def disconnect(self, scope: ConnectorScope) -> bool: ...
    async def execute(self, action: str, params: dict, scope: ConnectorScope) -> ActionResult: ...
    async def sync(self, scope: ConnectorScope) -> SyncResult: ...
    async def schema(self) -> dict: ...
    async def health(self, scope: ConnectorScope) -> ConnectorHealth: ...
```

`ActionSchema` gains two fields so the runtime knows where each action
is allowed to execute:

```python
class ExecutionMode(StrEnum):
    CLOUD = "cloud"        # runs in the FastAPI process (REST APIs, in-process logic)
    LOCAL = "local"        # runs in the user's pocketpaw runtime (CLI tools)
    SANDBOX = "sandbox"    # ephemeral container per call (deferred — see Out of scope)


class ActionSchema:
    name: str
    description: str
    method: str
    params: list[ParamSpec]
    trust_level: TrustLevel
    execution_mode: ExecutionMode = ExecutionMode.CLOUD   # NEW
    requires_binary: str | None = None                    # NEW — "gcloud", "firebase", "gh", …
```

Notes on the change from today's protocol:

1. **`pocket_id: str` → `scope: ConnectorScope`.** Scope is one of
   `pocket | workspace | user`. The current API is pocket-only, but home
   widgets and email/calendar are workspace-scoped (sometimes user-scoped).
   `ConnectorScope` is a tagged union the runtime resolves to the right
   token + ACL set.
2. **`widgets()` is new.** Each connector returns a list of pre-baked
   widget recipes — `("Inbox", display="feed", action="gmail.search",
   params={"q":"is:unread", "max":10})`. AddWidgetPicker reads them
   directly.
3. **`health()` is new.** The frontend ConnectorPanel shows
   connected / disconnected / errored. Today that's inferred from the
   last execute() — fragile. Explicit health check keeps the UI honest.
4. **`IngestAdapter.permissions()` stays as-is.** It's already correct
   for Fabric scope inheritance.
5. **`ExecutionMode` + `requires_binary` are new on `ActionSchema`.**
   YAML connectors default to `cloud` (no behaviour change). CLI
   adapters (`firebase`, `gcp`) declare `local` per action with the
   binary they shell out to. The cloud router refuses to execute
   `local` actions in-process — it forwards them to the local-agent
   bus instead (see §6.2 below).

`AuthSpec`, `ActionResult`, `ConnectionResult`, `SyncResult`,
`IngestACL` exist already and are reused without change.

## 5 — Layers in their final shape

```
src/pocketpaw/connectors/                  ← runtime (stateless)
├── protocol.py                            Connector + ConnectorScope + WidgetRecipe + AuthSpec
├── registry.py                            discovers YAML specs + native adapters
├── yaml_engine.py                         compiles YAML → DirectRESTAdapter
├── oauth.py                               (moved from integrations/)
├── token_store.py                         (moved from integrations/)
├── adapters/
│   ├── gmail.py                           (moved from integrations/gmail.py + tools/builtin/gmail.py)
│   ├── gcalendar.py                       (moved from integrations/gcalendar.py + tools/builtin/calendar.py)
│   ├── gdocs.py                           (moved from integrations/gdocs.py + tools/builtin/gdocs.py)
│   ├── gdrive.py                          (moved from integrations/gdrive.py + tools/builtin/gdrive.py)
│   ├── reddit.py
│   ├── spotify.py
│   ├── db.py                              (was db_adapter.py)
│   ├── firebase.py                        (was firebase_adapter.py)
│   ├── gcp.py                             (was gcp_adapter.py)
│   └── mongo.py                           (was mongo_adapter.py)
└── specs/                                  (moved from pocketpaw/connectors/*.yaml)
    └── *.yaml

ee/cloud/connectors/                       ← tenanted state (Mongo via Beanie)
├── domain.py                              WorkspaceConnector value object — frozen, requires workspace_id
├── dto.py                                 EnableConnectorRequest / ConnectorResponse / SyncStatusResponse
├── service.py                             enable / disable / list / set_config / record_sync
└── router.py                              GET/POST/PATCH/DELETE /api/v1/connectors

src/pocketpaw/tools/builtin/__init__.py   ← shrinks
                                            connector_tools_for(c) generates BaseTool instances
                                            from c.actions; replaces the per-tool .py files
```

## 6 — Consumer adapters (no behaviour change)

Each consumer ships a small adapter so it reads from the registry instead
of one of the four layers.

### Agent tools

```python
# src/pocketpaw/tools/builtin/__init__.py (replacement for the per-tool registry dict)
def connector_tools_for(c: Connector) -> list[BaseTool]:
    return [_action_to_tool(c, action) for action in c.actions]

def all_builtin_tools(registry: ConnectorRegistry) -> list[BaseTool]:
    return [t for c in registry.list() for t in connector_tools_for(c)]
```

The 18 hand-written Google Workspace tool classes get deleted in favour
of generated ones with identical names + descriptions (named tests pin
them).

### KB ingestion

```python
# ee/cloud/uploads/listeners.py + kb/source_adapters.py
async def kb_source_for(c: Connector, scope: ConnectorScope) -> SourceAdapter: ...
```

### Home widgets

```python
# ee/cloud/connectors/service.py
async def list_widget_recipes(ctx: RequestContext) -> list[WidgetRecipe]:
    enabled = await list_enabled_connectors(ctx)
    return [recipe for c in enabled for recipe in c.widgets]
```

The frontend AddWidgetPicker calls `GET /api/v1/connectors/widget-recipes`
to populate the new "From connectors" section alongside today's "From
your pockets" + generative paths.

### Automations

```python
# ee/cloud/automations/runtime.py
async def connector_step(c: Connector, action: str, params: dict, scope: ConnectorScope) -> StepResult: ...
```

### 6.2 — CLI connectors and the local-agent bus

`firebase`, `gcp`, `gh`, `kubectl`, and any future CLI-driven connectors
cannot execute on the cloud's FastAPI host — there's no way to multi-tenant
per-workspace `gcloud` configs cleanly on a shared server. Their
`ActionSchema.execution_mode` is `local`, and execution flows through the
user's already-running pocketpaw runtime via the existing chat WebSocket
bus.

**Path for a local action:**

```
Frontend  →  POST /api/v1/cloud/connectors/{name}/execute
                  {action, params, scope, request_id}
                       ↓
          ee/cloud/connectors/service.execute()
                       ↓
      inspects action.execution_mode
       ┌──────────┴──────────┐
   cloud                    local
     ↓                        ↓
  adapter.execute()      publish "connector.exec.requested"
  in-process             onto chat WS to (workspace_id, user_id)
  return result            ↓
                         user's local pocketpaw runtime
                         picks up, runs gcloud/firebase
                         publishes "connector.exec.completed"
                           {request_id, result}
                           ↓
                         service correlates by request_id,
                         resolves the awaiting future,
                         returns 200 + result to caller
```

**Bus topics added (Phase 1):**

- `connector.exec.requested` — cloud → local-agent
  `{request_id, workspace_id, connector, action, params, scope}`
- `connector.exec.completed` — local-agent → cloud
  `{request_id, success, result?, error?}`

The transport reuses the chat WebSocket (`InProcessBus` + the existing
client→server channel `ChatPill` already has). No new socket, no new
auth — the agent that runs the user's chat IS the agent that runs the
CLI. One persistent connection, one identity.

**Failure modes:**

- *User offline / runtime not running.* Cloud router times out at 30s,
  returns `503 connector.local_agent_unavailable`. Frontend shows
  "Open your local PocketPaw to run this widget."
- *Binary not installed on user's machine.* Local agent introspects
  `action.requires_binary`, fails fast with
  `connector.binary_missing`. Frontend shows "Install `gcloud` to use
  Firebase widgets" with a link to setup docs.
- *Cloud service is offline.* Local actions don't fire because they
  originate from the cloud router. Read-only home widgets fall back
  to last-known cached values (cached `last_sync_status` already in
  the entity).

**What the local agent needs (PR-9 deliverable):**

Listener bound at boot in `pocketpaw/runtime/connector_bus.py` that:

1. Subscribes to `connector.exec.requested` for messages addressed to
   the local user_id.
2. Looks up the connector adapter from the local registry.
3. Calls `adapter.execute(action, params, scope)` on the running host
   (the user's machine has gcloud/firebase configured).
4. Publishes `connector.exec.completed` with the result.

This wiring already mirrors how chat messages flow today — the streaming
chunks come in over the same WS, just on a different topic.

## 7 — OAuth + token store

The existing `clients/oauth.py` + `clients/token_store.py`
become the single OAuth surface, moved to
`src/pocketpaw/connectors/{oauth,token_store}.py`. No protocol change.

Per-workspace token references (not the token bytes) are persisted via
`ee/cloud/connectors/service.py` so the cloud knows which workspaces
have which connectors authed. Token bytes stay in the local token store
for now — moving them to Mongo is part of Cluster H (audit persistence)
and out of scope here.

## 8 — Migration

Touch-time, not big-bang. Per workspace CLAUDE.md rule on entity
migration: when a PR touches any connector-layer file, that file moves
to the new shape in the same PR. We don't ship a 30-PR sweep.

Migration order, by risk:

1. **Charter merged** (this doc). No code.
2. **`ee/cloud/connectors/` entity** with empty registry-backing service +
   `GET /api/v1/connectors` returning the existing YAML registry's list.
   Frontend ConnectorPanel keeps working, new endpoint replaces old.
3. **`Connector.widgets()` + `health()`** added to the protocol.
   `DirectRESTAdapter` provides a default empty list for both. The home
   widget picker reads `GET /api/v1/connectors/widget-recipes` and
   continues to fall back to the mock POCKET_MAP if the registry is
   empty.
4. **Gmail adopts the protocol** — first integration to migrate.
   `connector_tools_for(gmail)` generates the existing 8 BaseTool classes.
   Snapshot tests pin tool names + descriptions. The home widget picker
   shows real "Inbox / Important / Stats" recipes.
5. **Calendar / Docs / Drive / Reddit / Spotify** in subsequent PRs,
   one per session. Same pattern as Gmail.
6. **Drive directory subpackage merge** — `src/pocketpaw/connectors/drive/`
   folds into `adapters/gdrive.py` (or stays if it's substantially bigger).
7. **Native adapters renamed** — `db_adapter.py` → `adapters/db.py`,
   `firebase_adapter.py` → `adapters/firebase.py`, etc. Each declares
   `execution_mode=local` on its actions and includes
   `requires_binary` (`firebase` / `gcloud`).
8. **Local-agent bus listener** — `pocketpaw/runtime/connector_bus.py`
   lands in the runtime. Subscribes to `connector.exec.requested`,
   runs CLI adapters against the user's machine, publishes
   `connector.exec.completed`. The cloud router's
   `local`-mode dispatch becomes operational.
9. **Phase 1 sealed.** Protocol stable, all consumers reading from one
   registry, no behaviour change to the agent or to existing UIs.

Each migration step is a single PR. Captain reviews before merge.

## 9 — Open questions

1. **Scope semantics.** Today's protocol is pocket-scoped. Home widgets
   are workspace-scoped. Email/calendar are arguably user-scoped (each
   member has their own inbox). Does `ConnectorScope` need three discrete
   values, or do we always resolve up the chain (user → workspace → pocket)
   and let the connector decide? **Captain's call before §4 is locked.**
2. **Where does fleet-template auth live?** The fleet system already
   bundles connectors with templates. After Phase 1, do fleets reference
   connectors by name + scope, or carry their own connector overrides?
3. **OAuth broker centralization.** There's an existing credential broker
   pattern in the YAML connectors. Does it move into the new
   `connectors/oauth.py`, or stay as-is and just be called by adapters?
4. **What happens to `pocketpaw/connectors/*.yaml` (workspace root)?** Move
   into `src/pocketpaw/connectors/specs/` or keep them at the package root
   for OSS contribution surface? Phase 2 has a strong opinion (move),
   Phase 1 is neutral.
5. **Should `Connector.widgets()` return Ripple UISpec directly or a
   higher-level `WidgetRecipe` that compiles to UISpec at render time?**
   Recipe gives us a stable contract that survives Ripple version bumps.
   Strong vote for Recipe.

### Resolved by captain (2026-05-03)

- **Local mode requires the user's pocketpaw runtime to be online.**
  Same constraint Slack/iMessage live with. CLI widgets show a
  "needs local agent" banner when the agent isn't connected.
- **`SANDBOX` execution mode deferred.** Ships when a Nerve client
  needs DB-CLI widgets running 24/7. Enum carries `SANDBOX` from day
  one so the future PR is runtime-only, not a schema change.
- **Local-agent bus reuses the existing chat WebSocket.** Two new
  topics (`connector.exec.requested` / `connector.exec.completed`),
  one persistent socket, one identity. No new transport.

## 10 — Phase 2 preview (informational, not committed)

After Phase 1 stabilizes (~3–4 weeks live, all five consumers wired,
no protocol churn), we extract:

```
paw-workspace/
├── pocketpaw/                           runtime, depends on paw-connectors
├── paw-enterprise/                      consumes via REST router
├── soul-protocol/                       can also depend on paw-connectors
├── paw-connectors/   ← NEW SIBLING
│   ├── pyproject.toml
│   ├── src/paw_connectors/             (← src/pocketpaw/connectors/* moves here)
│   ├── tests/
│   ├── cli.py                           connectors list / test / oauth / health
│   └── docs/
└── ripple-svelte/
```

`ee/cloud/connectors/` stays in pocketpaw (it's tenanted Mongo state).

Phase 3 (informational): OSS the registry. Public spec, contribution
guide, scope hygiene rules. Strategic surface area = "30+ first-party
connectors, community-extensible, agent-native."

## 11 — Phase 1 acceptance criteria

Phase 1 is done when **all six** are true:

1. Every existing connector — Gmail, Calendar, Docs, Drive, Reddit, Spotify,
   30 YAML — implements `Connector`. Registry returns them all.
2. The home widget picker reads `GET /api/v1/cloud/connectors/widget-recipes`
   and shows real recipes from at least three connectors (Gmail target).
3. The agent's tool list generates from `connector_tools_for(c)`. Snapshot
   tests pin the existing 18 Google tool names + descriptions.
4. ConnectorPanel.svelte's `getConnectors()` reads from
   `GET /api/v1/cloud/connectors` (cloud router) for workspace-level
   state. `PocketDataPanel` keeps reading the legacy `/api/v1/connectors`
   path for pocket-scoped operations until its own migration PR.
5. `pocketpaw/clients/` is empty (or holds only `oauth_integrations`
   the API endpoint). The body has moved into `connectors/adapters/`.
6. **CLI connectors (firebase, gcp) execute via the local-agent bus.**
   Cloud router refuses `local`-mode actions in-process and forwards
   them to `connector.exec.requested`. The local pocketpaw runtime's
   `connector_bus.py` listener completes them. End-to-end test pins
   the round-trip.

When these six hold for two weeks with zero hotfixes, Phase 2
extraction can start.

---

## Appendix A — Why we're not extracting in Phase 1

1. The current four-layer mess has real coupling to pocketpaw internals
   (`BaseTool`, `OAuthManager`, `Settings`, `pocket_id` everywhere). Extracting
   first means eating two weeks of "unbreak imports" with nothing to show.
2. We don't yet know what `Connector.widgets()` needs from the home
   widget consumer because we haven't built that consumer yet. Lock the
   protocol after the consumer is real, not before.
3. The 4-file ee/cloud rule applies only to tenanted state. Connector
   implementations are declarative — they don't fit. Pulling them out
   without first defining the protocol risks copying the wrong pattern.

## Appendix B — Files this charter implies will move (Phase 1)

- `src/pocketpaw/clients/gmail.py` → `src/pocketpaw/connectors/adapters/gmail.py`
- `src/pocketpaw/clients/gcalendar.py` → `src/pocketpaw/connectors/adapters/gcalendar.py`
- `src/pocketpaw/clients/gdocs.py` → `src/pocketpaw/connectors/adapters/gdocs.py`
- `src/pocketpaw/clients/gdrive.py` → `src/pocketpaw/connectors/adapters/gdrive.py`
- `src/pocketpaw/clients/reddit.py` → `src/pocketpaw/connectors/adapters/reddit.py`
- `src/pocketpaw/clients/spotify.py` → `src/pocketpaw/connectors/adapters/spotify.py`
- `src/pocketpaw/clients/oauth.py` → `src/pocketpaw/connectors/oauth.py`
- `src/pocketpaw/clients/token_store.py` → `src/pocketpaw/connectors/token_store.py`
- `src/pocketpaw/connectors/db_adapter.py` → `src/pocketpaw/connectors/adapters/db.py`
- `src/pocketpaw/connectors/firebase_adapter.py` → `src/pocketpaw/connectors/adapters/firebase.py`
- `src/pocketpaw/connectors/gcp_adapter.py` → `src/pocketpaw/connectors/adapters/gcp.py`
- `src/pocketpaw/connectors/mongo_adapter.py` → `src/pocketpaw/connectors/adapters/mongo.py`
- `src/pocketpaw/tools/builtin/{gmail,calendar,gdocs,gdrive}.py` → deleted, generated by `connector_tools_for(c)`
- `pocketpaw/connectors/*.yaml` (workspace root) → **stays** for Phase 1; moves in Phase 2.

## Appendix C — Files created by Phase 1

- `pocketpaw/ee/cloud/connectors/CHARTER.md` (this doc)
- `pocketpaw/ee/cloud/connectors/__init__.py`
- `pocketpaw/ee/cloud/connectors/domain.py`
- `pocketpaw/ee/cloud/connectors/dto.py`
- `pocketpaw/ee/cloud/connectors/service.py`
- `pocketpaw/ee/cloud/connectors/router.py`
- `pocketpaw/src/pocketpaw/connectors/adapters/__init__.py` + the moved adapter files
- New tests under `pocketpaw/tests/connectors/`

No new dependencies. No schema migrations beyond the new
`WorkspaceConnectorDocument` Beanie model.

---

**Action requested:** captain reviews §4 (protocol) and §9 (open questions),
signs off, then PR-1 of the migration begins (the empty `ee/cloud/connectors/`
entity + new REST router + frontend ConnectorPanel pointed at it).
