import {
  ApiError,
  type BackendInfo,
  type ChannelConfig,
  type ChannelStatusMap,
  type ChannelTestResult,
  type ChatMessage,
  type FileContext,
  type HealthErrorEntry,
  type HealthSummary,
  type IdentityFiles,
  type IdentitySaveResponse,
  type MCPPreset,
  type MCPStatusMap,
  type MCPTestResponse,
  type MediaAttachment,
  type MemoryEntry,
  type MemorySettings,
  type MemoryStats,
  type Reminder,
  type RemindersResponse,
  type SSEAskUser,
  type SSEChunk,
  type SSEError,
  type SSEStreamEnd,
  type SSEThinking,
  type SSEToolResult,
  type SSEToolStart,
  type SecurityAuditResponse,
  type Session,
  type SessionListResponse,
  type Settings,
  type Skill,
  type VersionInfo,
} from "./types";
import type { TaskStatus, TaskPriority, DocumentType } from "$lib/types/pawkit";
import { BACKEND_URL, API_PREFIX } from "./config";

export class PocketPawClient {
  private baseUrl: string;
  private apiBase: string;
  private token: string | null;

  constructor(baseUrl?: string, token?: string) {
    this.baseUrl = (baseUrl ?? BACKEND_URL).replace(/\/+$/, "");
    this.apiBase = `${this.baseUrl}${API_PREFIX}`;
    this.token = token ?? null;
  }

  setToken(token: string) {
    this.token = token;
  }

  /** Returns the API base URL (e.g. "http://localhost:8888/api/v1") used for direct fetch calls */
  getApiBase(): string {
    return this.apiBase;
  }

  /** Returns the WebSocket URL derived from the base URL */
  getWsUrl(): string {
    return this.baseUrl.replace(/^http/, "ws") + `${API_PREFIX}/ws`;
  }

  // ---------------------------------------------------------------------------
  // Internal helpers
  // ---------------------------------------------------------------------------

  private headers(extra?: Record<string, string>): Record<string, string> {
    const h: Record<string, string> = { "Content-Type": "application/json", ...extra };
    if (this.token) h["Authorization"] = `Bearer ${this.token}`;
    return h;
  }

