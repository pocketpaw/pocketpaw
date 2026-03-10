# Task 02: Svelte 5 Stores

> Reactive state management using Svelte 5 runes, powered by the API layer.

## Goal

Create `$lib/stores/` — reactive state that the entire UI reads from and writes to.

## Depends On

- **Task 01** (API layer): `PocketPawClient`, `PocketPawWebSocket`, all types

## Files to Create

```
src/lib/stores/
├── connection.svelte.ts   # Connection state + auth
├── chat.svelte.ts         # Current chat messages + streaming state
├── sessions.svelte.ts     # Session list + active session
├── settings.svelte.ts     # App settings
├── activity.svelte.ts     # Agent activity feed (tools, thinking)
├── skills.svelte.ts       # Skills list
└── index.ts               # Re-exports
```

## Svelte 5 Runes Pattern

Use Svelte 5's `$state` and `$derived` runes. Export class-based stores:

```typescript
// Pattern: class with $state fields
class ChatStore {
  messages = $state<ChatMessage[]>([]);
  isStreaming = $state(false);
  streamingContent = $state("");

  // Methods mutate $state directly
  addMessage(msg: ChatMessage) {
    this.messages.push(msg);
  }
}

export const chatStore = new ChatStore();
```

## 1. connection.svelte.ts

Manages the WebSocket lifecycle and authentication.

```typescript
class ConnectionStore {
  // State
  status = $state<"disconnected" | "connecting" | "connected">("disconnected");
  token = $state<string | null>(null);
  sessionId = $state<string | null>(null);
  error = $state<string | null>(null);

  // Derived
  isConnected = $derived(this.status === "connected");

  // Internals (not exported)
  private ws: PocketPawWebSocket | null = null;
  private client: PocketPawClient | null = null;

  // Methods
  async initialize(token: string): Promise<void>
    // Create client + ws, connect, set status
  disconnect(): void
  getClient(): PocketPawClient  // throws if not connected
  getWebSocket(): PocketPawWebSocket  // throws if not connected
}
```

This store is the single owner of `PocketPawClient` and `PocketPawWebSocket` instances. Other stores access them through `connectionStore.getClient()`.

## 2. chat.svelte.ts

Manages the current conversation.

```typescript
class ChatStore {
  messages = $state<ChatMessage[]>([]);
  isStreaming = $state(false);
  streamingContent = $state("");  // accumulates chunks during streaming
  error = $state<string | null>(null);

  // Derived
  isEmpty = $derived(this.messages.length === 0);
  lastMessage = $derived(this.messages[this.messages.length - 1] ?? null);

  // Methods
  sendMessage(content: string, media?: MediaAttachment[]): void
    // 1. Push user message to messages[]
    // 2. Send via WebSocket: {action: "chat", content, media}
    // 3. Set isStreaming = true
    // Streaming chunks are handled by WebSocket event listeners (see bindEvents)

  stopGeneration(): void
    // Send {action: "stop"} via WebSocket

  loadHistory(messages: ChatMessage[]): void
    // Replace messages[] with loaded history

  clearMessages(): void
    // Reset for new session

  // Called during initialization to wire up WebSocket events
  bindEvents(ws: PocketPawWebSocket): void
    // on("stream_start") → isStreaming = true, streamingContent = ""
    // on("chunk") → streamingContent += chunk.data.content
    // on("stream_end") → push assistant message, isStreaming = false
    // on("message") → push assistant message (non-streaming)
    // on("error") → set error
}
```

## 3. sessions.svelte.ts

Manages the session list and active session switching.

