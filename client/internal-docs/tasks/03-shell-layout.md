# Task 03: App Shell & Layout

> The outer frame: custom title bar, sidebar, main content area, routing, and shadcn component installation.

## Goal

Build the app shell that everything else lives inside. Platform-native custom title bar + collapsible sidebar on the left + main content on the right.

## Key Reference Docs

- **`internal-docs/design-system.md`** — colors, typography, glass effects, spacing
- **`internal-docs/titlebar-spec.md`** — platform-specific title bar with session info, agent progress, quick actions, connection badge

## Depends On

- **Task 02** (Stores): `connectionStore`, `sessionStore`

## Step 1: Install Required shadcn-svelte Components

Run these via the CLI (bun):

```bash
bunx shadcn-svelte@latest add button
bunx shadcn-svelte@latest add input
bunx shadcn-svelte@latest add scroll-area
bunx shadcn-svelte@latest add separator
bunx shadcn-svelte@latest add tooltip
bunx shadcn-svelte@latest add avatar
bunx shadcn-svelte@latest add badge
bunx shadcn-svelte@latest add sidebar
bunx shadcn-svelte@latest add sonner
```

These will be placed in `$lib/components/ui/` automatically.

## Step 1.5: Install Fonts + Dependencies

```bash
bun add @fontsource-variable/inter
bun add @fontsource-variable/jetbrains-mono
bun add @tauri-apps/plugin-os
```

Import fonts in `global.css` and update the CSS custom properties to match the design system (see `design-system.md`).

## Step 2: Files to Create

```
src/
├── routes/
│   ├── +layout.svelte         # Root layout: title bar + sidebar + main slot
│   ├── +layout.ts             # SSR disabled (already exists)
│   ├── +page.svelte           # Chat view (default route)
│   ├── onboarding/
│   │   └── +page.svelte       # Onboarding wizard (Task 04)
│   └── settings/
│       └── +page.svelte       # Settings page (Task 08)
├── lib/
│   ├── components/
│   │   ├── AppShell.svelte          # Main layout wrapper
│   │   ├── titlebar/
│   │   │   ├── TitleBar.svelte          # Root: detects OS, renders variant
│   │   │   ├── TitleBarMacOS.svelte     # macOS: vibrancy + traffic lights
│   │   │   ├── TitleBarWindows.svelte   # Windows: Mica + Fluent buttons
│   │   │   ├── TitleBarLinux.svelte     # Linux: clean + DE-adaptive
│   │   │   ├── WindowControls.svelte    # Platform window buttons
│   │   │   ├── SessionTitle.svelte      # Click-to-edit session title
│   │   │   ├── ModelBadge.svelte        # AI model pill
│   │   │   ├── QuickActions.svelte      # New Chat + Search buttons
│   │   │   ├── ConnectionBadge.svelte   # Status dot with tooltip
│   │   │   └── AgentProgressBar.svelte  # Thin progress bar at bottom
│   │   ├── AppSidebar.svelte        # Left sidebar (glass effect)
│   │   ├── SidebarHeader.svelte     # Logo + navigation
│   │   ├── SidebarSessions.svelte   # Session list (placeholder, Task 06)
│   │   ├── SidebarExplore.svelte    # Skill categories (placeholder, Task 07)
│   │   └── SidebarFooter.svelte     # Settings + Activity links
│   └── ...
```

## Step 3: Layout Design

```
┌─────────────────────────────────────────────────────────────────────┐
│ [● ● ●] ≡  🐾 Budget Review · Claude 4.5    [+] [⌘K] [◉] [─ □ ✕] │ ← Custom title bar
│▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░░░░░│ ← Agent progress
├──────────────┬──────────────────────────────────────────────────────┤
│              │                                                      │
│  [Sidebar]   │  [Main Content Area — <slot />]                     │
│  (glass bg)  │                                                      │
│              │  Routes render here:                                 │
│  🐾 Pocket   │  - / → Chat (default)                               │
│    Paw       │  - /onboarding → Wizard                             │
│              │  - /settings → Settings                              │
│  ● New Chat  │                                                      │
│              │                                                      │
│  Sessions    │                                                      │
│  (list)      │                                                      │
│              │                                                      │
│  ──────────  │                                                      │
│              │                                                      │
│  Explore     │                                                      │
│  (skills)    │                                                      │
│              │                                                      │
│  ──────────  │                                                      │
│  ⚙ Settings  │                                                      │
│  📊 Activity │                                                      │
│              │                                                      │
└──────────────┴──────────────────────────────────────────────────────┘
```

The title bar is platform-specific — see `internal-docs/titlebar-spec.md` for full details:
- **macOS**: traffic lights left, vibrancy bg, 38px height
- **Windows**: Fluent buttons right, Mica material, 32px height
- **Linux**: neutral buttons (left/right based on DE), clean bg, 34px height

Title bar features: session name (click-to-edit), model badge, agent progress bar, quick actions (+, ⌘K), connection dot.

## Component Specs

### +layout.svelte (Root)

```svelte
<script>
  import "../styles/global.css";
  import { Toaster } from "$lib/components/ui/sonner";
  import AppShell from "$lib/components/AppShell.svelte";
  const { children } = $props();
</script>

<AppShell>
  {@render children()}
</AppShell>
<Toaster />
```

