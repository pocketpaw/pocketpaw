# Mission Control Full Integration — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Wire all 39 MC + 8 Deep Work backend endpoints into the desktop client — agent management, task execution with live streaming, Deep Work project lifecycle, notifications, task discussions, document editing, and standup summaries.

**Architecture:** New types + API methods on the existing `PocketPawClient`, two new Svelte 5 stores (`mcStore`, `projectStore`), WebSocket event handling for real-time MC/DW events, new panel types (`agent-roster`, `standup`), an execution slide-over panel, a `/projects` route for Deep Work, and a notification bell in the titlebar. Backend gets two new data source resolvers (`api:agents`, `api:standup`).

**Tech Stack:** SvelteKit 2 + Svelte 5 runes, TypeScript, Tailwind CSS 4, shadcn-svelte (bits-ui), Lucide icons, existing `PocketPawClient` REST + `PocketPawWebSocket` push infra. Backend: FastAPI + Pydantic.

---

## Task 1: Add MC/DW Types

**Files:**
- Modify: `client/src/lib/types/pawkit.ts:78-93` (extend MC types section)

**Step 1: Add types after existing TaskStatus/TaskPriority/DocumentType**

In `client/src/lib/types/pawkit.ts`, add after line 93 (`DocumentType`), before the Kit Catalog section:

```typescript
export type AgentStatus = "idle" | "active" | "blocked" | "offline";
export type AgentLevel = "intern" | "specialist" | "lead";

export type ProjectStatus =
  | "draft"
  | "planning"
  | "awaiting_approval"
  | "approved"
  | "executing"
  | "paused"
  | "completed"
  | "failed"
  | "cancelled";

export interface AgentProfile {
  id: string;
  name: string;
  role: string;
  description: string;
  session_key: string;
  backend: string;
  status: AgentStatus;
  level: AgentLevel;
  current_task_id: string | null;
  specialties: string[];
  last_heartbeat: string | null;
  created_at: string;
  updated_at: string;
  metadata: Record<string, unknown>;
}

export interface MCTask {
  id: string;
  title: string;
  description: string;
  status: TaskStatus;
  priority: TaskPriority;
  assignee_ids: string[];
  creator_id: string | null;
  parent_task_id: string | null;
  blocked_by: string[];
  tags: string[];
  due_date: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
  project_id: string | null;
  task_type: "agent" | "human" | "review";
  blocks: string[];
  active_description: string;
  estimated_minutes: number | null;
  output: string | null;
  retry_count: number;
  max_retries: number;
  timeout_minutes: number | null;
  error_message: string | null;
  metadata: Record<string, unknown>;
}

export interface MCMessage {
  id: string;
  task_id: string;
  from_agent_id: string;
  content: string;
  attachment_ids: string[];
  mentions: string[];
  created_at: string;
}

export interface MCDocument {
  id: string;
  title: string;
  content: string;
  type: DocumentType;
  task_id: string | null;
  author_id: string | null;
  tags: string[];
  version: number;
  created_at: string;
  updated_at: string;
  metadata: Record<string, unknown>;
}

export interface MCNotification {
  id: string;
  agent_id: string;
  type: string;
  content: string;
  source_message_id: string | null;
  source_task_id: string | null;
  delivered: boolean;
  read: boolean;
  created_at: string;
}

export interface MCProject {
  id: string;
  title: string;
  description: string;
  status: ProjectStatus;
  planner_agent_id: string | null;
  team_agent_ids: string[];
  task_ids: string[];
  prd_document_id: string | null;
  creator_id: string;
  tags: string[];
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
  folder_path?: string;
  file_count?: number;
  metadata: Record<string, unknown>;
}

export interface MCProjectProgress {
  total: number;
  completed: number;
  skipped: number;
  in_progress: number;
  blocked: number;
  human_pending: number;
  percent: number;
}
```

Also update the `PanelConfig.type` union (line 26) to include new panel types:

```typescript
export interface PanelConfig {
  id: string;
  type: "metrics-row" | "table" | "kanban" | "chart" | "feed" | "markdown" | "agent-roster" | "standup";
  [key: string]: unknown;
}
```

**Step 2: Verify no syntax errors**

Run: `cd client && bun run check 2>&1 | grep -E "ERROR.*pawkit"`
Expected: No new errors.

**Step 3: Commit**

