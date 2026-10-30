# Surface Context — Architecture

Created: 2026-05-24
Status: Living document — module is shipped and active.

The `surface_context` module gives the cloud chat agent a per-turn preamble describing which paw-enterprise surface the user is currently on — `home`, `pocket`, `mission_control`, `files`, and the rest — plus a live snapshot of what's pinned or visible there. The chat router resolves the preamble once per send and prepends it to the dynamic context block, ahead of the legacy `<scope>` / `<participants>` / `<current-pocket>` tags. The module ships in PR [#1209](https://github.com/pocketpaw/pocketpaw/pull/1209), paired with paw-enterprise PR [#250](https://github.com/pocketpaw/paw-enterprise/pull/250) which stamps the client hint on every chat send.

---

## 1. Problem Statement

Before this module, the cloud chat agent received three lines of dynamic context per turn — `<scope>`, `<participants>`, and (when applicable) `<current-pocket>`. That was the entire window into "where is the user right now?" Everything else — what surface they're on, what's pinned on that surface, what live data is already visible, what tools are available — the agent had to guess or rediscover with tool calls.

Three concrete failure modes followed from the gap:

- **Tool-call discovery overhead.** The agent re-read state every turn because it had no per-turn snapshot. A user asking "what's on my home page?" produced a sequence of read calls when the answer was already on-screen.
- **Mis-classified intent.** With no `surface` field, the agent could not tell whether the user was on `/` (home dashboard) or `/pockets/[id]` (pocket detail). It would route widget-creation requests through `pocket_specialist` even when the user was on home and wanted a home-pocket widget added directly.
- **Questions the user could see the answer to.** "Which widgets do you have pinned?" / "What's the name of this pocket?" — both visible on screen, both invisible to the agent.

The surface preamble closes the gap by giving the agent a server-rendered snapshot keyed off a tiny client hint.

---

## 2. Goals and Non-Goals

### Goals

- **Per-turn preamble** — resolved on every chat send so the agent always sees the current surface state, not a stale snapshot.
- **Surface-agnostic** — every chat-bearing paw-enterprise route eventually gets a handler. The enum (`SurfaceKind`) is the contract; handlers fill in.
- **Tenant-safe** — workspace boundary enforced inside every handler. A cross-workspace `pocket_id` / `agent_id` stamp drops to the unavailable-snapshot path rather than leaking the other workspace's data.
- **Graceful** — handler failures never break the chat stream. The resolver absorbs every exception path and returns a `GENERIC` context with an empty preamble.
- **Wire-compatible** — clients that don't send a surface stamp (older builds, the OSS local dashboard) keep working unchanged. Missing `surface` defaults to `GENERIC`; missing `surface_meta` defaults to `{}`.
- **Cheap** — total preamble is capped at ~1500 chars per turn (see `_helpers.truncate_preamble`). No N+1 reads — every handler is a single workspace-scoped query or a small constant fan-out.

### Non-Goals

- **Not a replacement for the existing per-prompt tags** (`HOME_POCKET_PROMPT`, `INLINE_RIPPLE_SYSTEM_PROMPT`, the pocket-specialist preambles). Those describe behaviour; the surface preamble describes state. They coexist.
- **Not a tool the agent can call.** The preamble is server-injected and automatic. The agent cannot ask for "the home surface preamble" — it just receives one when the chat send lands.
- **Not coupled to any specific frontend framework.** The wire contract is `{surface: str | null, surface_meta: dict | null}`. Any client (Svelte today, mobile or terminal later) can stamp the same shape.
- **Not a write surface.** Every handler is read-only. State mutations stay on the existing entity services.

---

## 3. Architecture