  private async request<T>(
    method: string,
    path: string,
    body?: unknown,
    _retried = false,
  ): Promise<T> {
    const url = `${this.apiBase}${path}`;
    const res = await fetch(url, {
      method,
      headers: this.headers(),
      body: body != null ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) {
      // On 401, try refreshing the token and retrying once
      if (res.status === 401 && !_retried) {
        try {
          const { readTokens } = await import("$lib/auth/token-store");
          const { refreshAccessToken } = await import("$lib/auth/token-refresh");
          const tokens = await readTokens();
          if (tokens) {
            const newTokens = await refreshAccessToken(tokens);
            this.setToken(newTokens.access_token);
            return this.request<T>(method, path, body, true);
          }
        } catch {
          // Refresh failed — fall through to original error
        }
      }
      let detail: string | undefined;
      try {
        const json = await res.json();
        detail = json.detail ?? json.message ?? JSON.stringify(json);
      } catch {
        detail = await res.text().catch(() => undefined);
      }
      throw new ApiError(res.status, `${method} ${path} failed: ${res.status}`, detail);
    }
    const text = await res.text();
    if (!text) return undefined as T;
    return JSON.parse(text) as T;
  }

  private get<T>(path: string) {
    return this.request<T>("GET", path);
  }

  private post<T>(path: string, body?: unknown) {
    return this.request<T>("POST", path, body);
  }

  private put<T>(path: string, body?: unknown) {
    return this.request<T>("PUT", path, body);
  }

  private del<T>(path: string, body?: unknown) {
    return this.request<T>("DELETE", path, body);
  }

  /** Like request(), but targets /api/mission-control instead of /api/v1 */
  private async mcRequest<T>(
    method: string,
    path: string,
    body?: unknown,
  ): Promise<T> {
    const url = `${this.baseUrl}/api/mission-control${path}`;
    const res = await fetch(url, {
      method,
      headers: this.headers(),
      body: body != null ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) {
      let detail: string | undefined;
      try {
        const json = await res.json();
        detail = json.detail ?? json.message ?? JSON.stringify(json);
      } catch {
        detail = await res.text().catch(() => undefined);
      }
      throw new ApiError(res.status, `${method} /mc${path} failed: ${res.status}`, detail);
    }
    const text = await res.text();
    if (!text) return undefined as T;
    return JSON.parse(text) as T;
  }

  /** Like request(), but targets /api/deep-work instead of /api/v1 */
  private async dwRequest<T>(
    method: string,
    path: string,
    body?: unknown,
  ): Promise<T> {
    const url = `${this.baseUrl}/api/deep-work${path}`;
    const res = await fetch(url, {
      method,
      headers: this.headers(),
      body: body != null ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) {
      let detail: string | undefined;
      try {
        const json = await res.json();
        detail = json.detail ?? json.message ?? JSON.stringify(json);
      } catch {
        detail = await res.text().catch(() => undefined);
      }
      throw new ApiError(res.status, `${method} /dw${path} failed: ${res.status}`, detail);
    }
    const text = await res.text();
    if (!text) return undefined as T;
    return JSON.parse(text) as T;
  }

  // ---------------------------------------------------------------------------
  // Auth
  // ---------------------------------------------------------------------------

  async login(token: string): Promise<void> {
    this.token = token;
    await this.post("/auth/login", { token });
  }

  /**
   * Call the login endpoint with `credentials: "include"` so the browser
   * stores the session cookie for the backend origin.  The WebSocket handler
   * validates this cookie, avoiding the need to pass tokens in the URL.
   */
  async loginForSession(token: string): Promise<void> {
    const url = `${this.apiBase}/auth/login`;
    const res = await fetch(url, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    });
    if (!res.ok) {
      console.warn(`[PocketPawClient] loginForSession failed: ${res.status}`);
    }
  }

  async logout(): Promise<void> {
    await this.post("/auth/logout");
    this.token = null;
  }

  async getSessionToken(token: string): Promise<string> {
    this.token = token;
    const res = await this.post<{ session_token: string }>("/auth/session", {});
    return res.session_token;
  }

  async regenerateToken(): Promise<string> {
    const res = await this.post<{ token: string }>("/token/regenerate");
    return res.token;
  }

  // ---------------------------------------------------------------------------
  // Chat (REST)
  // ---------------------------------------------------------------------------

  async chat(content: string, media?: MediaAttachment[]): Promise<string> {
    const res = await this.post<{ response: string }>("/chat", {
      content,
      media,
    });
    return res.response;
  }

  async chatStream(
    content: string,
    handlers: {
      onChunk?: (chunk: SSEChunk) => void;
      onToolStart?: (data: SSEToolStart) => void;
      onToolResult?: (data: SSEToolResult) => void;
      onThinking?: (data: SSEThinking) => void;
      onAskUser?: (data: SSEAskUser) => void;
      onStreamEnd?: (data: SSEStreamEnd) => void;
      onError?: (data: SSEError) => void;
    },
    media?: MediaAttachment[],
    sessionId?: string,
    signal?: AbortSignal,
    fileContext?: FileContext,
  ): Promise<void> {
    const url = `${this.apiBase}/chat/stream`;
    const body: Record<string, unknown> = { content, media, session_id: sessionId };
    if (fileContext) body.file_context = fileContext;
    const res = await fetch(url, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify(body),
      signal,
    });

    if (!res.ok) {
      const detail = await res.text().catch(() => "");
      throw new ApiError(res.status, `POST /chat/stream failed: ${res.status}`, detail);
    }

    const reader = res.body?.getReader();
    if (!reader) throw new ApiError(0, "No readable stream");

    const decoder = new TextDecoder();
    let buffer = "";
    let currentEvent = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";

      for (const line of lines) {
        if (line.startsWith("event: ")) {
          currentEvent = line.slice(7).trim();
        } else if (line.startsWith("data: ")) {
          const raw = line.slice(6);
          try {
            const data = JSON.parse(raw);
            switch (currentEvent) {
              case "chunk":
                handlers.onChunk?.(data);
                break;
              case "tool_start":
                handlers.onToolStart?.(data);
                break;
              case "tool_result":
                handlers.onToolResult?.(data);
                break;
              case "thinking":
                handlers.onThinking?.(data);
                break;
              case "ask_user_question":
                handlers.onAskUser?.(data);
                break;
              case "stream_end":
                handlers.onStreamEnd?.(data);
                break;
              case "error":
                handlers.onError?.(data);
                break;
            }
          } catch {
            // skip unparseable lines
          }
          currentEvent = "";
        }
      }
    }
  }

