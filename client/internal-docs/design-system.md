# PocketPaw Design System

> Not a macOS clone. Not generic Material. A PocketPaw identity — modern, warm, glassy, precise.
>
> Inspired by: Linear, Raycast, Arc Browser, Apple HIG principles.
> Runs on: macOS, Windows, Linux — feels native-quality on all three.

---

## Design Philosophy

1. **Depth through translucency.** Layers are visible through each other. Sidebar bleeds into the background. Popovers float with frosted glass. This creates spatial hierarchy without heavy borders.

2. **Warmth over cold.** Most developer tools are cold blue-gray. PocketPaw is an agent you *talk* to — it should feel approachable. Warm neutrals, a signature accent color, soft shadows.

3. **Precision without rigidity.** Tight spacing, consistent alignment, small text that's still readable. But rounded corners and soft edges keep it from feeling sterile.

4. **Dark mode is the default.** Most users will run this in the system tray alongside their work. Dark mode blends in. Light mode is available but dark is the primary design surface.

5. **Platform-respectful.** Window controls follow the OS. Scrollbars follow the OS. Keyboard shortcuts adapt. The *content* looks identical; the *chrome* respects where you are.

---

## Color System

### Brand Colors

PocketPaw's identity color: a warm amber-orange. The paw print, the accent buttons, the active states — all use this.

```
Brand Amber:     oklch(0.78 0.15 65)     → #E5A04D-ish — warm, friendly, distinctive
Brand Amber dim: oklch(0.65 0.12 65)     → muted variant for dark mode accents
```

This replaces the default slate-blue primary. Slate is the neutral base; amber is the brand accent.

### Neutral Palette (Dark Mode — Primary Surface)

Built on a warm gray, not pure cool slate. Slight warm undertone so it doesn't feel clinical.

```css
/* Dark mode (default) */
--paw-bg-base:      oklch(0.13 0.015 260);    /* App background — near black, warm */
--paw-bg-raised:    oklch(0.17 0.015 260);    /* Cards, sidebar bg */
--paw-bg-overlay:   oklch(0.21 0.015 260);    /* Popovers, dialogs */
--paw-bg-surface:   oklch(0.25 0.015 260);    /* Hover states, input backgrounds */

--paw-border-subtle:   oklch(1 0 0 / 6%);    /* Very faint borders */
--paw-border-default:  oklch(1 0 0 / 10%);   /* Standard borders */
--paw-border-strong:   oklch(1 0 0 / 15%);   /* Emphasized borders */

--paw-text-primary:    oklch(0.95 0.005 260); /* Main text — almost white, warm */
--paw-text-secondary:  oklch(0.65 0.015 260); /* Muted text, labels */
--paw-text-tertiary:   oklch(0.45 0.015 260); /* Placeholders, disabled */
```

```css
/* Light mode */
--paw-bg-base:      oklch(0.97 0.005 260);    /* Off-white, warm */
--paw-bg-raised:    oklch(1.0 0 0);            /* Pure white cards */
--paw-bg-overlay:   oklch(1.0 0 0);            /* Popovers */
--paw-bg-surface:   oklch(0.95 0.005 260);     /* Hover, inputs */

--paw-border-subtle:   oklch(0 0 0 / 5%);
--paw-border-default:  oklch(0 0 0 / 10%);
--paw-border-strong:   oklch(0 0 0 / 15%);

--paw-text-primary:    oklch(0.15 0.015 260);
--paw-text-secondary:  oklch(0.45 0.015 260);
--paw-text-tertiary:   oklch(0.65 0.015 260);
```

### Semantic Colors

```css
--paw-accent:         oklch(0.78 0.15 65);    /* Brand amber — buttons, links, active */
--paw-accent-hover:   oklch(0.72 0.15 65);    /* Darker on hover */
--paw-accent-subtle:  oklch(0.78 0.15 65 / 15%); /* Tinted backgrounds */

--paw-success:        oklch(0.72 0.17 155);   /* Green — connected, success */
--paw-warning:        oklch(0.80 0.16 80);    /* Yellow-orange — working, caution */
--paw-error:          oklch(0.65 0.22 25);    /* Red — errors, destructive */
--paw-info:           oklch(0.70 0.12 250);   /* Blue — notifications, info */
```

### Glass Colors