```
┌──────────────────────────┐    POST /api/v1/cloud/chat/{scope}/{scope_id}/agent
│ paw-enterprise (client)  │    body: { content, surface, surface_meta, ... }
│                          │
│  src/lib/core/chat/      │
│  surface-context.ts      │    getCurrentSurface() merges $page.route.id
│  → getCurrentSurface()   │    + registered SurfaceMetaProvider plugins
└────────────┬─────────────┘
             │
             ▼
┌──────────────────────────┐
│ chat/agent_router.py     │    1. resolve_scope_context(...)
│  (POST handler)          │    2. resolve_surface_context(workspace_id, user_id,
│                          │         {"surface": body.surface,
│                          │          "meta": body.surface_meta or {}})
│                          │    3. ctx.surface_context = <SurfaceContext>
└────────────┬─────────────┘
             │
             ▼
┌──────────────────────────┐
│ surface/service.py       │    SurfaceRequest.model_validate(body or {})
│  resolve_surface_context │  → _resolve_kind(validated.surface) → SurfaceKind
│                          │  → _HANDLERS[kind](workspace_id, user_id, meta)
│                          │  → SurfaceContext(workspace_id, user_id, kind,
│                          │                   meta, preamble)
└────────────┬─────────────┘
             │
             ▼
┌──────────────────────────┐
│ chat/agent_service.py    │    build_dynamic_context(ctx) PREPENDS
│  build_dynamic_context   │    ctx.surface_context.preamble ahead of
│                          │    <scope> / <participants> / <current-pocket>.
└──────────────────────────┘
```

Step-by-step:

1. **Client stamp.** Paw-enterprise's `src/lib/core/chat/surface-context.ts:getCurrentSurface` derives `surface` from `$page.route.id` and merges meta hints from a `SurfaceMetaProvider` registry (ephemeral state like the widget-focus modal can register a provider). The stamp is attached to every send through the chat store.
2. **Router receives.** `ee/pocketpaw_ee/cloud/chat/agent_router.py` (the `POST /cloud/chat/{scope}/{scope_id}/agent` handler) decodes `CloudAgentChatRequest`, which gained `surface: str | None` and `surface_meta: dict | None` fields in this same PR — see `ee/pocketpaw_ee/cloud/chat/agent_schemas.py`.
3. **Scope resolves first.** The router calls `resolve_scope_context` so `workspace_id` and `user_id` are confirmed before the surface step runs.
4. **Surface resolves.** The router calls `resolve_surface_context(workspace_id, user_id, {"surface": body.surface, "meta": body.surface_meta or {}})`. The resolver validates the body, dispatches to the handler registered for the resolved `SurfaceKind`, wraps the rendered string in a `SurfaceContext`, and returns it. Failures collapse to a `GENERIC` context with empty preamble — the resolver never raises.
5. **Attach to ScopeContext.** `ctx.surface_context = <SurfaceContext>` (the field exists on `ScopeContext` in `chat/agent_service.py`).
6. **Prepend to dynamic context.** `build_dynamic_context(ctx)` checks `ctx.surface_context.preamble` and, if non-empty, prepends it to the per-turn block before `<scope>` / `<participants>` / `<current-pocket>`. This makes the surface snapshot the most information-dense thing the agent sees in dynamic context.

---

## 4. Module Shape

The module follows the 4-file shape from the cloud entity rules (`pocketPaw/CLAUDE.md`, "pocketpaw_ee/cloud Code Rules"), with handlers as a sub-package. There is no `router.py` — the surface is consumed in-process by `chat/agent_service`, not exposed over HTTP.

```
ee/pocketpaw_ee/cloud/surface/
    __init__.py        # Public re-exports: SurfaceContext, SurfaceKind,
                       # SurfaceMeta, resolve_surface_context.
    domain.py          # SurfaceKind enum, SurfaceMeta dataclass,
                       # SurfaceContext dataclass (frozen).
    dto.py             # SurfaceMetaRequest, SurfaceRequest — inbound
                       # Pydantic schemas.
    service.py         # resolve_surface_context — the dispatcher and
                       # handler registry.
    handlers/
        __init__.py
        _helpers.py    # truncate_preamble, composio_tool_names,
                       # format_widget_line — shared by every handler.
        home.py        # rich
        pocket.py      # rich, tenancy-guarded
        pocket_widget.py  # rich, tenancy-guarded (extends pocket.py)
        pockets_list.py   # rich
        mission_control.py  # rich
        files.py       # rich
        audit.py       # rich
        activity.py    # rich, workspace-scoped reader
        agents.py      # rich
        agent.py       # rich, tenancy-guarded
        knowledge.py   # minimal placeholder
        calendar.py    # minimal placeholder
        chat.py        # minimal — best-effort session count
        quickask.py    # minimal — overlay surface
        settings.py    # minimal — leaks-prevention
        sidepanel.py   # minimal — side-panel window
        generic.py     # fallback for unknown surface strings
```

### `domain.py`

Three value objects:

