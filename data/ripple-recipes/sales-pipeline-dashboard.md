---
title: Sales Pipeline Dashboard
pattern: dashboard
domain: sales-revenue
keywords: [sales, pipeline, quota, funnel, conversion, leaderboard, reps, deals, crm, revenue]
focal_widgets: [pipeline-dashboard]
source: ripple/src/routes/showcase/+page.svelte (pipelineDashboardSpec)
---

# When to use

A "build me a sales pipeline / quota tracker / revenue dashboard"
brief. The user wants to see deal volume + stage conversion + top
reps + recent activity in one composed view, NOT a hand-rolled grid
of stat tiles + bar chart + table.

# Composition

Single ``pipeline-dashboard`` widget at the root. ALL data goes in
its props — no nested children. The widget composes the funnel,
leaderboard, deals table, and ticker internally.

```json
{
  "version": "1.0",
  "ui": {
    "type": "pipeline-dashboard",
    "props": {
      "title": "Sales pipeline",
      "period": "Q2 2026",
      "quota": {
        "label": "Team quota",
        "current": 1820000,
        "target": 2500000,
        "currency": "$",
        "period": "Q2 — 28 days remaining"
      },
      "funnel": {
        "title": "Pipeline funnel",
        "stages": [
          { "label": "Leads",       "value": 1240 },
          { "label": "Qualified",   "value":  480 },
          { "label": "Proposal",    "value":  180 },
          { "label": "Negotiation", "value":   92 },
          { "label": "Closed won",  "value":   38 }
        ]
      },
      "conversion": [
        { "from": "Leads",       "to": "Qualified",   "rate": 38.7 },
        { "from": "Qualified",   "to": "Proposal",    "rate": 37.5 },
        { "from": "Proposal",    "to": "Negotiation", "rate": 51.1 },
        { "from": "Negotiation", "to": "Closed won",  "rate": 41.3 }
      ],
      "leaderboard": {
        "title": "Top reps",
        "items": [
          { "name": "Alex Liu",  "value": "$420k", "delta": "+$80k", "sublabel": "12 deals" },
          { "name": "Sam Patel", "value": "$380k", "delta": "+$45k", "sublabel": "9 deals" },
          { "name": "Jess Tan",  "value": "$310k", "delta": "+$22k", "sublabel": "14 deals" },
          { "name": "Rico Diaz", "value": "$240k",                   "sublabel": "8 deals" }
        ]
      },
      "deals": {
        "title": "Recent deals",
        "columns": [
          { "key": "name",  "label": "Deal" },
          { "key": "stage", "label": "Stage" },
          { "key": "value", "label": "Value", "align": "right" },
          { "key": "owner", "label": "Owner" }
        ],
        "rows": [
          { "name": "Globex Q2 expansion", "stage": "Negotiation", "value": "$120k", "owner": "Alex Liu" },
          { "name": "Hooli SSO add-on",     "stage": "Proposal",    "value": "$48k",  "owner": "Sam Patel" },
          { "name": "Initech renewal",      "stage": "Closed won",  "value": "$62k",  "owner": "Jess Tan"  }
        ]
      },
      "ticker": [
        { "time": "2m ago",  "label": "Closed: Globex Q2 expansion",   "actor": "Alex Liu", "icon": "trophy" },
        { "time": "14m ago", "label": "Demo scheduled: Massive Dynamic", "actor": "Sam Patel" },
        { "time": "38m ago", "label": "Lead qualified: Pied Piper",     "actor": "Jess Tan" }
      ]
    }
  }
}
```

# Anti-patterns to avoid

- ❌ Building this from scratch with ``grid`` + ``stat`` tiles + ``chart`` + ``table``
- ❌ Using ``flex`` as the root and stacking funnel / leaderboard / deals as siblings
- ❌ Hand-rolling a "funnel" as inverted ``bar-chart`` — use the funnel prop on pipeline-dashboard
- ❌ Empty quota / placeholder numbers — the LLM should plug in realistic mock data

# Variations

- **Smaller team** (≤4 reps): drop ``ticker`` and ``conversion``, keep quota + funnel + leaderboard + deals
- **Solo SDR**: replace ``leaderboard`` with a single ``quota`` block; keep funnel + deals
- **Renewals dashboard**: rename ``Pipeline funnel`` → ``Renewal funnel``, stages = [Up for renewal, Engaged, Quote sent, Closed]