  async stopChat(sessionId: string): Promise<void> {
    await this.post(`/chat/stop?session_id=${encodeURIComponent(sessionId)}`);
  }

  // ---------------------------------------------------------------------------
  // Sessions
  // ---------------------------------------------------------------------------

  async createSession(): Promise<{ id: string; title: string }> {
    return this.post("/sessions");
  }

  async listSessions(limit = 50): Promise<SessionListResponse> {
    return this.get<SessionListResponse>(`/sessions?limit=${limit}`);
  }

  async getSessionHistory(sessionId: string): Promise<ChatMessage[]> {
    return this.get<ChatMessage[]>(
      `/sessions/${encodeURIComponent(sessionId)}/history`,
    );
  }

  async deleteSession(sessionId: string): Promise<void> {
    await this.del(`/sessions/${encodeURIComponent(sessionId)}`);
  }

  async updateSessionTitle(sessionId: string, title: string): Promise<void> {
    await this.post(
      `/sessions/${encodeURIComponent(sessionId)}/title`,
      { title },
    );
  }

  async searchSessions(query: string): Promise<Session[]> {
    const res = await this.get<{ sessions: Session[] }>(
      `/sessions/search?q=${encodeURIComponent(query)}`,
    );
    return res.sessions;
  }

  async exportSession(
    sessionId: string,
    format: "json" | "md" = "json",
  ): Promise<string> {
    const url = `${this.apiBase}/sessions/${encodeURIComponent(sessionId)}/export?format=${format}`;
    const res = await fetch(url, { headers: this.headers() });
    if (!res.ok) throw new ApiError(res.status, "Export failed");
    return res.text();
  }

  // ---------------------------------------------------------------------------
  // Settings
  // ---------------------------------------------------------------------------

  async getSettings(): Promise<Settings> {
    return this.get<Settings>("/settings");
  }

  async updateSettings(settings: Partial<Settings>): Promise<void> {
    await this.put("/settings", settings);
  }

  // ---------------------------------------------------------------------------
  // Skills
  // ---------------------------------------------------------------------------

  async listSkills(): Promise<Skill[]> {
    return this.get<Skill[]>("/skills");
  }

  async searchSkills(query: string): Promise<Skill[]> {
    const res = await this.get<{ results: Skill[] }>(
      `/skills/search?q=${encodeURIComponent(query)}`,
    );
    return res.results;
  }

  async installSkill(identifier: string): Promise<void> {
    await this.post("/skills/install", { identifier });
  }

  async removeSkill(name: string): Promise<void> {
    await this.post("/skills/remove", { name });
  }

  async reloadSkills(): Promise<void> {
    await this.post("/skills/reload");
  }

  // ---------------------------------------------------------------------------
  // Memory
  // ---------------------------------------------------------------------------

  async getLongTermMemory(limit = 50): Promise<MemoryEntry[]> {
    return this.get<MemoryEntry[]>(`/memory/long_term?limit=${limit}`);
  }

  async deleteMemory(entryId: string): Promise<void> {
    await this.del(`/memory/long_term/${encodeURIComponent(entryId)}`);
  }

  async getMemorySettings(): Promise<MemorySettings> {
    return this.get<MemorySettings>("/memory/settings");
  }

  async saveMemorySettings(settings: Partial<MemorySettings>): Promise<void> {
    await this.post("/memory/settings", settings);
  }

  async getMemoryStats(): Promise<MemoryStats> {
    return this.get<MemoryStats>("/memory/stats");
  }

  // ---------------------------------------------------------------------------
  // Channels
  // ---------------------------------------------------------------------------

