# PocketPaw Architecture

This document describes the high-level architecture of PocketPaw, a self-hosted AI agent that runs locally and communicates via multiple channels. It serves as the canonical reference for understanding how the system's components interact.

## System Overview

PocketPaw is an event-driven, message-bus-based system where all communication flows through a central `MessageBus`. The architecture emphasizes modularity, async-first design, and protocol-oriented interfaces for easy extension.

### End-to-End Request Flow

```
Inbound Channels (Telegram, Discord, Slack, WhatsApp, WebSocket)
         │
         ▼
   ChannelAdapter (bus/adapters/)
         │  InboundMessage
         ▼
     MessageBus  (bus/queue.py)
         │  fan-out to subscribers
         ▼
      AgentLoop  (agents/loop.py)
         │  builds context + history
         ▼
     AgentRouter (agents/router.py)
         │  lazy-loads backend via registry
         ▼
   AgentBackend  (agents/<name>.py)
   ┌────────────────────────────────┐
   │  Tools      (tools/)           │
   │  Memory     (memory/)          │
   │  Security   (security/)        │
   │  Browser    (browser/)         │
   └────────────────────────────────┘
         │  AgentEvent stream
         ▼
      AgentLoop  (flush stream)
         │  OutboundMessage / SystemEvent
         ▼
     MessageBus  (fan-out)
         │
         ├──► ChannelAdapter.send()  →  Telegram / Discord / etc.
         └──► WebSocketAdapter.send() → Web dashboard
```

## Module Responsibilities

| Package | Responsibility |
|---------|---------------|
| `bus/` | Event types, `MessageBus` pub/sub queue, all channel adapters |
| `agents/` | `AgentLoop`, `AgentRouter`, backend registry, all SDK wrappers |
| `tools/` | `ToolProtocol`, `BaseTool`, `ToolRegistry`, built-in tools |
| `memory/` | Session history, long-term fact storage, `MemoryStoreProtocol` |
| `security/` | GuardianAgent, `security/rails.py` danger patterns, audit log |
| `browser/` | Playwright automation, accessibility-tree snapshots, `BrowserDriver` |
| `bootstrap/` | `AgentContextBuilder` — assembles system prompt from identity + memory |
| `config.py` | Pydantic `Settings`, `POCKETPAW_` env prefix, JSON config |
| `api/` | FastAPI app, REST routes, WebSocket endpoint, auth middleware |
| `frontend/` | Vanilla JS/HTML/CSS dashboard served via Jinja2 (no build step) |
| `daemon/` | Background process management, auto-restart, PID file |
| `deep_work/` | Structured focus sessions with goals, scheduler, and human-review tasks |
| `mission_control/` | Multi-agent orchestration and dashboard aggregation |

## Sync vs Async Boundaries

Everything from `MessageBus` inward is fully async (Python `asyncio`).

- `ChannelAdapter` implementations bridge sync SDKs (e.g., python-telegram-bot callbacks) to the async bus via `asyncio.run_coroutine_threadsafe` or equivalent.
- The audit log uses a sync file write wrapped in `asyncio.to_thread` to avoid blocking the event loop.
- `BrowserDriver` (Playwright) is async-native; no bridging needed.

## Streaming Flow

- Backend `run()` yields `AgentEvent(type="message", content=chunk)` for each token.
- `AgentLoop` forwards each chunk as `OutboundMessage(is_stream_chunk=True)`.
- `WebSocketAdapter` sends each chunk JSON frame to the dashboard immediately.
- `TelegramAdapter` / `DiscordAdapter` buffer chunks and edit-in-place; Discord uses a 1.5s rate-limit guard.
- `AgentLoop` detects `AgentEvent(type="done")` and flushes with `OutboundMessage(is_stream_end=True)`.
- `WhatsAppAdapter` accumulates all chunks and sends a single message on `stream_end` (no streaming supported by the API).

## REST, WebSocket, and Channel Adapters

- **REST API** (`/api/v1/…`) — stateless CRUD for settings, memory, channels, sessions, skills, reminders, MCP, etc. Auth-protected; all mutation routes require a valid token.
- **WebSocket** (`/ws`) — bidirectional; carries `InboundMessage` from dashboard user → bus, and `OutboundMessage` + `SystemEvent` from bus → dashboard. Single persistent connection per session.
- **Channel adapters** — each adapter subscribes to `OutboundMessage` for its own `Channel` enum value and publishes `InboundMessage` on user input. They never call agent code directly.

## Dashboard ↔ Backend Interaction

```
Browser (dashboard)
   │
   ├── WebSocket (/ws)
   │     ├── send: { type: "message", content: "…" }  → InboundMessage on bus
   │     └── recv: OutboundMessage chunks / SystemEvent (tool_start, thinking, …)
   │
   └── REST (/api/v1/)
         ├── GET  /channels/status   → adapter running states
         ├── POST /channels/toggle   → start / stop adapter
         ├── GET  /memory            → long-term facts
         ├── POST /settings          → write config.json
         └── … (see api/ for full list)
```

The Activity panel in the dashboard consumes `SystemEvent` frames (tool use, thinking, errors) that are **not** forwarded to non-dashboard channels.

## Deep Work and Mission Control

- **Deep Work** (`deep_work/`) — structured focus sessions with goals, scheduler, and human-review tasks. Exposed via `/api/v1/deep-work/…`.
- **Mission Control** — dashboard view aggregating agent status, active sessions, channel health, and audit log tail.