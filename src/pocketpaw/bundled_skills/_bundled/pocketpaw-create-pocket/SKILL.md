---
name: pocketpaw-create-pocket
description: |
  Create a new PocketPaw pocket — an interactive app, viewer, dashboard,
  composer, browser, wizard, or feed — with enterprise-quality design.
  Invoke when the user asks to create / build / make a pocket, dashboard,
  tracker, tool, viewer, recipe, todo, kanban, profile, or any other
  workspace canvas. The skill bundles PocketPaw's full design guidance,
  the 150-widget catalog reference, pattern-first decision logic, and the
  canonical invocation flow. Loading this skill into context keeps the
  chat agent's always-on system prompt small while still delivering rich
  design quality when pocket creation is actually requested.
---

# Pocket Creation Workflow

You're being asked to create a new PocketPaw pocket. A pocket is a
workspace canvas — a JSON ``rippleSpec`` tree of typed widget nodes
(``{type, props, children}``) that renders into a polished UI on the
PocketPaw dashboard. The PocketPaw runtime ships **150 widgets** ranging
from primitives (``flex``, ``stat``, ``button``) to composed enterprise
layouts (``pipeline-dashboard``, ``invoice-layout``, ``entity-detail``,
``order-status``, ``ops-dashboard``).

Your job is to translate the user's brief into the **right pattern**, the
**right focal widget**, and a **mock-data-filled** rippleSpec that lands
through the ``pocket_specialist__create`` MCP tool.

## STEP 1 — Pick the pattern (do this FIRST, before any widget choice)

Before reaching for a layout or widget, name the **pattern** this pocket
fits. The pattern decides what content is primary and which widgets are
even on the table. Pick ONE — and **do not default to ``dashboard``**.
Most briefs are NOT dashboards.

- **``dashboard``** — overview of metrics + trends + roll-up tables. KPI
  tiles + charts + summary roster. **Only pick when the user explicitly
  asked for metrics / KPIs / overview / "dashboard for X".** A brief
  like "team page" or "project tracker" is NOT automatically a dashboard.
- **``app``** — interactive tool the user OPERATES. Persistent state,
  controls that mutate it, an action verb in the title (todo, kanban,
  planner, calculator, tracker, journal, scratchpad).
- **``viewer``** — read-only inspection of ONE thing. Article, recipe,
  profile card, dataset detail, runbook, glossary entry. Text +
  structured facts; no KPI tiles.
- **``composer``** — focused authoring surface. Writer, mood logger,
  idea capture, daily check-in. Input is the focal widget.
- **``browser``** — list + drill into the selection. File explorer,
  mailbox, archive, reading list. Master-detail.
- **``wizard``** — multi-step linear flow. Setup, onboarding, quiz,
  multi-page form, interview prep.
- **``feed``** — reverse-chronological stream. Activity log, news,
  timeline of events, audit trail.

**Default rule:** if the user did not name one of these patterns AND did
not explicitly ask for metrics / KPIs / overview, do NOT pick
``dashboard``. Pick the pattern that fits the *primary action* (read,
operate, write, browse, walk through, scroll). Visual style follows
pattern — not the other way around.

## STEP 2 — Pick the focal widget (reach for polished layouts)

The PocketPaw widget library has **150 widgets**. Many briefs map
directly to a composed pattern layout that already gives you the
funnel, the leaderboard, the conversion rates, etc. — composed and
styled to match. **Reach for these before composing from primitives.**

### Rich widgets by pattern

When ``dashboard``:
- ``pipeline-dashboard`` — sales pipeline (funnel + reps leaderboard + conversion + quota progress, composed)
- ``analytics-dashboard`` — product / web / marketing analytics
- ``ops-dashboard`` — on-call / incidents / SRE
- ``project-dashboard`` — delivery / sprint / project status
- ``exec-dashboard`` — leadership / board / weekly review

When ``viewer``:
- ``entity-detail`` — record / profile / facts about ONE thing (NOT page-header + grid of stats)
- ``pricing-table`` — plans / tiers / pricing
- ``invoice-layout`` — invoice / quote / receipt
- ``order-status`` — order / shipment tracking with timeline + map
- ``report-layout`` — quarterly / status write-up
- ``comparison-layout`` — X vs Y / feature comparison

When ``app``:

For **single-purpose tools** (one focal widget IS the app):
- ``kanban`` — workflow / board / sprint board (NOT a table with status badges)
- ``calendar`` — schedule / month view / time blocks
- ``gantt`` — roadmap / sprint plan
- ``form-layout`` — signup / contact / multi-field form
- ``wizard-layout`` — multi-step setup / onboarding
- ``checklist-layout`` — launch checklist / runbook / pre-flight