  async getChannelStatus(): Promise<ChannelStatusMap> {
    return this.get<ChannelStatusMap>("/channels/status");
  }

  async saveChannel(
    channel: string,
    config: Record<string, string>,
    autostart?: boolean,
  ): Promise<void> {
    const body: Record<string, unknown> = { channel, config };
    if (autostart !== undefined) body.autostart = autostart;
    await this.post("/channels/save", body);
  }

  async getChannelConfig(channel: string): Promise<ChannelConfig | null> {
    try {
      return await this.get<ChannelConfig>(
        `/channels/config?channel=${encodeURIComponent(channel)}`,
      );
    } catch {
      return null;
    }
  }

  async testChannel(channel: string): Promise<ChannelTestResult> {
    try {
      return await this.post<ChannelTestResult>("/channels/test", { channel });
    } catch {
      return { ok: false, error: "Not yet available" };
    }
  }

  async toggleChannel(
    channel: string,
    action: "start" | "stop",
  ): Promise<Record<string, unknown>> {
    return this.post("/channels/toggle", { channel, action });
  }

  async getWhatsAppQR(): Promise<{ qr: string | null; connected: boolean }> {
    return this.get("/whatsapp/qr");
  }

  async checkExtra(channel: string): Promise<{ installed: boolean; package?: string; pip_spec?: string }> {
    return this.get(`/extras/check?channel=${encodeURIComponent(channel)}`);
  }

  async installExtra(channel: string): Promise<{ status?: string; error?: string }> {
    return this.post("/extras/install", { extra: channel });
  }

  // ---------------------------------------------------------------------------
  // Backends
  // ---------------------------------------------------------------------------

  async listBackends(): Promise<BackendInfo[]> {
    return this.get<BackendInfo[]>("/backends");
  }

  async installBackend(name: string): Promise<void> {
    await this.post("/backends/install", { backend: name });
  }

  async fetchOllamaModels(host?: string): Promise<string[]> {
    const params = host ? `?host=${encodeURIComponent(host)}` : "";
    return this.get<string[]>(`/backends/ollama-models${params}`);
  }

  // ---------------------------------------------------------------------------
  // Health
  // ---------------------------------------------------------------------------

  async getHealth(): Promise<HealthSummary> {
    return this.get<HealthSummary>("/health");
  }

  async runHealthCheck(): Promise<HealthSummary> {
    return this.post<HealthSummary>("/health/check");
  }

  async getHealthErrors(limit = 20, search?: string): Promise<HealthErrorEntry[]> {
    let path = `/health/errors?limit=${limit}`;
    if (search) path += `&search=${encodeURIComponent(search)}`;
    return this.get<HealthErrorEntry[]>(path);
  }

  async clearHealthErrors(): Promise<void> {
    await this.del("/health/errors");
  }

  async runSecurityAudit(): Promise<SecurityAuditResponse> {
    return this.post<SecurityAuditResponse>("/security-audit");
  }

  async getVersion(): Promise<VersionInfo> {
    return this.get<VersionInfo>("/version");
  }

  // ---------------------------------------------------------------------------
  // Reminders
  // ---------------------------------------------------------------------------

  async listReminders(): Promise<Reminder[]> {
    const res = await this.get<RemindersResponse>("/reminders");
    return res.reminders;
  }

  async addReminder(text: string): Promise<void> {
    await this.post("/reminders", { text });
  }

  async deleteReminder(id: string): Promise<void> {
    await this.del(`/reminders/${encodeURIComponent(id)}`);
  }

  // ---------------------------------------------------------------------------
  // Identity
  // ---------------------------------------------------------------------------

  async getIdentity(): Promise<IdentityFiles> {
    return this.get<IdentityFiles>("/identity");
  }

  async updateIdentity(files: Partial<IdentityFiles>): Promise<IdentitySaveResponse> {
    return this.put<IdentitySaveResponse>("/identity", files);
  }

  // ---------------------------------------------------------------------------
  // Plan Mode
  // ---------------------------------------------------------------------------

  async approvePlan(): Promise<void> {
    await this.post("/plan/approve");
  }

