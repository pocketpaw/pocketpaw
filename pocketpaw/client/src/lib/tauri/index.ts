export { sendNotification, notifyAgentComplete, notifyGuardianBlock, requestNotificationPermission } from "./notifications";
export { registerHotkeys, unregisterHotkeys } from "./hotkeys";
export { isAutoStartEnabled, enableAutoStart, disableAutoStart, toggleAutoStart } from "./autostart";
export { setupTrayListeners, cleanupTrayListeners } from "./tray";
export {
  emitSessionSwitch,
  emitChatSync,
  emitSettingsUpdate,
  emitSidePanelReady,
  onSessionSwitch,
  onChatSync,
  onSettingsUpdate,
  onSidePanelReady,
  disposeAllBridgeListeners,
} from "./bridge";
export type { SessionSwitchPayload, ChatSyncPayload } from "./bridge";
