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

  async loadAgents(): Promise<void> {
    try {
      const client = connectionStore.getClient();
      const res = await client.mcListAgents();
      this.agents = res.agents;
    } catch (err) {
      console.error("[MCStore] Failed to load agents:", err);
    }
  }

  async loadRunningTasks(): Promise<void> {
    try {
      const client = connectionStore.getClient();
      const res = await client.mcGetRunningTasks();
      this.runningTaskIds = new Set(res.running_tasks.map((t) => String(t.task_id)));
    } catch (err) {
      console.error("[MCStore] Failed to load running tasks:", err);
    }
  }

  async loadNotifications(): Promise<void> {
    try {
      const client = connectionStore.getClient();
      const res = await client.mcListNotifications();
      this.notifications = res.notifications;
    } catch (err) {
      console.error("[MCStore] Failed to load notifications:", err);
    }
  }

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

  closeExecution(): void {
    this.activeExecution = null;
  }

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
