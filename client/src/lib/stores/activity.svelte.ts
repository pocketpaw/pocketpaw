// Activity entries are fed exclusively from SSE via pushSSEEvent() in chatStore.

export interface ActivityEntry {
  id: string;
  type: "tool_start" | "tool_result" | "thinking" | "error" | "status";
  content: string;
  data?: Record<string, unknown>;
  timestamp: string;
}

let entryCounter = 0;
function nextId(): string {
  return `activity-${++entryCounter}`;
}

function formatToolStart(data: Record<string, unknown>): string {
  const tool = data.tool ?? "unknown";
  const input = data.input;
  if (input && typeof input === "object") {
    const keys = Object.keys(input as Record<string, unknown>);
    if (keys.length > 0) {
      return `${tool}(${keys.join(", ")})`;
    }
  }
  return String(tool);
}

function formatToolResult(data: Record<string, unknown>): string {
  const output = String(data.output ?? "");
  // Truncate long outputs
  return output.length > 120 ? output.slice(0, 120) + "..." : output;
}

class ActivityStore {
  entries = $state<ActivityEntry[]>([]);
  isAgentWorking = $state(false);
  currentModel = $state<string | null>(null);
  tokenUsage = $state<{ input?: number; output?: number } | null>(null);
  sseActive = $state(false);

  recentEntries = $derived(this.entries.slice(-50));
  latestEntry = $derived(this.entries.at(-1) ?? null);

  clear(): void {
    this.entries = [];
    this.isAgentWorking = false;
    this.tokenUsage = null;
    this.sseActive = false;
  }

  /** Push an event from the SSE chat stream into the activity log. */
  pushSSEEvent(
    eventType: "tool_start" | "tool_result" | "thinking",
    data: Record<string, unknown>,
  ): void {
    const now = new Date().toISOString();
    switch (eventType) {
      case "tool_start":
        this.entries.push({
          id: nextId(),
          type: "tool_start",
          content: formatToolStart(data),
          data,
          timestamp: now,
        });
        break;
      case "tool_result":
        this.entries.push({
          id: nextId(),
          type: "tool_result",
          content: formatToolResult(data),
          data,
          timestamp: now,
        });
        break;
      case "thinking":
        this.entries.push({
          id: nextId(),
          type: "thinking",
          content: String(data.content ?? ""),
          data,
          timestamp: now,
        });
        break;
    }
  }

}

export const activityStore = new ActivityStore();
