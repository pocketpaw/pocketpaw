# PocketPaw AI File Explorer — Implementation Plan

**Date:** 2026-02-26
**Design doc:** `docs/plans/2026-02-26-multi-mode-workspace-client-design.md`
**Stack:** Tauri 2.0 + SvelteKit 2 + Svelte 5 runes + Tailwind CSS 4

---

## Phase 1: File Explorer Core

Goal: Replace the current chat-first layout with a file grid + sidebar layout. Get basic file browsing working with local files.

### 1.1 — Install frontend dependencies

```bash
cd client && bun add monaco-editor pdfjs-dist xlsx marked diff2html
```

**File:** `client/package.json`

### 1.2 — Rust filesystem commands

**New file:** `client/src-tauri/src/fs_commands.rs`

```rust
use serde::Serialize;
use std::fs;
use std::path::Path;
use std::time::UNIX_EPOCH;

#[derive(Serialize)]
pub struct FileEntry {
    pub name: String,
    pub path: String,
    pub is_dir: bool,
    pub size: u64,
    pub modified: u64,      // unix timestamp ms
    pub extension: String,  // lowercase, no dot
}

#[tauri::command]
pub async fn fs_read_dir(path: String) -> Result<Vec<FileEntry>, String> { ... }

#[tauri::command]
pub async fn fs_read_file(path: String) -> Result<Vec<u8>, String> { ... }

#[tauri::command]
pub async fn fs_read_file_text(path: String) -> Result<String, String> { ... }

#[tauri::command]
pub async fn fs_write_file(path: String, content: Vec<u8>) -> Result<(), String> { ... }

#[tauri::command]
pub async fn fs_delete(path: String, recursive: bool) -> Result<(), String> { ... }

#[tauri::command]
pub async fn fs_rename(old_path: String, new_path: String) -> Result<(), String> { ... }

#[tauri::command]
pub async fn fs_stat(path: String) -> Result<FileEntry, String> { ... }

#[tauri::command]
pub async fn fs_create_dir(path: String) -> Result<(), String> { ... }

#[tauri::command]
pub async fn fs_exists(path: String) -> Result<bool, String> { ... }
```

**Update:** `client/src-tauri/src/lib.rs` — add `mod fs_commands;` and register all commands in `invoke_handler`.

### 1.3 — Rust filesystem watcher

**New file:** `client/src-tauri/src/fs_watcher.rs`

```rust
use notify::{Watcher, RecursiveMode, Event};
use std::collections::HashMap;
use std::sync::Mutex;
use tauri::State;

pub struct WatcherState {
    watchers: Mutex<HashMap<String, notify::RecommendedWatcher>>,
}

#[tauri::command]
pub async fn fs_watch(
    path: String,
    app: tauri::AppHandle,
    state: State<'_, WatcherState>
) -> Result<String, String> { ... }
// Returns watch_id. Emits "fs-change" event: { watch_id, path, kind }

#[tauri::command]
pub async fn fs_unwatch(
    watch_id: String,
    state: State<'_, WatcherState>
) -> Result<(), String> { ... }
```

**Update:** `client/src-tauri/Cargo.toml` — add `notify = "7"`.
**Update:** `client/src-tauri/src/lib.rs` — add `WatcherState` to managed state.

### 1.4 — FileSystemProvider interface + LocalFileSystem

**New file:** `client/src/lib/filesystem/types.ts`

```typescript
export interface FileEntry {
  name: string
  path: string
  isDir: boolean
  size: number
  modified: number  // unix ms
  extension: string
  source: "local" | "remote" | "cloud"
}

export interface FileStat { ... }
export interface FileContent { data: Uint8Array; text?: string }
export type FileChangeKind = "create" | "modify" | "delete"
export interface FileChangeEvent { path: string; kind: FileChangeKind }

export interface FileSystemProvider {
  scheme: "local" | "remote" | "cloud"
  readDir(path: string): Promise<FileEntry[]>
  readFile(path: string): Promise<FileContent>
  readFileText(path: string): Promise<string>
  writeFile(path: string, content: string | Uint8Array): Promise<void>
  deleteFile(path: string, recursive?: boolean): Promise<void>
  rename(oldPath: string, newPath: string): Promise<void>
  stat(path: string): Promise<FileEntry>
  createDir(path: string): Promise<void>
  exists(path: string): Promise<boolean>
  watch(path: string, callback: (event: FileChangeEvent) => void): () => void
}
```

