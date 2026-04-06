/**
 * Global hotkey registration using @tauri-apps/plugin-global-shortcut.
 * Gracefully no-ops in non-Tauri environments.
 */

let shortcutModule: typeof import("@tauri-apps/plugin-global-shortcut") | null = null;

async function getModule() {
  if (shortcutModule) return shortcutModule;
  try {
    shortcutModule = await import("@tauri-apps/plugin-global-shortcut");
    return shortcutModule;
  } catch {
    return null;
  }
}

export interface HotkeyHandlers {
  onQuickAsk?: () => void;
  onToggleSidePanel?: () => void;
}

const QUICK_ASK_SHORTCUT = "CommandOrControl+Shift+P";
const SIDE_PANEL_SHORTCUT = "CommandOrControl+Shift+L";

export async function registerHotkeys(handlers: HotkeyHandlers): Promise<void> {
  const mod = await getModule();
  if (!mod) return;

  try {
    if (handlers.onQuickAsk) {
      const cb = handlers.onQuickAsk;
      await mod.register(QUICK_ASK_SHORTCUT, (event) => {
        if (event.state === "Pressed") cb();
      });
    }

    if (handlers.onToggleSidePanel) {
      const cb = handlers.onToggleSidePanel;
      await mod.register(SIDE_PANEL_SHORTCUT, (event) => {
        if (event.state === "Pressed") cb();
      });
    }
  } catch (err) {
    console.warn("[Hotkeys] Failed to register shortcuts:", err);
  }
}

export async function unregisterHotkeys(): Promise<void> {
  const mod = await getModule();
  if (!mod) return;

  try {
    await mod.unregisterAll();
  } catch {
    // Silently fail
  }
}
