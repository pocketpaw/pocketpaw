# Files Tab — Unified File Browser (v2)

- **Date:** 2026-04-20
- **Scope:** `backend/ee/cloud/files/` (new module, layered on Cluster E) + `paw-enterprise/src/routes/files/`
- **Stacks on:** `pocketpaw/pocketpaw#996`, `pocketpaw/pocketpaw#998`, `qbtrix/paw-enterprise#107`, `qbtrix/paw-enterprise#109` (all OPEN, "Do not merge — captain reviews")
- **Related plans:** `docs/plans/FEATURE-HARDENING-PLAN.md` — Cluster E §10/§14 (gaps E7/E8/E9)

## Goal

Surface every file a user is entitled to see in one place — the Files tab in paw-enterprise — regardless of which subsystem uploaded it (chat attachments, workspace KB, pockets, agent artifacts, memory documents, direct uploads). Visibility is filtered by RBAC (role baseline) and ABAC (attribute restrict overlay). Organization is folder-based with a **configurable mount tree** so locations can be reshaped later without migration. Writes (upload/rename/delete/move) are permitted subject to permissions, but cross-scope moves are blocked.

## Non-goals (v2)

- Full-text content search (defer; add when retrieval is wired).
- Replacing the flat `/api/v1/files` endpoint shipped in #998 — it stays as a legacy surface.
- External storage integrations beyond the existing Drive stub.
- Versioning/history UI, trash/restore.
- Cross-scope moves (explicitly disallowed — provenance and permission transfer are out of scope).

## Context — what Cluster E already provides

The following lands via the referenced PRs and is treated as a given:

| PR | Delivers |
|---|---|
| `pocketpaw#996` | `GET /uploads/{file_id}/download-url` — signed URL, 15min TTL, workspace-scoped, same pipeline as `/grant`. |
| `pocketpaw#998` | `ee/cloud/files/` module with `UnifiedFilesService`, `GET /api/v1/files?source=...`, `MongoFileStore.list_by_workspace`, chat-uploads source + Drive stub, `workspace_id` mismatch → 403, 500-row cap. |
| `paw-enterprise#107` | `FilesPanel.svelte` consumes `/files` + Tauri `localFs` bridge. Source chips: All / Chat / Local / Drive. Removes fake AI sidebar. |
| `paw-enterprise#109` | Hover-visible download button on chat attachment chips using `/uploads/{id}/grant`. |

The flat `source=` model and `{workspace_id, source, files[], warnings[]}` response shape are the shipped v1 contract. **v2 does not change them.**

## Design

### Architecture

```
paw-enterprise /files
  ├─ FolderTree (left, new)         — GET /api/v1/files/tree
  ├─ FileList   (center, new)       — GET /api/v1/files/browse?mount=...
  └─ Preview    (right, new)        — GET /api/v1/files/:id
  + existing FilesPanel flat view   — retained, view-mode toggle
  + Socket.IO: files:ws:<workspace_id>, files:user:<user_id>

backend/ee/cloud/files/  (extended)
  service.py         (from #998 — unchanged external contract;
                      internally becomes a consumer of registry.py)
  router.py          (from #998 — add new routes; keep /api/v1/files)
  mongo_store.py     (from #998 — unchanged)
  registry.py        (NEW — FolderProvider registry + mount config)
  providers/
    chat.py          (NEW — wraps existing MongoFileStore chat uploads)
    drive.py         (NEW — wraps current Drive stub)
    local.py         (NEW — Tauri local-fs bridge provider)
    uploads.py       (NEW — "My Files" direct uploads)
    kb.py            (NEW — workspace KB documents)
    pockets.py       (NEW — per-pocket uploads)
    memory.py        (NEW — memory documents)
    agents.py        (NEW — agent artifacts)
    rooms.py         (NEW — per-room attachments view; dedupes with chat)
  tree.py            (NEW — tree builder, uses registry)
  browse.py          (NEW — per-mount paginated listing)
  schemas.py         (NEW — FileEntry, FolderNode, MountConfig)
  permissions.py     (NEW — RBAC baseline + ABAC overlay)
  realtime.py        (NEW — Socket.IO bridge over ee/cloud/realtime)

backend/src/pocketpaw/ee/guards/  (existing — reused unchanged)
  rbac.py, abac.py, policy.py, audit.py
```

### FolderProvider interface

