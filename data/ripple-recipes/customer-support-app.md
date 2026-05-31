---
title: Customer Support App
pattern: app
domain: support-helpdesk
keywords: [support, helpdesk, ticket, inbox, customer-service, crm, agent-tool, inbox-zero]
focal_widgets: [app-shell, sidebar, master-detail, comment-thread, sheet, notification-center]
source: ripple/src/routes/showcase/+page.svelte (appShellSpec + masterDetailSpec)
---

# When to use

A "build me an app for X" brief where X is a queue of work items
the user triages and drills into — tickets, leads, inbound requests,
moderation queue. The shape: app-shell with sidebar nav by status,
master-detail in the content area, comment-thread inside detail,
slide-in sheet for "New X" creation.

Use this AS-IS for support tools. Adapt the section labels +
column shape for adjacent domains (CRM contacts, helpdesk, mod
queue, lead inbox).

# Composition

```json
{
  "version": "1.0",
  "state": {
    "selected": 1,
    "newTicketOpen": false,
    "tickets": [
      { "id": 1, "title": "Stripe webhook 500s on payment.created",
        "customer": "Globex", "severity": "sev1",
        "status": "in-progress", "assignee": "Alex Liu",
        "lastReply": "12m ago", "unread": true },
      { "id": 2, "title": "Slow exports for monthly invoices",
        "customer": "Stark Industries", "severity": "sev2",
        "status": "in-progress", "assignee": "Sam Patel",
        "lastReply": "1h ago" },
      { "id": 3, "title": "Can we add SSO via Okta?",
        "customer": "Wayne Enterprises", "severity": "sev3",
        "status": "waiting-on-customer", "assignee": "Jess Tan",
        "lastReply": "yesterday" }
    ]
  },
  "ui": {
    "type": "app-shell",
    "children": [
      { "slot": "topbar", "type": "flex",
        "props": { "align": "center", "gap": "12px" },
        "children": [
          { "type": "text", "props": { "text": "Acme Support", "weight": "semibold" } },
          { "type": "command-palette", "props": { "placeholder": "Search tickets, customers, macros…" } },
          { "type": "notification-center", "props": { "unread": 3 } }
        ]
      },
      { "slot": "sidebar", "type": "sidebar",
        "props": {
          "groups": [
            { "label": "INBOX", "items": [
              { "label": "All tickets",      "value": "all",        "count": 47 },
              { "label": "Assigned to me",   "value": "mine",       "count": 12 },
              { "label": "Unassigned",       "value": "unassigned", "count": 8 }
            ]},
            { "label": "BY STATUS", "items": [
              { "label": "Open",                  "value": "open",       "count": 29 },
              { "label": "In progress",           "value": "in-progress","count": 12 },
              { "label": "Waiting on customer",   "value": "waiting",    "count": 4 },
              { "label": "Resolved this week",    "value": "resolved",   "count": 18 }
            ]},
            { "label": "LIBRARY", "items": [
              { "label": "Customers", "value": "customers", "icon": "users" },
              { "label": "Macros",    "value": "macros",    "icon": "zap" },
              { "label": "Reports",   "value": "reports",   "icon": "bar-chart" }
            ]}
          ]
        }
      },
      { "type": "master-detail", "bind": "selected",
        "props": {
          "items": "{state.tickets}",
          "width": "360px",
          "emptyText": "Select a ticket from the left to start",
          "detail": {
            "type": "flex", "props": { "direction": "column", "gap": "12px" },
            "children": [
              { "type": "breadcrumb", "props": { "items": [
                { "label": "Inbox" }, { "label": "All tickets" },
                { "label": "{item.title}" }
              ]}},
              { "type": "flex", "props": { "align": "center", "gap": "8px" }, "children": [
                { "type": "heading", "props": { "text": "{item.title}", "level": 3 } },
                { "type": "badge", "props": { "text": "{item.severity}", "variant": "destructive" } },
                { "type": "badge", "props": { "text": "{item.status}",   "variant": "secondary" } }
              ]},
              { "type": "comment-thread", "props": { "messages": [
                { "author": "Customer", "body": "Webhooks are returning 500 for the last 2 hours.", "time": "2h ago" },
                { "author": "Alex Liu", "body": "Acknowledged. Looking at the queue depth.", "time": "1h ago" },
                { "author": "Alex Liu", "body": "Found it — retry storm from yesterday's outage. Rolling fix now.", "time": "12m ago" }
              ]}}
            ]
          }
        }
      }
    ]
  }
}
```

# Anti-patterns to avoid

- ❌ Root is ``flex`` with sidebar as a sibling — use ``app-shell`` so the chrome is structural, not painted
- ❌ Sidebar built from a ``flex`` of ``button`` widgets — use ``sidebar`` widget with grouped items
- ❌ Conversation thread as a ``flex`` of ``card`` widgets — use ``comment-thread``
- ❌ "New ticket" as a separate route — use ``sheet`` triggered from a top-right button

# Variations

- **CRM contacts**: ``items`` shape changes (name / company / last-touch), drop ``severity`` badge, swap ``comment-thread`` for ``activity-timeline``
- **Lead inbox**: sidebar sections become Hot / Warm / Cold; add a ``filter-bar`` above the list
- **Moderation queue**: ``severity`` → ``flag-reason``, ``comment-thread`` → ``audit-log`` showing prior actions
- **Project task tracker**: swap ``master-detail`` for ``kanban``; keep app-shell + sidebar