  async rejectPlan(): Promise<void> {
    await this.post("/plan/reject");
  }

  // ---------------------------------------------------------------------------
  // Audit
  // ---------------------------------------------------------------------------

  async getAuditLog(limit = 100): Promise<unknown[]> {
    return this.get(`/audit?limit=${limit}`);
  }

  async clearAuditLog(): Promise<void> {
    await this.del("/audit");
  }

  // ---------------------------------------------------------------------------
  // MCP
  // ---------------------------------------------------------------------------

  async getMcpStatus(): Promise<MCPStatusMap> {
    return this.get<MCPStatusMap>("/mcp/status");
  }

  async addMcpServer(config: {
    name: string;
    transport: string;
    command?: string;
    args?: string[];
    url?: string;
    env?: Record<string, string>;
    enabled?: boolean;
  }): Promise<{ status: string; error?: string }> {
    return this.post("/mcp/add", config);
  }

  async removeMcpServer(name: string): Promise<void> {
    await this.post("/mcp/remove", { name });
  }

  async toggleMcpServer(name: string): Promise<{
    status: string;
    enabled?: boolean;
    connected?: boolean;
    error?: string;
  }> {
    return this.post("/mcp/toggle", { name });
  }

  async testMcpServer(config: {
    transport: string;
    command?: string;
    args?: string[];
    url?: string;
    env?: Record<string, string>;
  }): Promise<MCPTestResponse> {
    return this.post<MCPTestResponse>("/mcp/test", config);
  }

  async getMcpPresets(): Promise<MCPPreset[]> {
    return this.get<MCPPreset[]>("/mcp/presets");
  }

  async installMcpPreset(
    presetId: string,
    env?: Record<string, string>,
    extraArgs?: string[],
  ): Promise<{ status: string; connected?: boolean; error?: string }> {
    return this.post("/mcp/presets/install", {
      preset_id: presetId,
      env,
      extra_args: extraArgs,
    });
  }

  // ---------------------------------------------------------------------------
  // Remote Access
  // ---------------------------------------------------------------------------

  async getRemoteStatus(): Promise<unknown> {
    return this.get("/remote/status");
  }

  async startRemote(): Promise<void> {
    await this.post("/remote/start");
  }

  async stopRemote(): Promise<void> {
    await this.post("/remote/stop");
  }

  // ---------------------------------------------------------------------------
  // Files
  // ---------------------------------------------------------------------------

  async browseFiles(path: string): Promise<{ path: string; files: import("./types").FileEntry[] }> {
    return this.get(`/files/browse?path=${encodeURIComponent(path)}`);
  }

  // ---------------------------------------------------------------------------
  // Kits (Command Centers)
  // ---------------------------------------------------------------------------

  async listKits(): Promise<import("$lib/types/pawkit").InstalledKit[]> {
    const res = await this.get<{ kits: import("$lib/types/pawkit").InstalledKit[] }>("/kits");
    return res.kits;
  }

  async getKit(kitId: string): Promise<import("$lib/types/pawkit").InstalledKit> {
    const res = await this.get<{ kit: import("$lib/types/pawkit").InstalledKit }>(
      `/kits/${encodeURIComponent(kitId)}`,
    );
    return res.kit;
  }

  async installKit(yaml: string): Promise<{ id: string }> {
    return this.post("/kits/install", { yaml });
  }

  async removeKit(kitId: string): Promise<void> {
    await this.del(`/kits/${encodeURIComponent(kitId)}`);
  }

  async getKitData(kitId: string): Promise<Record<string, unknown>> {
    const res = await this.get<{ data: Record<string, unknown> }>(
      `/kits/${encodeURIComponent(kitId)}/data`,
    );
    return res.data;
  }

  async activateKit(kitId: string): Promise<void> {
    await this.post(`/kits/${encodeURIComponent(kitId)}/activate`);
  }

  async listKitCatalog(): Promise<import("$lib/types/pawkit").KitCatalogEntry[]> {
    const res = await this.get<{ catalog: import("$lib/types/pawkit").KitCatalogEntry[] }>(
      "/kits/catalog",
    );
    return res.catalog;
  }