```python
class FolderProvider(Protocol):
    provider_id: str

    async def list_mounts(ctx: RequestContext) -> list[ResolvedMount]: ...
    async def list_entries(ctx, mount_path: str, cursor, limit, filters) -> Page[FileEntry]: ...
    async def get_entry(ctx, entry_id: str) -> FileEntry: ...
    async def open_stream(ctx, entry_id: str) -> AsyncIterator[bytes]: ...
    async def upload(ctx, mount_path: str, upload: UploadPart) -> FileEntry: ...       # may NotSupported
    async def rename(ctx, entry_id: str, new_name: str) -> FileEntry: ...
    async def move(ctx, entry_id: str, dest_mount_path: str) -> FileEntry: ...          # same-scope
    async def delete(ctx, entry_id: str) -> None: ...
    async def search(ctx, query: SearchQuery) -> Page[FileEntry]: ...                   # filename/metadata

    def baseline_rbac(ctx, entry: FileEntry) -> Permission: ...                         # r/w/manage
```

Unsupported operations raise `ProviderUnsupported`; aggregator translates to HTTP 405.

### Schemas

```python
class FileEntry(BaseModel):
    id: str                      # "provider_id:native_id"
    provider_id: str
    mount_path: str              # "/Workspaces/Acme/KB/handbook.pdf"
    name: str
    mime: str
    size: int
    owner_id: str | None
    workspace_id: str | None
    scope: Literal["personal", "shared", "workspace"]
    tags: list[str]              # drives ABAC (e.g., "confidential","pii","hr-only")
    created_at: datetime
    updated_at: datetime
    source_ref: dict             # provider-defined (room_id, pocket_id, agent_id, ...)
    capabilities: list[Literal["read","download","rename","delete","move","replace"]]

class FolderNode(BaseModel):
    path: str
    name: str
    provider_id: str
    children: list["FolderNode"]
    capabilities: list[str]

class MountConfig(BaseModel):
    provider_id: str
    mount_template: str          # "/Workspaces/{workspace_id}/KB"
    writable: bool
    order: int
```

### Mount config (reshape without migration)

```yaml
# ee/cloud/files/mounts.yaml (loaded at startup; hot-reloadable)
- provider_id: uploads
  mount_template: "/My Files"
  writable: true
  order: 10
- provider_id: kb
  mount_template: "/Workspaces/{workspace_id}/Knowledge Base"
  writable: true
  order: 20
- provider_id: pockets
  mount_template: "/Workspaces/{workspace_id}/Pockets/{pocket_id}"
  writable: true
  order: 30
- provider_id: rooms
  mount_template: "/Workspaces/{workspace_id}/Rooms/{room_id}"
  writable: false
  order: 40
- provider_id: chat
  mount_template: "/Shared with me/Chat uploads"
  writable: false
  order: 50
- provider_id: memory
  mount_template: "/Workspaces/{workspace_id}/Memory"
  writable: false
  order: 60
- provider_id: agents
  mount_template: "/Workspaces/{workspace_id}/Agents/{agent_id}/Artifacts"
  writable: false
  order: 70
- provider_id: drive
  mount_template: "/Connected/Google Drive"
  writable: false
  order: 80
- provider_id: local
  mount_template: "/Local"
  writable: true
  order: 90
```

Reshaping `Workspaces/<ws>/Rooms/*` → `Rooms/*` is a YAML edit, not a migration.

### Permissions — RBAC baseline + ABAC overlay

- **RBAC baseline** is enforced per-provider (each provider already knows its ACL: chat membership, workspace role, pocket owner, etc.). Returns `Permission{read, write, manage}`.
- **ABAC overlay** is a post-filter in `permissions.apply_abac(ctx, entries)`:
  - Reads `entry.tags` and `ctx.user.attributes` (role, department, clearance).
  - Applies a rule set loaded from `ee/cloud/files/abac_rules.yaml` (e.g., `tag=confidential → require ctx.user.role in {admin, owner}`; `tag=hr-only → require ctx.user.department=hr`).
  - **Can only restrict**, never grant. Untagged entries unaffected.
- **Effective capabilities** on an entry = `baseline_rbac ∩ abac_allowed` ∩ `mount.writable` (for write-side caps).

### Data flows

