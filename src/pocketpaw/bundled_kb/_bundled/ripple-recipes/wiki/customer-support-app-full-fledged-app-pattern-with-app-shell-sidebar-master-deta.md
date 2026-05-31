---
{
  "title": "Customer Support App — full-fledged app pattern with app-shell + sidebar + master-detail",
  "summary": "Canonical recipe for triage-style internal apps (support tickets, CRM contacts, lead inbox, moderation queue). Root is app-shell with grouped sidebar nav by status, master-detail content area with comment-thread inside the detail pane, and notification-center + command-palette in the topbar. Use this whenever the user asks for an app that queues work items.",
  "concepts": [
    "customer-support",
    "helpdesk",
    "ticket-inbox",
    "app-shell",
    "sidebar-nav",
    "master-detail",
    "comment-thread",
    "notification-center",
    "command-palette",
    "sheet-drawer",
    "crm-contacts",
    "moderation-queue",
    "lead-inbox",
    "triage-pattern",
    "queue-of-work",
    "agent-tool"
  ],
  "categories": [
    "recipe",
    "app-pattern",
    "support-tools",
    "internal-apps"
  ],
  "source_path": "customer-support-app.md",
  "source_docs": [
    "572335a659574ad3"
  ],
  "backlinks": null,
  "word_count": 409,
  "compiled_at": "2026-05-14T08:42:35Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

# Customer Support App

## When this recipe applies

Any brief that maps to 'an app for X' where X is a queue of work items the user triages and drills into. Examples: support tickets, CRM contacts, lead inbox, helpdesk requests, moderation queue, task tracker. The user picks an item from a list, sees full detail in the right pane, and acts on it (reply, close, reassign, escalate).

Key markers in the brief: 'inbox', 'queue', 'triage', 'tickets', 'leads', 'customers', 'support team', 'helpdesk', 'agents', 'assigned to me'.

## Why this composition specifically

The LLM's failure mode without this recipe: it composes the app chrome from flex + tabs and stacks the ticket list above the detail pane in a single column. That reads as 'one page with two sections' instead of 'an application with persistent navigation'. The app-shell widget is structural — it provides the sidebar slot, topbar slot, and content slot as a unit; the chrome reads as scaffolding, not decoration.

The master-detail widget exists precisely for this 'list + selected detail' pattern. Building it by hand from a flex of cards plus a conditional render works visually but loses the selection-state plumbing, the empty-state, and the keyboard navigation. Use the widget.

The comment-thread widget for conversation history avoids the trap of rendering messages as a flex of card widgets — which loses the threading affordances (avatars, timestamps, indent for replies) the polished widget provides.

## Anti-patterns this recipe replaces

- Root flex with sidebar painted as a column of buttons → use app-shell + sidebar widget
- Ticket list as a table with status badges → master-detail with item shape carrying severity / status / assignee
- 'New ticket' form as a separate page or modal → sheet (slide-in drawer, page stays visible)
- Conversation history as a flex of cards → comment-thread
- Notifications as a text block → notification-center

## Adjacent domains (variations of the same shape)

- **CRM contacts**: items become name / company / last-touched; swap comment-thread for activity-timeline
- **Lead inbox**: sidebar sections become Hot / Warm / Cold; add filter-bar above the list
- **Moderation queue**: severity → flag-reason; comment-thread → audit-log
- **Project task tracker**: swap master-detail for kanban; keep app-shell + sidebar

## Known gaps

The recipe uses static counts in the sidebar group items. In a live app these would be bound to query results — Ripple's expression syntax (`{state.queries.unassigned.count}`) handles this when the LLM wires actual data sources.