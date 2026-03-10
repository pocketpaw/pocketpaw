import type { MCProject, MCProjectProgress } from "$lib/types/pawkit";
import type { WSDWPlanningPhase, WSDWPlanningComplete } from "$lib/api/types";
import { connectionStore } from "./connection.svelte";

export type PlanningPhase = "goal_analysis" | "research" | "prd" | "tasks" | "team";

class ProjectStore {
  projects = $state<MCProject[]>([]);
  isLoading = $state(false);

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

  async parseGoal(goal: string): Promise<Record<string, unknown> | null> {
    try {
      const client = connectionStore.getClient();
      const res = await client.dwParseGoal(goal);
      // Backend returns { success, goal_analysis: { ... } } — unwrap
      return (res as any).goal_analysis ?? res;
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
      // Backend returns { success, project: { id, ... } }
      const projectId = (res as any).project?.id ?? (res as any).project_id;
      this.planningProjectId = projectId;
      return projectId;
    } catch (err) {
      console.error("[ProjectStore] Failed to start project:", err);
      this.planningError = String(err);
      return null;
    }
  }

  async getPlan(projectId: string) {
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
          this.loadProjects();
        }),
      );

      this.unsubWs.push(
        ws.on("dw_project_cancelled" as any, (_event: any) => {
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
