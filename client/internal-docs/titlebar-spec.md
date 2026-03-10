# PocketPaw Title Bar — Platform-Native + Feature-Rich

> The title bar isn't decoration — it's a dashboard. Session context, agent status, quick actions, and connection state, all in ~36px of height.

---

## The Anatomy

Every platform gets the same **content layout**, but wrapped in platform-native **material and chrome**.

```
┌─────────────────────────────────────────────────────────────────────────┐
│ [OS Controls]  [≡]  🐾 Session Title  •  Model Badge    [+] [⌘K] [◉] │
│▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓ agent progress bar ▓▓▓▓▓▓▓▓▓▓░░░░░░│
└─────────────────────────────────────────────────────────────────────────┘
 ↑                ↑        ↑              ↑              ↑  ↑    ↑
 platform         sidebar  session        AI model       new search connection
 controls         toggle   name           indicator      chat      status
```

### Content Zones (left → right)

| Zone | Content | Behavior |
|------|---------|----------|
| **1. OS Controls** | Platform window buttons | macOS: left. Windows/Linux: right |
| **2. Sidebar Toggle** | `≡` hamburger or `«` collapse icon | Toggles sidebar. Hidden when sidebar is pinned open on wide screens |
| **3. Session Info** | 🐾 icon + session title + model badge | Title is click-to-edit. Model badge is a small pill |
| **4. Quick Actions** | Icon buttons: New Chat `+`, Search `⌘K` | Small, ghost-style buttons |
| **5. Connection Badge** | Colored dot + hover detail | Live status of backend connection |
| **6. Progress Bar** | Thin animated bar at bottom edge of title bar | Shows when agent is working |

---

## Platform Designs

### macOS — Vibrancy + Traffic Lights

```
┌─ ● ● ●  ──  ≡   🐾 Budget Review  · Claude 4.5   ──  +  ⌘K  ◉ ──┐
│▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░░░░░░░│
```

**Material**: `NSVisualEffectView` vibrancy through Tauri's macOS API. The title bar is part of the window's vibrancy layer — it blurs the desktop behind it. Falls back to `backdrop-filter: blur()` in the webview if native vibrancy isn't achievable.

**Details**:
- Height: **38px** (macOS unified toolbar standard)
- Traffic lights: native macOS position (12px from left, vertically centered)
- Inset for traffic lights: session info starts ~76px from left to avoid overlap
- Font: system font (`-apple-system`) to match OS
- Background: transparent (inherits window vibrancy)
- No bottom border — blends into the sidebar/content below
- The sidebar and title bar share the same vibrancy layer = unified look

**Tauri config**:
```json
{
  "macOSPrivateApi": true,
  "windows": [{
    "decorations": false,
    "transparent": true,
    "titleBarStyle": "Overlay",
    "hiddenTitle": true
  }]
}
```

`titleBarStyle: "Overlay"` keeps native traffic lights but lets us draw under them.

---

### Windows — Mica + Fluent Controls

```
┌──  ≡   🐾 Budget Review  · Claude 4.5   ──  +  ⌘K  ◉  ── ─  □  ✕ ┐
│▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░░░░░░░░│
```

**Material**: Mica effect (Windows 11) or Acrylic fallback (Windows 10). Mica samples the desktop wallpaper behind the window for a subtle tinted translucency. On Windows 10 or if Mica isn't available, fall back to solid `--paw-bg-raised`.

**Details**:
- Height: **32px** (Windows standard)
- Window buttons: custom-rendered **on the right**, matching Fluent Design:
  - Minimize (`─`), Maximize/Restore (`□` / `⧉`), Close (`✕`)
  - Close button: red background on hover (Windows convention)
  - Maximize: supports **Snap Layouts** hover menu (Windows 11)
  - Buttons are taller than macOS traffic lights, filling the title bar height
- Font: `Segoe UI Variable` or `Segoe UI` to match Windows
- Background: Mica material or semi-transparent `--paw-bg-raised`
- Subtle bottom border: `1px solid oklch(1 0 0 / 5%)`

