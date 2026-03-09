# Task 01: API Layer

> TypeScript client for the PocketPaw REST API and WebSocket connection.

## Goal

Create `$lib/api/` — the foundation every other component uses to talk to the backend.

## Files to Create

```
src/lib/api/
├── types.ts          # All TypeScript types/interfaces
├── client.ts         # REST API client (fetch-based)
├── websocket.ts      # WebSocket client with reconnect
└── index.ts          # Re-exports
```

## 1. types.ts — Shared Types

Define interfaces matching the backend's event model:

```typescript
// Message types matching bus/events.py
interface InboundMessage {
  action: "chat";
  content: string;
  media?: MediaAttachment[];
}

interface OutboundMessage {
  type: "message" | "chunk" | "stream_start" | "stream_end" | "error";
  content?: string;
  data?: Record<string, unknown>;
}

interface SystemEvent {
  type: "tool_start" | "tool_result" | "thinking" | "error" | "status";
  content?: string;
  data?: Record<string, unknown>;
  timestamp?: string;
}

// WebSocket actions
type WSAction =
  | { action: "chat"; content: string; media?: MediaAttachment[] }
  | { action: "stop" }
  | { action: "new_session" }
  | { action: "switch_session"; session_id: string }
  | { action: "resume_session" }
  | { action: "get_settings" }
  | { action: "settings"; [key: string]: unknown }
  | { action: "save_api_key"; provider: string; key: string }
  | { action: "authenticate"; token: string }
  | { action: "tool"; tool: string; path?: string }
  | { action: "file_browse"; path: string; context?: string };

// WebSocket incoming events
type WSEvent =
  | { type: "connection_info"; content: string; id: string }
  | { type: "session_history"; session_id: string; messages: ChatMessage[] }
  | { type: "new_session"; id: string }
  | { type: "message"; content: string }
  | { type: "chunk"; data: { content: string; type: string } }
  | { type: "stream_start" }
  | { type: "stream_end"; usage?: TokenUsage }
  | { type: "tool_start"; tool: string; input: Record<string, unknown> }
  | { type: "tool_result"; tool: string; output: string }
  | { type: "thinking"; content: string }
  | { type: "error"; content: string }
  | { type: "status"; content: string }
  | { type: "settings"; content: Settings }
  | { type: "notification"; content: string }
  | { type: "files"; path: string; files: FileEntry[] };

// Domain models
interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  timestamp?: string;
  media?: MediaAttachment[];
}

interface MediaAttachment {
  type: "image" | "file" | "audio";
  url?: string;
  data?: string; // base64
  filename?: string;
  mime_type?: string;
}

interface Session {
  id: string;
  title: string;
  channel: string;
  last_activity: string;
  message_count: number;
}

interface TokenUsage {
  input_tokens?: number;
  output_tokens?: number;
}

interface Settings {
  agent_backend: string;
  llm_provider?: string;
  model?: string;
  // ... other fields from GET /settings
}

interface Skill {
  name: string;
  description: string;
  category?: string;
  // from GET /skills
}

interface ChannelStatus {
  channel: string;
  configured: boolean;
  running: boolean;
  autostart: boolean;
}

interface FileEntry {
  name: string;
  type: "file" | "directory";
  size?: number;
  modified?: string;
}

interface HealthSummary {
  status: string;
  checks: Record<string, unknown>;
}
```

## 2. client.ts — REST API Client

A class that wraps `fetch` for all REST endpoints.

Requirements:
- Constructor takes `baseUrl` (default: `http://localhost:8888`) and `token`
- All methods return typed responses
- Handle auth: `Authorization: Bearer <token>` header
- Handle errors: throw typed errors with status code and message
- Support both JSON and SSE responses

Key methods to implement:

