/**
 * Auto-start on boot using @tauri-apps/plugin-autostart.
 * Gracefully no-ops in non-Tauri environments.
 */

let autostartModule: typeof import("@tauri-apps/plugin-autostart") | null = null;

async function getModule() {
  if (autostartModule) return autostartModule;
  try {
    autostartModule = await import("@tauri-apps/plugin-autostart");
    return autostartModule;
  } catch {
    return null;
  }
}

export async function isAutoStartEnabled(): Promise<boolean> {
  const mod = await getModule();
  if (!mod) return false;
  try {
    return await mod.isEnabled();
  } catch {
    return false;
  }
}

export async function enableAutoStart(): Promise<void> {
  const mod = await getModule();
  if (!mod) return;
  try {
    await mod.enable();
  } catch (err) {
    console.warn("[Autostart] Failed to enable:", err);
  }
}

export async function disableAutoStart(): Promise<void> {
  const mod = await getModule();
  if (!mod) return;
  try {
    await mod.disable();
  } catch (err) {
    console.warn("[Autostart] Failed to disable:", err);
  }
}

export async function toggleAutoStart(enabled: boolean): Promise<void> {
  if (enabled) {
    await enableAutoStart();
  } else {
    await disableAutoStart();
  }
}
