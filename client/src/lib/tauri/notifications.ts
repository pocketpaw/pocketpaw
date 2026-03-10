/**
 * Native notification helpers using @tauri-apps/plugin-notification.
 * Gracefully no-ops when running in browser (non-Tauri) mode.
 */

let notificationModule: typeof import("@tauri-apps/plugin-notification") | null = null;

async function getModule() {
  if (notificationModule) return notificationModule;
  try {
    notificationModule = await import("@tauri-apps/plugin-notification");
    return notificationModule;
  } catch {
    return null;
  }
}

export async function requestNotificationPermission(): Promise<boolean> {
  const mod = await getModule();
  if (!mod) return false;
  try {
    const granted = await mod.isPermissionGranted();
    if (granted) return true;
    const result = await mod.requestPermission();
    return result === "granted";
  } catch {
    return false;
  }
}

export async function sendNotification(title: string, body: string): Promise<void> {
  const mod = await getModule();
  if (!mod) return;
  try {
    const granted = await mod.isPermissionGranted();
    if (!granted) return;
    mod.sendNotification({ title, body });
  } catch {
    // Silently fail in non-Tauri environments
  }
}

/**
 * Notify when agent completes a task while window is not focused.
 */
export async function notifyAgentComplete(summary: string): Promise<void> {
  if (typeof document !== "undefined" && document.hasFocus()) return;
  await sendNotification("PocketPaw", summary);
}

/**
 * Notify when Guardian AI blocks something.
 */
export async function notifyGuardianBlock(detail: string): Promise<void> {
  await sendNotification("PocketPaw - Guardian AI", detail);
}