```css
--paw-glass-bg:     oklch(0.17 0.015 260 / 70%);   /* Glass panel background */
--paw-glass-border: oklch(1 0 0 / 8%);              /* Glass border */
--paw-glass-blur:   16px;                            /* Blur radius */
```

---

## Typography

### Font Stack

**Primary**: Inter — the cross-platform standard. Geometric, readable at small sizes, variable font. Available on all platforms via web font or system fallback.

```css
--paw-font-sans: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
--paw-font-mono: 'JetBrains Mono', 'SF Mono', 'Cascadia Code', 'Fira Code', ui-monospace, monospace;
```

Install Inter as a local asset (don't rely on Google Fonts — Tauri is offline):
```bash
bun add @fontsource-variable/inter
bun add @fontsource-variable/jetbrains-mono
```

Import in `global.css`:
```css
@import "@fontsource-variable/inter";
@import "@fontsource-variable/jetbrains-mono";
```

### Type Scale

Tight, compact scale. macOS-level density. Everything is slightly smaller than web defaults.

```css
--paw-text-xs:    0.6875rem;   /* 11px — badges, captions */
--paw-text-sm:    0.75rem;     /* 12px — secondary text, sidebar items */
--paw-text-base:  0.8125rem;   /* 13px — body text, chat messages */
--paw-text-md:    0.875rem;    /* 14px — input fields, buttons */
--paw-text-lg:    1rem;        /* 16px — section headers */
--paw-text-xl:    1.25rem;     /* 20px — page titles */
--paw-text-2xl:   1.5rem;      /* 24px — onboarding headers */

--paw-leading-tight:  1.3;    /* Headings */
--paw-leading-normal: 1.5;    /* Body text */
--paw-leading-relaxed: 1.65;  /* Long-form content, chat messages */
```

### Font Weights

```css
--paw-font-normal:   400;
--paw-font-medium:   500;    /* Most UI text */
--paw-font-semibold: 600;    /* Headers, active states */
```

Use `font-medium` (500) for most UI elements — not `font-normal`. This gives that Apple-like crispness. Use `font-normal` (400) for long-form text (chat messages, descriptions).

---

## Spacing

8px base grid, but allow 4px increments for fine-tuning.

```css
--paw-space-0:   0;
--paw-space-0.5: 0.125rem;   /* 2px */
--paw-space-1:   0.25rem;    /* 4px */
--paw-space-1.5: 0.375rem;   /* 6px */
--paw-space-2:   0.5rem;     /* 8px — base unit */
--paw-space-3:   0.75rem;    /* 12px */
--paw-space-4:   1rem;       /* 16px */
--paw-space-5:   1.25rem;    /* 20px */
--paw-space-6:   1.5rem;     /* 24px */
--paw-space-8:   2rem;       /* 32px */
--paw-space-10:  2.5rem;     /* 40px */
--paw-space-12:  3rem;       /* 48px */
```

**Sidebar width**: 260px expanded, 48px collapsed.
**Content max-width**: 720px for chat messages (like iMessage — don't let messages stretch full width).
**Input height**: 36px default, 40px for primary inputs.

---

## Border Radius

Generous but not cartoonish. macOS uses ~10px for windows, ~8px for buttons, ~6px for inputs.

```css
--radius: 0.5rem;          /* 8px — base (reduce from current 10px) */
--radius-sm: 0.25rem;      /* 4px — badges, small chips */
--radius-md: 0.375rem;     /* 6px — inputs, buttons */
--radius-lg: 0.5rem;       /* 8px — cards */
--radius-xl: 0.75rem;      /* 12px — dialogs, sheets */
--radius-2xl: 1rem;        /* 16px — large panels, onboarding cards */
--radius-full: 9999px;     /* Pills, avatars */
```

---

## Glass & Blur Effects

The signature visual. Translucent panels with backdrop blur.

### Glass Utilities (Tailwind)

Define as Tailwind utilities in `global.css`:

```css
@utility glass {
  background: var(--paw-glass-bg);
  backdrop-filter: blur(var(--paw-glass-blur));
  -webkit-backdrop-filter: blur(var(--paw-glass-blur));
  border: 1px solid var(--paw-glass-border);
}

@utility glass-subtle {
  background: oklch(0.17 0.015 260 / 40%);
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  border: 1px solid oklch(1 0 0 / 5%);
}

@utility glass-strong {
  background: oklch(0.17 0.015 260 / 85%);
  backdrop-filter: blur(24px);
  -webkit-backdrop-filter: blur(24px);
  border: 1px solid oklch(1 0 0 / 10%);
}
```

### Where to Use Glass

| Element | Glass Level | Why |
|---------|-------------|-----|
| Sidebar | `glass` (70% opacity, 16px blur) | Lets the background bleed through, creates depth |
| Popovers/Dropdowns | `glass-strong` (85%, 24px blur) | Readable but still feels layered |
| Quick Ask bar | `glass` (70%, 16px blur) | Floating, Spotlight-like |
| Side panel | `glass-subtle` (40%, 8px blur) | Subtle, doesn't distract from the app behind it |
| Dialog overlay | solid `--paw-bg-overlay` | Dialogs need full readability |
| Chat area | solid `--paw-bg-base` | No glass on the main reading surface |

### Light Mode Glass

```css
/* Light mode overrides */
.light {
  --paw-glass-bg: oklch(1 0 0 / 70%);
  --paw-glass-border: oklch(0 0 0 / 8%);
}
```

### Performance Note

`backdrop-filter` is GPU-accelerated in all Tauri webviews (WebKit on macOS, WebView2 on Windows, WebKitGTK on Linux). It's safe to use. But avoid animating blur radius — keep it static.

---

## Shadows & Elevation

Soft, layered shadows. Not flat, not Material's harsh elevation.

```css
--paw-shadow-xs:  0 1px 2px oklch(0 0 0 / 5%);
--paw-shadow-sm:  0 1px 3px oklch(0 0 0 / 8%), 0 1px 2px oklch(0 0 0 / 4%);
--paw-shadow-md:  0 4px 8px oklch(0 0 0 / 10%), 0 2px 4px oklch(0 0 0 / 5%);
--paw-shadow-lg:  0 8px 24px oklch(0 0 0 / 12%), 0 4px 8px oklch(0 0 0 / 6%);
--paw-shadow-xl:  0 16px 48px oklch(0 0 0 / 16%), 0 8px 16px oklch(0 0 0 / 8%);

/* Glow — for accent elements (active buttons, focus rings) */
--paw-glow-accent: 0 0 12px oklch(0.78 0.15 65 / 25%);
```

In dark mode, shadows are barely visible (dark on dark). Instead, use **border highlights** (1px white at low opacity) on the top edge of elevated elements to suggest light hitting the surface from above:

```css
@utility elevated {
  border-top: 1px solid oklch(1 0 0 / 5%);
  box-shadow: var(--paw-shadow-md);
}
```

---

## Animations & Transitions

Smooth, fast, purposeful. No bouncing, no overshooting.

### Timing

```css
--paw-duration-fast:   100ms;   /* Hover states, color changes */
--paw-duration-normal: 200ms;   /* Panel open/close, transitions */
--paw-duration-slow:   300ms;   /* Page transitions, complex animations */

--paw-ease-default:  cubic-bezier(0.25, 0.1, 0.25, 1.0);   /* Smooth, natural */
--paw-ease-out:      cubic-bezier(0.0, 0.0, 0.2, 1.0);     /* Decelerate (enter) */
--paw-ease-in:       cubic-bezier(0.4, 0.0, 1.0, 1.0);     /* Accelerate (exit) */
--paw-ease-spring:   cubic-bezier(0.34, 1.56, 0.64, 1.0);  /* Slight overshoot (dialogs, popovers) */
```

### What Gets Animated

| Element | Animation | Duration | Easing |
|---------|-----------|----------|--------|
| Hover states | Background color, opacity | `fast` | `default` |
| Sidebar collapse | Width, content opacity | `normal` | `ease-out` |
| Dialog open | Scale(0.95→1) + opacity | `normal` | `spring` |
| Dialog close | Scale(1→0.95) + opacity | `fast` | `ease-in` |
| Sheet slide | translateX | `slow` | `ease-out` |
| Tooltip | Opacity + translateY(4px→0) | `fast` | `ease-out` |
| Dropdown open | Opacity + translateY(-4px→0) + scale(0.98→1) | `normal` | `spring` |
| Status dot pulse | Scale + opacity (loop) | `1.5s` | `ease-in-out` |
| Streaming cursor | Opacity blink | `1s` | `steps(2)` |

### What Does NOT Get Animated

- Scrolling (let the OS handle scroll physics)
- Layout reflows (no animating width/height on content that changes frequently)
- Chat message appearance (messages just appear — no slide-in per message)
- Text rendering

### Tailwind Utilities

```css
@utility transition-colors-fast {
  transition: color var(--paw-duration-fast) var(--paw-ease-default),
              background-color var(--paw-duration-fast) var(--paw-ease-default),
              border-color var(--paw-duration-fast) var(--paw-ease-default);
}

@utility transition-transform-normal {
  transition: transform var(--paw-duration-normal) var(--paw-ease-out);
}

@utility animate-pulse-dot {
  animation: pulse-dot 1.5s ease-in-out infinite;
}

@keyframes pulse-dot {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.6; transform: scale(1.15); }
}

@keyframes cursor-blink {
  0%, 100% { opacity: 1; }
  50% { opacity: 0; }
}
```

---

## Window Chrome

### Custom Title Bar

Tauri allows `decorations: false` for a custom title bar. But the approach differs by platform:

**macOS**: Traffic light buttons (close/minimize/fullscreen) stay on the **left**. Title centered. Draggable area is the full title bar.

**Windows**: Close/minimize/maximize buttons on the **right** (standard Windows layout). Title left-aligned.

**Linux**: Follow the desktop environment's preference (usually right, but GNOME uses left). Default to right.

Implementation: detect platform via `@tauri-apps/api/os` and render the appropriate button layout.

```
macOS:
┌─ ● ● ●  ─────────── PocketPaw ──────────────────────────┐

Windows / Linux:
┌──── 🐾 PocketPaw ───────────────────────── ─  □  ✕ ──┐
```

### Title Bar Component

```svelte
<!-- TitleBar.svelte -->
<!-- Height: 38px (macOS) or 32px (Windows/Linux) -->
<!-- Background: transparent (blends with sidebar/content) -->
<!-- Drag region: data-tauri-drag-region attribute -->
<!-- Window controls: conditional by platform -->
```

The title bar background should be transparent so the sidebar glass effect extends to the top edge — creating that unified toolbar look macOS apps have.

---

## Component Patterns

### Buttons

```
Primary:   bg-[--paw-accent] text-white, rounded-md, h-8 px-3, font-medium text-[13px]
Secondary: bg-[--paw-bg-surface] text-[--paw-text-primary], border border-[--paw-border-default]
Ghost:     bg-transparent hover:bg-[--paw-bg-surface], no border
Danger:    bg-[--paw-error] text-white
```

Small buttons (h-7, text-xs) for inline actions. Default buttons (h-8, text-sm).

### Inputs

```
bg-[--paw-bg-surface], border border-[--paw-border-default], rounded-md
h-9, px-3, text-[13px]
Focus: ring-2 ring-[--paw-accent] ring-offset-0, border-[--paw-accent]
Placeholder: text-[--paw-text-tertiary]
```

### Cards

```
bg-[--paw-bg-raised], border border-[--paw-border-subtle], rounded-lg
p-4, shadow-none (or shadow-xs in light mode)
Hover: border-[--paw-border-default] (slightly more visible border)
```

### Chat Bubbles

Not traditional "bubbles." Clean, minimal, full-width messages like Linear's comments or iMessage on desktop:

```
User message:      bg-[--paw-accent-subtle], rounded-xl, p-3, max-w-[85%], ml-auto
Assistant message: bg-transparent, text only, full width, with 🐾 avatar on left
```

User messages get a subtle amber tint. Assistant messages are plain text with just a paw icon — no background, no bubble. This is intentional: the assistant's response should feel like content, not a chat bubble.

### Sidebar Items

```
Default:   bg-transparent, text-[--paw-text-secondary], px-2 py-1.5, rounded-md
Hover:     bg-[--paw-bg-surface]
Active:    bg-[--paw-accent-subtle], text-[--paw-accent], font-medium
```

Height: 32px per item. Compact but clickable.

### Status Dots

```
Connected (idle):    bg-[--paw-success], w-2 h-2, rounded-full
Working:             bg-[--paw-warning], w-2 h-2, rounded-full, animate-pulse-dot
Error:               bg-[--paw-error], w-2 h-2, rounded-full
Notification:        bg-[--paw-info], w-2 h-2, rounded-full
```

---

## Dark / Light Mode

**Dark is default.** The toggle should be in Settings > About, not prominent.

Switching mechanism:
- `.dark` class on `<html>` element
- Respects `prefers-color-scheme` as default
- User override saved to localStorage

All colors are defined as CSS custom properties, so switching is instant — just swap the class.

---

## Iconography

**Lucide** icons throughout (already installed as `@lucide/svelte`). Use at these sizes:

```
Sidebar icons:    16px (w-4 h-4), stroke-width 1.75
Inline icons:     14px (w-3.5 h-3.5), stroke-width 2
Button icons:     16px (w-4 h-4), stroke-width 1.75
Header icons:     20px (w-5 h-5), stroke-width 1.5
Empty states:     48px (w-12 h-12), stroke-width 1
```

Lower stroke-width at larger sizes, higher at smaller sizes. This keeps the visual weight consistent.

PocketPaw paw icon (🐾): use as emoji in text, or as a custom SVG icon for the sidebar header and system tray.

---

## Scrollbars

Use thin, auto-hiding scrollbars that feel native:

```css
/* Thin scrollbar for all platforms */
::-webkit-scrollbar {
  width: 6px;
  height: 6px;
}

::-webkit-scrollbar-track {
  background: transparent;
}

::-webkit-scrollbar-thumb {
  background: oklch(1 0 0 / 15%);
  border-radius: 3px;
}

::-webkit-scrollbar-thumb:hover {
  background: oklch(1 0 0 / 25%);
}

/* Light mode */
.light ::-webkit-scrollbar-thumb {
  background: oklch(0 0 0 / 15%);
}
.light ::-webkit-scrollbar-thumb:hover {
  background: oklch(0 0 0 / 25%);
}
```

---

## Focus & Accessibility

- Focus rings: 2px ring in `--paw-accent`, no offset (`ring-offset-0`)
- All interactive elements must be keyboard-navigable
- Minimum contrast ratio: 4.5:1 for body text, 3:1 for large text
- Use `aria-label` on icon-only buttons
- Respect `prefers-reduced-motion`: disable animations when set

```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    transition-duration: 0.01ms !important;
  }
}
```

---

## File Structure for Design System

```
src/styles/
├── global.css              # UPDATE: replace with design system tokens
├── fonts.css               # Font imports (Inter, JetBrains Mono)
├── scrollbar.css           # Custom scrollbar styles
└── utilities.css           # Custom Tailwind utilities (glass, elevated, etc.)
```

All imported into `global.css` via `@import`.

---

## Implementation Notes

### Updating global.css

The current `global.css` uses shadcn's default slate tokens. These need to be replaced with the PocketPaw design system tokens above. The `@theme inline` block needs updating to map our custom properties to Tailwind's color system.

Keep shadcn's variable naming convention (`--background`, `--foreground`, `--primary`, etc.) so that shadcn components continue to work. Map our PocketPaw tokens into those slots:

```css
:root {
  /* shadcn compatibility layer */
  --primary: var(--paw-accent);
  --primary-foreground: oklch(1 0 0);
  --background: var(--paw-bg-base);
  --foreground: var(--paw-text-primary);
  --card: var(--paw-bg-raised);
  --card-foreground: var(--paw-text-primary);
  --sidebar: var(--paw-glass-bg);  /* Glass sidebar */
  /* etc. */
}
```

This way shadcn components automatically pick up PocketPaw's design system without customizing each component individually.

### Tauri Window Config

For the glass sidebar to work, the Tauri window needs transparency:

```json
// tauri.conf.json
{
  "app": {
    "windows": [
      {
        "transparent": true,
        "decorations": false
      }
    ]
  }
}
```

On macOS, also enable `"macOSPrivateApi": true` for vibrancy effects. On Linux, transparency depends on the compositor (works on most modern desktops).

---

## Summary: The PocketPaw Look

- **Dark by default.** Warm dark grays, not cold slate.
- **Amber accent.** Warm, friendly, distinctive. Not the typical blue.
- **Glass sidebar.** Translucent with backdrop blur. Content visible through it.
- **Precise typography.** Inter at 13px base. Compact but readable. Medium weight for UI.
- **Soft everything.** Rounded corners, soft shadows, smooth transitions.
- **Clean chat.** User messages get amber tint. Assistant text is plain — no bubble.
- **Platform-aware chrome.** Window buttons where the OS expects them. Custom title bar that blends with the sidebar.

This is PocketPaw — not macOS, not Windows, not Linux. Its own thing that feels right everywhere.