**Snap Layouts integration**: When hovering the maximize button, Windows 11 shows its native snap layout picker. To enable this, the maximize button needs `WM_NCHITTEST` returning `HTMAXBUTTON`. Tauri v2 supports this via window event hooks in Rust.

**Tauri config**:
```json
{
  "windows": [{
    "decorations": false,
    "transparent": true
  }]
}
```

**Rust side** (for Mica):
```rust
// Enable Mica/Acrylic on Windows 11
#[cfg(target_os = "windows")]
{
    use window_vibrancy::apply_mica;
    apply_mica(&window, Some(true))?; // true = dark mode
}
```

Crate: `window-vibrancy` (add to Cargo.toml).

---

### Linux — Clean + DE-Adaptive

```
┌──  ≡   🐾 Budget Review  · Claude 4.5   ──  +  ⌘K  ◉  ── ─  □  ✕ ┐
│▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░░░░░░░░│
```

**Material**: Glass effect via CSS `backdrop-filter: blur()` when the compositor supports it (most modern Linux DEs — KDE Plasma, GNOME on Wayland). Falls back to solid `--paw-bg-raised` on older systems/Xfce.

**Details**:
- Height: **34px** (between macOS and Windows, feels natural)
- Window buttons: custom-rendered **on the right** (default)
  - Detect GNOME's `button-layout` gsetting — if set to `close,minimize,maximize:`, render on **left**
  - Style: clean, minimal circles or subtle shapes that don't copy macOS or Windows
  - Close: `✕`, Minimize: `─`, Maximize: `□` — same as Windows but with Linux-neutral styling
- Font: system font (`system-ui`) to match whatever the user has
- Background: glass if compositor supports transparency, solid otherwise
- Tiling support: respect WM tiling (i3/Sway/Hyprland) — title bar stays functional when tiled

**Detecting compositor transparency support**:
```rust
#[cfg(target_os = "linux")]
fn supports_transparency() -> bool {
    // Check if compositor supports transparency
    // Wayland: usually yes
    // X11: check _NET_WM_CM_Sn atom
    std::env::var("WAYLAND_DISPLAY").is_ok()
        || check_x11_compositor()
}
```

**Detecting button layout (GNOME)**:
```rust
#[cfg(target_os = "linux")]
fn get_button_layout() -> String {
    // gsettings get org.gnome.desktop.wm.preferences button-layout
    // Returns "appmenu:minimize,maximize,close" (right) or "close,minimize,maximize:" (left)
    std::process::Command::new("gsettings")
        .args(["get", "org.gnome.desktop.wm.preferences", "button-layout"])
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).to_string())
        .unwrap_or_else(|_| "appmenu:minimize,maximize,close".into())
}
```

---

## Feature Details

### 1. Session Title (click-to-edit)

```
    🐾 Budget Review  ·  Claude 4.5
    ↑       ↑              ↑
    paw   editable        model pill
    icon   title
```

- Displays the active session's title (from `sessionStore.activeSession.title`)
- **Click**: enters edit mode (inline text input, same styling, auto-select all)
- **Enter/blur**: saves new title via `sessionStore.renameSession()`
- **Escape**: cancels edit
- Truncated with `...` if too long (max ~200px)
- New sessions show "New Chat" in muted text until the first response, then auto-generates a title
- The 🐾 paw icon is always present — it's the brand anchor

### 2. Model Badge

```
    · Claude 4.5          · Ollama (llama3.2)          · GPT-4o
      ↑                     ↑                           ↑
      small pill            shows model name             clickable?
```

- Small pill/badge next to the session title
- Shows the active AI model from `settingsStore.model`
- Styling: `text-xs font-medium px-1.5 py-0.5 rounded-full bg-[--paw-bg-surface] text-[--paw-text-secondary]`
- Separated from session title by a `·` dot
- Optional: clicking opens a quick model switcher dropdown (nice-to-have, not v1)

### 3. Agent Progress Bar

```
    ┌─────────────────────────────────────────────────────┐
    │  title bar content                                  │
    │▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░░░░░░░░░░░░░░│  ← 2px bar
    └─────────────────────────────────────────────────────┘
```

