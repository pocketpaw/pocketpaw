# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PocketPaw is a Tauri v2 desktop client for the PocketPaw AI assistant backend. The frontend is SvelteKit (Svelte 5 runes mode) with Tailwind CSS 4 and shadcn-svelte components. The backend lives at the repo root (`../`) and runs on `localhost:8888`. This client directory is part of the pocketpaw monorepo.

## Development Commands

```bash
# Frontend dev server (port 1420)
bun run dev

# Full desktop app (frontend + Tauri shell)
bun run tauri dev

# Type checking
bun run check
bun run check:watch

# Build
bun run build              # Frontend only
bun run tauri build        # Full desktop app

# Mobile
bun run tauri:android      # Android dev
bun run tauri:ios          # iOS dev
```

**Note:** The backend must be running on `localhost:8888` for the app to function. There are no test commands configured.

## Architecture

### App Initialization Flow

`+layout.svelte` is the entry point. It handles authentication (OAuth token from `~/.pocketpaw/access_token` or OAuth flow), then calls `initializeStores(token)` which:
1. Creates the REST client and exchanges the token for a session cookie
2. Connects the WebSocket
3. Binds WebSocket events to all stores
4. Loads sessions and settings via REST in the background

### Routing

SPA mode — SSR is disabled (`+layout.ts`), static adapter with `fallback: "index.html"`. Routes:
- `/` — Chat (main view)
- `/settings`, `/explore`, `/activity` — wrapped in AppShell with sidebar
- `/sidepanel`, `/quickask` — separate Tauri windows, skip AppShell, initialize stores independently
- `/onboarding` — first-run wizard
- `/oauth-callback` — OAuth redirect handler

The layout detects the route and conditionally renders AppShell vs standalone window layouts.

### State Management

Stores are Svelte 5 rune-based class singletons in `src/lib/stores/`. Each store exports a single instance:

- **connectionStore** — REST client + WebSocket lifecycle
- **chatStore** — Messages, streaming, abort control
- **sessionStore** — Session list, active session
- **settingsStore** — Backend settings, channel configs
- **activityStore** — Activity log entries
- **skillStore** — Available skills
- **uiStore** — Sidebar state, search focus
- **platformStore** — Platform detection (desktop/mobile/tablet)

Stores that need real-time updates expose `bindEvents(ws)` / `disposeEvents()` for WebSocket wiring.

### API Layer

- **REST:** `PocketPawClient` in `src/lib/api/client.ts` — auto-retries on 401 with token refresh, SSE streaming for chat
- **WebSocket:** `PocketPawWebSocket` in `src/lib/api/websocket.ts` — auto-reconnect with exponential backoff, heartbeat every 30s
- **Config:** `src/lib/api/config.ts` — `BACKEND_URL = "http://localhost:8888"`, `API_PREFIX = "/api/v1"`

### Tauri (Rust Side)

`src-tauri/src/lib.rs` is the entry point. Desktop-only features use `#[cfg(desktop)]`:
- **System tray** (`tray.rs`) — menu with Open, Quick Ask, Settings, Quit
- **Close-to-tray** — intercepts window close, hides instead
- **Multi-window** (`side_panel.rs`, `quick_ask.rs`) — created via `WebviewWindowBuilder`
- **Global shortcuts** — registered from the frontend via `tauri-plugin-global-shortcut`
- **Invoke commands** (`commands.rs`, `oauth.rs`) — `read_access_token`, `toggle_side_panel`, OAuth token CRUD

### Auth Flow

OAuth PKCE flow in `src/lib/auth/`:
1. Read stored tokens → if valid, use directly
2. If expired, refresh via refresh token
3. If no tokens, open OAuth browser window, handle callback at `/oauth-callback`
4. Exchange access token for session cookie via `POST /auth/login`
5. Token updates emit `token-updated` Tauri event for multi-window sync

## Svelte 5 Rules (Critical)

- **Props:** `let { prop } = $props()` — use `let`, not `const`
- **Derived:** `$derived(expr)` for expressions, `$derived.by(() => {...})` for function bodies. `$derived(() => {...})` is WRONG (wraps a function, doesn't execute it)
- **Dynamic components:** `<svelte:component>` is deprecated. Use uppercase variables: `let Icon = $derived(...)` then `<Icon />`
- **`{@const}`** only works inside block tags (`{#if}`, `{#each}`), not at template top level

## Tailwind CSS 4 Rules (Critical)

- **Never use string interpolation in class attributes:** `class="static {dynamic}"` breaks Tailwind 4's Vite plugin. Always use `class={cn("static", dynamic)}` or `class={expression}`
- Dark mode uses `.dark` class variant: `@custom-variant dark (&:is(.dark *));`
- Design tokens are oklch-based CSS custom properties in `src/styles/global.css`

## Conventions

- **Package manager:** Bun
- **Icons:** `@lucide/svelte` — import from `@lucide/svelte/icons/icon-name`
- **UI components:** shadcn-svelte in `src/lib/components/ui/`
- **Utility function:** `cn()` from `$lib/utils` (clsx + tailwind-merge)
- **Path aliases:** `$lib` → `src/lib`, `@/*` → `src/lib/*`
- **Fonts:** Inter (variable) for body, JetBrains Mono (variable) for code
- **DOMPurify:** Option is `ALLOWED_TAGS` (not `ALLOW_TAGS`)

## Tauri 2 Gotchas

- `Image::from_bytes(include_bytes!("..."))` — no `from_png` method
- `tauri::Emitter` trait must be imported for `.emit()` on `AppHandle`
- Tray closures need explicit types: `|app: &AppHandle<Wry>, event|`
- `tauri-plugin-autostart` v2 has no `xdg` feature
- Capabilities defined in `src-tauri/capabilities/default.json` (desktop) and `mobile.json`