```bash
git add client/src/lib/types/pawkit.ts
git commit -m "feat(types): add MC agent, task, document, project, notification types"
```

---

## Task 2: Add WebSocket Event Types

**Files:**
- Modify: `client/src/lib/api/types.ts:390-405` (WSEvent union)

**Step 1: Add MC/DW WS event interfaces**

Before the `WSEvent` union type (line 397), add:

```typescript
export interface WSMCTaskStarted {
  type: "mc_task_started";
  task_id: string;
  agent_id: string;
  agent_name: string;
  task_title: string;
  timestamp: string;
}

export interface WSMCTaskOutput {
  type: "mc_task_output";
  task_id: string;
  content: string;
  output_type: "message" | "tool_use" | "tool_result";
  timestamp: string;
}

export interface WSMCTaskCompleted {
  type: "mc_task_completed";
  task_id: string;
  agent_id: string;
  status: "completed" | "error" | "stopped" | "timeout";
  error?: string;
  retry?: boolean;
  retry_count?: number;
  max_retries?: number;
  timestamp: string;
}

export interface WSMCTaskRetry {
  type: "mc_task_retry";
  task_id: string;
  agent_id: string;
  retry_count: number;
  max_retries: number;
  error: string;
  timestamp: string;
}

export interface WSMCActivityCreated {
  type: "mc_activity_created";
  activity: Record<string, unknown>;
}

export interface WSDWPlanningPhase {
  type: "dw_planning_phase";
  project_id: string;
  phase: "goal_analysis" | "research" | "prd" | "tasks" | "team";
  message: string;
}

export interface WSDWPlanningComplete {
  type: "dw_planning_complete";
  project_id: string;
  status: string;
  title: string;
  error?: string;
}

export interface WSDWProjectCancelled {
  type: "dw_project_cancelled";
  project_id: string;
  title: string;
}
```

**Step 2: Extend the WSEvent union**

Replace the `WSEvent` type (line 397-405):

```typescript
export type WSEvent =
  | WSNotification
  | WSError
  | WSHealthUpdate
  | WSReminders
  | WSReminderAdded
  | WSReminderDeleted
  | WSSkills
  | WSKitDataUpdate
  | WSMCTaskStarted
  | WSMCTaskOutput
  | WSMCTaskCompleted
  | WSMCTaskRetry
  | WSMCActivityCreated
  | WSDWPlanningPhase
  | WSDWPlanningComplete
  | WSDWProjectCancelled;
```

**Step 3: Commit**

```bash
git add client/src/lib/api/types.ts
git commit -m "feat(types): add MC/DW WebSocket event types"
```

---

## Task 3: Add API Methods (~25 methods + dwRequest helper)

**Files:**
- Modify: `client/src/lib/api/client.ts:131-156` (after mcRequest) and `693-733` (MC section)

**Step 1: Add dwRequest helper**

After the existing `mcRequest` method (line 156), add:

```typescript
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
```

**Step 2: Add MC agent/task/execution/notification/doc/standup methods**

Add after the existing `mcDeleteDocument` method (line 733), before the closing `}`:

```typescript
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
    return this.dwRequest("POST", "/parse-goal", { goal });
  }

  async dwStartProject(
    goal: string,
    description?: string,
  ): Promise<{ project_id: string; status: string; message: string }> {
    const body: Record<string, unknown> = { goal };
    if (description) body.description = description;
    return this.dwRequest("POST", "/start", body);
  }

  async dwGetPlan(
    projectId: string,
  ): Promise<{ project: Record<string, unknown>; tasks: Record<string, unknown>[]; progress: Record<string, unknown>; prd?: string; execution_levels?: Record<string, unknown>[][]; goal_analysis?: Record<string, unknown> }> {
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
```

**Step 3: Verify**

Run: `cd client && bun run check 2>&1 | grep -E "ERROR.*client\.ts"`
Expected: No new errors.

**Step 4: Commit**

```bash
git add client/src/lib/api/client.ts
git commit -m "feat(api): add 25 MC/DW API methods + dwRequest helper"
```

---

## Task 4: Create MC Store

**Files:**
- Create: `client/src/lib/stores/mission-control.svelte.ts`
- Modify: `client/src/lib/stores/index.ts`

**Step 1: Create the MC store**

Create `client/src/lib/stores/mission-control.svelte.ts`:

```typescript
import type { AgentProfile, MCNotification } from "$lib/types/pawkit";
import type { WSMCTaskStarted, WSMCTaskOutput, WSMCTaskCompleted, WSMCTaskRetry } from "$lib/api/types";
import { connectionStore } from "./connection.svelte";

export interface ExecutionLogEntry {
  content: string;
  output_type: "message" | "tool_use" | "tool_result";
  timestamp: string;
}

export interface ActiveExecution {
  taskId: string;
  agentId: string;
  agentName: string;
  taskTitle: string;
  log: ExecutionLogEntry[];
  status: "running" | "completed" | "error" | "stopped" | "timeout";
}

class MissionControlStore {
  agents = $state<AgentProfile[]>([]);
  runningTaskIds = $state<Set<string>>(new Set());
  activeExecution = $state<ActiveExecution | null>(null);
  notifications = $state<MCNotification[]>([]);
  unreadCount = $derived(this.notifications.filter((n) => !n.read).length);

  private notificationPollInterval: ReturnType<typeof setInterval> | null = null;
  private unsubWs: (() => void)[] = [];

  /** Load agents from backend */
  async loadAgents(): Promise<void> {
    try {
      const client = connectionStore.getClient();
      const res = await client.mcListAgents();
      this.agents = res.agents;
    } catch (err) {
      console.error("[MCStore] Failed to load agents:", err);
    }
  }

  /** Load running tasks */
  async loadRunningTasks(): Promise<void> {
    try {
      const client = connectionStore.getClient();
      const res = await client.mcGetRunningTasks();
      this.runningTaskIds = new Set(res.running_tasks.map((t) => String(t.task_id)));
    } catch (err) {
      console.error("[MCStore] Failed to load running tasks:", err);
    }
  }

  /** Load notifications */
  async loadNotifications(): Promise<void> {
    try {
      const client = connectionStore.getClient();
      const res = await client.mcListNotifications();
      this.notifications = res.notifications;
    } catch (err) {
      console.error("[MCStore] Failed to load notifications:", err);
    }
  }

  /** Mark a notification as read */
  async markRead(notificationId: string): Promise<void> {
    try {
      const client = connectionStore.getClient();
      await client.mcMarkNotificationRead(notificationId);
      this.notifications = this.notifications.map((n) =>
        n.id === notificationId ? { ...n, read: true } : n,
      );
    } catch (err) {
      console.error("[MCStore] Failed to mark notification read:", err);
    }
  }

  /** Subscribe to MC WebSocket events */
  bindEvents(): void {
    this.unbindEvents();
    try {
      const ws = connectionStore.getWebSocket();

      this.unsubWs.push(
        ws.on("mc_task_started" as any, (event: any) => {
          const e = event as WSMCTaskStarted;
          this.runningTaskIds = new Set([...this.runningTaskIds, e.task_id]);
          this.activeExecution = {
            taskId: e.task_id,
            agentId: e.agent_id,
            agentName: e.agent_name,
            taskTitle: e.task_title,
            log: [],
            status: "running",
          };
        }),
      );

      this.unsubWs.push(
        ws.on("mc_task_output" as any, (event: any) => {
          const e = event as WSMCTaskOutput;
          if (this.activeExecution && this.activeExecution.taskId === e.task_id) {
            this.activeExecution = {
              ...this.activeExecution,
              log: [
                ...this.activeExecution.log,
                { content: e.content, output_type: e.output_type, timestamp: e.timestamp },
              ],
            };
          }
        }),
      );

      this.unsubWs.push(
        ws.on("mc_task_completed" as any, (event: any) => {
          const e = event as WSMCTaskCompleted;
          const newSet = new Set(this.runningTaskIds);
          newSet.delete(e.task_id);
          this.runningTaskIds = newSet;
          if (this.activeExecution && this.activeExecution.taskId === e.task_id) {
            this.activeExecution = { ...this.activeExecution, status: e.status };
          }
        }),
      );

      this.unsubWs.push(
        ws.on("mc_task_retry" as any, (event: any) => {
          const e = event as WSMCTaskRetry;
          if (this.activeExecution && this.activeExecution.taskId === e.task_id) {
            this.activeExecution = {
              ...this.activeExecution,
              log: [
                ...this.activeExecution.log,
                {
                  content: `Retrying (${e.retry_count}/${e.max_retries}): ${e.error}`,
                  output_type: "message",
                  timestamp: e.timestamp,
                },
              ],
            };
          }
        }),
      );
    } catch {
      // WS not available yet
    }
  }

  unbindEvents(): void {
    for (const unsub of this.unsubWs) unsub();
    this.unsubWs = [];
  }

  /** Start notification polling (60s) */
  startNotificationPolling(): void {
    this.stopNotificationPolling();
    this.loadNotifications();
    this.notificationPollInterval = setInterval(() => {
      this.loadNotifications();
    }, 60_000);
  }

  stopNotificationPolling(): void {
    if (this.notificationPollInterval) {
      clearInterval(this.notificationPollInterval);
      this.notificationPollInterval = null;
    }
  }

  /** Close the execution panel */
  closeExecution(): void {
    this.activeExecution = null;
  }

  /** Initialize: load agents, bind WS events, start notification polling */
  async initialize(): Promise<void> {
    this.bindEvents();
    this.startNotificationPolling();
    await Promise.allSettled([this.loadAgents(), this.loadRunningTasks()]);
  }

  dispose(): void {
    this.unbindEvents();
    this.stopNotificationPolling();
  }
}

export const mcStore = new MissionControlStore();
```