  async installCatalogKit(
    kitId: string,
  ): Promise<{ id: string; kit: import("$lib/types/pawkit").InstalledKit; activated: boolean }> {
    return this.post(`/kits/catalog/${encodeURIComponent(kitId)}/install`);
  }

  // ---------------------------------------------------------------------------
  // Mission Control
  // ---------------------------------------------------------------------------

  async mcCreateTask(
    title: string,
    priority?: TaskPriority,
    description?: string,
    status?: TaskStatus,
  ): Promise<{ task: Record<string, unknown> }> {
    const body: Record<string, unknown> = { title };
    if (priority) body.priority = priority;
    if (description) body.description = description;
    if (status) body.status = status;
    return this.mcRequest("POST", "/tasks", body);
  }

  async mcUpdateTaskStatus(
    taskId: string,
    status: TaskStatus,
  ): Promise<{ task: Record<string, unknown> }> {
    return this.mcRequest("POST", `/tasks/${encodeURIComponent(taskId)}/status`, { status });
  }

  async mcDeleteTask(taskId: string): Promise<void> {
    await this.mcRequest("DELETE", `/tasks/${encodeURIComponent(taskId)}`);
  }

  async mcCreateDocument(
    title: string,
    content: string,
    type?: DocumentType,
  ): Promise<{ document: Record<string, unknown> }> {
    const body: Record<string, unknown> = { title, content };
    if (type) body.type = type;
    return this.mcRequest("POST", "/documents", body);
  }

  async mcDeleteDocument(docId: string): Promise<void> {
    await this.mcRequest("DELETE", `/documents/${encodeURIComponent(docId)}`);
  }

  // -- Agents --

  async mcListAgents(status?: string): Promise<{ agents: import("$lib/types/pawkit").AgentProfile[]; count: number }> {
    const params = status ? `?status=${encodeURIComponent(status)}` : "";
    return this.mcRequest("GET", `/agents${params}`);
  }

  async mcCreateAgent(body: {
    name: string;
    role: string;
    description?: string;
    specialties?: string[];
    backend?: string;
    level?: string;
  }): Promise<{ agent: import("$lib/types/pawkit").AgentProfile }> {
    return this.mcRequest("POST", "/agents", body);
  }

  async mcUpdateAgent(
    agentId: string,
    fields: Record<string, unknown>,
  ): Promise<{ agent: import("$lib/types/pawkit").AgentProfile }> {
    return this.mcRequest("PATCH", `/agents/${encodeURIComponent(agentId)}`, fields);
  }

  async mcDeleteAgent(agentId: string): Promise<void> {
    await this.mcRequest("DELETE", `/agents/${encodeURIComponent(agentId)}`);
  }

  // -- Task assignment + execution --

  async mcAssignTask(
    taskId: string,
    agentIds: string[],
  ): Promise<{ task: Record<string, unknown> }> {
    return this.mcRequest("POST", `/tasks/${encodeURIComponent(taskId)}/assign`, { agent_ids: agentIds });
  }

  async mcRunTask(
    taskId: string,
    agentId: string,
  ): Promise<{ status: string; task_id: string; agent_id: string; agent_name: string; message: string }> {
    return this.mcRequest("POST", `/tasks/${encodeURIComponent(taskId)}/run`, { agent_id: agentId });
  }

  async mcStopTask(taskId: string): Promise<void> {
    await this.mcRequest("POST", `/tasks/${encodeURIComponent(taskId)}/stop`);
  }

  async mcGetRunningTasks(): Promise<{ running_tasks: Record<string, unknown>[]; count: number }> {
    return this.mcRequest("GET", "/tasks/running");
  }

  // -- Task messages --

  async mcGetTaskMessages(
    taskId: string,
    limit = 100,
  ): Promise<{ messages: import("$lib/types/pawkit").MCMessage[]; count: number }> {
    return this.mcRequest("GET", `/tasks/${encodeURIComponent(taskId)}/messages?limit=${limit}`);
  }

