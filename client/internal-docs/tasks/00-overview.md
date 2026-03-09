# PocketPaw Desktop Client — Task Overview

> Build order matters. Each task builds on the previous ones.

## Current State

- **Tauri v2** scaffold with SvelteKit, Svelte 5 (runes), adapter-static (SPA mode)
- **Tailwind CSS 4.2** via Vite plugin, OKLch color tokens, dark mode ready
- **shadcn-svelte** configured (components.json, utils.ts, lucide icons) — no components installed yet
- **Bun** as package manager
- Default template page — no app code yet

## Build Order

| #  | Task File                        | What                                      | Depends On |
|----|----------------------------------|-------------------------------------------|------------|
| 01 | `01-api-layer.md`                | TypeScript API client + WebSocket + types | —          |
| 02 | `02-stores.md`                   | Svelte 5 stores (agent, sessions, settings) | 01       |
| 03 | `03-shell-layout.md`             | App shell: sidebar + main area + routing  | 02         |
| 04 | `04-onboarding.md`               | 3-screen onboarding wizard                | 03         |
| 05 | `05-chat-interface.md`           | Chat panel: messages, input, streaming    | 03         |
| 06 | `06-session-management.md`       | Session list, create/switch/delete/search | 05         |
| 07 | `07-skill-cards.md`              | Skill browser + skill interaction flow    | 05         |
| 08 | `08-settings-panel.md`           | Settings UI (AI model, channels, security)| 03         |
| 09 | `09-activity-panel.md`           | Agent activity feed (tools, thinking)     | 05         |
| 10 | `10-system-tray.md`              | Tauri system tray, global hotkey, notifs  | 05         |
| 11 | `11-side-panel.md`               | Universal AI side panel (second window)   | 10         |

## Backend Connection

The client connects to PocketPaw Python backend at `http://localhost:8888`:
- **REST API**: `/api/v1/*` endpoints for CRUD operations
- **WebSocket**: `/ws?token=<token>` for real-time chat streaming
- **Auth**: Master token from `~/.pocketpaw/access_token` → session token via `/auth/session`

## Design Reference Docs

| Doc | What |
|-----|------|
| `internal-docs/design-system.md` | Colors, typography, glass effects, shadows, animations, component patterns |
| `internal-docs/titlebar-spec.md` | Platform-specific title bar: macOS vibrancy, Windows Mica, Linux adaptive + PocketPaw features (session info, progress bar, quick actions, connection badge) |
| `internal-docs/desktop-client-ux.md` | Full UX spec: onboarding, chat, skill cards, side panel, notifications |
| `internal-docs/agentic-os-vision.md` | Product vision, competitive landscape, architecture |

## Tech Stack

| Layer     | Choice              |
|-----------|---------------------|
| Shell     | Tauri v2 (Rust)     |
| UI        | SvelteKit + Svelte 5 (runes) |
| Styling   | Tailwind CSS 4 + shadcn-svelte |
| Icons     | Lucide Svelte       |
| Fonts     | Inter (UI) + JetBrains Mono (code) |
| State     | Svelte 5 `$state` runes |
| Transport | WebSocket + fetch   |
| Build     | Vite + Bun          |

## Design Identity

- **Dark mode default** — warm dark grays, not cold slate
- **Amber accent** (`oklch(0.78 0.15 65)`) — warm, friendly, distinctive
- **Glass sidebar** — translucent with `backdrop-filter: blur(16px)`
- **Platform-native title bar** — macOS vibrancy, Windows Mica, Linux DE-adaptive
- **Inter font at 13px** — macOS-level density, `font-medium` for UI elements
- **No chat bubbles on assistant** — clean text, user messages get amber tint