### AppShell.svelte

- **TitleBar** at the top (platform-specific, see `titlebar-spec.md`)
- Uses shadcn `Sidebar` component for the left panel (glass background)
- Sidebar is collapsible (icon-only mode when collapsed)
- Main area takes remaining width
- Sidebar default width: ~260px
- Collapsed width: ~48px (icon-only)
- Toggle via sidebar toggle button in title bar or `Cmd+B`
- Body area starts below the title bar (content doesn't overlap)

### Title Bar Components

Full spec in `internal-docs/titlebar-spec.md`. Key components:
- **TitleBar.svelte**: detects OS via `@tauri-apps/plugin-os`, renders correct variant
- **SessionTitle.svelte**: click-to-edit session name
- **ModelBadge.svelte**: small pill showing active AI model
- **AgentProgressBar.svelte**: 2px amber bar at title bar bottom edge, shows during agent work
- **QuickActions.svelte**: `+` (new chat) and `⌘K` (search) ghost buttons
- **ConnectionBadge.svelte**: colored status dot with hover tooltip
- **WindowControls.svelte**: platform-specific close/min/max buttons

### AppSidebar.svelte

Glass background (`backdrop-filter: blur(16px)`, semi-transparent). Sections (top to bottom):
1. **SidebarHeader**: PocketPaw logo (🐾) + app name
2. **New Chat button**: prominent button to create new session
3. **SidebarSessions**: scrollable list of sessions grouped by date (Today, Yesterday, This Week, Older)
4. **Separator**
5. **SidebarExplore**: skill category links (Quick Tasks, Analyze Data, Write Content, Dev Tools, Files)
6. **Separator**
7. **SidebarFooter**: Settings link + Activity link

## Routing

SvelteKit file-based routing. All routes are SPA (SSR disabled).

| Path          | Component              | Purpose              |
|---------------|------------------------|----------------------|
| `/`           | `+page.svelte`         | Chat (main view)     |
| `/onboarding` | `onboarding/+page.svelte` | First-run wizard  |
| `/settings`   | `settings/+page.svelte`   | Settings panel     |

The layout should check if the user has completed onboarding (e.g., token exists). If not, redirect to `/onboarding`.

## Styling Notes

- Use Tailwind CSS 4 utility classes throughout
- Use the CSS custom properties defined in `global.css` for theming (--primary, --sidebar, etc.)
- Dark mode: the theme tokens in global.css already support `.dark` class
- Sidebar uses `--sidebar-*` color tokens from the theme
- All shadcn components will automatically use the theme tokens

## Styling

- Apply design system from `internal-docs/design-system.md`
- Update `global.css` with PocketPaw tokens (warm amber accent, warm gray neutrals)
- Import Inter and JetBrains Mono fonts
- Add glass utilities (`glass`, `glass-subtle`, `glass-strong`)
- Add custom animation utilities (pulse-dot, progress-slide, cursor-blink)
- Add thin scrollbar styles
- Sidebar uses `glass` utility (translucent with blur)

## Tauri Config Updates

```json
{
  "app": {
    "windows": [{
      "title": "PocketPaw",
      "width": 1000,
      "height": 700,
      "minWidth": 480,
      "minHeight": 400,
      "decorations": false,
      "transparent": true,
      "titleBarStyle": "Overlay"
    }],
    "macOSPrivateApi": true
  }
}
```

Add to Cargo.toml:
```toml
tauri-plugin-os = "2"
# Windows only:
[target.'cfg(target_os = "windows")'.dependencies]
window-vibrancy = "0.5"
```

## Acceptance Criteria

- [ ] shadcn-svelte components installed (button, input, scroll-area, separator, tooltip, avatar, badge, sidebar, sonner)
- [ ] Design system applied: warm amber accent, warm gray neutrals, Inter font, glass utilities
- [ ] Custom title bar renders correctly on current platform
- [ ] Title bar shows: session name, model badge, progress bar, quick actions, connection dot
- [ ] Window buttons work: close, minimize, maximize (platform-appropriate style)
- [ ] Close button minimizes to tray (doesn't quit) — or at minimum, hides window
- [ ] Title bar is draggable (window can be moved by dragging)
- [ ] Agent progress bar appears/hides based on `activityStore.isAgentWorking`
- [ ] App shell renders with collapsible sidebar + main content area
- [ ] Sidebar has glass/blur effect (translucent background)
- [ ] Sidebar sections are present (header, sessions placeholder, explore placeholder, footer)
- [ ] Routes work: `/`, `/onboarding`, `/settings`
- [ ] Sidebar collapses to icon-only mode
- [ ] Toast notifications work (sonner)
- [ ] Layout is responsive — sidebar collapses on narrow windows, title bar truncates

## Notes

- Session list and skill browser are placeholders in this task. They'll be fully built in Tasks 06 and 07.
- The chat view (`+page.svelte`) is also a placeholder here — Task 05 builds it out.
- Focus on getting the structural layout + title bar + design system right. Content comes later.
- The title bar is the first thing users see — get the platform feel right.
- Test title bar drag, window controls, and glass effects on your platform before moving on.