```typescript
class SessionStore {
  sessions = $state<Session[]>([]);
  activeSessionId = $state<string | null>(null);
  isLoading = $state(false);

  // Derived
  activeSession = $derived(
    this.sessions.find(s => s.id === this.activeSessionId) ?? null
  );

  // Methods
  async loadSessions(): Promise<void>
    // GET /sessions → populate sessions[]

  async switchSession(sessionId: string): Promise<void>
    // 1. Send {action: "switch_session", session_id} via WS
    // 2. Set activeSessionId
    // 3. Load history into chatStore

  async createNewSession(): Promise<void>
    // 1. Send {action: "new_session"} via WS
    // 2. Clear chatStore
    // 3. Prepend new session to sessions[]

  async deleteSession(sessionId: string): Promise<void>
    // DELETE /sessions/{id}
    // Remove from sessions[]
    // If active, switch to another

  async renameSession(sessionId: string, title: string): Promise<void>
    // POST /sessions/{id}/title

  async searchSessions(query: string): Promise<Session[]>
    // GET /sessions/search?q=...

  // Wire up WebSocket events
  bindEvents(ws: PocketPawWebSocket): void
    // on("new_session") → update activeSessionId
    // on("session_history") → update chatStore
}
```

## 4. settings.svelte.ts

App configuration state.

```typescript
class SettingsStore {
  settings = $state<Settings | null>(null);
  isLoading = $state(false);

  // Derived convenience accessors
  agentBackend = $derived(this.settings?.agent_backend ?? "claude_agent_sdk");
  model = $derived(this.settings?.model ?? "");

  // Methods
  async load(): Promise<void>
    // GET /settings

  async update(patch: Partial<Settings>): Promise<void>
    // PUT /settings
    // Merge into local state

  async saveApiKey(provider: string, key: string): Promise<void>
    // Send via WS: {action: "save_api_key", provider, key}
}
```

## 5. activity.svelte.ts

Agent execution activity feed (tool calls, thinking, errors).

```typescript
interface ActivityEntry {
  id: string;
  type: "tool_start" | "tool_result" | "thinking" | "error" | "status";
  content: string;
  data?: Record<string, unknown>;
  timestamp: string;
}

class ActivityStore {
  entries = $state<ActivityEntry[]>([]);
  isAgentWorking = $state(false);

  // Derived
  recentEntries = $derived(this.entries.slice(-50)); // keep last 50

  // Methods
  clear(): void

  // Wire up WebSocket events
  bindEvents(ws: PocketPawWebSocket): void
    // on("tool_start") → push entry, isAgentWorking = true
    // on("tool_result") → push entry
    // on("thinking") → push entry
    // on("error") → push entry
    // on("stream_end") → isAgentWorking = false
}
```

## 6. skills.svelte.ts

Skills list and search.

```typescript
class SkillStore {
  skills = $state<Skill[]>([]);
  isLoading = $state(false);
  searchResults = $state<Skill[]>([]);

  // Methods
  async load(): Promise<void>
    // GET /skills

  async search(query: string): Promise<void>
    // GET /skills/search?q=...

  async install(identifier: string): Promise<void>
    // POST /skills/install

  async remove(name: string): Promise<void>
    // POST /skills/remove
}
```

## 7. index.ts — Initialize + Re-export

```typescript
export { connectionStore } from "./connection.svelte";
export { chatStore } from "./chat.svelte";
export { sessionStore } from "./sessions.svelte";
export { settingsStore } from "./settings.svelte";
export { activityStore } from "./activity.svelte";
export { skillStore } from "./skills.svelte";

// Master init function — called once on app startup
export async function initializeStores(token: string): Promise<void> {
  await connectionStore.initialize(token);
  const ws = connectionStore.getWebSocket();
  chatStore.bindEvents(ws);
  sessionStore.bindEvents(ws);
  activityStore.bindEvents(ws);
  await sessionStore.loadSessions();
  await settingsStore.load();
  await skillStore.load();
}
```

## Acceptance Criteria

- [ ] All stores use Svelte 5 `$state` and `$derived` runes (NOT writable/readable stores from Svelte 4)
- [ ] Stores are class-based singletons exported as `const xxxStore = new XxxStore()`
- [ ] WebSocket events are correctly wired to store mutations
- [ ] Chat streaming works: chunks accumulate → final message pushed on stream_end
- [ ] Session switching loads history into chatStore
- [ ] No direct DOM/UI code — pure state management

## Notes

- Svelte 5 runes (`$state`, `$derived`) only work in `.svelte.ts` files or `.svelte` components.
- Don't use Svelte 4 store syntax (`writable`, `readable`, `$:`)
- The connection store is the central hub — other stores depend on it for API access.
- Keep stores focused — each owns one domain concept.