**New file:** `client/src/lib/filesystem/local.ts`
- Implements `FileSystemProvider` using `invoke("fs_read_dir", ...)` etc.
- Watch uses Tauri event listener for `fs-change` events.

**New file:** `client/src/lib/filesystem/index.ts`
- Exports `localFs` singleton + future `remoteFs`, `cloudFs`.

### 1.5 — Explorer store

**New file:** `client/src/lib/stores/explorer.svelte.ts`

```typescript
class ExplorerStore {
  // State (Svelte 5 runes)
  currentPath = $state<string>("")         // Current folder path
  currentSource = $state<"local" | "remote" | "cloud">("local")
  files = $state<FileEntry[]>([])          // Files in current folder
  selectedFiles = $state<Set<string>>(new Set())  // Selected file paths
  viewMode = $state<"icon" | "grid" | "list" | "column" | "gallery">("icon")
  sortBy = $state<"name" | "modified" | "size" | "type">("name")
  sortAsc = $state<boolean>(true)
  isLoading = $state<boolean>(false)
  error = $state<string | null>(null)

  // Navigation history
  history = $state<string[]>([])
  historyIndex = $state<number>(-1)

  // Detail view
  openFile = $state<FileEntry | null>(null)  // Currently opened file (null = browse view)

  // Derived
  sortedFiles = $derived.by(() => { /* sort files by sortBy + sortAsc */ })
  canGoBack = $derived(this.historyIndex > 0)
  canGoForward = $derived(this.historyIndex < this.history.length - 1)
  isDetailView = $derived(this.openFile !== null)
  breadcrumbs = $derived.by(() => { /* parse currentPath into segments */ })

  // Methods
  async navigateTo(path: string, source?: string): Promise<void>
  async refresh(): Promise<void>
  goBack(): void
  goForward(): void
  openFileDetail(file: FileEntry): void
  closeDetail(): void
  selectFile(path: string, multi?: boolean): void
  selectAll(): void
  clearSelection(): void
  setViewMode(mode: string): void
  setSortBy(field: string): void
}

export const explorerStore = new ExplorerStore()
```

### 1.6 — ExplorerShell (main layout)

**New file:** `client/src/lib/components/explorer/ExplorerShell.svelte`

The top-level layout component that replaces `AppShell` for the explorer mode:

```svelte
<div class="explorer-shell">
  <NavBar />
  <div class="explorer-body">
    <div class="file-area">
      {#if explorerStore.isDetailView}
        <DetailView />
      {:else if explorerStore.viewMode === "icon"}
        <FileGrid size="large" />
      {:else if explorerStore.viewMode === "grid"}
        <FileGrid size="small" />
      {:else if explorerStore.viewMode === "list"}
        <FileList />
      {:else if explorerStore.viewMode === "column"}
        <FileColumn />
      {:else if explorerStore.viewMode === "gallery"}
        <FileGallery />
      {/if}
    </div>
    <ChatSidebar />
  </div>
</div>
```

### 1.7 — NavBar

**New file:** `client/src/lib/components/explorer/NavBar.svelte`

```
┌─[←][→]─[🏠 Home › Documents › Project X]──[🔍]──[Icon ▾]──[⚙]─┐
```

- Back/forward buttons (linked to explorerStore.goBack/goForward)
- Breadcrumb path (each segment clickable)
- Search button (opens SearchOverlay)
- View mode dropdown selector
- Settings gear icon

### 1.8 — FileCard + FileGrid

**New file:** `client/src/lib/components/explorer/FileCard.svelte`

