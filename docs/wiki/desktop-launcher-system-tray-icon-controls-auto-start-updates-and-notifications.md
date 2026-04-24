---
{
  "title": "Desktop Launcher System Tray Icon: Controls, Auto-Start, Updates, and Notifications",
  "summary": "The `TrayIcon` class provides the persistent system tray presence for the PocketPaw desktop launcher using `pystray`. It exposes a context menu for all runtime operations — starting, stopping, restarting the server, opening the dashboard, managing auto-start, checking for updates, viewing logs, uninstalling, and quitting.",
  "concepts": [
    "TrayIcon",
    "pystray",
    "system tray",
    "context menu",
    "auto-start",
    "LaunchAgents",
    "Windows registry",
    "update check",
    "server toggle",
    "daemon thread",
    "tooltip polling",
    "OS notifications"
  ],
  "categories": [
    "installer",
    "launcher",
    "ui",
    "desktop"
  ],
  "source_docs": [
    "d37aac04bb2be602"
  ],
  "backlinks": null,
  "word_count": 574,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`installer/launcher/tray.py` is the primary user interface once PocketPaw is installed. After the splash window closes, the tray icon becomes the only visible presence of the launcher. All runtime operations are accessible from its context menu without requiring a full window. The module has a hard dependency on `pystray` and `Pillow`, but both are guarded with a try/except — if either is unavailable, `HAS_TRAY = False` and the tray silently disables itself rather than crashing the launcher.

## Component Wiring

`TrayIcon.__init__()` takes a `ServerManager` and an `Updater` — both are injected, not created internally. This allows the same server and updater instances to be shared with other launcher components (e.g., the splash window). The tray does not own the server lifecycle; it delegates.

## Menu Construction

`_build_menu()` returns a `pystray.Menu` with dynamic items. `_server_toggle_text()` and `_update_text()` are callable items — pystray calls them each time the menu is rendered, ensuring the label reflects current state ("Stop Server" vs. "Start Server"). This avoids the pattern of rebuilding the entire menu on every state change.

## Server Toggle

`_on_toggle_server()` runs in the pystray event thread. It calls `_start_and_open()` (which starts the server and then opens the dashboard) or `server.stop()` depending on current state. The start path runs in a new thread to avoid blocking the tray event loop — a blocked tray appears frozen to the user.

## Auto-Start

`_on_toggle_autostart()` adds or removes the launcher from the OS login items. The implementation is platform-specific:
- **macOS**: writes/removes a `.plist` in `~/Library/LaunchAgents/`
- **Windows**: sets/removes a registry key under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`

Using the OS-native mechanisms ensures the launcher behaves consistently with other apps in the user's login items list — it appears in System Settings on macOS and in Task Manager's Startup tab on Windows.

## Update Flow

`_on_check_update()` runs `_do_update()` in a background thread. `_do_update()` calls `updater.check()` first; if an update is available, it calls `updater.apply()` and then notifies the user. The notification (`_notify()`) uses `pystray`'s notification API, which maps to OS-native notifications (Notification Center on macOS, Windows toast). Updates are not applied silently — the user always sees a notification before the server is restarted.

## Periodic Update Check

`_periodic_update_check()` runs in a daemon thread that sleeps 24 hours between checks. Starting it as a daemon thread means it does not prevent the process from exiting when the user quits. The 24-hour interval balances freshness against PyPI rate limiting.

## Tooltip

`_get_tooltip()` returns a string containing the server status and version. `_update_tooltip_loop()` refreshes this in a background thread every 2 seconds. Frequent tooltip updates are necessary because pystray on macOS does not support event-driven tooltip updates — the only way to reflect state changes is polling.

## Log Viewer

`_on_view_logs()` opens `LOG_FILE` (the server's log file in `POCKETPAW_HOME`) in the OS default text editor (`xdg-open` on Linux, `open` on macOS, `os.startfile` on Windows). This is intentionally the system editor rather than a custom log viewer, keeping the launcher's UI footprint minimal.

## Known Gaps

- `_notify()` falls back to a no-op if `pystray` does not support notifications on the current platform (e.g., some Linux desktop environments). There is no visual fallback.
- The 2-second tooltip polling creates a persistent background thread for the lifetime of the tray; on battery-powered devices this is a minor but nonzero power cost.
- Uninstall (`_do_uninstall()`) does not confirm with the user before removing files — the confirmation happens in the tray menu action, not in the `Uninstaller` class itself.