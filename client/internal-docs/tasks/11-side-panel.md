# Task 11: Universal AI Side Panel

> The defining feature. A floating chat panel docked to the screen edge that works alongside any app.

## Goal

Build the Universal AI Side Panel — a second Tauri window that docks to the screen edge and provides context-aware AI assistance alongside whatever app the user is working in.

## Depends On

- **Task 10** (System Tray): hotkey `Cmd+Shift+L` to toggle, Tauri multi-window
- **Task 05** (Chat Interface): reuses `ChatPanel.svelte`
- **Task 02** (Stores): shared connection + chat stores

## Files to Create / Modify

```
src-tauri/src/
├── lib.rs              # UPDATE: register side panel window
├── side_panel.rs       # Side panel window management
└── context.rs          # OS-level active app/file detection

src/lib/components/
├── sidepanel/
│   ├── SidePanelLayout.svelte  # Side panel root layout
│   ├── ContextBar.svelte       # Shows detected active file/app
│   └── SidePanelChat.svelte    # Slim chat (reuses ChatPanel)

src/routes/
└── sidepanel/
    └── +page.svelte            # Side panel page (loaded in second window)
```

## Architecture

```
Tauri App
├── Main Window       → Full PocketPaw client (chat, skills, settings)
│   URL: /
└── Side Panel Window → Slim floating chat, contextually aware
    URL: /sidepanel
```

Both windows share the same Tauri process and can communicate via Tauri events or shared WebSocket connection.

## Side Panel Window Properties

```rust
// In Rust
let side_panel = tauri::WebviewWindowBuilder::new(
    &app,
    "sidepanel",
    tauri::WebviewUrl::App("/sidepanel".into()),
)
.title("PocketPaw")
.inner_size(320.0, 600.0)      // Slim width
.min_inner_size(280.0, 400.0)
.position(screen_width - 320.0, 0.0)  // Right edge
.always_on_top(true)            // Optional, user toggle
.decorations(false)             // Custom title bar for slim look
.transparent(true)              // Rounded corners
.resizable(true)
.skip_taskbar(true)             // Don't show in taskbar
.build()?;
```

## Side Panel Layout

```
┌────────────────────┐
│ 🐾 PocketPaw    ─  │  ← Minimal title bar (drag area + minimize)
│                    │
│ Working on:        │
│ 📄 Proposal.docx   │  ← ContextBar (detected file/app)
│                    │
│ ────────────────── │
│                    │
│ You: "Make the     │
│ deadline dates     │  ← ChatPanel (reused, slim variant)
│ two weeks later"   │
│                    │
│ 🐾: Done. Updated  │
│ all 5 phase dates. │
│ Word should prompt │
│ you to reload.     │
│                    │
│ ────────────────── │
│                    │
│ [___________] 📎▶ │  ← ChatInput (reused)
└────────────────────┘
```

## Context Detection (Rust)

### Active App/File Detection

The Rust sidecar polls the OS for the active window and extracts context:

```rust
// src-tauri/src/context.rs

#[derive(Serialize)]
struct ActiveContext {
    app_name: String,       // "Microsoft Word", "VS Code", "Chrome"
    window_title: String,   // "Proposal.docx - Word"
    file_path: Option<String>, // "/Users/.../Proposal.docx" (if detectable)
    icon: String,           // Emoji: 📄, 📂, 🌐, 🎨, etc.
}

#[tauri::command]
fn get_active_context() -> Result<ActiveContext, String> {
    // Linux: xdotool getactivewindow + xprop _NET_WM_NAME + /proc/pid/cwd
    // macOS: NSWorkspace.shared.frontmostApplication + AX API
    // Windows: GetForegroundWindow() + GetWindowText()
}
```

### Platform-Specific Implementation

**Linux:**
```rust
// Use x11 or wayland protocols
// xdotool getactivewindow → window ID
// xprop _NET_WM_NAME → window title
// xprop _NET_WM_PID → process PID
// /proc/{pid}/cwd → working directory
// /proc/{pid}/fd/ → open file descriptors
```

**macOS:**
```rust
// NSWorkspace.shared.frontmostApplication → app info
// AXUIElement for accessibility API → document title/path
```

**Windows:**
```rust
// GetForegroundWindow() → HWND
// GetWindowText() → title
// Shell API for document path
```

### Context Update Flow

1. Poll active window every 2 seconds (configurable)
2. Emit Tauri event `context-changed` with `ActiveContext`
3. Frontend ContextBar updates
4. When user sends a message, include context as metadata:
   ```json
   {
     "action": "chat",
     "content": "make the dates two weeks later",
     "metadata": {
       "active_file": "/path/to/Proposal.docx",
       "active_app": "Microsoft Word"
     }
   }
   ```

## ContextBar.svelte

Shows the detected context at the top of the side panel:

```
Working on:
📄 Proposal.docx
```

Updates automatically as the user switches apps. States:
- File detected: `📄 Proposal.docx`
- App detected (no file): `📂 ~/projects/myapp (VS Code)`
- Browser: `🌐 github.com/pocketpaw/pocketpaw`
- No detection: `🐾 Ready to help`

## Side Panel Modes

### Docked (default)
- Docked to right edge of screen
- Takes up ~320px width
- The rest of the screen is for the user's app

### Collapsed
- Shrinks to a thin strip (~40px) with just the 🐾 icon
- Click or hover to expand
- Keyboard shortcut to toggle

### Detached
- Float freely, drag anywhere, any monitor
- Behaves like a regular window (but still slim)

### Hidden
- Fully hidden
- Only accessible via tray icon or `Cmd+Shift+L`

## Window Management

```typescript
// Toggle side panel
async function toggleSidePanel() {
  const sidePanel = await WebviewWindow.getByLabel("sidepanel");
  if (sidePanel) {
    const isVisible = await sidePanel.isVisible();
    if (isVisible) {
      await sidePanel.hide();
    } else {
      await sidePanel.show();
      await sidePanel.setFocus();
    }
  } else {
    // Create the window
    new WebviewWindow("sidepanel", { url: "/sidepanel", ... });
  }
}
```

## Shared State

Both windows (main + side panel) share the same WebSocket connection via the stores. Options:
1. **Shared stores via Tauri events**: main window owns the WS, side panel communicates via inter-window events
2. **Independent WS connections**: both windows connect independently to the backend (simpler but uses 2 WS connections)

Recommend option 2 for simplicity. The backend handles multiple WebSocket connections fine.

The side panel uses a separate chat session or the same active session (user configurable).

## Acceptance Criteria

- [ ] Side panel opens as a second Tauri window
- [ ] `Cmd+Shift+L` toggles side panel visibility
- [ ] ContextBar shows the active app/file
- [ ] Context updates when the user switches apps
- [ ] Messages sent from side panel include active file context
- [ ] ChatPanel component is reused (slim variant)
- [ ] Side panel can be docked, collapsed, detached, or hidden
- [ ] Side panel resizable (width only when docked)
- [ ] Custom title bar with drag area
- [ ] Side panel persists position across restarts

## Notes

- Context detection is best-effort. Not every app exposes its open file path. When it can't detect: show "Ready to help" — no error.
- Linux context detection is the most variable. X11 is well-supported; Wayland is harder. Start with X11, add Wayland later.
- The side panel is a premium feature. If context detection isn't perfect, that's fine for v1.
- The side panel shares the same PocketPaw backend instance as the main window.
- Don't build file editing into the side panel. The agent edits files via its Python tools. The side panel just provides the chat interface with context.
- This is the "killer feature" — but it should be built last because it depends on everything else working.