**Step 2: Create the project store**

Create `client/src/lib/stores/projects.svelte.ts`:

```typescript
import type { MCProject, MCProjectProgress } from "$lib/types/pawkit";
import type { WSDWPlanningPhase, WSDWPlanningComplete } from "$lib/api/types";
import { connectionStore } from "./connection.svelte";

export type PlanningPhase = "goal_analysis" | "research" | "prd" | "tasks" | "team";

class ProjectStore {
  projects = $state<MCProject[]>([]);
  isLoading = $state(false);

  /** Planning progress for the active project being planned */
  planningProjectId = $state<string | null>(null);
  planningPhase = $state<PlanningPhase | null>(null);
  planningMessage = $state("");
  planningDone = $state(false);
  planningError = $state<string | null>(null);

  private unsubWs: (() => void)[] = [];

  async loadProjects(status?: string): Promise<void> {
    this.isLoading = true;
    try {
      const client = connectionStore.getClient();
      const res = await client.mcListProjects(status);
      this.projects = res.projects;
    } catch (err) {
      console.error("[ProjectStore] Failed to load projects:", err);
    } finally {
      this.isLoading = false;
    }
  }

  async getProjectDetail(
    projectId: string,
  ): Promise<{ project: MCProject; tasks: Record<string, unknown>[]; progress: MCProjectProgress } | null> {
    try {
      const client = connectionStore.getClient();
      return await client.mcGetProject(projectId);
    } catch (err) {
      console.error("[ProjectStore] Failed to get project:", err);
      return null;
    }
  }

  async parseGoal(goal: string): Promise<{
    domain: string;
    complexity: string;
    ai_roles: string[];
    human_roles: string[];
    questions: string[];
  } | null> {
    try {
      const client = connectionStore.getClient();
      return await client.dwParseGoal(goal);
    } catch (err) {
      console.error("[ProjectStore] Failed to parse goal:", err);
      return null;
    }
  }

  async startProject(goal: string, description?: string): Promise<string | null> {
    this.planningPhase = null;
    this.planningMessage = "";
    this.planningDone = false;
    this.planningError = null;
    try {
      const client = connectionStore.getClient();
      const res = await client.dwStartProject(goal, description);
      this.planningProjectId = res.project_id;
      return res.project_id;
    } catch (err) {
      console.error("[ProjectStore] Failed to start project:", err);
      this.planningError = String(err);
      return null;
    }
  }

  async getPlan(projectId: string): Promise<Record<string, unknown> | null> {
    try {
      const client = connectionStore.getClient();
      return await client.dwGetPlan(projectId);
    } catch (err) {
      console.error("[ProjectStore] Failed to get plan:", err);
      return null;
    }
  }

  async approveProject(projectId: string): Promise<boolean> {
    try {
      const client = connectionStore.getClient();
      await client.dwApproveProject(projectId);
      await this.loadProjects();
      return true;
    } catch (err) {
      console.error("[ProjectStore] Failed to approve:", err);
      return false;
    }
  }

  async pauseProject(projectId: string): Promise<boolean> {
    try {
      const client = connectionStore.getClient();
      await client.dwPauseProject(projectId);
      await this.loadProjects();
      return true;
    } catch (err) {
      console.error("[ProjectStore] Failed to pause:", err);
      return false;
    }
  }

  async resumeProject(projectId: string): Promise<boolean> {
    try {
      const client = connectionStore.getClient();
      await client.dwResumeProject(projectId);
      await this.loadProjects();
      return true;
    } catch (err) {
      console.error("[ProjectStore] Failed to resume:", err);
      return false;
    }
  }

  async cancelProject(projectId: string): Promise<boolean> {
    try {
      const client = connectionStore.getClient();
      await client.dwCancelProject(projectId);
      await this.loadProjects();
      return true;
    } catch (err) {
      console.error("[ProjectStore] Failed to cancel:", err);
      return false;
    }
  }

  async deleteProject(projectId: string): Promise<boolean> {
    try {
      const client = connectionStore.getClient();
      await client.mcDeleteProject(projectId);
      this.projects = this.projects.filter((p) => p.id !== projectId);
      return true;
    } catch (err) {
      console.error("[ProjectStore] Failed to delete:", err);
      return false;
    }
  }

  async skipTask(projectId: string, taskId: string): Promise<boolean> {
    try {
      const client = connectionStore.getClient();
      await client.dwSkipTask(projectId, taskId);
      return true;
    } catch (err) {
      console.error("[ProjectStore] Failed to skip task:", err);
      return false;
    }
  }

  async retryTask(projectId: string, taskId: string): Promise<boolean> {
    try {
      const client = connectionStore.getClient();
      await client.dwRetryTask(projectId, taskId);
      return true;
    } catch (err) {
      console.error("[ProjectStore] Failed to retry task:", err);
      return false;
    }
  }

  /** Subscribe to DW WebSocket events */
  bindEvents(): void {
    this.unbindEvents();
    try {
      const ws = connectionStore.getWebSocket();

      this.unsubWs.push(
        ws.on("dw_planning_phase" as any, (event: any) => {
          const e = event as WSDWPlanningPhase;
          if (this.planningProjectId === e.project_id) {
            this.planningPhase = e.phase;
            this.planningMessage = e.message;
          }
        }),
      );

      this.unsubWs.push(
        ws.on("dw_planning_complete" as any, (event: any) => {
          const e = event as WSDWPlanningComplete;
          if (this.planningProjectId === e.project_id) {
            this.planningDone = true;
            if (e.error) this.planningError = e.error;
          }
          // Refresh project list
          this.loadProjects();
        }),
      );

      this.unsubWs.push(
        ws.on("dw_project_cancelled" as any, (event: any) => {
          this.loadProjects();
        }),
      );
    } catch {
      // WS not available yet
    }
  }

  unbindEvents(): void {
    for (const unsub of this.unsubWs) unsub();
    this.unsubWs = [];
  }

  initialize(): void {
    this.bindEvents();
  }

  dispose(): void {
    this.unbindEvents();
  }
}

export const projectStore = new ProjectStore();
```

