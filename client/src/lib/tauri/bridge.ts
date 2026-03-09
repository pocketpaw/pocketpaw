/**
 * Cross-window event bridge using Tauri's emit/listen system.
 * Enables main window <-> side panel synchronization.
 */

import type { ChatMessage, Settings } from "$lib/api";

// -- Event names ---------------------------------------------------------------

const EVENTS = {
  SESSION_SWITCH: "pp:session-switch",
  CHAT_SYNC: "pp:chat-sync",
  SETTINGS_UPDATED: "pp:settings-updated",
  SIDEPANEL_READY: "pp:sidepanel-ready",
  ATTACH_CHANGED: "attach-changed",
  ATTACH_ERROR: "attach-error",
} as const;

// -- Payload types -------------------------------------------------------------

export interface SessionSwitchPayload {
  sessionId: string;
}

export interface ChatSyncPayload {
  messages: ChatMessage[];
  streaming: boolean;
  streamingContent: string;
}

export type AttachMode = "auto" | "docked" | "disabled";

export interface AttachChangedPayload {
  mode: AttachMode;
  attached: boolean;
  app_name: string;
  window_title: string;
}

export interface AttachErrorPayload {
  message: string;
}

// -- Internal helpers ----------------------------------------------------------

type UnlistenFn = () => void;

const activeListeners: UnlistenFn[] = [];

async function emitEvent<T>(event: string, payload: T): Promise<void> {
  try {
    const { emit } = await import("@tauri-apps/api/event");
    await emit(event, payload);
  } catch {
    // Not in Tauri environment
  }
}

async function listenEvent<T>(
  event: string,
  handler: (payload: T) => void,
): Promise<UnlistenFn> {
  try {
    const { listen } = await import("@tauri-apps/api/event");
    const unlisten = await listen<T>(event, (e) => handler(e.payload));
    activeListeners.push(unlisten);
    return unlisten;
  } catch {
    return () => {};
  }
}

// -- Emitters ------------------------------------------------------------------

export function emitSessionSwitch(sessionId: string): void {
  emitEvent<SessionSwitchPayload>(EVENTS.SESSION_SWITCH, { sessionId });
}

export function emitChatSync(state: ChatSyncPayload): void {
  emitEvent(EVENTS.CHAT_SYNC, state);
}

export function emitSettingsUpdate(settings: Settings): void {
  emitEvent(EVENTS.SETTINGS_UPDATED, settings);
}

export function emitSidePanelReady(): void {
  emitEvent(EVENTS.SIDEPANEL_READY, {});
}

// -- Listeners -----------------------------------------------------------------

export function onSessionSwitch(
  handler: (payload: SessionSwitchPayload) => void,
): Promise<UnlistenFn> {
  return listenEvent(EVENTS.SESSION_SWITCH, handler);
}

export function onChatSync(
  handler: (payload: ChatSyncPayload) => void,
): Promise<UnlistenFn> {
  return listenEvent(EVENTS.CHAT_SYNC, handler);
}

export function onSettingsUpdate(
  handler: (settings: Settings) => void,
): Promise<UnlistenFn> {
  return listenEvent(EVENTS.SETTINGS_UPDATED, handler);
}

export function onSidePanelReady(
  handler: () => void,
): Promise<UnlistenFn> {
  return listenEvent(EVENTS.SIDEPANEL_READY, handler);
}

export function onAttachChanged(
  handler: (payload: AttachChangedPayload) => void,
): Promise<UnlistenFn> {
  return listenEvent(EVENTS.ATTACH_CHANGED, handler);
}

export function onAttachError(
  handler: (payload: AttachErrorPayload) => void,
): Promise<UnlistenFn> {
  return listenEvent(EVENTS.ATTACH_ERROR, handler);
}

// -- Cleanup -------------------------------------------------------------------

export function disposeAllBridgeListeners(): void {
  for (const unlisten of activeListeners) {
    unlisten();
  }
  activeListeners.length = 0;
}