**Tree**  `GET /api/v1/files/tree?workspace_id=<ws>`
1. Load `MountConfig[]`; resolve templates against `ctx`.
2. Parallel `provider.list_mounts(ctx)` for every registered provider; providers return only mounts the user has baseline read on.
3. `permissions.apply_abac` drops mounts with out-of-reach tags (rare; most tags are per-file).
4. Merge `FolderNode` tree, sort by `order`.
5. Cache `(user_id, workspace_id)` → tree for 30s; invalidate on `mount.changed` realtime event.

**Browse**  `GET /api/v1/files/browse?mount=<path>&cursor=&limit=50&filters=...`
1. Longest-prefix match on registered mount templates → provider.
2. `provider.list_entries` returns a page.
3. `permissions.apply_abac` post-filter.
4. Recompute per-entry capabilities.
5. Return page; cursor is opaque, provider-defined.

**Upload**  `POST /api/v1/files/upload?mount=<path>` (multipart)
1. Resolve provider + check `MountConfig.writable` + baseline write + ABAC (e.g., `confidential` tag rejected on `/Shared`).
2. `provider.upload` streams into provider storage (chat → existing S3 pipeline, uploads → `EEUploadService`, kb → its own ingester, etc.).
3. Provider emits `file.added` on `ee/cloud/realtime` bus.
4. `realtime.py` ABAC-filters recipients → Socket.IO `files:added` to entitled clients only.

**Rename / delete / move** — same pattern. Move validates `src.scope == dst.scope AND src.workspace_id == dst.workspace_id AND src.provider_id == dst.provider_id`; else HTTP 409 `files.cross_scope_move`.