- **Position**: bottom edge of the title bar, full width, 2px height
- **Behavior**:
  - Hidden when agent is idle
  - Indeterminate animation when agent is working (sliding gradient, like browser loading bars)
  - Uses `--paw-accent` color (amber)
- **Animation**: a gradient that slides left→right continuously
  ```css
  @keyframes progress-slide {
    0%   { transform: translateX(-100%); }
    100% { transform: translateX(100%); }
  }
  ```
- Subtle — not aggressive. 50% opacity, thin line. The user notices it peripherally.
- Appears on `activityStore.isAgentWorking === true`, disappears on `false`

### 4. Quick Action Buttons

```
    [+]  [⌘K]
```

Two icon buttons in the title bar:

| Button | Icon | Action | Shortcut |
|--------|------|--------|----------|
| New Chat | `Plus` (lucide) | Create new session | `Cmd+N` |
| Search | `Search` (lucide) | Focus session search / open command palette | `Cmd+K` |

**Styling**:
- Ghost buttons: `bg-transparent hover:bg-[--paw-bg-surface]`
- Size: 28px square, icons at 14px
- `rounded-md`
- Tooltip on hover showing action + shortcut

These are the two most frequent actions. Everything else (settings, skills, activity) lives in the sidebar.

### 5. Connection Badge

```
    ◉          ◉          ◉          ◉
    green      amber      red        blue
    connected  working    error      notification
```

- Colored dot (6px circle) at the right end of the title bar (before window buttons on Windows/Linux, after quick actions on macOS)
- Color maps to `connectionStore.status`:
  - 🟢 `--paw-success`: connected, idle
  - 🟠 `--paw-warning`: agent working (also animate-pulse)
  - 🔴 `--paw-error`: disconnected, error
  - 🔵 `--paw-info`: has unread notification
- **Hover tooltip**: shows detail
  - Connected: `"Connected · Claude Sonnet 4.5 · localhost:8888"`
  - Working: `"Working · Reading receipt3.pdf"`
  - Error: `"Disconnected · Reconnecting in 3s..."`
- **Click**: opens a connection detail popover (optional, nice-to-have)

---

## Responsive Behavior

| Window Width | Title Bar Adaptation |
|-------------|---------------------|
| > 900px | Full layout: all zones visible |
| 600–900px | Session title truncates. Model badge hides. Quick actions stay. |
| < 600px | Only: sidebar toggle + 🐾 + connection dot + window buttons. Title in tooltip. |

---

## Window Button Styles

### macOS — Native Traffic Lights

Use Tauri's `titleBarStyle: "Overlay"` — the native traffic lights render automatically. We draw our content around them.

- Position: 12px left, vertically centered
- Our content starts after ~76px to clear the traffic lights
- On hover, traffic lights show their icons (✕, ─, +)
- Standard macOS behavior: green = fullscreen, yellow = minimize, red = close

### Windows — Custom Fluent-Style

Custom-rendered buttons matching Windows 11's Fluent Design:

```svelte
<div class="flex h-full">
  <button class="w-[46px] h-full flex items-center justify-center
                 hover:bg-white/10 transition-colors-fast"
          onclick={minimize}>
    <Minus class="w-3 h-0.5" />
  </button>
  <button class="w-[46px] h-full flex items-center justify-center
                 hover:bg-white/10 transition-colors-fast"
          onclick={toggleMaximize}>
    <Square class="w-2.5 h-2.5" />
  </button>
  <button class="w-[46px] h-full flex items-center justify-center
                 hover:bg-red-500 hover:text-white transition-colors-fast"
          onclick={close}>
    <X class="w-3 h-3" />
  </button>
</div>
```

Key: close button goes red on hover. 46px wide per button (Windows standard). Icons are thin/minimal.

### Linux — Neutral Custom