For **full-fledged apps** (sidebar nav + multiple views + chrome —
CRM, support tool, project tracker, knowledge base), compose with:
- ``app-shell`` — root container: sidebar + topbar + content slots
- ``sidebar`` — nav rail with sections (Inbox / Tickets / Settings …)
- ``breadcrumb`` — context trail inside content (Tickets › 4587)
- ``master-detail`` — list + drill into selected record
- ``sheet`` — slide-in drawer for "New X" / "Edit X" flows
- ``modal`` — focused dialog for destructive confirms / quick forms
- ``dropdown-menu`` — overflow menus on toolbar / row actions
- ``command-palette`` — ``cmd-k`` / quick-jump / search-everywhere
- ``coachmark`` — first-time-user product tour
- ``notification-center`` — bell icon + unread inbox dropdown
- ``empty-state`` / ``error-state`` — when no selection or load failed

A "build me an app for X" brief should usually start with ``app-shell``
as the root, with the focal widget (kanban / master-detail / form-
layout / etc.) in the content slot.

When ``browser``:
- ``master-detail`` — list on the left, detail on the right (Material 3 "list-detail")
- ``tree-table`` — hierarchical table
- ``filter-bar`` — sidebar / chip filters
- ``saved-views`` — view picker

When ``wizard``: ``wizard-layout``, ``checklist-layout``, ``form-layout``

When ``feed``:
- ``audit-log`` — activity log / audit trail
- ``timeline`` — event history / dated milestones
- ``comment-thread`` — discussion
- ``notification-center`` — bell / inbox

### Use-the-widget rule (intent → widget)

| User intent | Widget |
| --- | --- |
| sales pipeline / quota tracker | ``pipeline-dashboard`` |
| product / web analytics | ``analytics-dashboard`` |
| on-call / incidents / sre | ``ops-dashboard`` |
| delivery / project status | ``project-dashboard`` |
| exec / leadership weekly review | ``exec-dashboard`` |
| record / profile / entity facts | ``entity-detail`` |
| invoice / quote / receipt | ``invoice-layout`` |
| order / shipment tracking | ``order-status`` |
| pricing / plans / tiers | ``pricing-table`` |
| comparison X vs Y | ``comparison-layout`` |
| kanban / board / sprint board | ``kanban`` |
| gantt / roadmap / sprint plan | ``gantt`` |
| calendar / month view / schedule | ``calendar`` |
| timeline / event history | ``timeline`` |
| heatmap / cohort grid / activity | ``heatmap`` |
| treemap / breakdown rectangles | ``treemap`` |
| funnel / conversion drop-off | ``funnel`` |
| sankey / flow diagram | ``sankey`` |
| org chart / team tree | ``org-chart`` |
| file tree / folder tree | ``tree`` |
| hierarchical table | ``tree-table`` |
| audit log / activity log | ``audit-log`` |
| comments / discussion thread | ``comment-thread`` |
| signup / contact / multi-field form | ``form-layout`` |
| multi-step setup / onboarding | ``wizard-layout`` |
| launch checklist / runbook | ``checklist-layout`` |
| product tour / onboarding hint | ``coachmark`` |
| notification bell / inbox | ``notification-center`` |
| code editor (IDE-like) | ``code-editor`` |
| terminal / shell output | ``terminal`` |
| rich text editor | ``rich-text`` |

When the brief matches one of the rows above, **use the named widget
directly**. Don't rebuild it from primitives. The polished widget
already encapsulates the visual hierarchy, mock data shape, and
typography of that pattern.

For widgets not listed above, call ``mcp__pocketpaw_pocket__get_widget_spec``
to fetch the widget's allowed props before drafting.

## STEP 3 — Draft the rippleSpec

The rippleSpec is a JSON tree. Every node has a ``type`` (widget kind)
and ``props`` (flat dict of widget-specific props). Containers add a
``children`` array.

### Shape reference

```json
{
  "type": "flex",
  "props": {"direction": "column", "gap": "16px"},
  "children": [
    {"type": "page-header", "props": {"title": "..."}},
    {"type": "<focal-widget>", "props": {...}},
    ...
  ]
}
```

For dashboards specifically, the top-level node is often the dashboard
widget itself (``pipeline-dashboard``, ``ops-dashboard``, etc.) with
domain-specific props — NOT a flex of stat tiles assembled by hand.

### Mock data is required

Every chart point, table row, KPI value, kanban card, and leaderboard
entry must be a **concrete value**, not a placeholder. Use realistic
names (Alex Liu, Sam Patel, Globex, Stark Industries) and realistic
numbers ($1.8M, 73%, 28 days, 312 deals). PocketPaw's house style is:
populated, never empty. A blank canvas captioned "auto-created from a
brief" is a failure mode — vague briefs get plausible mock data, not
"TBD".

### Interactive apps need state

``app`` pattern pockets carry a ``state`` object at the top level
alongside ``ui``:

```json
{
  "state": {
    "draft": "",
    "tasks": [{"id": "t1", "title": "...", "done": false}]
  },
  "ui": { ... }
}
```

Input widgets ``bind`` to state paths; button ``on_click`` chains
mutate state via ``set`` / ``push`` / ``remove`` actions.

### Visual variation (don't dashboard everything)

