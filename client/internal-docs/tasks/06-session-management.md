# Task 06: Session Management

> Session list in the sidebar: create, switch, delete, rename, search.

## Goal

Build the session list UI in the sidebar and wire it to the session store.

## Depends On

- **Task 05** (Chat Interface): `ChatPanel` to display messages when switching sessions
- **Task 02** (Stores): `sessionStore`, `chatStore`

## Files to Create / Modify

```
src/lib/components/
├── SidebarSessions.svelte      # UPDATE: replace placeholder with full implementation
├── SessionItem.svelte          # Single session row
├── SessionSearch.svelte        # Search input for sessions
└── SessionContextMenu.svelte   # Right-click menu (rename, delete, export)
```

## Layout (in Sidebar)

```
┌──────────────────┐
│  🔍 Search...    │  ← SessionSearch
│                  │
│  ● New Chat      │  ← Button (already in sidebar)
│                  │
│  Today           │  ← Date group header
│  ○ Budget review │  ← SessionItem (active = highlighted)
│  ○ Recipe finder │
│                  │
│  Yesterday       │
│  ○ Travel plans  │
│                  │
│  This Week       │
│  ○ Code review   │
│  ○ Email draft   │
│                  │
└──────────────────┘
```

## Component Specs

### SidebarSessions.svelte

- On mount: call `sessionStore.loadSessions()`
- Group sessions by date: Today, Yesterday, This Week, This Month, Older
- Render `SessionItem` for each session
- Show `SessionSearch` at the top
- When search is active, show flat filtered results instead of grouped

### SessionItem.svelte

- Shows: session title (truncated), relative timestamp ("2h ago")
- Active session: highlighted background
- Click: switch session (`sessionStore.switchSession(id)`)
- Right-click: show `SessionContextMenu`
- Double-click: inline rename (contenteditable or input)
- Subtle message count badge

### SessionSearch.svelte

- Small search input at the top of session list
- Debounced search (300ms) via `sessionStore.searchSessions(query)`
- Clear button (✕) to reset
- Show result count when searching
- `Cmd+K` keyboard shortcut focuses the search (from AppShell level)

### SessionContextMenu.svelte

Right-click context menu using shadcn `DropdownMenu`:
- **Rename** — enters inline edit mode
- **Delete** — confirmation dialog, then `sessionStore.deleteSession(id)`
- **Export as Markdown** — calls REST API, triggers download
- **Export as JSON** — calls REST API, triggers download

## Date Grouping Logic

```typescript
function groupSessionsByDate(sessions: Session[]): Map<string, Session[]> {
  const now = new Date();
  const today = startOfDay(now);
  const yesterday = subDays(today, 1);
  const thisWeek = subDays(today, 7);
  const thisMonth = subDays(today, 30);

  // Group into: Today, Yesterday, This Week, This Month, Older
}
```

Keep it simple — don't add a date library. Use native `Date` comparisons.

## Acceptance Criteria

- [ ] Session list loads on app start
- [ ] Sessions grouped by date (Today, Yesterday, This Week, etc.)
- [ ] Clicking a session switches the chat view
- [ ] Active session is visually highlighted
- [ ] "New Chat" creates a new session and switches to it
- [ ] Right-click context menu with rename/delete/export
- [ ] Inline rename works
- [ ] Delete shows confirmation, removes session
- [ ] Search filters sessions with debounce
- [ ] `Cmd+K` focuses search

## Notes

- Keep the session list performant — users might have 100+ sessions.
- The session list should update when a new message comes in (re-sort by last activity).
- When the active session is deleted, auto-switch to the most recent remaining session.
