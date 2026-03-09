# Task 09: Activity Panel

> See what the agent is doing: tool calls, thinking, errors. Progressive disclosure.

## Goal

Build an expandable activity feed that shows the agent's internal execution (tool calls, thinking, results). Collapsed by default, expandable for power users.

## Depends On

- **Task 05** (Chat Interface): activity appears alongside/below streaming messages
- **Task 02** (Stores): `activityStore`

## Files to Create

```
src/lib/components/activity/
├── ActivityPanel.svelte        # Expandable panel container
├── ActivityEntry.svelte        # Single activity line
├── ActivityCollapsed.svelte    # Collapsed status bar (one-liner)
└── ActivityExpanded.svelte     # Expanded full log
```

## How It Appears

The activity panel is embedded in the chat view, appearing during agent execution.

### Collapsed (default — inline in chat):

```
┌─────────────────────────────────────────┐
│  ● Working...  Reading receipt3.pdf     │
└─────────────────────────────────────────┘
```

- Shown below the streaming message
- Single line: status dot + latest activity description
- Click to expand

### Expanded (click to reveal):

```
┌─────────────────────────────────────────┐
│  Activity                          ▲    │
│                                         │
│  10:32:01  ✓ Tool: OCR → receipt1.jpg   │
│  10:32:03  ✓ Result: "Uber $45.00"      │
│  10:32:04  ✓ Tool: OCR → receipt2.png   │
│  10:32:06  ✓ Result: "AWS $40.00"       │
│  10:32:07  ● Tool: OCR → receipt3.pdf   │
│  10:32:09  ● Processing...              │
│                                         │
│  Guardian AI: ✓ All actions approved    │
│  Model: claude-sonnet-4-5               │
│  Tokens: 2,847 used                     │
└─────────────────────────────────────────┘
```

## Component Specs

### ActivityPanel.svelte

Props: none (reads from `activityStore`)

- When `activityStore.isAgentWorking` is true: show the panel
- When agent is done: panel remains visible for a few seconds, then fades
- Toggle between collapsed and expanded
- Remember expansion state (localStorage)

### ActivityCollapsed.svelte

- Status dot (● animated pulse when working)
- Latest entry text (truncated to one line)
- Click handler to expand

### ActivityExpanded.svelte

- Scrollable list of `ActivityEntry` items
- Auto-scroll to bottom
- Summary footer: Guardian AI status, model name, token usage
- Collapse button (▲)

### ActivityEntry.svelte

Each entry shows:
- Timestamp (HH:MM:SS)
- Status icon:
  - ✓ (green) — completed
  - ● (orange, pulsing) — in progress
  - ✕ (red) — error
- Type label: "Tool:", "Thinking:", "Result:", "Error:"
- Content (truncated, expandable on click for long outputs)

Entry types from WebSocket:
- `tool_start` → "🔧 tool_name(input_summary)"
- `tool_result` → "✓ output_summary" (truncated to ~100 chars)
- `thinking` → "💭 thinking_content" (show first line)
- `error` → "❌ error_message"
- `status` → "ℹ status_message"

## Sidebar Activity Link

The sidebar footer has an "Activity" link. Clicking it opens a full activity log in the main content area (or a Sheet panel) showing all activity from the current session, not just the current message.

## Acceptance Criteria

- [ ] Activity panel appears during agent execution
- [ ] Collapsed view shows latest activity as one-liner
- [ ] Expanded view shows all activity entries with timestamps
- [ ] Entries have correct status icons (✓, ●, ✕)
- [ ] Panel auto-hides when agent finishes (with delay)
- [ ] Token usage shown in expanded view
- [ ] Sidebar "Activity" link shows full session activity
- [ ] Long outputs are truncated with expand-on-click

## Notes

- This is a power-user feature. Don't make it visually dominant.
- The collapsed view should be subtle — just a small status bar below the streaming message.
- Don't show raw JSON. Format tool inputs/outputs into readable summaries.
- Guardian AI status ("✓ All actions approved") is the trust signal — always show it when available.