Individual file card:
```svelte
<button class="file-card" class:selected={isSelected} on:dblclick={openFile}>
  <div class="thumbnail">
    <!-- Placeholder colored rectangle with file type icon for now -->
    <!-- Rust thumbnail generation added in Phase 4 -->
    <FileTypeIcon extension={file.extension} />
  </div>
  <div class="info">
    <span class="name" title={file.name}>{file.name}</span>
    <span class="meta">{relativeTime(file.modified)}</span>
  </div>
</button>
```

Props: `file: FileEntry`, `isSelected: boolean`, `size: "large" | "small"`
Events: `onclick`, `ondblclick`, `oncontextmenu`

**New file:** `client/src/lib/components/explorer/FileGrid.svelte`

Responsive CSS Grid of FileCard components:
- `size="large"` (Icon view): `grid-template-columns: repeat(auto-fill, minmax(160px, 1fr))`
- `size="small"` (Grid view): `grid-template-columns: repeat(auto-fill, minmax(110px, 1fr))`
- Handles click (select), dblclick (open), right-click (context menu), drag (multi-select area)

### 1.9 — ContextMenu

**New file:** `client/src/lib/components/explorer/ContextMenu.svelte`

Floating context menu rendered at cursor position:
- Open / Open in Detail View
- Rename (inline rename in the card)
- Delete (with confirmation)
- Copy Path
- Move to...
- Open in System App
- "Ask AI about this" (sends to chat)

### 1.10 — Integrate into SvelteKit routes

**Update:** `client/src/routes/+page.svelte`
- Replace current chat-only page with `ExplorerShell`
- Chat becomes the sidebar (reuse existing `ChatPanel` component inside `ChatSidebar`)

**Update:** `client/src/lib/components/AppShell.svelte`
- The sidebar navigation (Chat, Settings, Explore, Activity, etc.) moves into a menu accessible from the ⚙ gear or a hamburger menu — no longer a persistent left sidebar

**New file:** `client/src/lib/components/explorer/ChatSidebar.svelte`
- Wraps existing chat functionality in a collapsible right sidebar
- Adds context pill at bottom showing current folder/file
- Adds drag-drop zone for files
- Collapse to floating 💬 icon when hidden

### 1.11 — Home view

**New file:** `client/src/lib/components/explorer/HomeView.svelte`

Shown when `currentPath === ""` (Home):
- Section: "Pinned Folders" (configured by user, saved to localStorage)
- Section: "Local" with quick links to Documents, Downloads, Desktop
- Section: "Workspace" link to agent workspace
- Section: "Cloud" (placeholder for Phase 5)
- Each section renders as a row of folder cards

---

## Phase 2: Chat Sidebar + Agent Context

Goal: Make the AI chat context-aware and show agent file operations in the grid.

### 2.1 — Enhanced ChatSidebar with context

**Update:** `client/src/lib/components/explorer/ChatSidebar.svelte`

- Display `ContextPill` at bottom of chat showing what AI is "looking at"
- When in browse view: context = current folder path
- When in detail view: context = open file path
- When files selected: context = selected file paths
- Send context with every chat message to backend

**New file:** `client/src/lib/components/explorer/ContextPill.svelte`
- Renders: `📁 Project X` or `📄 main.py` or `📄 3 files selected`
- Clickable to change/clear context

### 2.2 — Drag files into chat

**Update:** `client/src/lib/components/explorer/ChatSidebar.svelte`
- Chat input accepts drag-and-drop of FileCard components
- Dropped files appear as pills above the input: `📄 main.py ✕` `📄 report.pdf ✕`
- These file paths are sent as explicit context with the message

**Update:** `client/src/lib/components/explorer/FileCard.svelte`
- Add `draggable="true"` and `ondragstart` that sets file path data

### 2.3 — Multiple chat sessions

**Update:** `client/src/lib/stores/chat.svelte.ts`
- Support multiple independent chat sessions (conversations)
- `chats: ChatSession[]` each with own message history
- `activeChatId: string`

**Update:** `client/src/lib/components/explorer/ChatSidebar.svelte`
- Dropdown at top to switch between chats
- "+ New Chat" button
- Each chat can have different context

