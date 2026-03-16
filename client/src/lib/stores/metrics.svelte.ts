import type {
  SystemMetrics,
  HealthSummary,
  HealthErrorEntry,
  SecurityAuditResponse,
  VersionInfo,
  UsageSummary,
  UsageRecord,
} from "$lib/api/types";
import type { Session } from "$lib/api/types";
import { connectionStore } from "./connection.svelte";
import { activityStore } from "./activity.svelte";
import { sessionStore } from "./sessions.svelte";

const REFRESH_INTERVAL_MS = 5_000;

class MetricsStore {
  system = $state<SystemMetrics | null>(null);
  health = $state<HealthSummary | null>(null);
  errors = $state<HealthErrorEntry[]>([]);
  securityAudit = $state<SecurityAuditResponse | null>(null);
  version = $state<VersionInfo | null>(null);
  usage = $state<UsageSummary | null>(null);
  recentUsage = $state<UsageRecord[]>([]);
  loading = $state(false);
  lastRefresh = $state<string | null>(null);

  private intervalId: ReturnType<typeof setInterval> | null = null;

  // Derived from other stores
  get tokenUsage() {
    return activityStore.tokenUsage;
  }
  get currentModel() {
    return activityStore.currentModel;
  }
  get isAgentWorking() {
    return activityStore.isAgentWorking;
  }
  get sessions(): Session[] {
    return sessionStore.sessions;
  }
  get totalMessages(): number {
    return this.sessions.reduce((sum, s) => sum + (s.message_count ?? 0), 0);
  }

  private getClient() {
    try {
      return connectionStore.getClient();
    } catch {
      return null;
    }
  }

  async fetchAll(): Promise<void> {
    const client = this.getClient();
    if (!client) return;

    this.loading = true;
    try {
      const [system, health, errors, version, usage, recentUsage] = await Promise.allSettled([
        client.getSystemMetrics(),
        client.getHealth(),
        client.getHealthErrors(5),
        client.getVersion(),
        client.getUsageSummary(),
        client.getRecentUsage(20),
      ]);

      if (system.status === "fulfilled") this.system = system.value;
      if (health.status === "fulfilled") this.health = health.value;
      if (errors.status === "fulfilled") this.errors = errors.value;
      if (version.status === "fulfilled") this.version = version.value;
      if (usage.status === "fulfilled") this.usage = usage.value;
      if (recentUsage.status === "fulfilled") this.recentUsage = recentUsage.value;

      this.lastRefresh = new Date().toISOString();
    } finally {
      this.loading = false;
    }
  }

  async runSecurityAudit(): Promise<void> {
    const client = this.getClient();
    if (!client) return;
    this.securityAudit = await client.runSecurityAudit();
  }

  async clearUsage(): Promise<void> {
    const client = this.getClient();
    if (!client) return;
    await client.clearUsage();
    this.usage = null;
    this.recentUsage = [];
  }

  startAutoRefresh(): void {
    this.stopAutoRefresh();
    this.fetchAll();
    this.intervalId = setInterval(() => this.fetchAll(), REFRESH_INTERVAL_MS);
  }

  stopAutoRefresh(): void {
    if (this.intervalId) {
      clearInterval(this.intervalId);
      this.intervalId = null;
    }
  }
}

export const metricsStore = new MetricsStore();