**Step 3: Wire stores into index.ts**

In `client/src/lib/stores/index.ts`, add imports and exports:

```typescript
import { mcStore } from "./mission-control.svelte";
import { projectStore } from "./projects.svelte";
```

Add to exports: `mcStore, projectStore`

Add to `initializeStores` after `kitStore.load();`:

```typescript
  // Initialize MC store (agents, running tasks, notifications, WS events)
  mcStore.initialize();

  // Initialize project store (WS events for planning)
  projectStore.initialize();
```

**Step 4: Commit**

```bash
git add client/src/lib/stores/mission-control.svelte.ts client/src/lib/stores/projects.svelte.ts client/src/lib/stores/index.ts
git commit -m "feat(stores): add mcStore and projectStore with WS event binding"
```

---

## Task 5: Agent Roster Panel

**Files:**
- Create: `client/src/lib/components/panels/AgentRoster.svelte`
- Modify: `client/src/lib/components/panels/PanelRenderer.svelte`

**Step 1: Create AgentRoster.svelte**

Create `client/src/lib/components/panels/AgentRoster.svelte` — a grid of agent cards with create/edit dialogs. Uses `mcStore.agents`, `connectionStore.getClient()` for CRUD, and `onDataChanged` callback.

Key features:
- Grid of cards with initials avatar, name, role, status badge, level tag, specialty chips
- "+" button to create agent: dialog with name, role, description, specialties, backend, level
- Card click opens edit dialog with same fields + delete
- Status colors: idle=gray, active=green+pulse, blocked=orange, offline=red

