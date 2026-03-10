# Task 07: Skill Cards

> The "Canva moment" — pre-built workflows that non-tech users browse and click.

## Goal

Build the skill browser in the sidebar's Explore section, and the skill interaction flow (click card → fill form → agent executes).

## Depends On

- **Task 05** (Chat Interface): skill execution results appear in chat
- **Task 02** (Stores): `skillStore`

## Install Additional shadcn Components

```bash
bunx shadcn-svelte@latest add sheet
bunx shadcn-svelte@latest add select
bunx shadcn-svelte@latest add checkbox
bunx shadcn-svelte@latest add tabs
```

## Files to Create / Modify

```
src/lib/components/
├── SidebarExplore.svelte       # UPDATE: replace placeholder
├── skills/
│   ├── SkillBrowser.svelte     # Full-page skill browser (opens as sheet/panel)
│   ├── SkillCard.svelte        # Single skill card
│   ├── SkillCategory.svelte    # Category header + cards grid
│   ├── SkillForm.svelte        # Dynamic form rendered from skill inputs
│   └── SkillSearch.svelte      # Search bar for marketplace
```

## Sidebar Explore Section

Compact category links in the sidebar:

```
┌──────────────────┐
│  Explore         │
│                  │
│  ⚡ Quick Tasks  │  ← Click opens SkillBrowser filtered to category
│  📊 Analyze Data │
│  📝 Write Content│
│  🔧 Dev Tools   │
│  📁 Files       │
│                  │
└──────────────────┘
```

Clicking a category opens the `SkillBrowser` as a `Sheet` (slide-over panel) from the right, filtered to that category.

## SkillBrowser.svelte

Full-width panel (shadcn `Sheet` from right side) showing all skills:

```
┌──────────────────────────────────────────────────────────┐
│  Explore Skills                              🔍 Search   │
│                                                          │
│  ⚡ Quick Tasks                                          │
│  ┌────────────────┐ ┌────────────────┐ ┌──────────────┐ │
│  │ 📝 Summarize   │ │ 🌐 Translate   │ │ 📧 Draft     │ │
│  │ Drop a file,   │ │ Any text, any  │ │ Email        │ │
│  │ get key points.│ │ language pair. │ │              │ │
│  │ [Use This →]   │ │ [Use This →]   │ │ [Use This →] │ │
│  └────────────────┘ └────────────────┘ └──────────────┘ │
│                                                          │
│  📊 Analyze Data                                         │
│  ┌────────────────┐ ┌────────────────┐                  │
│  │ 🧾 Expense     │ │ 📈 CSV         │                  │
│  │    Report      │ │    Analyzer    │                  │
│  │ [Use This →]   │ │ [Use This →]   │                  │
│  └────────────────┘ └────────────────┘                  │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

## SkillCard.svelte

Each card shows:
- Icon (emoji or lucide icon)
- Name (bold)
- Short description (1-2 lines)
- "Use This →" button

Size: fixed width card in a responsive grid (2-3 columns depending on panel width).

Clicking "Use This →" opens `SkillForm` as a dialog.

## SkillForm.svelte

A dialog that renders a form based on the skill's input definition.

For now, since the backend skills system returns invocable skills (not YAML-defined forms), the form is simplified:

```
┌──────────────────────────────────────────┐
│  📝 Summarize                            │
│                                          │
│  What would you like to summarize?       │
│  ┌──────────────────────────────────┐    │
│  │                                  │    │
│  │     📎 Drop files or type here  │    │
│  │                                  │    │
│  └──────────────────────────────────┘    │
│                                          │
│  Additional instructions: (optional)     │
│  ┌──────────────────────────────────┐    │
│  │ e.g., "Focus on action items"   │    │
│  └──────────────────────────────────┘    │
│                                          │
│       [ Cancel ]    [ Go → ]             │
│                                          │
└──────────────────────────────────────────┘
```

Clicking "Go" sends a message to the chat (via `chatStore.sendMessage`) with the skill name as context, then closes the dialog and navigates to the chat view.

## Skill Categories

Define default categories with icons. Map skills from the backend to categories based on their metadata:

```typescript
const SKILL_CATEGORIES = [
  { id: "quick", name: "Quick Tasks", icon: "Zap", skills: ["summarize", "translate", "draft-email"] },
  { id: "analyze", name: "Analyze Data", icon: "BarChart3", skills: ["expense-report", "csv-analyzer", "compare"] },
  { id: "write", name: "Write Content", icon: "PenTool", skills: ["blog-post", "social-media", "report"] },
  { id: "dev", name: "Dev Tools", icon: "Wrench", skills: ["debug", "explain-code", "code-review"] },
  { id: "files", name: "File Management", icon: "FolderOpen", skills: ["organize", "convert"] },
];
```

Skills that don't match a known category go into "Other".

## Marketplace Search

`SkillSearch.svelte` at the top of the browser:
- Search the backend skill marketplace: `GET /skills/search?q=...`
- Show results as cards with "Install" button
- Installing a skill: `POST /skills/install`

## Acceptance Criteria

- [ ] Sidebar shows skill category links
- [ ] Clicking a category opens the skill browser (Sheet panel)
- [ ] Skill cards render in a grid with icon, name, description
- [ ] "Use This" opens a form dialog
- [ ] Form submission sends a message to chat
- [ ] Marketplace search works
- [ ] Install/remove skills works
- [ ] Skill browser is searchable
- [ ] Responsive grid (2-3 columns)

## Notes

- The "Canva moment" only works if the skill cards feel effortless. No configuration overload.
- Start with hardcoded "built-in" skill definitions for the UI (summarize, translate, email draft, etc.) and supplement with skills from the API.
- The skill form is simple for v1: text input + optional file attachment. Richer forms (dropdowns, checkboxes, multi-file) come later when YAML skill definitions are supported.