**Download**  `GET /api/v1/files/:id/content` → permission check → `provider.open_stream` with Range support, OR if provider exposes a signed-URL path, 302-redirect (chat/uploads providers reuse `/uploads/:id/download-url` from #996).

**Realtime**
- Client joins `files:ws:<workspace_id>` and `files:user:<user_id>` on page mount; server authorizes membership before accepting join.
- Events: `files:added`, `files:updated`, `files:removed`, `files:moved` — each carries ABAC-filtered `FileEntry` with `old_path` on moves.
- Tag-change events trigger re-evaluation; users who lose access get `files:removed` (not `updated`).

### Error taxonomy

| Condition | HTTP | Code |
|---|---|---|
| Not authenticated | 401 | `auth.required` |
| Read denied (RBAC or ABAC) | 404 | `files.not_found` (no existence leak) |
| Read ok, write/delete denied | 403 | `files.forbidden` |
| Mount not writable | 403 | `files.mount_readonly` |
| Operation unsupported | 405 | `files.operation_unsupported` |
| Cross-scope move | 409 | `files.cross_scope_move` |
| Name collision | 409 | `files.name_conflict` |
| Too large / quota | 413 / 429 | `files.too_large` / `files.quota` |
| MIME blocked | 415 | `files.mime_blocked` |
| Provider upstream failure | 502 | `files.provider_error` |
| Partial tree (provider down) | 200 | response includes `warnings: [{provider_id, code}]` |

### Edge cases

- **Partial tree availability** — one provider erroring on `list_mounts` yields a `warnings` entry; UI marks that subtree "unavailable" instead of failing the tab.
- **Duplicate IDs across providers** — prevented by `id = "{provider_id}:{native_id}"`.
- **Rename that changes mount path** — emit `files:moved` with `old_path` + `new_path`.
- **Tag change** — provider emits `file.updated`; aggregator re-runs ABAC per recipient; demoted users see `files:removed`.
- **Race (deleted between list and open)** — `get_entry` returns 404; realtime `files:removed` catches the UI up.
- **Cursor stability** — providers must return stable cursors across insertions; aggregator never merges paginated pages across providers (one browse = one provider).
- **Socket.IO join auth** — server verifies workspace membership before accepting join.
- **Large file streaming** — chunked multipart up, `StreamingResponse` with Range down.
- **Audit** — every write logged via `guards/audit.py`: `{user, action, entry_id, provider, workspace, result}`.

## Rollout

1. **Cluster E lands** (captain-gated): #996, #998, #107, #109. v2 depends on this baseline.
2. **Phase 1 — Invisible refactor.** Introduce `registry.py`, `schemas.py`, `permissions.py`. Refactor `UnifiedFilesService` to delegate to providers for chat/drive/local. Response shape of `GET /api/v1/files` byte-for-byte identical; contract test proves no change.
3. **Phase 2 — Tree + browse endpoints.** Ship `GET /api/v1/files/tree` and `GET /api/v1/files/browse`. Baseline RBAC via providers; ABAC overlay wired (initially empty rule set — no-op until tags used). Frontend view-mode toggle added; tree view off by default behind a feature flag.
4. **Phase 3 — New providers.** Add `uploads`, `kb`, `pockets`, `memory`, `agents`, `rooms` providers one by one, each behind its own flag. Contract tests gate each.
5. **Phase 4 — CRUD + realtime.** `POST/PATCH/DELETE/move` endpoints. Socket.IO `files:*` events wired through `realtime.py`. Frontend optimistic updates + rollback.
6. **Phase 5 — Polish.** ABAC rule admin, audit viewer, keyboard navigation, empty/error states, preview types (image/pdf/text/markdown).

## Testing strategy

**Unit** (`backend/tests/ee/cloud/files/`)
- `test_registry.py` — mount template resolution, longest-prefix routing, config reload.
- `test_permissions.py` — RBAC × ABAC truth tables (role × tag × scope), deny-overrides.
- `test_tree.py`, `test_browse.py` — merge, ordering, partial-failure warnings, ABAC post-filter.
- `test_schemas.py` — capability derivation, id namespacing.

**Provider contract** (`test_provider_contract.py` — reusable)
- Listing, pagination cursor stability, CRUD semantics, unsupported → `ProviderUnsupported`, events emitted on mutations. Every real provider subclasses with its fixtures.

**Integration** (`tests/ee/integration/test_files_flow.py`)
- Full REST round-trip against real Mongo + providers.
- Cross-scope move → 409.
- Tag change → recipient loses access → `files:removed` observed.
- Partial provider outage → tree has `warnings`, other mounts work.
- `GET /api/v1/files` legacy contract unchanged (regression test).

**Realtime** — two Socket.IO clients in one workspace; `confidential` tag on file; only entitled client receives `files:added`.

**Frontend** (`paw-enterprise`)
- `src/lib/files/__tests__/` — `store`, `api`, `socket`, `permissions` unit tests (vitest).
- Component tests — `FolderTree`, `FileList`, `Preview` with mocked API + Socket.IO.
- E2E (when Playwright wired) — upload → appears → rename → appears in other session via realtime → delete.

**Coverage target:** `ee/cloud/files/` ≥ 85%; all providers pass contract suite; every RBAC×ABAC decision edge covered by table-driven cases.

## Frontend structure

```
paw-enterprise/src/routes/files/
  +page.svelte          — shell: view-mode toggle (flat | tree)
  +page.ts              — initial load per mode

paw-enterprise/src/lib/files/
  api.ts                — typed REST client (tree, browse, get, upload, rename, delete, move, download)
  socket.ts             — Socket.IO subscription helper
  store.svelte.ts       — $state store (tree, currentMount, entries, selection, uploads)
  permissions.ts        — derive UI affordances from entry.capabilities

paw-enterprise/src/lib/files/components/
  FolderTree.svelte     — recursive, lazy-expand, drop-target for same-scope move
  FileList.svelte       — virtualized table, multi-select, sort, filter chips
  FileRow.svelte        — context menu enabled from capabilities
  Preview.svelte        — dispatches to PreviewImage / PreviewPdf / PreviewText / PreviewFallback
  UploadDropzone.svelte — chunked multipart with progress
  Toolbar.svelte        — search box, filters, upload button (hidden if !mount.writable)
  WarningBanner.svelte  — shown when response has `warnings`
```

- Svelte 5 runes (`$state`, `$derived`, `$effect`) — project convention.
- shadcn-svelte + bits-ui + Tailwind 4 oklch tokens.
- Capabilities drive UI affordances; disabled buttons carry a "why" tooltip.
- Optimistic updates on rename/delete; rollback on error toast.
- Realtime events mutate store in place; tree position updates on `files:moved`.

## Open questions (tracked, not blocking v2)

- ABAC tag schema authoring — admin UI or YAML-only?
- Preview-type plugin registry for additional MIME types?
- Policy for chat attachments shown in Rooms provider AND Chat provider (dedupe vs. expose both) — current plan: dedupe by `source_ref.message_id`.

---

## References

- Plan anchors: `docs/plans/FEATURE-HARDENING-PLAN.md` Cluster E §10, §14
- PRs: `pocketpaw/pocketpaw#996`, `#998`; `qbtrix/paw-enterprise#107`, `#109`
- Reality brief referenced by PRs: `docs/plans/cluster-E-reality.md`