**Step 2: Register in PanelRenderer.svelte**

Add import and route for `agent-roster` panel type:

```svelte
import AgentRoster from "./AgentRoster.svelte";

{:else if panel.type === "agent-roster"}
  <AgentRoster config={panel} {data} {onDataChanged} />
```

**Step 3: Commit**

```bash
git add client/src/lib/components/panels/AgentRoster.svelte client/src/lib/components/panels/PanelRenderer.svelte
git commit -m "feat(panels): add AgentRoster panel with CRUD"
```

---

## Task 6: Enhanced Kanban — Assignment, Run, Stop, Messages, Running Indicator

**Files:**
- Modify: `client/src/lib/components/panels/KanbanBoard.svelte`

**Step 1: Add assignment section to card detail dialog**

Import `mcStore` from stores. In the detail dialog, add an "Assign" section showing `detailItem.assignee_ids` as agent name chips (resolve from `mcStore.agents`) with a Select to pick agents.

**Step 2: Add Run/Stop buttons**

- Run button: visible when task has assignees and status is not `in_progress`/`done`. Calls `mcRunTask(taskId, firstAssigneeId)`.
- Stop button: visible when task is in `mcStore.runningTaskIds`. Calls `mcStopTask(taskId)`.

**Step 3: Add running indicator on kanban cards**

On each card, check if `item.id` is in `mcStore.runningTaskIds`. If so, add a pulsing green dot.

**Step 4: Add messages section to card detail**

Below status/priority, add a "Discussion" section:
- On dialog open, fetch messages via `mcGetTaskMessages(taskId)`
- Render thread: `from_agent_id` + content + relative time
- Input + "Send" button at bottom → `mcPostTaskMessage(taskId, content)`

**Step 5: Commit**

```bash
git add client/src/lib/components/panels/KanbanBoard.svelte
git commit -m "feat(kanban): add agent assignment, run/stop execution, messages, running indicator"
```

---

## Task 7: Task Execution Slide-over Panel

**Files:**
- Create: `client/src/lib/components/command-center/TaskExecutionPanel.svelte`
- Modify: `client/src/lib/components/command-center/LayoutRenderer.svelte`

**Step 1: Create TaskExecutionPanel.svelte**

A fixed slide-over on the right edge (`fixed top-0 right-0 h-full w-96 z-40`):
- Reads `mcStore.activeExecution` for state
- Header: task title, agent name, close button
- Body: scrollable log entries (auto-scroll to bottom via `$effect`)
  - `message` → paragraph with `text-sm`
  - `tool_use` → collapsible block with tool name
  - `tool_result` → result block
- Footer: "Stop Execution" red button → `client.mcStopTask(taskId)`
- Shows only when `mcStore.activeExecution !== null`

**Step 2: Add to LayoutRenderer**

Import and render `TaskExecutionPanel` after the grid layout (it's position:fixed, so placement doesn't matter):

```svelte
<TaskExecutionPanel />
```

**Step 3: Commit**

```bash
git add client/src/lib/components/command-center/TaskExecutionPanel.svelte client/src/lib/components/command-center/LayoutRenderer.svelte
git commit -m "feat(execution): add slide-over panel for live task execution streaming"
```

---

## Task 8: Notification Bell in Titlebar

**Files:**
- Create: `client/src/lib/components/titlebar/NotificationBell.svelte`
- Modify: `client/src/lib/components/titlebar/QuickActions.svelte`

**Step 1: Create NotificationBell.svelte**

Bell icon with unread count badge. Click toggles dropdown of notifications.
- Imports `mcStore` for `notifications` and `unreadCount`
- Bell icon from `@lucide/svelte` (`Bell`)
- Red badge with count when > 0
- Click opens popover/dropdown (use bits-ui Popover or simple state toggle)
- Notification list: type icon, content, relative time, read/unread styling
- Click notification → `mcStore.markRead(id)`

**Step 2: Add to QuickActions**

In `QuickActions.svelte`, import `NotificationBell` and add it before the Side Panel button:

```svelte
<NotificationBell />
```

**Step 3: Commit**