```typescript
class PocketPawClient {
  constructor(baseUrl?: string, token?: string)

  // Auth
  async login(token: string): Promise<void>
  async logout(): Promise<void>
  async regenerateToken(): Promise<string>

  // Chat (REST — used for non-streaming or SSE fallback)
  async chat(content: string, media?: MediaAttachment[]): Promise<string>
  async chatStream(content: string, onChunk: (chunk: string) => void): Promise<void>
  async stopChat(sessionId: string): Promise<void>

  // Sessions
  async listSessions(limit?: number): Promise<Session[]>
  async getSessionHistory(sessionId: string): Promise<ChatMessage[]>
  async deleteSession(sessionId: string): Promise<void>
  async updateSessionTitle(sessionId: string, title: string): Promise<void>
  async searchSessions(query: string): Promise<Session[]>

  // Settings
  async getSettings(): Promise<Settings>
  async updateSettings(settings: Partial<Settings>): Promise<void>

  // Skills
  async listSkills(): Promise<Skill[]>
  async searchSkills(query: string): Promise<Skill[]>
  async installSkill(identifier: string): Promise<void>
  async removeSkill(name: string): Promise<void>

  // Memory
  async getLongTermMemory(): Promise<MemoryEntry[]>
  async deleteMemory(entryId: string): Promise<void>
  async getMemoryStats(): Promise<Record<string, unknown>>

  // Channels
  async getChannelStatus(): Promise<ChannelStatus[]>
  async saveChannel(channel: string, config: Record<string, string>): Promise<void>
  async toggleChannel(channel: string, action: "start" | "stop"): Promise<void>

  // Health
  async getHealth(): Promise<HealthSummary>
  async getVersion(): Promise<{ version: string; update_available?: boolean }>

  // Backends
  async listBackends(): Promise<BackendInfo[]>

  // Reminders
  async listReminders(): Promise<Reminder[]>
  async addReminder(text: string): Promise<void>
  async deleteReminder(id: string): Promise<void>
}
```

## 3. websocket.ts — WebSocket Client

A reactive WebSocket client with auto-reconnect and event dispatching.

Requirements:
- Connect to `ws://localhost:8888/ws?token=<token>`
- Auto-reconnect with exponential backoff (1s, 2s, 4s, 8s, max 30s)
- Expose connection state as observable (`connected`, `connecting`, `disconnected`)
- Event callback system: `on(eventType, handler)`
- Send typed actions: `send(action: WSAction)`
- Handle authentication flow
- Heartbeat/ping to detect dead connections

```typescript
type ConnectionState = "connecting" | "connected" | "disconnected";
type EventHandler = (event: WSEvent) => void;

class PocketPawWebSocket {
  constructor(url?: string, token?: string)

  state: ConnectionState;
  sessionId: string | null;

  connect(): void
  disconnect(): void
  send(action: WSAction): void

  // Event listeners
  on(type: string, handler: EventHandler): () => void  // returns unsubscribe fn
  onAny(handler: EventHandler): () => void

  // Convenience
  chat(content: string, media?: MediaAttachment[]): void
  stopGeneration(): void
  newSession(): void
  switchSession(sessionId: string): void
  requestSettings(): void
}
```

## 4. index.ts — Re-exports

```typescript
export * from "./types";
export { PocketPawClient } from "./client";
export { PocketPawWebSocket } from "./websocket";
```

## Backend Reference

- REST base: `http://localhost:8888/api/v1/`
- WebSocket: `ws://localhost:8888/ws?token=<token>`
- Auth token: read from `~/.pocketpaw/access_token` (Tauri can read this via Rust)
- All REST endpoints expect `Authorization: Bearer <token>` or session cookie

## Acceptance Criteria

- [ ] All types match the backend event model
- [ ] REST client covers all endpoints listed above
- [ ] WebSocket client connects, authenticates, reconnects on disconnect
- [ ] WebSocket dispatches typed events to registered handlers
- [ ] No external HTTP library — use native `fetch` and `WebSocket`
- [ ] Exports are clean — single import point from `$lib/api`

## Notes

- Don't add UI code in this task. This is pure infrastructure.
- The WebSocket URL and REST URL should be configurable (env var or constructor param) for development vs production.
- Consider that in Tauri, the backend runs locally so `localhost` is always correct, but the port might differ.