```svelte
<div class="flex gap-1.5 items-center px-2">
  <button class="w-6 h-6 rounded-full flex items-center justify-center
                 bg-[--paw-bg-surface] hover:bg-[--paw-text-tertiary]/20
                 transition-colors-fast"
          onclick={minimize}>
    <Minus class="w-2.5 h-2.5" />
  </button>
  <button class="w-6 h-6 rounded-full flex items-center justify-center
                 bg-[--paw-bg-surface] hover:bg-[--paw-text-tertiary]/20
                 transition-colors-fast"
          onclick={toggleMaximize}>
    <Square class="w-2.5 h-2.5" />
  </button>
  <button class="w-6 h-6 rounded-full flex items-center justify-center
                 bg-[--paw-bg-surface] hover:bg-red-500/80 hover:text-white
                 transition-colors-fast"
          onclick={close}>
    <X class="w-2.5 h-2.5" />
  </button>
</div>
```

Soft rounded circles, neutral gray, red hint on close hover. Doesn't look like macOS (no color coding) or Windows (not rectangular). Just clean, neutral, PocketPaw.

---

## Implementation Components

```
src/lib/components/titlebar/
├── TitleBar.svelte              # Root: detects OS, renders correct variant
├── TitleBarMacOS.svelte         # macOS layout (traffic lights + vibrancy)
├── TitleBarWindows.svelte       # Windows layout (Mica + Fluent buttons)
├── TitleBarLinux.svelte         # Linux layout (adaptive + neutral buttons)
├── WindowControls.svelte        # Window buttons (close/min/max) — per platform
├── SessionTitle.svelte          # Click-to-edit session title
├── ModelBadge.svelte            # AI model pill
├── QuickActions.svelte          # New Chat + Search buttons
├── ConnectionBadge.svelte       # Status dot with tooltip
└── AgentProgressBar.svelte      # Thin progress bar at bottom edge
```

### TitleBar.svelte (root)

```svelte
<script lang="ts">
  import { platform } from '@tauri-apps/plugin-os';

  const os = $state(platform());  // "macos" | "windows" | "linux"
</script>

{#if os === 'macos'}
  <TitleBarMacOS />
{:else if os === 'windows'}
  <TitleBarWindows />
{:else}
  <TitleBarLinux />
{/if}
```

### Drag Region

All three variants need `data-tauri-drag-region` on the draggable area:

```svelte
<div data-tauri-drag-region class="h-[38px] flex items-center px-4 select-none">
  <!-- title bar content -->
</div>
```

**Important**: interactive elements (buttons, inputs) inside the drag region need `data-tauri-drag-region` excluded — they handle their own click events.

---

## Rust Dependencies

Add to `src-tauri/Cargo.toml`:

```toml
[dependencies]
tauri-plugin-os = "2"              # Platform detection

[target.'cfg(target_os = "windows")'.dependencies]
window-vibrancy = "0.5"           # Mica/Acrylic on Windows

[target.'cfg(target_os = "macos")'.dependencies]
# Vibrancy is handled via Tauri's titleBarStyle + macOSPrivateApi
```

Add to `package.json`:
```bash
bun add @tauri-apps/plugin-os
```

---

## Tauri Configuration Updates

```json
// tauri.conf.json
{
  "app": {
    "windows": [
      {
        "title": "PocketPaw",
        "width": 1000,
        "height": 700,
        "minWidth": 480,
        "minHeight": 400,
        "decorations": false,
        "transparent": true,
        "titleBarStyle": "Overlay"
      }
    ],
    "macOSPrivateApi": true
  }
}
```

`decorations: false` = we draw our own title bar.
`transparent: true` = needed for glass/vibrancy effects.
`titleBarStyle: "Overlay"` = macOS: native traffic lights rendered on top of our content.

---

## Summary

The title bar becomes a **live status dashboard**:

| Feature | What it tells you | Interaction |
|---------|-------------------|-------------|
| Session title | What conversation you're in | Click to rename |
| Model badge | What AI is powering this session | Passive info (future: click to switch) |
| Progress bar | Agent is working right now | Passive (auto-shows/hides) |
| New Chat `+` | Quick action | Click or `Cmd+N` |
| Search `⌘K` | Quick action | Click or `Cmd+K` |
| Connection dot | Backend alive? What state? | Hover for detail |

Three platform skins, one information architecture. Each OS feels native; the content is identically useful everywhere.
