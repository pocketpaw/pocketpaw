/**
 * Frontend tray state management.
 * Listens for tray events emitted by the Rust backend.
 */

let listenFn: typeof import("@tauri-apps/api/event").listen | null = null;

async function getListen() {
  if (listenFn) return listenFn;
  try {
    const mod = await import("@tauri-apps/api/event");
    listenFn = mod.listen;
    return listenFn;
  } catch {
    return null;
  }
}

export interface TrayEventHandlers {
  onNavigate?: (path: string) => void;
}

const unlisten: (() => void)[] = [];

export async function setupTrayListeners(handlers: TrayEventHandlers): Promise<void> {
  const listen = await getListen();
  if (!listen) return;

  try {
    if (handlers.onNavigate) {
      const cb = handlers.onNavigate;
      const u = await listen<string>("tray-navigate", (event) => cb(event.payload));
      unlisten.push(u);
    }
  } catch (err) {
    console.warn("[Tray] Failed to setup listeners:", err);
  }
}

export function cleanupTrayListeners(): void {
  for (const u of unlisten) u();
  unlisten.length = 0;
}