Two pockets you create back-to-back should not be mistakable for each
other at a glance. If they would be, change the layout (single-pane vs
master-detail vs stacked vs wizard) or the focal widget — not just the
labels. The ``hero+grid`` layout (page-header + KPI tile grid + chart +
table) is for ``dashboard`` pattern ONLY — do not use it for ``viewer``,
``app``, ``browser``, ``wizard``, or ``feed`` pockets.

## STEP 4 — Persist via the MCP tool

Call ``mcp__pocketpaw_pocket_specialist__create`` with the brief, any
caller-supplied hints, and (in agent mode) the drafted ``spec``:

```json
{
  "brief": "<the user's original brief>",
  "hints": {
    "name": "Sales Pipeline · Q2 2026",
    "description": "Enterprise sales review",
    "color": "#3b82f6",
    "icon": "trending-up",
    "purpose": "<one-sentence: what this pocket accomplishes>",
    "layout": "hero+grid",
    "focal_widget": "pipeline-dashboard",
    "key_interactions": ["filter by quarter", "drill into rep"]
  },
  "spec": { ... your drafted rippleSpec ... }
}
```

In **agent mode** (``POCKETPAW_POCKET_SPECIALIST_MODE=agent``), the first
call returns a draft kit with the structural plan echoed, widget hints,
and instructions. Draft the spec inline, then call the tool AGAIN with
``spec=<your draft>`` for validate-and-persist.

In **subagent mode** (default), the tool spawns its own specialist
backend and returns the created pocket — you only call once with the
brief + hints (no ``spec``).

### Validation warnings → redraft

If the response has ``action: "redraft"`` with warnings, address each
warning and call the tool again with a corrected ``spec``. The retry
budget is bounded; after that the tool persists with warnings and the
user can fix manually.

## Canonical example — a viewer (non-dashboard)

This shows a ``viewer`` pattern with ``entity-detail``-style content
(``page-header`` + ``text`` + ``kv-table`` + ``text``). Use as a shape
reference, not a template to copy verbatim.

```json
{
  "name": "Espresso 101",
  "description": "Pull notes from my favorite barista",
  "type": "business",
  "ripple_spec": {
    "type": "flex",
    "props": {"direction": "column", "gap": "16px"},
    "children": [
      {
        "type": "page-header",
        "props": {
          "title": "Espresso 101",
          "subtitle": "Notes from my favorite barista"
        }
      },
      {
        "type": "text",
        "props": {
          "content": "A double shot is 14 g of finely ground coffee extracted with 36 g of water at 93 °C in 25-30 seconds.",
          "variant": "lead"
        }
      },
      {
        "type": "kv-table",
        "props": {
          "items": [
            {"k": "Dose", "v": "14 g"},
            {"k": "Yield", "v": "36 g"},
            {"k": "Water temp", "v": "93 °C"},
            {"k": "Time", "v": "25-30 s"},
            {"k": "Grind", "v": "fine"}
          ]
        }
      },
      {
        "type": "text",
        "props": {
          "content": "Cup before you pull. Tare the scale. Start the timer when you press the button — not when the first drop appears. Stop at yield, not at time.",
          "variant": "body"
        }
      }
    ]
  }
}
```

## Quality bar

A pocket reads as designed when:

1. **The pattern was chosen first.** You can describe it in one word:
   *"this is a viewer for a recipe"*, *"this is an app for tracking
   workouts"*, *"this is a dashboard for sales pipeline"*.
2. **The focal widget is named.** One widget dominates. If you can't
   name the focal widget, the layout is wrong.
3. **Mock data is concrete.** No "TBD", no "Lorem ipsum", no "[user
   name]". Real-sounding names and numbers.
4. **The layout matches the pattern.** ``hero+grid`` for dashboards
   ONLY. Apps lean ``single-pane`` or ``split``. Viewers lean ``stacked``
   or ``entity-detail``. Browsers lean ``master-detail``.
5. **Two consecutive pockets don't look alike.** Vary the focal widget
   and the layout shape per pocket.

## Related tools (also available via MCP)

- ``mcp__pocketpaw_pocket__list_pockets`` — see existing pockets in the
  workspace before creating, to avoid duplicates
- ``mcp__pocketpaw_pocket__get_widget_spec`` — look up allowed props for
  any widget kind before drafting
- ``mcp__pocketpaw_pocket__get_pocket`` — fetch an existing pocket's
  current rippleSpec
- ``mcp__pocketpaw_pocket_specialist__edit`` — edit an existing pocket
  (separate workflow; this skill is for CREATE)

## External design grounding

The pattern names above map to widely-documented UI layouts in design
systems you've likely seen in training data:

- ``viewer`` / ``browser`` → Material 3 "list-detail" / Apple HIG list
- ``feed`` → Material 3 "feed"
- ``app`` / ``composer`` → Material 3 "supporting-pane" / single-pane
- ``dashboard`` → KPI overview (Datadog, Stripe, Linear Insights)
- ``wizard`` → multi-step flow (Stripe Onboarding, Apple Setup)

Use that grounding to pick the right shape. An "article reader" isn't a
PocketPaw-specific construct — it's the ``viewer`` / ``list-detail``
pattern that's existed in design systems for a decade. Use the focal
widget for the pattern and the layout follows.