### 2.4 — Agent file events in the grid

**Update:** `client/src/lib/api/websocket.ts`
- Listen for new event types: `file_create`, `file_edit`, `file_delete`, `file_move`

**Update:** `client/src/lib/stores/explorer.svelte.ts`
- On `file_create`: add new FileEntry to `files` array, trigger a brief highlight animation on the card
- On `file_edit`: flash the existing card's thumbnail (CSS animation)
- On `file_delete`: remove from `files` with a fade-out animation, show undo toast
- On `file_move`: remove from current view if moved out, add if moved in

**New file:** `client/src/lib/components/explorer/UndoToast.svelte`
- Bottom-center toast: "Deleted report.pdf [Undo]"
- Auto-dismiss after 5 seconds
- Undo restores the file (calls fs_write_file with backed-up content)

### 2.5 — Inline terminal blocks in chat

**Update:** `client/src/lib/components/chat/` (existing chat components)
- New message type: `terminal_block`
- Renders as a collapsible dark block with monospace text
- Shows live output while command is running (streaming)
- Collapses to single line after completion: `$ npm test — PASS (1.2s) ▾`
- Click to expand full output

**Backend (Python) changes needed:**
- **Update:** `src/pocketpaw/bus/events.py` — Add file operation fields to `SystemEvent`
- **Update:** `src/pocketpaw/agents/loop.py` — Emit file events when agent uses file tools
- **Update:** `src/pocketpaw/dashboard/websocket.py` — Forward file events to clients

### 2.6 — File action cards in chat

When the agent creates/edits/deletes a file, a compact card appears in the chat:

**New file:** `client/src/lib/components/chat/FileActionCard.svelte`

```
┌─────────────────────────────┐
│ ✅ Created summary.md        │  ← Clickable → opens in detail view
│    📄 2.1 KB · Markdown      │
└─────────────────────────────┘

┌─────────────────────────────┐
│ ✏️ Edited main.py            │  ← Clickable → opens diff view
│    +12 −3 lines              │
└─────────────────────────────┘
```

---

## Phase 3: View Modes + Detail View

Goal: Implement all five view modes and the full file preview/editor system.

### 3.1 — List view

**New file:** `client/src/lib/components/explorer/FileList.svelte`

Table with sortable columns:
```
| [✓] | Icon | Name          | Size    | Modified    | Type   |
|-----|------|---------------|---------|-------------|--------|
|  □  | 📄   | report.pdf    | 2.4 MB  | 3 min ago   | PDF    |
|  □  | 🖼️   | hero.png      | 450 KB  | 2 hours ago | Image  |
```

- Click column header to sort
- Checkbox for multi-select
- Row hover highlights
- Double-click row to open detail view

### 3.2 — Column view

**New file:** `client/src/lib/components/explorer/FileColumn.svelte`

macOS Finder-style cascading columns:
- Each column shows contents of a directory
- Clicking a folder opens its contents in the next column
- Clicking a file shows its preview in the rightmost column
- Horizontal scroll for deep paths
- Columns are ~200px wide each

### 3.3 — Gallery view

**New file:** `client/src/lib/components/explorer/FileGallery.svelte`

Full-width cards, one per row:
```
┌─────────────────────────────────────────────┐
│ ┌──────────┐                                │
│ │          │  report.pdf                    │
│ │  Large   │  PDF · 12 pages · 2.4 MB      │
│ │ thumbnail│  Modified 3 minutes ago        │
│ │          │  [Open] [Ask AI]               │
│ └──────────┘                                │
├─────────────────────────────────────────────┤
│ ┌──────────┐                                │
│ │          │  hero.png                      │
│ │  ...     │  PNG · 1920×1080 · 450 KB      │
│ └──────────┘                                │
└─────────────────────────────────────────────┘
```

### 3.4 — Detail view

**New file:** `client/src/lib/components/explorer/DetailView.svelte`