```bash
git add client/src/lib/components/titlebar/NotificationBell.svelte client/src/lib/components/titlebar/QuickActions.svelte
git commit -m "feat(titlebar): add notification bell with unread badge and dropdown"
```

---

## Task 9: Standup Panel + Document Viewer/Editor

**Files:**
- Create: `client/src/lib/components/panels/StandupPanel.svelte`
- Modify: `client/src/lib/components/panels/PanelRenderer.svelte`
- Modify: `client/src/lib/components/panels/DataTable.svelte`

**Step 1: Create StandupPanel.svelte**

Fetches standup markdown from `mcGetStandup()` on mount, renders as styled markdown (pre-wrap, or basic markdown parsing).

**Step 2: Register standup in PanelRenderer**

```svelte
import StandupPanel from "./StandupPanel.svelte";

{:else if panel.type === "standup"}
  <StandupPanel config={panel} {data} />
```

**Step 3: Enhance DataTable with document viewer/editor**

Add row click handler → opens a dialog showing document title + content:
- Read mode: plain text / pre-wrap rendering
- "Edit" toggle → textarea
- "Save" → `mcUpdateDocument(docId, content)`

**Step 4: Commit**

```bash
git add client/src/lib/components/panels/StandupPanel.svelte client/src/lib/components/panels/PanelRenderer.svelte client/src/lib/components/panels/DataTable.svelte
git commit -m "feat(panels): add standup panel and document viewer/editor"
```

---

## Task 10: Projects Page (/projects route)

**Files:**
- Create: `client/src/routes/projects/+page.svelte`
- Modify: `client/src/lib/components/titlebar/WorkspaceTabs.svelte`

**Step 1: Create projects page**

Three sub-views managed by a local `$state` variable (`view: "list" | "create" | "detail"`):

**Project List:**
- Grid of project cards. Each card: title, status badge, progress bar, created date.
- Status filter tabs: All / Active / Completed
- "New Project" button

**Project Creator:**
- Step 1: Textarea + "Analyze" button → shows goal analysis result
- Step 2: "Start Planning" → shows planning phase indicators from `projectStore.planningPhase`
- Step 3: On `planningDone` → button to view project detail

**Project Detail:**
- Header with lifecycle buttons (approve/pause/resume/cancel based on status)
- PRD section (collapsible, pre-wrap text)
- Task table with skip/retry buttons per row
- Progress bar

**Step 2: Add Projects tab to WorkspaceTabs**

In `WorkspaceTabs.svelte`, add to the `tabs` array:

```typescript
{ href: "/projects", label: "Projects", icon: FolderKanban, match: (p) => p.startsWith("/projects") },
```

Import `FolderKanban` from `@lucide/svelte`.

**Step 3: Commit**

```bash
git add client/src/routes/projects/+page.svelte client/src/lib/components/titlebar/WorkspaceTabs.svelte
git commit -m "feat(projects): add /projects page with creation wizard, plan review, and lifecycle controls"
```

---

## Task 11: Backend — Add api:agents and api:standup Source Resolvers

**Files:**
- Modify: `src/pocketpaw/api/v1/kits.py:193-237`

**Step 1: Add resolvers**

In the `_API_SOURCE_RESOLVERS` dict (line 193), add:

```python
_API_SOURCE_RESOLVERS: dict[str, str] = {
    "api:stats": "stats",
    "api:tasks": "tasks",
    "api:activities": "activities",
    "api:documents": "documents",
    "api:agents": "agents",
    "api:standup": "standup",
}
```

In `_resolve_source` function, add cases:

```python
        if key == "agents":
            agents = await mc.list_agents()
            return [a.to_dict() for a in agents]

        if key == "standup":
            return await mc.get_standup_summary()
```

**Step 2: Commit**

```bash
git add src/pocketpaw/api/v1/kits.py
git commit -m "feat(kits): add api:agents and api:standup data source resolvers"
```

---

## Task 12: Final Verification

**Step 1: Type check client**

Run: `cd client && bun run check 2>&1 | grep -c ERROR`
Expected: Same count as before (41 pre-existing, 0 new).

**Step 2: Lint backend**

Run: `uv run ruff check src/pocketpaw/api/v1/kits.py`
Expected: No errors.

**Step 3: Build client**

Run: `cd client && bun run build`
Expected: Build succeeds.

**Step 4: Final commit (if any fixups needed)**

```bash
git add -A
git commit -m "chore: fixups from final verification"
```
