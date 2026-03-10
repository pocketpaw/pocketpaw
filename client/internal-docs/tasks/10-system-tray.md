# Task 10: System Tray, Global Hotkey & Notifications

> Tauri-specific OS integration: tray icon, hotkeys, native notifications.

## Goal

Make PocketPaw a persistent presence on the desktop — system tray icon, global hotkey for quick ask, and native OS notifications.

## Depends On

- **Task 05** (Chat Interface): quick ask sends messages to chat
- **Task 02** (Stores): `connectionStore`, `activityStore`

## Tauri Plugin Dependencies

Add to `src-tauri/Cargo.toml`:

```toml
[dependencies]
tauri-plugin-notification = "2"
tauri-plugin-global-shortcut = "2"
tauri-plugin-autostart = "2"
```

Add to `package.json`:

```bash
bun add @tauri-apps/plugin-notification
bun add @tauri-apps/plugin-global-shortcut
bun add @tauri-apps/plugin-autostart
```

Update `tauri.conf.json` permissions/capabilities as needed.

## Files to Create / Modify

```
src-tauri/src/
├── lib.rs              # UPDATE: register plugins, setup tray
├── tray.rs             # System tray setup and event handling
└── commands.rs         # Tauri commands (read token file, etc.)

src/lib/
├── tauri/
│   ├── tray.ts         # Frontend tray state management
│   ├── hotkeys.ts      # Global hotkey registration
│   ├── notifications.ts # Notification helpers
│   └── autostart.ts    # Auto-start on boot
├── components/
│   └── QuickAsk.svelte # Floating quick-ask input bar
```

## System Tray

### Tray Icon States

The tray icon changes color based on state:
- 🟢 **Idle**: connected, ready
- 🟠 **Working**: agent processing a request
- 🔴 **Error/Attention**: disconnected or agent asking a question
- 🔵 **New message**: result ready, user hasn't seen it

Implement with 4 icon variants (PNG) in `src-tauri/icons/tray/`:
- `tray-idle.png`
- `tray-working.png`
- `tray-error.png`
- `tray-notification.png`

### Tray Menu (Right-click)

```
┌─────────────────────────┐
│  Open PocketPaw         │
│  ─────────────────────  │
│  Quick Ask...     ⌘⇧P  │
│  ─────────────────────  │
│  ● Connected (Claude)   │
│  ○ 2 tasks running      │
│  ─────────────────────  │
│  Settings               │
│  Check for Updates      │
│  ─────────────────────  │
│  Quit PocketPaw         │
│  ─────────────────────  │
│  Start on Boot  [✓]     │
└─────────────────────────┘
```

### Tray Behavior

- **Close button (✕)**: minimize to tray, don't quit
- **Tray left-click**: toggle main window visibility
- **"Quit PocketPaw"**: actually quit the app
- **"Open PocketPaw"**: show and focus main window

## Global Hotkeys

| Shortcut      | Action                    |
|---------------|---------------------------|
| `Cmd+Shift+P` | Open Quick Ask floating bar |
| `Cmd+Shift+L` | Toggle Side Panel (Task 11) |

### QuickAsk.svelte

A floating input bar (separate Tauri window or overlay) that appears over everything:

```
┌──────────────────────────────────────────────────────┐
│  🐾  What can I help with?                      ▶   │
└──────────────────────────────────────────────────────┘
```

- Appears centered on screen (like Spotlight/Raycast)
- Single text input + send button
- `Enter` sends the message, closes the bar
- `Esc` closes without sending
- Result appears as a notification (simple result) or opens main window (complex result)
- Always-on-top, no title bar, rounded corners

Implementation options:
1. **Separate Tauri window** (preferred): small, always-on-top, transparent background
2. **OS overlay**: more complex, less portable

## Native Notifications

Use `tauri-plugin-notification` to send OS-level notifications.

When to notify:
- Agent finished a task (stream_end) while window is not focused
- Agent needs attention (error, question with action buttons)
- Guardian AI blocked a command
- Scheduled reminder triggered

Notification format:
```
┌──────────────────────────────────────────┐
│  🐾 PocketPaw                    just now │
│                                          │
│  Your expense report is ready.           │
│  3 receipts, $247.50 total.              │
│                                          │
│  [ Open ]              [ Dismiss ]       │
└──────────────────────────────────────────┘
```

Clicking a notification: show and focus the main window, navigate to the relevant chat.

## Auto-start on Boot

Use `tauri-plugin-autostart`:
- Setting toggle in tray menu and Settings > About
- When enabled: app starts minimized to tray on login
- Default: off (user must opt in)

## Tauri Commands (Rust → Frontend)

```rust
#[tauri::command]
fn read_access_token() -> Result<String, String>
  // Read ~/.pocketpaw/access_token

#[tauri::command]
fn get_pocketpaw_config_dir() -> Result<String, String>
  // Return ~/.pocketpaw/ path

#[tauri::command]
fn check_backend_running(port: u16) -> Result<bool, String>
  // Check if localhost:port is reachable
```

## Acceptance Criteria

- [ ] System tray icon appears and reflects connection state
- [ ] Tray right-click menu works with all items
- [ ] Close button minimizes to tray (doesn't quit)
- [ ] Tray left-click toggles window
- [ ] Global hotkey `Cmd+Shift+P` opens quick ask
- [ ] Quick ask sends message and shows result as notification
- [ ] Native notifications fire when agent completes while window unfocused
- [ ] Notification click opens main window
- [ ] Auto-start toggle works
- [ ] Tauri commands read access token from filesystem

## Notes

- Hotkey registration may fail if another app uses the same shortcut. Handle gracefully.
- On Linux, system tray behavior varies by desktop environment. Test with common ones (GNOME, KDE).
- The quick ask bar should feel instant — no loading state, no splash screen.
- Auto-start should be off by default. Users don't like apps that auto-start without asking.
- Generate the 4 tray icon PNGs from the PocketPaw paw icon with colored dots.