Full-width file preview with header:
```svelte
<div class="detail-view">
  <DetailHeader file={explorerStore.openFile} />
  <div class="preview-content">
    <FilePreviewDispatcher file={explorerStore.openFile} />
  </div>
</div>
```

**New file:** `client/src/lib/components/explorer/DetailHeader.svelte`
- `← Back` button, filename, file metadata, Edit toggle, more menu (···)

**New file:** `client/src/lib/components/explorer/FilePreviewDispatcher.svelte`
- Reads file extension → selects the right preview component
- Lazy-loads heavy components (Monaco, pdf.js)

### 3.5 — Preview renderers

**New files** in `client/src/lib/components/explorer/previews/`:

- `CodePreview.svelte` — Monaco Editor wrapper. Props: content, language, readOnly. Handles save (Cmd+S).
- `PdfPreview.svelte` — `pdfjs-dist` page viewer. Page nav, zoom, fit-to-width.
- `ImagePreview.svelte` — `<img>` with CSS transform for zoom/pan. Mouse wheel zoom, click+drag pan.
- `SpreadsheetPreview.svelte` — `xlsx` parse → render as HTML `<table>`. Column headers, row numbers.
- `MarkdownPreview.svelte` — `marked` render to HTML. Toggle button to switch to raw editor (Monaco).
- `JsonPreview.svelte` — Collapsible tree view. Toggle to raw editor (Monaco).
- `HtmlPreview.svelte` — Sandboxed `<iframe>` render. Toggle to source (Monaco).
- `VideoPreview.svelte` — `<video>` with controls. Supports common formats.
- `AudioPreview.svelte` — `<audio>` with controls + simple waveform visualization.
- `TextPreview.svelte` — Monospace text with optional edit mode.
- `HexPreview.svelte` — Hex dump table (offset | hex bytes | ASCII).

### 3.6 — File navigation in detail view

**Update:** `client/src/lib/components/explorer/DetailView.svelte`
- Left/Right arrow keys → previous/next file in the current folder
- Update `explorerStore.openFile` accordingly
- Smooth transition animation between files

---

## Phase 4: Thumbnails + Search

Goal: Rich visual thumbnails for all file types and powerful search.

### 4.1 — Rust thumbnail generation

**New file:** `client/src-tauri/src/thumbnail.rs`

```rust
use image::DynamicImage;
use std::path::Path;

const CACHE_DIR: &str = ".pocketpaw/cache/thumbnails";

#[tauri::command]
pub async fn generate_thumbnail(
    path: String,
    width: u32,
    height: u32
) -> Result<String, String> {
    // Returns base64 PNG string (or file:// URL to cached thumbnail)
    // 1. Check cache (keyed by path + modified timestamp)
    // 2. Generate based on file type
    // 3. Cache result
    // 4. Return
}

#[tauri::command]
pub async fn generate_thumbnails_batch(
    entries: Vec<ThumbnailRequest>,
    width: u32,
    height: u32
) -> Result<Vec<ThumbnailResult>, String> {
    // Parallel batch generation using tokio::spawn
}
```

**Dependencies to add to Cargo.toml:**
- `image = "0.25"` — Image resizing
- `syntect = "5"` — Code syntax highlighting for code thumbnails

For PDFs: use `mupdf-rs` or shell out to a PDF renderer.
For video: shell out to `ffmpeg` (if available) to extract first frame.

### 4.2 — Thumbnail integration in FileCard

**Update:** `client/src/lib/components/explorer/FileCard.svelte`
- On mount: call `generate_thumbnail` for the file
- Show placeholder (colored bg + file icon) while loading
- Fade in the real thumbnail when ready
- Cache thumbnail URLs in an in-memory map to avoid re-generating

**Update:** `client/src/lib/components/explorer/FileGrid.svelte`
- Use `IntersectionObserver` for lazy loading — only generate thumbnails for visible cards
- Batch thumbnail requests for visible cards

### 4.3 — Search overlay

**New file:** `client/src/lib/components/explorer/SearchOverlay.svelte`

