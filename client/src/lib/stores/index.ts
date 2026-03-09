import { connectionStore } from "./connection.svelte";
import { chatStore } from "./chat.svelte";
import { sessionStore } from "./sessions.svelte";
import { settingsStore } from "./settings.svelte";
import { activityStore } from "./activity.svelte";
import { skillStore } from "./skills.svelte";
import { uiStore } from "./ui.svelte";
import { platformStore } from "./platform.svelte";
import { explorerStore } from "./explorer.svelte";
import { kitStore } from "./kits.svelte";
import { mcStore } from "./mission-control.svelte";
import { projectStore } from "./projects.svelte";

export { connectionStore, chatStore, sessionStore, settingsStore, activityStore, skillStore, uiStore, platformStore, explorerStore, kitStore, mcStore, projectStore };
export type { FileTypeCategory, ExplorerTab } from "./explorer.svelte";
export type { ActivityEntry } from "./activity.svelte";
export type { ActiveExecution, ExecutionLogEntry } from "./mission-control.svelte";
export type { PlanningPhase } from "./projects.svelte";

// Master initialization — called once on app startup after obtaining a token.
// Sets up REST client, connects WebSocket (push-only), and loads initial data.
export async function initializeStores(token: string, baseUrl?: string, wsToken?: string): Promise<void> {
  // Create REST client, obtain session cookie, then connect WebSocket
  await connectionStore.initialize(token, baseUrl, wsToken);

  // No bindEvents — stores no longer depend on WS for request-response flows.
  // WS is kept for push-only events (notifications, reminders, health, skills).

  // Load initial data via REST in background (don't block UI)
  Promise.allSettled([
    sessionStore.loadSessions(),
    settingsStore.load(),
  ]);

  // Initialize explorer store (loads default dirs, pinned folders, WS events)
  explorerStore.initialize();
  explorerStore.bindEvents();

  // Load installed kits in background
  kitStore.load();

  // Initialize MC store (agents, running tasks, notifications, WS events)
  mcStore.initialize();

  // Initialize project store (WS events for planning)
  projectStore.initialize();
}