  async mcPostTaskMessage(
    taskId: string,
    content: string,
    fromAgentId?: string,
  ): Promise<{ message: import("$lib/types/pawkit").MCMessage }> {
    const body: Record<string, unknown> = { content, from_agent_id: fromAgentId ?? "user" };
    return this.mcRequest("POST", `/tasks/${encodeURIComponent(taskId)}/messages`, body);
  }

  // -- Notifications --

  async mcListNotifications(
    unreadOnly = false,
  ): Promise<{ notifications: import("$lib/types/pawkit").MCNotification[]; count: number }> {
    const params = unreadOnly ? "?unread_only=true" : "";
    return this.mcRequest("GET", `/notifications${params}`);
  }

  async mcMarkNotificationRead(notificationId: string): Promise<void> {
    await this.mcRequest("POST", `/notifications/${encodeURIComponent(notificationId)}/read`);
  }

  // -- Documents (enhanced) --

  async mcGetDocument(
    docId: string,
  ): Promise<{ document: import("$lib/types/pawkit").MCDocument }> {
    return this.mcRequest("GET", `/documents/${encodeURIComponent(docId)}`);
  }

  async mcUpdateDocument(
    docId: string,
    content: string,
  ): Promise<{ document: import("$lib/types/pawkit").MCDocument }> {
    return this.mcRequest("PATCH", `/documents/${encodeURIComponent(docId)}`, { content });
  }

  // -- Standup --

  async mcGetStandup(): Promise<{ summary: string }> {
    return this.mcRequest("GET", "/standup");
  }

  // -- MC Projects --

  async mcListProjects(
    status?: string,
  ): Promise<{ projects: import("$lib/types/pawkit").MCProject[]; count: number }> {
    const params = status ? `?status=${encodeURIComponent(status)}` : "";
    return this.mcRequest("GET", `/projects${params}`);
  }

  async mcGetProject(
    projectId: string,
  ): Promise<{ project: import("$lib/types/pawkit").MCProject; tasks: Record<string, unknown>[]; progress: import("$lib/types/pawkit").MCProjectProgress }> {
    return this.mcRequest("GET", `/projects/${encodeURIComponent(projectId)}`);
  }

  async mcDeleteProject(projectId: string): Promise<void> {
    await this.mcRequest("DELETE", `/projects/${encodeURIComponent(projectId)}`);
  }

  // ---------------------------------------------------------------------------
  // Deep Work
  // ---------------------------------------------------------------------------

  async dwParseGoal(
    goal: string,
  ): Promise<{ domain: string; complexity: string; ai_roles: string[]; human_roles: string[]; questions: string[] }> {
    return this.dwRequest("POST", "/parse-goal", { description: goal });
  }

  async dwStartProject(
    goal: string,
    description?: string,
  ): Promise<{ project_id: string; status: string; message: string }> {
    const body: Record<string, unknown> = { description: goal };
    if (description) body.research_depth = "auto";
    return this.dwRequest("POST", "/start", body);
  }

  async dwGetPlan(
    projectId: string,
  ): Promise<Record<string, unknown>> {
    return this.dwRequest("GET", `/projects/${encodeURIComponent(projectId)}/plan`);
  }

  async dwApproveProject(projectId: string): Promise<Record<string, unknown>> {
    return this.dwRequest("POST", `/projects/${encodeURIComponent(projectId)}/approve`);
  }

  async dwPauseProject(projectId: string): Promise<Record<string, unknown>> {
    return this.dwRequest("POST", `/projects/${encodeURIComponent(projectId)}/pause`);
  }

  async dwResumeProject(projectId: string): Promise<Record<string, unknown>> {
    return this.dwRequest("POST", `/projects/${encodeURIComponent(projectId)}/resume`);
  }

  async dwCancelProject(projectId: string): Promise<Record<string, unknown>> {
    return this.dwRequest("POST", `/projects/${encodeURIComponent(projectId)}/cancel`);
  }

  async dwSkipTask(projectId: string, taskId: string): Promise<Record<string, unknown>> {
    return this.dwRequest("POST", `/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(taskId)}/skip`);
  }

  async dwRetryTask(projectId: string, taskId: string): Promise<Record<string, unknown>> {
    return this.dwRequest("POST", `/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(taskId)}/retry`);
  }
}