Modal overlay (Cmd+K or 🔍 button):
- Search input with instant results
- Three tabs: "Files" (filename match), "Content" (full-text), "AI" (natural language)
- Results show: file icon, name, path, match preview snippet
- Click result → navigate to file
- Enter on first result → open it

### 4.4 — Full-text search (Rust)

**New file:** `client/src-tauri/src/search.rs`

```rust
#[tauri::command]
pub async fn search_files(
    root: String,
    query: String,
    max_results: u32
) -> Result<Vec<SearchResult>, String> {
    // Walk directory tree, search file contents
    // Use ripgrep-style matching for speed
    // Return file path + matching line + context
}
```

For AI-powered search: route the query through the chat backend and return file paths from the agent's response.

### 4.5 — Spacebar quick peek

**New file:** `client/src/lib/components/explorer/QuickPeek.svelte`

Modal overlay that appears when pressing Spacebar with a file selected:
- Centered modal with file preview (uses same preview renderers)
- Press Spacebar again or Esc to close
- Press Enter to open in full detail view
- Smaller than full detail view (~70% of screen)

---

## Phase 5: Remote Files + Cloud Storage

Goal: Connect agent workspace and cloud storage providers.

### 5.1 — Remote filesystem provider

**New file:** `client/src/lib/filesystem/remote.ts`

Implements `FileSystemProvider` using REST API:
- `readDir` → `GET /api/v1/workspace/files?path=...`
- `readFile` → `GET /api/v1/workspace/files/content?path=...`
- `writeFile` → `PUT /api/v1/workspace/files/content?path=...`
- `deleteFile` → `DELETE /api/v1/workspace/files?path=...`
- `watch` → Listen for WebSocket file events from agent

**Backend (Python) — new endpoints:**
- **New file:** `src/pocketpaw/dashboard/routes/workspace.py`
  - File CRUD endpoints for `~/.pocketpaw/workspace/`
  - Serve file content with proper MIME types
  - Generate thumbnails for remote files

### 5.2 — Cloud providers

**New file:** `client/src/lib/filesystem/cloud/types.ts`
- `CloudAccount` interface: id, provider, name, accessToken, refreshToken

**New file:** `client/src/lib/filesystem/cloud/google-drive.ts`
- `GoogleDriveProvider implements FileSystemProvider`
- Uses Google Drive REST API v3
- OAuth token from settings

**New file:** `client/src/lib/filesystem/cloud/dropbox.ts`
- `DropboxProvider implements FileSystemProvider`

**New file:** `client/src/lib/filesystem/cloud/s3.ts`
- `S3Provider implements FileSystemProvider`
- Configurable endpoint (supports R2, MinIO, etc.)

### 5.3 — Cloud settings UI

**New file:** `client/src/lib/components/explorer/CloudSettings.svelte`
- Add/remove cloud accounts
- Configure S3 endpoints
- OAuth flows for Google Drive and Dropbox
- Test connection button

### 5.4 — Update Home view

**Update:** `client/src/lib/components/explorer/HomeView.svelte`
- Show cloud providers as source sections
- Connected accounts show their root folders
- Quick setup cards for unconfigured providers

---

## Phase 6: Polish + Mobile

Goal: Animations, mobile adaptation, keyboard shortcuts, final polish.

### 6.1 — File animations

**Update:** `client/src/lib/components/explorer/FileGrid.svelte` and `FileCard.svelte`
- New file created by agent: card fades in with a subtle scale animation
- File deleted: card fades out + shrinks
- File moved: card slides out of old position
- File edited: thumbnail briefly pulses/glows

Use Svelte transitions: `transition:fly`, `transition:fade`, `transition:scale`.

### 6.2 — Mobile responsive

**Update:** `client/src/lib/components/explorer/ExplorerShell.svelte`
- `< 640px`: Hide chat sidebar, show 💬 floating button
- 💬 button opens `ChatBottomSheet.svelte` (slides up from bottom, 70% height)
- File grid: 2 columns on phone
- NavBar: compact mode (hamburger menu for breadcrumbs)