- **`SurfaceKind`** — `StrEnum` listing every chat-bearing surface. Today: `HOME`, `POCKETS_LIST`, `POCKET`, `POCKET_WIDGET`, `MISSION_CONTROL`, `FILES`, `AUDIT`, `ACTIVITY`, `AGENTS`, `AGENT`, `KNOWLEDGE`, `CALENDAR`, `CHAT`, `QUICKASK`, `SETTINGS`, `SIDEPANEL`, `GENERIC`. Unknown strings from the client fall back to `GENERIC` at the resolver — the enum is closed but the wire is open.
- **`SurfaceMeta`** — frozen dataclass of optional hints (`pocket_id`, `widget_id`, `focus_node_id`, `agent_id`, `file_id`, `route_path`). Every field optional. The stamp stays small; anything heavy is fetched server-side by the handler.
- **`SurfaceContext`** — the resolved value the chat path consumes. `workspace_id` and `user_id` are **required at construction** per the cloud entity rules' tenancy-at-construction contract. Constructing one without tenancy info is a type error. Carries `kind`, `meta`, and the rendered `preamble` string (`""` when the handler failed or had nothing meaningful to say).

### `dto.py`

Two Pydantic schemas:

- **`SurfaceMetaRequest`** — wire mirror of `SurfaceMeta`. Every field optional. Unknown fields are dropped by default Pydantic behaviour.
- **`SurfaceRequest`** — composite `{surface: str | None, meta: SurfaceMetaRequest}`. The `surface` field is `str | None` (not the `SurfaceKind` enum) on purpose — so a client can ship a new surface name before the backend ships the corresponding handler.

Validation runs inside `resolve_surface_context` via `SurfaceRequest.model_validate`, not at the chat router. That's one fewer validation layer to maintain on the hot path of the chat router and matches the "validate at entry to the service" rule from the cloud charter.

### `service.py`

Module-level async function `resolve_surface_context(workspace_id, user_id, body)`. Three contracts:

1. **Always returns a `SurfaceContext` — never raises.** Validation failures, unknown surface kinds, and handler exceptions all collapse to `GENERIC` with empty preamble. The chat send keeps going regardless.
2. **Dispatch table is lazy.** Handlers are imported on first call via `_load_handlers()` and cached on the module-level `_HANDLERS`. Import-time errors in any handler module surface as a clear failure rather than silently breaking every preamble.
3. **Failure isolation.** Each handler runs inside a `try/except Exception` block in the dispatcher. Exceptions log at `exception` level (so they're discoverable in logs) but downgrade to a `GENERIC` context for the caller.

### `handlers/`

Each handler exports a single async function:

```python
async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> str
```

Handlers are split between **rich** (read a real entity service and render a snapshot) and **minimal** (placeholder until the upstream entity exposes a list helper or the surface stabilises). One-liners:

| Handler | Mode | One-line summary |
|---|---|---|
| `home.py` | rich | Pinned widgets on the home pocket, snapshot count, available data tools (WebSearch / WebFetch + Composio), recent activity. Reads via `pockets_service.ensure_home_pocket` + `audit_service.agent_list_audit`. |
| `pocket.py` | rich, tenancy-guarded | Current pocket name, widget list, node summary (from `rippleSpec.ui`), backend config summary. Reads via `pockets_service.get`; rejects cross-workspace pockets. |
| `pocket_widget.py` | rich, tenancy-guarded | Delegates to `pocket.py` then appends a `<widget-focus>` block with `widget_id` / `focus_node_id`. The focus block is suppressed when the pocket fetch would be cross-workspace. |
| `pockets_list.py` | rich | Per-pocket name / type / widget count / agent count via `pockets_service.list_pockets`. |
| `mission_control.py` | rich | Section-by-section work-item counts via `mc_service.agent_list_work_items` (using a `RequestContext`). |
| `files.py` | rich | Recent files via `UnifiedFilesService.list_unified` (name + mime). |
| `audit.py` | rich | Last 10 audit entries via `audit_service.agent_list_audit`. |
| `activity.py` | rich, workspace-scoped reader | Recent in-process activity via `activity.buffer.get_buffer().get_recent(workspace_id, limit)`. Emits a placeholder when the buffer doesn't expose a workspace-scoped read path. |
| `agents.py` | rich | List of workspace agents (name + slug) via `agents_service.list_agents`. |
| `agent.py` | rich, tenancy-guarded | Single-agent detail via `agents_service.get`; rejects when the returned `workspace_id` doesn't match. |
| `knowledge.py` | minimal | Placeholder scope listing (`workspace:<workspace_id>`). Pending a `KnowledgeService.list_scopes` helper. |
| `calendar.py` | minimal | Placeholder — points the agent at `GOOGLECALENDAR_LIST_EVENTS` via Composio. Pending a calendar entity. |
| `chat.py` | minimal | Best-effort session count via `sessions_service.list_for_user` when the helper exists. |
| `quickask.py` | minimal | Surface tag only — QuickAsk overlay has no persistent state. |
| `settings.py` | minimal | Surface tag only — no config values leaked into chat by design. |
| `sidepanel.py` | minimal | Surface tag only — thin side-panel chat surface. |
| `generic.py` | fallback | Catch-all for unknown surface strings. Echoes `route_path` when present. |

Seventeen handlers register against seventeen `SurfaceKind` values today; the dispatch table in `service.py:_load_handlers` is the single source of truth for the mapping.

---

## 5. The Wire Contract

The contract between paw-enterprise and the cloud chat router is two optional fields on `CloudAgentChatRequest` (`ee/pocketpaw_ee/cloud/chat/agent_schemas.py`):

```jsonc
POST /api/v1/cloud/chat/{scope}/{scope_id}/agent
Content-Type: application/json

{
  "content": "what's pinned on my home page?",
  "agent_id": null,
  "client_message_id": "c-7d3...",
  "intent": null,
  "surface": "home",
  "surface_meta": {
    "route_path": "/(app)/(authed)"
  }
}
```

A pocket-detail send looks the same with `surface: "pocket"` and `surface_meta: {"pocket_id": "p-abc"}`. A widget-focus modal adds `widget_id` and / or `focus_node_id`.

Key wire rules:

- **Both fields are optional.** Older clients that don't know about the stamp send neither — the resolver treats that as `{surface: None, meta: {}}` and produces a `GENERIC` context. The chat works unchanged.
- **Unknown `surface` strings silently fall through to `GENERIC`.** No 4xx. A client that ships a new surface name before the backend ships its handler still gets a chat response — just without the rich preamble. This is intentional: the wire is liberal in what it accepts so client and server can ship independently.
- **`surface_meta` is loosely validated.** Unknown keys are dropped at `SurfaceMetaRequest.model_validate`. A bad shape (e.g., `surface_meta` is not a dict) collapses to `GENERIC` with empty preamble.

The resolver always returns a `SurfaceContext` — the chat router cannot encounter a code path where the field is missing.

---

## 6. The Preamble Shape

Each handler renders an XML-ish text block. The chat router prepends it verbatim to the dynamic context block (`build_dynamic_context` in `chat/agent_service.py`). A real `home` surface preamble — produced by `handlers/home.py` for a workspace with three pinned widgets — looks like:

```xml
<surface kind="home" route="/" />
<pinned-widgets count="3">
- Active agents (native)
- 7-day sales (chart — live)
- Tasks (list — live)
</pinned-widgets>
<live-snapshot>home.pinned_widgets=3</live-snapshot>
<available-data-tools>WebSearch · WebFetch · GMAIL_FETCH_EMAILS · GMAIL_SEND_EMAIL · GOOGLECALENDAR_LIST_EVENTS · SLACK_SEND_MESSAGE · GITHUB_LIST_ISSUES_FOR_REPOSITORY · NOTION_SEARCH</available-data-tools>
<recent-activity count="2">
- 2026-05-23T17:42:11Z: prakash@snctm.com pocket.widget.added
- 2026-05-23T17:39:08Z: prakash@snctm.com pocket.widget.updated
</recent-activity>
```

### Canonical blocks

Handlers are free to add their own blocks, but the following tags are shared vocabulary and should be reused where they fit:

- **`<surface kind="..." route="..." />`** — every handler emits this first. Even minimal handlers and failure paths emit it so the agent always knows which route the user is on.
- **`<pinned-widgets count="N">…</pinned-widgets>`** — widget list block. The home handler is the canonical producer; `pocket.py` uses `<pocket-widgets>` (different block name because the semantics differ — pinned vs canvas).
- **`<live-snapshot>…</live-snapshot>`** — one-line numeric snapshot the agent can quote without an extra call.
- **`<available-data-tools>…</available-data-tools>`** — pipe-separated tool names the agent can reach on this deploy. Handlers may omit it when there's nothing useful to add.
- **`<recent-activity count="N">…</recent-activity>`** — last few audit / activity entries.
- **`<…-snapshot>(unavailable)</…-snapshot>`** — the canonical failure-fall-back shape every rich handler emits when its upstream read fails. Preserves the surface tag so the agent still knows the route.

A handler that introduces a new block name should match the style: kebab-case-tag, optional `count=` attribute, line-prefixed `- ` items inside.

---

## 7. Tenancy Guards

The first review pass on PR #1209 flagged three handlers (M1, M2, M3) where the upstream service gated on user-or-id but not on workspace. Without a guard, a user belonging to W1 and W2 could stamp a W2 `pocket_id` / `agent_id` from a W1 chat and the preamble would render W2's data inside W1's context. The follow-up commit closed all three plus a fourth that the same pass discovered.

| Handler | Upstream gap | Guard |
|---|---|---|
| `pocket.py` | `pockets_service.get` gates by owner / shared_with / visibility, not workspace. | `_load_pocket` fetches, then rejects when `pocket["workspace"] != workspace_id`. Falls back to the `(pocket unavailable)` snapshot path. |
| `pocket_widget.py` | Delegates base preamble to `pocket.py`, but the client-supplied `widget_id` / `focus_node_id` were rendered unconditionally — so even after the base preamble dropped, the focus pointers could leak. | `_pocket_in_workspace` repeats the same workspace check; the focus block is suppressed when the pocket fetch would be cross-workspace. |
| `agent.py` | `agents_service.get(agent_id)` looks up by id alone — no workspace filter. | Compare `agent.workspace_id != workspace_id`; falls back to `(agent unavailable)`. |
| `activity.py` | Originally read `getattr(buf, "events", [])` — an attribute the singleton never exposed, so the list was always empty. A future refactor that exposed a flat `events` list would have leaked every workspace's activity into every chat. | Switched to `get_buffer().get_recent(workspace_id, limit)` — the only workspace-scoped read path the buffer supports. Emits a placeholder when no scoped reader is available. |

### The graceful fall-back

Every guard reuses the handler's existing unavailable-snapshot path. The user-visible effect of a cross-workspace stamp is identical to the user-visible effect of a missing artifact:

```xml
<surface kind="pocket" route="/pockets/p-abc" />
<pocket-snapshot>(pocket unavailable)</pocket-snapshot>
```

This is deliberate. The agent still knows which surface the user is on (route-level context preserved), but no W2 names, slugs, or widget data leak into the preamble. The audit trail captures the rejection at `warning` level so a future drift can be spotted.

---

## 8. Performance

The preamble is rendered on every chat send, so per-turn cost matters. The current numbers:

- **Length budget — ≤1500 chars per turn.** Enforced by `_helpers.truncate_preamble`. Truncation is line-aware: trailing lines are dropped one by one until the result fits, then `... (truncated)` is appended so the agent knows context was lost. Tag balance is the caller's responsibility — handlers are structured so dropping trailing detail lines doesn't break the outer XML shape.
- **Reads per turn — small constant.** The `home` handler makes three workspace-scoped reads (`pockets_service.ensure_home_pocket`, `audit_service.agent_list_audit`, `composio_service.is_enabled`). The list-style handlers (`pockets_list`, `agents`, `files`) make exactly one read each. Minimal handlers make zero reads. No handler iterates per-row or fans out.
- **All reads are workspace-scoped.** Every upstream service the handlers call enforces tenancy on the query path. The handler-side tenancy guards (Section 7) catch the cases where the upstream gate is by id alone.
- **Composio probe is gated.** `composio_tool_names()` returns early on `is_enabled()` false or any exception. It never enumerates the live tool catalog; the canonical tool name list is hand-curated to a handful of high-signal actions to keep the preamble bounded.

The total cost is "one chat send's worth of context-block expansion plus a small handful of reads." Acceptable per turn.

---

## 9. Test Scaffolding

Tests live at `tests/cloud/surface/`. Five files, 16 tests today:

- `test_surface_service.py` — resolver guarantees (unknown surface → `GENERIC`, invalid meta → `GENERIC`, handler failure → `GENERIC` empty preamble, `body=None` accepted).
- `test_generic_handler.py` — the catch-all renders a usable preamble.
- `test_home_handler.py` — three-way fixture (1 native + 2 spec widgets), the BROKEN-marker regression, the empty-workspace fall-back.
- `test_pocket_handler.py` — pocket detail rendering.
- `test_tenancy_guards.py` — the M1/M2/M3 cross-workspace rejections plus the activity-scope check (eight tests).

Two patterns recur and should be the template for new handler tests:

- **`_FakeDoc` / `_AttrDict` fixtures** — most handlers use `getattr` against the row object, so test fixtures pass plain dicts wrapped in a tiny attr-access shim. See the `_AttrDict` class in `home.py` and `pocket.py` for the production shape; tests can construct equivalent fakes.
- **Handler-failure-isolation pattern** — `test_surface_service.py::test_resolve_handler_failure_returns_empty_preamble` monkeypatches `_load_handlers` to swap in a handler that raises, asserts the resolver returns `GENERIC` with empty preamble, and resets the cached `_HANDLERS` to `None` at teardown. New handlers that touch I/O should pick up this pattern.

---

## 10. Extensibility

Adding a new handler is the most common extension. See the companion guide:
**[`surface-context-handler-guide.md`](./surface-context-handler-guide.md)** — when to add a handler, the contract, a worked example, the registration steps, the tenancy checklist, and the testing pattern.

The short version: define a new `SurfaceKind` enum value, drop a handler module under `handlers/`, register it in `service._load_handlers`, add a test under `tests/cloud/surface/`. Stack-rank against the four-question tenancy checklist before merging.

---

## 11. Open Questions and Known Gaps

- **Minimal-placeholder handlers.** `knowledge.py`, `calendar.py`, `chat.py`, and `sidepanel.py` are minimal today because their upstream services don't expose the right list helper. Each handler's docstring names the helper it expects:
  - `knowledge.py` waits on `KnowledgeService.list_scopes(workspace_id)`.
  - `calendar.py` waits on a calendar entity (today it points the agent at Composio's `GOOGLECALENDAR_LIST_EVENTS`).
  - `chat.py` already calls `sessions_service.list_for_user` but tolerates the helper being missing on older deploys.
- **`SurfaceMetaProvider` registry has no producers yet.** Paw-enterprise's `surface-context.ts` exposes `registerSurfaceMetaProvider` for components to plug in ephemeral state (most obviously the widget-focus modal's `widget_id` / `focus_node_id`). The registry exists; no component registers against it yet. Follow-up tracked on the paw-enterprise side.
- **Hand-curated `<available-data-tools>` block.** `_helpers.composio_tool_names` returns a static list of six canonical Composio action names. A more dynamic enumeration (e.g., querying the live MCP tool list per workspace) is future work — the static list is good enough for the agent to know what's wired, without ballooning the preamble.
- **`SurfaceKind.AUDIT` vs `SurfaceKind.ACTIVITY`.** Both surfaces exist (`/audit` for the persisted audit log, `/activity` for the in-process activity buffer). They look similar from the outside, and the handlers do diverge meaningfully — the audit handler hits a Beanie collection, the activity handler hits the in-process buffer. Worth keeping the two distinct for now; consolidate only if a unified surface ships.

---

## 12. Cross-References

- **PR [#1209](https://github.com/pocketpaw/pocketpaw/pull/1209)** — `feat(chat): per-turn surface preamble for the cloud chat agent`. The module itself plus the chat-router wiring, the four tenancy guards, and the test scaffolding.
- **PR [#250](https://github.com/pocketpaw/paw-enterprise/pull/250)** (paw-enterprise) — `feat(chat): stamp surface + meta on every chat send`. The paired client change that derives `surface` from `$page.route.id` and attaches it to every chat send.
- **`paw-enterprise/src/lib/core/chat/surface-context.ts`** — client-side stamp. Exports `getCurrentSurface()` and `registerSurfaceMetaProvider()`.
- **`ee/docs/architecture/pockets-builder-design.md`** — sibling design doc covering the pockets builder. Same template; useful reference when writing the next ee/cloud architecture doc.
- **`pocketPaw/CLAUDE.md`** — the cloud entity rules ("pocketpaw_ee/cloud Code Rules" section) govern the 4-file shape, the tenancy-at-construction contract, and the touch-time migration rule that brought the surface module onto canonical shape from day one.