**New file:** `client/src/lib/components/explorer/ChatBottomSheet.svelte`
- Bottom sheet overlay with drag handle
- Contains full chat functionality
- Swipe down to dismiss

**Update:** `client/src/lib/components/explorer/DetailView.svelte`
- Full screen on mobile
- Swipe right to go back to grid
- Chat accessible via floating 💬 button

### 6.3 — Keyboard shortcuts

**Update:** Various components

| Shortcut | Action |
|----------|--------|
| `Cmd+K` / `Ctrl+K` | Open search overlay |
| `Spacebar` | Quick peek selected file |
| `Enter` | Open selected file in detail view |
| `Esc` | Close detail view / search / quick peek |
| `Backspace` | Go back (detail → browse, or navigate up) |
| `←` `→` | Previous/next file in detail view |
| `Cmd+1-5` | Switch view mode (Icon/Grid/List/Column/Gallery) |
| `Cmd+A` | Select all files |
| `Delete` | Delete selected files |
| `Cmd+N` | New chat |
| `Cmd+/` | Toggle chat sidebar |

### 6.4 — Pinned folders

**Update:** `client/src/lib/stores/explorer.svelte.ts`
- `pinnedFolders: PinnedFolder[]` saved to localStorage
- Methods: `pinFolder(path, source)`, `unpinFolder(path)`, `reorderPins(...)`

**Update:** `client/src/lib/components/explorer/HomeView.svelte`
- Drag folders to the "Pinned" section
- Right-click → "Pin to Home"
- Reorder pinned folders by drag

### 6.5 — Version history (stretch goal)

**New file:** `client/src/lib/components/explorer/VersionHistory.svelte`
- Track file versions when the agent edits files
- Accessible from detail view → More menu → "Version History"
- Shows list of versions with timestamps and diffs
- Click to preview old version, button to restore

---

## Dependency Graph

```
1.1 (deps) ─────────────────────────────────────────┐
1.2 (rust fs) ──┐                                    │
1.3 (watcher) ──┼→ 1.4 (fs provider) → 1.5 (store) ─┼→ 1.6 (shell)
                │                                     │     │
                │   1.7 (navbar) ─────────────────────┘     │
                │   1.8 (file card + grid) ─────────────────┤
                │   1.9 (context menu) ─────────────────────┤
                │                                           │
                │                              1.10 (routes) ← 1.11 (home)
                │
                ├→ 2.1 (chat context) → 2.2 (drag files)
                ├→ 2.3 (multi chat)
                ├→ 2.4 (agent file events) → 2.5 (terminal blocks)
                └→ 2.6 (file action cards)

3.1 (list view)
3.2 (column view)     ─→ all independent, can be parallel
3.3 (gallery view)
3.4 (detail view) → 3.5 (preview renderers) → 3.6 (file nav)

4.1 (rust thumbnails) → 4.2 (thumbnail in cards)
4.3 (search overlay) → 4.4 (full-text search)
4.5 (quick peek)

5.1 (remote fs) → backend Python changes
5.2 (cloud providers)
5.3 (cloud settings)
5.4 (home view update)

6.1 (animations)
6.2 (mobile)
6.3 (keyboard shortcuts)
6.4 (pinned folders)
6.5 (version history)
```

## File Count Summary

| Category | New Files | Modified Files |
|----------|-----------|----------------|
| Rust (src-tauri) | 4 (fs_commands, fs_watcher, thumbnail, search) | 2 (lib.rs, Cargo.toml) |
| Filesystem TS | 6 (types, local, remote, index, cloud/*) | 0 |
| Stores | 1 (explorer.svelte.ts) | 2 (chat, index) |
| Explorer components | 18 (shell, navbar, grid, card, list, column, gallery, detail, chat sidebar, context pill, context menu, search, quick peek, home, bottom sheet, undo toast, file action card, cloud settings) | 2 (AppShell, +page) |
| Preview components | 11 (dispatcher + 10 renderers) | 0 |
| Backend (Python) | 1 (workspace routes) | 3 (events, loop, websocket) |
| **Total** | **~41 new** | **~9 modified** |
