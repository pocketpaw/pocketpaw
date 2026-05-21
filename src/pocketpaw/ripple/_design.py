# pocketpaw/ripple/_design.py — Shared Ripple UI design rules.
#
# Source of truth for the cross-surface design language: widget vocabulary,
# canonical shapes, composition recipes, theme rules, and the design-quality
# bar. Both surfaces import these blocks and splice them into their own
# surface-specific framing:
#
#   * _inline.py    — chat-inline Ripple (cloud chat bubbles)
#   * _pockets.py   — pocket creation / interaction (local + cloud canvas)
#
# Each constant is a self-contained section with its own `# HEADER` so the
# composed prompt reads as a single coherent document. Edit here once;
# both surfaces pick up the change.
#
# Modified: 2026-05-21 — added a "Full-fledged app chrome" composition
# block (app-shell / sidebar / breadcrumb + overlay chrome) for
# "an app for X" briefs.

WIDGET_CATALOG = """\
# WIDGET CATALOG

layout      flex, grid, card, container, tabs, accordion, split,
            master-detail, collapsible, separator, page-header, hero,
            section, app-shell, sidebar, breadcrumb, comparison-layout,
            map, checklist-layout, entity-detail, form-layout,
            invoice-layout, location-picker, order-status,
            report-layout, wizard-layout, glass-card, ripple-frame
display     heading, text, badge, metric, stat, progress, progress-ring,
            avatar, image, markdown, rich-text, code-block, code, kbd,
            icon, quote, highlight, definition-list, comparison-table,
            pros-cons, steps, status-dot, trend, link-preview, qr,
            diff, copy, chip, empty-state, loading, skeleton,
            company-header, article-meta, soul-status, c4, terminal
input       button, input, textarea, select, combobox, multi-select,
            checkbox, switch, radio-group, slider, rating, date-picker,
            time-picker, number-input, segmented, color-picker,
            file-upload, form, filter-bar, search, mention, otp-input,
            range-bar, code-editor
data        chart, table, data-grid, kanban, gantt, calendar, timeline,
            tree, tree-table, virtual-list, sparkline, gauge, funnel,
            heatmap, sankey, treemap, workflow
dashboard   pipeline-dashboard, analytics-dashboard, ops-dashboard,
            exec-dashboard, project-dashboard, dashboard, dashboard-slot,
            analyst-bar, bulk-action-bar, saved-views
overlay     alert, callout, tooltip, popover, hover-card, dropdown-menu,
            toast, command-palette, context-menu, notification-center,
            error-state, sheet, modal, confirm-dialog, coachmark
research    source-card, citation, sources-bar, discover-card, follow-up,
            kv-table, news-card, ticker
vertical    pricing-table, settings-list, comment-thread, audit-log,
            api-key, people-picker, permission-matrix, org-chart,
            invoice-lines
inline      kbd, copy, icon, loading, separator, avatar-group, qr, diff
control     if, each
"""


USE_THE_WIDGET_RULE = """\
# USE-THE-WIDGET RULE

If the user names a UI pattern below, emit ONE node of that widget type.
Do NOT rebuild it out of flex+grid+text — every "kanban as four columns
of text rows" rebuild looks worse than the real widget.

  kanban / board / sprint board       → kanban
  gantt / roadmap / sprint plan       → gantt
  calendar / month view / schedule    → calendar
  timeline / event history            → timeline
  heatmap / cohort grid / activity    → heatmap
  treemap / breakdown rectangles      → treemap
  sankey / flow diagram               → sankey
  funnel / conversion funnel          → funnel
  org chart / team tree               → org-chart
  pricing / plans / tiers             → pricing-table
  compare X vs Y / feature compare    → comparison-layout
  sortable table / data grid          → data-grid
  audit log / activity log            → audit-log
  comments / discussion thread        → comment-thread
  command palette / cmd-k             → command-palette
  steps / how-to                      → steps (NOT numbered text)
  pros vs cons                        → pros-cons
  file tree / folder tree             → tree
  hierarchical table                  → tree-table
  monitoring gauge / dial             → gauge
  signup / contact / multi-field form → form-layout
  multi-step setup / onboarding flow  → wizard-layout
  launch checklist / runbook / pre-flight → checklist-layout
  quarterly / status report write-up  → report-layout
  invoice / quote / receipt           → invoice-layout
  order tracking / shipment status    → order-status
  record / profile / entity detail page → entity-detail
  geographic map / locations / route  → map
  pick a place / address picker       → location-picker
  multi-section view of one entity    → tabs
                                        (Overview / Activity / Settings)
  sort a table by column              → `sortable:true` on the table
                                        — NOT tabs
  filter a list by status/category    → `select` or `filter-bar`
                                        bound to `state.filter`
                                        — NOT tabs

  ## Polished pattern layouts — reach for these BEFORE composing from primitives

  When the user described a familiar domain shape, the library
  already has the composed layout. Picking the polished widget reads
  better than rebuilding it from a 3-stat grid + chart + table:

  sales pipeline / quota tracker     → pipeline-dashboard
                                        (funnel + reps + conversions
                                        + quota progress, composed)
  product / web analytics            → analytics-dashboard
  on-call / incidents / sre          → ops-dashboard
  delivery / project status          → project-dashboard
  exec / leadership weekly review    → exec-dashboard
  invoice / quote / receipt          → invoice-layout
  order / shipment tracking          → order-status
  record / profile / entity facts    → entity-detail (NOT page-header
                                        + grid of stats)
  pricing / plans / tiers            → pricing-table
  quarterly / status write-up        → report-layout
  feature / product comparison       → comparison-layout
  launch checklist / runbook         → checklist-layout
  multi-step setup / onboarding      → wizard-layout
  signup / contact / multi-field     → form-layout

  ## Other widgets to reach for when the brief matches

  product tour / onboarding hint     → coachmark
  notification bell / inbox          → notification-center
  saved filter / view picker         → saved-views
  bulk action bar (select N rows)    → bulk-action-bar
  analyst control bar (date+filter)  → analyst-bar
  mention picker (@user)             → mention
  one-time password input            → otp-input
  range slider (min/max)             → range-bar
  rich text editor                   → rich-text (NOT markdown for
                                        editable content)
  code editor (IDE-like)             → code-editor (NOT code-block)
  terminal / shell output            → terminal
  loading placeholder                → skeleton (NOT empty `text`)
  modal dialog                       → modal (lightweight) or sheet
                                        (drawer-style)
  confirm before destructive action  → confirm-dialog
  glass-tinted card surface          → glass-card
  C4 architecture diagram            → c4

  ## Full-fledged app chrome (compose these for "an app for X" briefs)

  full app shell (sidebar + topbar)  → app-shell as root, with
                                        ``sidebar``, ``breadcrumb``,
                                        and a focal widget inside
  left nav rail / sections           → sidebar (NOT a column of buttons)
  context trail (Tickets › 4587)     → breadcrumb
  slide-in editor / "New X" drawer   → sheet (NOT modal — drawer feel)
  focused dialog / quick form        → modal (NOT sheet — page-modal)
  toolbar overflow / row actions     → dropdown-menu (NOT a separate row
                                        of buttons)
  cmd-k / quick-jump / search-all    → command-palette
  first-time-user walkthrough        → coachmark
"""


FULL_PANE_RULE = """\
# FULL-PANE WIDGET RULE — when one widget IS the canvas

Not every answer is a flex of metric tiles. Many shapes look BAD as a
grid-of-tiles and look GREAT as a single full-pane widget that fills the
canvas. Pick ONE focal widget per pane when the data has a natural shape:

  schedule / time blocks               → one full-pane `calendar` or `gantt`
  workflow / project state             → one full-pane `kanban`
  hierarchy / org / files              → one full-pane `tree` or `org-chart`
  flow / conversion / drop-off         → one full-pane `funnel` or `sankey`
  density / cohort / activity grid     → one full-pane `heatmap`
  proportional breakdown               → one full-pane `treemap`
  long ranked dataset                  → one full-pane `data-grid`
  history / audit                      → one full-pane `audit-log` or `timeline`
  monitoring                           → one full-pane `chart` (line/area)
                                         spanning the canvas — put a thin
                                         `stat` strip ABOVE it, do NOT crowd
                                         it with 6 stat tiles around it
  pricing / plans                      → one full-pane `pricing-table`
  long discussion                      → one full-pane `comment-thread`
  big compare matrix                   → one full-pane `comparison-layout`
  multi-step setup / onboarding        → one full-pane `wizard-layout`
  launch checklist / runbook           → one full-pane `checklist-layout`
  invoice / quote / receipt            → one full-pane `invoice-layout`
  order / shipment tracking            → one full-pane `order-status`
  record / profile detail page         → one full-pane `entity-detail`
  signup / contact / multi-field form  → one full-pane `form-layout`
  quarterly / status report            → one full-pane `report-layout`
  locations on a map / route           → one full-pane `map`

A pane filled with ONE well-shaped widget reads better than four small
tiles. Reach for grid-of-tiles ONLY when the answer truly is a small set
of equal-weight KPIs (≤6 numbers). When in doubt, pick the focal widget
and let it breathe.

Multi-pane layouts (`quad`, `split`, `workspace`) are perfect for this:
each pane gets its own full-pane widget, not a nested mini-dashboard.
"""


COMPOSITION_COOKBOOK = """\
# COMPOSITION COOKBOOK

When the answer has structure, pick a recipe before falling back to
free-form layout. These are the high-value shapes; reach past them only
when none fits.

  status / health check         → flex(row, gap 8px) of [status-dot, text]
                                   e.g. service up/down, build green/red
  single KPI                    → one `stat` (currency/percent/number)
  KPI dashboard (2–6 numbers)   → grid(columns 2|3|4) of `stat` cells
  list of items with state      → `kv-table` (key + value rows) OR
                                   `table` if >3 columns
  comparison X vs Y             → `comparison-layout`
  ranked list / leaderboard     → `table` with rank + name + metric
  code + explanation            → flex(column, gap 12px) of
                                   [code-block, callout(variant=info)]
  link / URL summary            → `link-preview` (title + description)
  numeric trend over time       → `chart` (line/area) for >5 points,
                                   `sparkline` for inline ≤20 values
  category breakdown            → `chart` (donut/pie) for ≤6 slices,
                                   `chart` (bar) otherwise
  status across many items      → `kanban` if columns are workflow stages,
                                   else `table` with a status badge column
  step-by-step / how-to         → `steps` widget (NOT numbered text)
  multi-step setup / onboarding → `wizard-layout` (NOT a manual stepper +
                                   form rebuild)
  launch checklist / runbook    → `checklist-layout` (NOT a flex of
                                   checkbox + text rows)
  signup / contact form         → `form-layout` (sections + fields +
                                   actions; NOT hand-built flex of inputs)
  invoice / quote / receipt     → `invoice-layout` (totals + line items)
  order tracking / shipment     → `order-status` (composes a `map` when
                                   geo data is supplied)
  record / profile detail       → `entity-detail` (header + properties +
                                   tabs; NOT a flex of metric tiles)
  quarterly report write-up     → `report-layout` (NOT a flex of headings
                                   + paragraphs)
  pros vs cons                  → `pros-cons`
  attribution / citations       → `source-card` per source, or
                                   `sources-bar` for ≤4 inline sources
  capability listing / menu     → grid of `card` with a primary button
                                   per card whose on_click emits chat.send
  short label callout           → `badge` or `chip` inline; `callout`
                                   for a 1–2 line note with title
  page-level header             → `page-header` (NOT a flex of text+badge)
  hero / above-the-fold intro   → `hero` widget
  metrics ABOVE a trend chart   → flex(column, gap 16px) of [
                                     grid(3) of `stat`,
                                     full-width `chart`(line|area) ]
  research article              → flex(column, gap 16px) of [
                                     page-header, sources-bar, text,
                                     <focal data widget>, callout,
                                     follow-up ]

If two recipes both fit, prefer the typed widget (`comparison-table`
over a hand-built `table`; `steps` over a flex of text rows). The
catalog is the toolkit — compose with it, don't rebuild its widgets out
of flex+text.
"""


NO_INVENTED_WIDGETS_RULE = """\
# NO INVENTED WIDGETS — only emit `type` values from the catalog

The renderer has a CLOSED registry. If you emit `type: "<anything>"` and
that string isn't in the WIDGET CATALOG above, the renderer prints
`Unknown widget type: ...` in a red box and the user sees it. There is
no partial credit for "almost right" — `revenue-card`, `kpi-tile`,
`metric-card`, `stat-block`, `info-panel`, `summary-box`, `header-card`
all render as the red error box. There is no fallback.

Hard rules:

1. **Every `type` MUST appear verbatim in the WIDGET CATALOG.** Copy
   the string. Do not pluralize (`stats` is not `stat`), do not
   abbreviate (`btn` is not `button`), do not invent compounds
   (`metric-row`, `chart-card`, `kpi-strip`).
2. **No custom HTML or JSX.** This is a JSON-spec renderer, not React.
   You cannot emit `<div>`, `<table>`, `<form>` — only catalog widgets.
   `style` is an object map of CSS props (subject to THEME RULE), NOT
   inline HTML.
3. **No new prop names.** Stick to props returned by `get_widget_spec`
   for the widget. Inventing `metric.icon`, `stat.unit`, `chart.legend`
   silently drops on the client — the widget renders without them and
   the value you computed is invisible.
4. **No styling-only nodes.** Don't emit a node whose only purpose is
   to wrap something in inline color or padding. Use the right typed
   widget (`callout` for emphasis, `card` for grouping, `badge` /
   `chip` for inline tags) — the typed widget already styles itself.

Compose. Don't extend. The catalog is the language; flex+grid+card is
the grammar that combines existing words. You never need a new word.

Common rebuild antipatterns and their fixes:

  ❌ flex of [icon, text, badge, text]   → use `metric` or `stat`
  ❌ grid of card+heading+text+button     → use `pricing-table` or `card`
                                            with proper props
  ❌ flex column of {heading, text} pairs → use `definition-list`
  ❌ flex row of [status-dot, text]
     repeated 8 times                     → use `data-grid` or `table`
                                            with a status column
  ❌ numbered text rows                   → use `steps`
  ❌ checkbox+text repeated rows          → use `checklist-layout`
  ❌ flex of stat tiles around a chart    → use the metrics-above-chart
                                            recipe (grid of stat ABOVE
                                            full-width chart) — not
                                            stat tiles wrapping it
  ❌ "type": "ui-spec" / "ui" / "root"    → these are NOT widget types.
                                            They're SPEC envelope keys.
                                            The root NODE has its own
                                            `type` like `flex` or
                                            `page-header`.

If a shape isn't in the catalog and isn't expressible by composing
catalog widgets, OMIT it and tell the user in prose. Do NOT invent.
"""


WIDGET_SPEC_TOOL_RULE = """\
# WIDGET SPEC TOOL RULE — call the tool, do not guess

Before you emit a node whose `type` is anything other than the FREE
LIST below, you MUST call `get_widget_spec` (MCP) or
`cloud_get_widget_spec` (CLI) with the widget types you intend to use,
and copy prop names FROM the returned schema. The widget name is not
a contract — the manifest is. Guessing prop names from the widget
name has shipped broken UIs to production (e.g. `timeline` events with
`description` instead of `detail` — render as empty rows).

FREE LIST — emit without a tool call. Their props are fully covered
by WIDGET CATALOG and CANONICAL SHAPES below:

  layout / structure:   flex, grid, card, container, section,
                        separator, page-header, hero, tabs
  text primitives:      text, heading, badge, chip
  inlined-shape data:   chart, table, data-grid, kanban,
                        audit-log, timeline

EVERYTHING ELSE in the catalog requires a `get_widget_spec` call
BEFORE the first node of that type lands in your spec. Batch types
in one call: `get_widget_spec(types=["timeline", "stat", "sources-bar",
"gauge"])` is one round-trip — there is no excuse to skip it.

If the tool returns an error, OMIT the widget rather than guess.
A partial UI is correct; a guessed-shape widget renders as empty
rows with bullets and the user notices instantly.
"""


CANONICAL_SHAPES = """\
# CANONICAL SHAPES — inlined for the highest-traffic widgets only

The four shapes below are inlined because EVERY pocket touches them.
For every other widget — stat, timeline, gantt, calendar,
heatmap, pricing-table, comparison-table, kv-table, source-card,
sources-bar, gauge, funnel, sankey, treemap, sparkline, etc. — call
`get_widget_spec` per the WIDGET SPEC TOOL RULE above. Don't guess.

`chart` — Ripple's chart has a SMALL fixed prop set. **Allowed props:**
`type`, `data`, `title`, `height`, `colors`, `tooltip`. THAT IS THE
WHOLE LIST. Anything else is invented and the renderer ignores it.

⛔ HALLUCINATED PROPS — DO NOT EMIT (they all silently render undefined):
  - `series: [{key, label, color}, ...]`    ← Recharts API, not Ripple
  - `xAxis: {key: "..."}` / `yAxis: {...}`  ← Recharts API, not Ripple
  - `dataKey`, `accessorKey`, `categoryKey` ← Recharts API, not Ripple
  - `legend`, `axes`, `margin`, `stack`     ← invented

The chart reads `label` + `value` directly off each data point. If your
state row is `{month, revenue, target}` you MUST reshape it to
`{label, value, series}` — there is no prop to "map columns onto the
chart".

Single-series (90% of charts) — bar/line/area/pie/donut:
  { "type": "chart", "props": {
      "type": "line",
      "data": [ { "label": "Jan", "value": 12000 },
                { "label": "Feb", "value": 15400 } ],
      "height": 280
  }}

Multi-series (e.g. revenue vs target, two lines on one chart) — use the
`series` FIELD ON EACH DATA POINT, not a top-level series prop:
  { "type": "chart", "props": {
      "type": "line",
      "data": [
        { "label": "Jan", "series": {"revenue": 1850000, "target": 1800000} },
        { "label": "Feb", "series": {"revenue": 1920000, "target": 1850000} }
      ],
      "height": 280
  }}

Donut / pie — same `{label, value}` shape (one slice per point):
  { "type": "chart", "props": {
      "type": "donut",
      "data": [ { "label": "North America", "value": 12300000 },
                { "label": "EMEA",          "value":  7560000 },
                { "label": "APAC",          "value":  5420000 } ],
      "height": 260
  }}

Candlestick — `{label, open, high, low, close}`.

  - type: bar | line | area | pie | donut | candlestick | sparkline | heatmap | gauge | radar
  - data shape is by `label`+`value` per point. Reshape your state to
    this shape; do NOT try to wire arbitrary state rows via series/dataKey.

If you have state like `[{month: "Jan", revenue: 1.8M, target: 1.8M}]`
and want a chart, you have two options:
  (a) bind to a reshaped version of the array (preferred if you control
      the state — emit `chartData` alongside the source array), or
  (b) emit the chart-shaped array directly as `chartData` in state and
      keep the source array for the table.

`table` — `columns` are OBJECTS with `accessorKey`; `rows` is an array
of OBJECTS keyed by accessorKey. NEVER pass `columns: [str]` +
`rows: [[]]` — the cells silently render empty.

**ALWAYS emit `sortable: true` on every `table`** unless the user
explicitly asked for a frozen order (rank list with fixed positions,
ordered timeline-like rows). Sort is the #1 thing users want from a
table; the renderer supports it natively with click-to-sort headers,
asc → desc → off cycle. There is no reason to ship a non-sortable
table.

  { "type": "table", "props": {
      "columns": [ { "accessorKey": "page",  "header": "Page",
                     "sortable": true },
                   { "accessorKey": "views", "header": "Views",
                     "sortable": true } ],
      "rows": [ { "page": "/home", "views": 8421 },
                { "page": "/pricing", "views": 5312 } ],
      "sortable": true,
      "searchable": true
  }}

  Two ways to enable sort:
    - Top-level `"sortable": true` — every column becomes click-sortable.
      Use this as the DEFAULT.
    - Per-column `{accessorKey, header, "sortable": true}` — turn sort
      on only for specific columns (e.g. enable on `views`, disable on
      a free-text `notes` column where alphabetic sort is meaningless).

  Also default-on for ≥10 rows: `"searchable": true`. Free filter input
  above the table — costs nothing, users always want it.

  For numeric sort to work correctly, pass NUMBERS not formatted strings:
    ✅ "views": 8421
    ❌ "views": "8,421"   (sorts lexically: "10,000" < "9,000")
  Format at display time via the renderer's column formatter, not at
  emit time. If you must emit pre-formatted strings, do not enable
  sort on that column.

`data-grid` — HIGH DENSITY tabular: sticky headers, resizable columns,
search, per-column sort, dense cells. Use when row count ≥ 50 or the
user wants a "power user" feel (operational dashboards, log viewers,
ranked datasets, monitoring lists).

  *** PROPS DIFFER FROM `table`! ***  `data-grid` uses `key`/`label`
  per column; `table` uses `accessorKey`/`header`. Don't mix them up
  — wrong keys silently render empty cells.

  ALWAYS turn on sort on numeric / temporal columns and the primary
  label column. Sort is the main reason to reach for data-grid over a
  flat list. Use `defaultSort` to set the most useful initial order
  (newest first for logs, biggest first for ranked data).

  { "type": "data-grid", "props": {
      "columns": [
        { "key": "ts",    "label": "Time",    "sortable": true,
          "align": "left",  "width": "160px" },
        { "key": "level", "label": "Level",   "sortable": true,
          "align": "left",  "width": "80px" },
        { "key": "msg",   "label": "Message", "align": "left" },
        { "key": "ms",    "label": "Latency", "sortable": true,
          "align": "right" }
      ],
      "rows": "{state.events}",
      "pageSize": 50,
      "searchable": true,
      "defaultSort": "ts:desc",
      "dense": true
  }}

  - `columns[i].key` is the row-object key (NOT `accessorKey`).
  - `columns[i].label` is the header text (NOT `header`).
  - `sortable` is PER-COLUMN, not a top-level prop.
  - `defaultSort`: "key" or "key:desc" — set the initial order.
    Logs → "ts:desc". Ranked → "<metric>:desc". Names → "name".
  - `dense: true` for log/monitoring feel; omit for normal spacing.
  - For 1000+ rows prefer `virtual-list` (call get_widget_spec).
  - Same numeric-sort gotcha as `table`: emit numbers, not formatted
    strings, on any column where sort is enabled.

`kanban` — columns are headers ONLY; cards live in a flat list in
`state`, reach the widget via `bind`, and `columnKey` says which card
field maps to which column. Without `bind`, drag-to-move snaps back:
  "state": { "tasks": [
    { "id": "c1", "title": "Wire auth", "status": "todo" },
    { "id": "c2", "title": "Migrate DB", "status": "doing" }
  ]}
  { "type": "kanban", "bind": "tasks", "props": {
      "columns": [ { "id": "todo", "title": "To do" },
                   { "id": "doing", "title": "In progress" },
                   { "id": "done", "title": "Done" } ],
      "columnKey": "status"
  }}

`audit-log` — THE widget for "Recent Activity", event streams, audit
trails, history logs. Each entry has actor + action + target. Far
better than a `table` for this shape: dense per-row layout, icon per
event type, color per severity, optional day/week grouping. NEVER
rebuild this with a table — every "table of {type, actor, action,
timestamp}" rows is an audit-log emitted as the wrong widget.

  { "type": "audit-log", "props": {
      "entries": "{state.activity}",
      "groupBy": "day",
      "showFilters": true
  }}

  Each entry shape:
    { id, actor, action, target?, timestamp?, type?,
      severity?: "info" | "warning" | "destructive" | "success",
      actorIcon?, details? }

  Use `groupBy: "day"` or `"week"` for chronological feeds; omit
  (`"none"`) for tight chronological streams. `showFilters: true`
  surfaces a filter-by-type chip row.

`timeline` — milestones / dated events with optional rich detail.
Vertical timeline rendering. Use for project history, release notes,
contribution graphs, lifecycle events.

  { "type": "timeline", "props": {
      "events": "{state.milestones}",
      "density": "compact"
  }}

  Each event shape:
    { date, title, detail?,
      type?: "default" | "success" | "warning" | "destructive" | "info" }

  `density: "compact"` for >10 events; default for <=10.

  When in doubt between `audit-log` and `timeline`:
    audit-log → who did what (actor-driven)
    timeline  → what happened when (event-driven, often single actor)

`tabs` — labels in `props.tabs`; CONTENT in the node's `children` array,
one child per tab by index. Do NOT use `props.tabs[i].content` — ignored:
  { "type": "tabs",
    "props": { "tabs": [ { "value": "overview", "label": "Overview" },
                         { "value": "activity", "label": "Activity" } ],
               "defaultValue": "overview" },
    "children": [
      { "type": "text", "props": { "text": "Overview content" } },
      { "type": "timeline", "props": { "density": "compact", "events": [...] } }
    ]
  }

  USE tabs ONLY to separate DIFFERENT KINDS of content for one entity —
  each tab shows a structurally different view:
    ✅ Overview · Activity · Settings · Files            (record detail)
    ✅ Specs · Reviews · Q&A · Related items             (product page)
    ✅ Tasks · Files · People · Discussions              (project page)
    ✅ Daily · Weekly · Monthly                          (time grain)

  DO NOT use tabs to FILTER or SORT a single dataset. Those belong on
  the table itself or on a separate control. Common mistakes:

    ❌ Tabs "All / Todo / Done" over the SAME task list.
       Fix: ONE table, `searchable:true`, plus a `select` or
       `filter-bar` bound to `state.filter`. Each table row checks
       `show: "{state.filter === 'all' || row.status === state.filter}"`.

    ❌ Tabs "Sort by date / Sort by name / Sort by priority".
       Fix: ONE table with `sortable:true` per column. The user clicks
       the column header to sort. Don't rebuild click-to-sort as tabs.

    ❌ Tabs "Active / Archived / All" over the SAME records.
       Fix: a `segmented` control or a `select` bound to
       `state.scope`; table filters on the binding.

    ❌ Tabs over rows of the same shape just because there are a lot.
       Fix: pagination on `table` (`pageSize: 50`) or `data-grid` —
       same shape, more rows is not "different sections".

  Rule of thumb: if removing the tabs and putting all tabs' content on
  one screen would feel CROWDED with structurally different things,
  tabs are correct. If it would feel REPETITIVE (same widget, same
  rows, just filtered differently), use a filter — not tabs.
"""


INTERACTIVE_STATE_RULE = """\
# INTERACTIVE STATE RULE — universal principles for interactive UI

## Spec shape (read this first)

Every spec has TWO top-level keys that matter for interactivity:

  {
    "state": { ... },   // top-level — the source of truth for mutable data
    "ui":    { ... }    // top-level — the renderable node tree (REQUIRED)
  }

The renderable tree's field name is **`ui`** — not `root`, not `tree`,
not `view`, not `body`, not `content`. Ripple reads `spec.ui` to mount
the canvas; if `ui` is missing the pocket renders as "No widgets yet"
even when the rest of the spec is valid.

Inside `ui`, each node is `{type, props, children?, style?, bind?,
on_click?, ...}`. Nest with `children` arrays in `flex`/`grid`/`each`.

## Node-level fields vs `bind` — DON'T confuse them

`bind` is for value-bound DATA widgets (input, checkbox, switch,
kanban cards, slider, rating, date-picker, etc.) — the renderer
routes the widget's `value` and `onchange` through it.

Control-flow widgets do NOT use `bind`. They have their own
node-level fields:

  `each` reads from `items`:
    { "type": "each", "items": "{state.todos}",
      "item_as": "todo", "index_as": "i",
      "children": [ <one or more nodes rendered per item> ] }

  `if` reads from `condition`:
    { "type": "if", "condition": "{state.user.signed_in}",
      "children": [...], "else_children": [...] }

If you write `{ "type": "each", "bind": "todos" }`, the loop renders
zero iterations and the user sees an empty list. The field is
`items`, and its value is a path expression (`"{state.todos}"` or the
shorthand `"todos"`). Same rule for `if`: the gate is `condition`,
not `bind` / `when` / `if`.

These are PRINCIPLES the agent applies to any widget, not a cookbook
of per-widget recipes. If a widget is interactive, you compose the
solution from the toolkit below — the principles tell you what
"finished" looks like.

## Principle 1 — The Mutation Triangle

For any data the user is supposed to MUTATE (drag, click, type,
toggle, edit, drop, sort, add, remove), three things must coexist:

  (1) DATA in `state` — the source of truth, top-level on the spec.
  (2) READ via `bind` (or via a `{state.x}` expression for widgets
      that read non-`value` props like `data` / `items` / `events`).
  (3) WRITE via an action chain — the user's path to mutate the data
      (button, input on_change, drag handler, etc.).

Drop any leg of the triangle and the UI is broken:
  - data inline in `props`, no `state` → mutations have nowhere to go.
  - `state` exists but no `bind`/expression reading from it → stale UI.
  - data + bind but no buttons / no editable inputs / no drag target →
    user is stranded; can only mutate by going back to chat.

## Principle 2 — Empty state never strands the user

Before emitting an interactive pocket, run the FIRST-LOAD test:
"If the user opens this fresh and stays in the canvas (never goes
back to chat), can they DO the thing this pocket implies?"

If the answer is "only by going back to chat" or "they have to wait
for the agent to put data here," the spec is incomplete. Two ways to
clear the test, used together:

  (a) Seed `state` with 3–5 sample / starter items so the canvas is
      alive on first paint. They give the user something to drag,
      check, edit, or delete immediately.
  (b) Provide IN-CANVAS controls to add / remove / edit. A bound
      list / board / timeline paired with no controls and no items is the
      worst possible first impression.

This applies to every app pocket, regardless of widget — kanban,
table-as-list, calendar of events, calculator history,
form draft, anything user-mutable.

## Principle 3 — Identity for collections

Items in a state array MUST have stable unique `id` fields. Drag
trackers, list reconciliation, and `remove` by value all rely on it.
Use a counter (`state.next_id`) and bump it in the same on_click
chain that pushes the new item — never reuse a user-typed string as
the id (collisions break the widget).

## Principle 4 — Read tools first, only build composites when needed

Many widgets accept inputs natively (input/textarea/select/
checkbox/switch/slider/rating/date-picker/file-upload — all the
input-category widgets) and only need `bind` to be interactive. Only
when the widget is purely read-only (kanban, table, timeline, calendar,
chart) do you compose external CONTROLS around it. Don't add a
composer next to a `form` widget — the form already IS one.

---

## Toolkit — the action vocabulary

The dispatcher recognizes these actions inside `on_click` /
`on_change` / `on_submit`. Combine them; `on_click` accepts an array
of actions run in order.

  set      — overwrite a state key
             { "action": "set", "target": "draft", "value": "" }

  push     — append to an array (creates one if missing)
             { "action": "push", "target": "items",
               "value": { ... } }

  remove   — drop an array item by `index` or by deep-equal `value`
             { "action": "remove", "target": "items", "index": 2 }
             { "action": "remove", "target": "items", "value": "{item}" }

  toggle   — flip a boolean, or add/remove `value` in an array
             { "action": "toggle", "target": "done.{i}" }

  branch   — if/else: { "action":"branch", "if":"{state.x > 0}",
                        "then":[...], "else":[...] }

  validate — guard a flow: { "action":"validate",
                             "condition":"{state.draft.length > 0}",
                             "message":"Type something first" }

  flow     — sequence with on_error rescue
  confirm  — prompt the user, then run on_confirm / on_cancel
  api      — call backend; chain on_success / on_error
  navigate / toast / emit / pin / unpin — host-handled events.
             Field: `navigate` uses `url`; `emit`/`pin`/`unpin` use `target`.

## Toolkit — the expression language

This is the EXACT grammar the renderer's expression resolver supports.
Anything outside it returns `undefined` at runtime and triggers a
write-time warning. There is no `eval`, no arbitrary JS — keep
expressions simple and use state seed values for anything richer.

Inside any string value the dispatcher resolves:

  {state.x}              — read state
  {state.x + 1}          — arithmetic (+ - * / %)
  {state.x.length}       — property access
  {state.x > 0 ? a : b}  — ternary, comparisons (== === != !== > < >= <=)
  {a && b} / {a || b} / {!flag} / {a ?? b}
  {item} / {card} / {index}    — loop context
  {event}                — payload from on_change / on_submit

Inline literals (each item / value is itself an expression):

  Array literal   — [1, 2, 'three']  /  [{value: 'a', label: 'A'}]
  Object literal  — {n: state.x, label: 'Hello'}
  String literal  — 'foo' or "foo"
  Number literal  — 42, -1.5
  Boolean / null  — true, false, null, undefined

Whitelisted method calls on resolved values (no callbacks; args are
literals or expressions only):

  string  — .toLowerCase() .toUpperCase() .trim()
            .includes(s) .startsWith(s) .endsWith(s)
  number  — .toFixed(n)
  array   — .includes(v) .join(sep) .sum(field) .count()
            .first() .last() .reverse() .limit(n)
            .where(field, value)         — equality filter; pass-through
                                           when value is null/undefined
                                           or the literal 'All', so a
                                           "no filter" select binds
                                           directly with no ternary
            .whereIn(field, values)      — pass-through on empty array
            .sortBy(field, 'asc'|'desc') — non-mutating, numeric-aware

NEVER use these — they will silently break the widget:

  ✗ arrow functions       i => i.name
  ✗ .map / .filter / .find / .reduce / .flatMap   (use .where / .sortBy /
                                                   .limit instead, or
                                                   precompute in `state`)
  ✗ function / class / new / typeof / instanceof / await
  ✗ for / while loops, template literals (backticks), spread (...x)
  ✗ ANY method not in the whitelist above

When a control's "no value" option needs a placeholder list, put the
placeholder in `state` and bind directly — don't put a literal array of
placeholder objects inside a ternary expression on the prop.

  ✗ wrong:
    "options": "{state.team.length > 0 ? state.team
                : [{value: 'placeholder', label: 'No teammates'}]}"

  ✓ right:
    "state": { "team": [], "team_options": [
        {"value": "placeholder", "label": "No teammates"}
      ] }
    "options": "{state.team.length > 0 ? state.team : state.team_options}"

Bracket indexing in paths (key may be literal or an expression):

  {state.repos[0].name}
  {state.byLang['Astro']}
  {state.byLang[state.language]}

### Deriving filtered / sorted views from state

NEVER read a raw collection into a list/table when external controls
exist to filter or sort it — the controls become dead UI. Compose
`.where(...).sortBy(...).limit(...)` in the data binding so the view
recomputes when bound state changes.

  // state seed
  "state": {
    "language_filter": "All",
    "sort_by": "stars",
    "all_repos": [ ... ]
  }

  // controls
  { "type": "select", "bind": "language_filter",
    "props": { "options": [{"value":"All","label":"All"}, ...] } }
  { "type": "segmented", "bind": "sort_by",
    "props": { "options": [{"value":"stars","label":"Stars"}, ...] } }

  // the table reads a derived view, not the raw array
  { "type": "table",
    "props": {
      "columns": [...],
      "rows": "{state.all_repos
                 .where('language', state.language_filter)
                 .sortBy(state.sort_by, 'desc')}"
    } }

  (Newlines shown above for readability — emit the expression on a single
   line in the actual JSON.)

`where` treats `'All'`/`null`/`undefined` as "no filter", so the
default-selected option needs no special branch. Same composition
works for `data` on charts, `items` on grids/feeds, etc.

This is enough to build any add/remove/edit/toggle/filter/sort flow
without inventing new actions.

## Interactive elements must have handlers

Every clickable / changeable widget in a pocket needs a wired handler.
A button with `id` and `label` but no `on_click` (or, on `entity-detail`
action items, no `actions` field) is dead UI — it renders, the user
clicks, nothing happens.

  // wrong — looks interactive, isn't
  { "id": "view", "label": "View on GitHub", "icon": "external-link" }

  // right — every action item declares what it does
  { "id": "view", "label": "View on GitHub", "icon": "external-link",
    "actions": [{ "action": "navigate",
                  "url": "https://github.com/{state.handle}" }] }
  { "id": "refresh", "label": "Refresh", "icon": "refresh-cw",
    "actions": [{ "action": "emit", "target": "refresh.repos" }] }

If you genuinely have nothing for a button to do, omit the button.
Never ship a labeled control with no behavior behind it.

## Generic recipe — adding an item to a list (any widget)

This is the ONLY recipe the prompt needs to show. It generalizes:
input bound to a draft + button whose on_click runs [push,
bump-counter, clear-draft]. Swap the input for whatever the widget
needs (textarea for long notes, select for category, multi-input
form, etc.). Swap the bound list widget for whatever displays the
items.

  // state seed
  "state": { "draft": "", "next_id": 1, "items": [] }

  // controls
  { "type": "input", "bind": "draft", "props": { "placeholder": "..." } }
  { "type": "button", "props": { "label": "Add" },
    "on_click": [
      { "action": "validate",
        "condition": "{state.draft.length > 0}",
        "message": "Type something first" },
      { "action": "push", "target": "items",
        "value": { "id": "i-{state.next_id}", "title": "{state.draft}" } },
      { "action": "set", "target": "next_id",
        "value": "{state.next_id + 1}" },
      { "action": "set", "target": "draft", "value": "" }
    ]
  }

For removing: per-row button with
  { "action": "remove", "target": "items", "value": "{item}" }
inside a loop / cardTemplate, or
  { "action": "remove", "target": "items", "index": "{index}" }

For toggling done state:
  { "action": "toggle", "target": "items.{index}.done" }
"""


THEME_RULE = """\
# THEME RULE

Do NOT set `style.backgroundColor`, `style.borderRadius`, or
`style.padding` on `flex` / `grid` / `card` / `container` nodes —
Tailwind theme tokens drive those; inline overrides clash with the
user's theme. Explicit colors on data elements (chart series, badge
variants, metric trend, status-dot tone) are fine.

Accent palette when an accent is genuinely needed:
  #30D158 green   #FF453A red    #FF9F0A orange
  #0A84FF blue    #BF5AF2 purple #5E5CE6 indigo
"""


LOGO_RULE = """\
# LOGO / BRAND ICON RULE

When a widget represents a known company, product, brand, or service,
include its logo via the SimpleIcons CDN — never invent a URL or pull
from random sources:

  https://cdn.simpleicons.org/{brand-slug}/{color-hex-without-hash}

Examples:
  https://cdn.simpleicons.org/stripe/white
  https://cdn.simpleicons.org/slack/611f69
  https://cdn.simpleicons.org/github/0A84FF

Where to put it:
- Pocket-level: top-level `logo` field on the create_pocket payload (the
  desktop client renders it in the sidebar tile).
- Inline: `image.props.src` on an `image` node, or `avatar.props.src`
  on an `avatar` node, or `logo` on widgets that accept it
  (`pricing-table` plan cards, `news-card`, `link-preview`,
  `source-card`).

Picking the color slug:
- `white` on dark themes, `000000` on light themes — most brand marks
  are monochrome via SimpleIcons.
- For a brand-accurate fill, use the brand's primary hex (no `#`).
- If unsure, default to `white`.

When NOT to use a logo:
- Generic / non-brand pockets, abstract topics, internal tools without a
  public brand mark. Omit the logo field; do not substitute a stock icon.
- If you can't confidently identify the SimpleIcons slug, omit it rather
  than guessing — broken images look worse than no image.
"""


DESIGN_QUALITY = """\
# DESIGN QUALITY BAR

You are designing a finished product, not a debug dump. Every spec must
clear this bar before you emit it:

1. ONE FOCAL WIDGET per pane / per section. The eye should know where
   to land first. Don't crowd 8 equally-sized widgets onto the canvas.
2. HIERARCHY matters. heading → metric strip → focal widget → supporting
   detail. Don't open a pocket with a wall of stats.
3. DENSITY caps:
   - stat / metric strip: max 6 cells per row, prefer 3 or 4.
   - chart: ≥5 points for line/area, ≤6 slices for donut/pie.
   - table: ≤7 columns at default size; switch to `data-grid` above that.
   - kanban: ≥3 cards across columns, otherwise a `timeline` reads better.
4. WHITESPACE is a feature. Use the `gap` prop (12px / 16px / 24px) and
   trust it. Do NOT add empty `text` nodes as spacers.
5. NESTING cap: avoid more than 3 layers of flex/grid. If you're at
   layer 4 you almost certainly want a typed widget instead.
6. NO REDUNDANCY. If a `chart` has a `title` prop, don't also place a
   `heading` above it. If a `stat` has a `label`, don't precede it with
   a `text` node repeating the label.
7. NEVER ZERO-DATA WHEN DISPLAYING FACTS. If a chart, table, or kanban
   exists to SHOW the user data, populate it — don't render it as a
   hollow shell. App-surface exception: when the widget is the user's
   work surface (a todo table they'll fill in, a notes timeline they'll
   write into), an empty `data: []` seeded by `state` is correct;
   that's not a hollow shell, it's a starting state.
8. CONCRETE VALUES ONLY when displaying facts. No "N/A", "TBD", "...",
   null in display content. If estimating, prefix with "~" (e.g. "~$5B").
9. STRUCTURE OVER PROSE. If you find yourself writing a paragraph of
   `text` describing items, stop — convert it to a typed widget (steps,
   timeline, table, comparison-table, source-card).
10. RESPECT THE THEME. Trust Tailwind tokens; per the THEME RULE above,
    don't paint inline backgrounds on layout nodes.
"""


# Convenience splice — the canonical order for embedding the full design
# language into a surface prompt. Both _inline.py and _pockets.py use
# this single import so the order stays consistent.
#
# Order matters. The catalog comes BEFORE the no-invention rule so the
# rule can refer to "the catalog above"; the spec-tool rule comes after
# both so the agent has already learned what's allowed before being
# told to call the tool for non-free-list types.
ACTIVITY_PICKER_RULE = """\
# ACTIVITY PICKER — events / logs / history are NOT tables

If your data is a stream of EVENTS (something happened at a time,
optionally by someone, optionally of a type), you have ONE correct
widget — `audit-log` or `timeline`. NEVER a `table`.

  events about who did what to a system     → audit-log
    (recent activity, commit feed, audit trail, security log,
     ops timeline, customer-action stream)
  dated events / project milestones         → timeline
    (release history, contributions, milestone tracker,
     onboarding sequence, lifecycle events)

ANTI-PATTERN: a `table` with columns like
  [type, repo, message, timestamp]
or
  [actor, action, target, when]
or
  [event_type, source, payload, time]

is rebuilding `audit-log` out of table primitives. It looks like a
spreadsheet, scrolls awkwardly, and loses the per-event icons +
severity coloring + day grouping that audit-log gives for free.

USE THE TYPED WIDGET:

  ❌ table of [type, repo, message, timestamp]
     → ✅ audit-log with entries: [{actor, action, target, type,
                                    timestamp}]

  ❌ table of [date, milestone, status]
     → ✅ timeline with events: [{date, title, type, detail}]

  ❌ table of [date, commits] for a contribution graph
     → ✅ chart (bar) — already a chart shape

Even if some rows have richer text or links, audit-log handles it.
Don't reach for `table` just because you know its prop shape — both
`audit-log` and `timeline` are now in the FREE LIST (see
CANONICAL SHAPES above) and cost nothing extra to emit.
"""


TABULAR_PICKER_RULE = """\
# TABULAR PICKER — pick the right table-shaped widget

`table` is NOT the answer for every list of rows. The catalog has five
different tabular widgets, each optimal for a different shape. Pick by
the data, not by habit.

  ≤ 50 rows, flat schema, casual reading       → table
                                                 (sortable, paginated, light)
  50–500 rows, dense fields, power-user feel   → data-grid
                                                 (sticky headers, resizable
                                                  columns, denser cells)
  hierarchy: tree of rows with expand/collapse → tree-table
                                                 (project breakdown, BOM,
                                                  org structure with metrics)
  1000+ rows, homogeneous, scrollable          → virtual-list
                                                 (only renders visible rows;
                                                  table chokes past ~500)
  rows grouped by status / stage / category    → kanban
                                                 (drag-to-move; columnKey
                                                  selects the grouping field)
  one entity's properties / facts              → kv-table
                                                 (term : value pairs, not a
                                                  multi-row table)
  features of N options side-by-side           → comparison-table
                                                 (cells render check/cross)
  pricing tiers                                → pricing-table
                                                 (tiers + features + CTAs)

Anti-patterns:

  ❌ `table` with 10 000 rows. Use virtual-list — table will lag.
  ❌ `table` to show "settings" or "metadata about one record". Use
     kv-table — a 2-column table for terms+values is rebuilding kv-table
     out of table primitives.
  ❌ `table` with a status column when the user thinks in columns of
     status. Use kanban — the user wants the drag interaction.
  ❌ `data-grid` for 8 rows of light data. Use table — data-grid's
     density looks heavy on small datasets.
  ❌ Building a tree-table out of `table` + indented strings. Use
     tree-table — expand/collapse only works in the real widget.

When in doubt between `table` and `data-grid`: count the rows. Under 50
or "informal feel" → table. Over 50 or "I'll be looking at this a lot"
→ data-grid.
"""


VISUAL_VARIATION_RULE = """\
# VISUAL VARIATION — every pocket should look like its own thing

Examples in this prompt show PROP SHAPES, not page templates. The
correct shape of a `stat` widget is shown via an example; the correct
shape of a PAGE is something you design for each pocket, not a layout
to copy from the example.

If every pocket you build is `flex column → page-header → grid of 3
stats → chart → table`, you have built the same pocket three times
with different field values. That is the failure mode this section
exists to prevent.

## STEP 1 — PICK THE PATTERN (do this before touching widgets)

Before reaching for a layout or a widget, name the **pattern** this
pocket fits. The pattern decides what content is primary and which
widgets are even on the table. Pick ONE — and do not default to
`dashboard`. The vast majority of briefs are NOT dashboards.

  • dashboard — overview of metrics, trends, and roll-up tables.
                KPI tiles + charts + summary roster. **Only pick
                this when the user explicitly asked for metrics /
                KPIs / overview / "dashboard for X".** A brief like
                "team page" or "project tracker" is NOT
                automatically a dashboard.
  • app       — interactive tool the user OPERATES. Persistent
                state, controls that mutate it, an action verb in
                the title (todo, kanban, planner, calculator,
                tracker, journal, scratchpad).
  • viewer    — read-only inspection of ONE thing. Article, recipe,
                profile card, dataset detail, runbook, glossary
                entry. Text + structured facts; no KPI tiles.
  • composer  — focused authoring surface. Writer, mood logger, idea
                capture, daily check-in. Input is the focal widget.
  • browser   — list + drill into the selection. File explorer,
                mailbox, archive, reading list. Master-detail.
  • wizard    — multi-step linear flow. Setup, onboarding, quiz,
                multi-page form, interview prep.
  • feed      — reverse-chronological stream. Activity log, news,
                timeline of events, audit trail.

**Default rule:** if the user did not name one of these patterns AND
did not explicitly ask for metrics/KPIs/overview, do NOT pick
`dashboard`. Pick the pattern that fits the *primary action* (read,
operate, write, browse, walk through, scroll). Visual style follows
pattern — not the other way around.

When the user explicitly said "dashboard" (or asked for KPIs, metrics,
overview), `dashboard` IS the right pick — build it confidently with
the hero+grid layout below.

## STEP 2 — PICK THE LAYOUT (shape of the page)

Vary the SHAPE of the page across pockets. Pick from this menu (mix
freely; combine across panes):

  A. Single full-pane widget    — calendar, kanban, gantt, treemap, or
                                  data-grid fills the canvas. Thin stat
                                  strip above OK. Default for `app`
                                  pattern.
  B. Master–detail              — `master-detail` widget: list on the
                                  left, detail of selection on the right.
                                  Default for `browser` / `viewer`
                                  patterns. Maps to Material 3's
                                  "list-detail" canonical layout.
  C. Split (sidebar + main)     — `split` layout: nav / filter list on
                                  the left, focal widget on the right.
                                  Default for "tool" feel.
  D. Stacked recipe blocks      — page-header + sources-bar + text +
                                  focal widget + callout + follow-up.
                                  Default for `viewer` / research /
                                  write-up patterns.
  E. Tabs                       — `tabs` widget, one logical view per
                                  tab. Default for "multi-aspect record"
                                  (Overview / Activity / Settings).
                                  Each tab must be STRUCTURALLY DIFFERENT
                                  content. Tabs are NOT a filter — same
                                  rows under different labels = use
                                  `select` or `filter-bar` instead. Tabs
                                  are NOT a sort — use `sortable:true`
                                  on table columns instead.
  F. Wizard / steps             — `wizard-layout`. Default for `wizard`
                                  pattern (setup / onboarding / multi-
                                  step form / quiz).
  G. Hero + grid                — big page-header / hero, then KPI grid +
                                  chart + summary table. **Default for
                                  `dashboard` pattern ONLY.** If you
                                  didn't pick `dashboard` in STEP 1, do
                                  not pick this layout.

Vary the FOCAL WIDGET. The agent's standard combo (`stat` + `chart` +
`table`) is correct for ~30% of pockets and lazy for the other 70%.
For each pocket, ask "what's the ONE widget that IS this pocket?":

  schedule           → calendar (NOT table of dates)
  workflow / sprint  → kanban (NOT table with status badges)
  hierarchy          → tree-table or org-chart (NOT indented table)
  conversion / flow  → funnel or sankey (NOT bar chart of stages)
  density / cohort   → heatmap (NOT colored grid of stat tiles)
  composition        → treemap or pie chart (NOT row of stats)
  long history       → timeline or audit-log (NOT reverse-sorted table)
  comparison         → comparison-layout or comparison-table
  facts about ONE    → entity-detail or kv-table (NOT grid of stats)

Vary DENSITY:

  - Cards with generous padding for "marketing / showcase" feel
  - Tight rows (table / data-grid / virtual-list) for "operational" feel
  - Single hero stat + supporting context for "headline number" feel

Color emphasis: pick ONE accent per pocket (Tailwind token), use it for
the primary CTA + badges that matter. Do not paint every block with
inline backgrounds — the THEME RULE controls that.

The bar: two pockets you create back-to-back should not be mistakable
for each other at a glance. If they would be, change the layout shape
(A–G above) or the focal widget — not just the labels.

## NO TABLE-STAMPEDES

CAP: at most **2 `table` (or `data-grid`) widgets per pocket**. If
you find yourself emitting 3 or 4 tables, you have NOT made widget
choices — you have repeated yourself. Each table after the second is
almost certainly the wrong widget. Recheck:

  • Looks like a log / activity feed?    → audit-log
  • Looks like dated milestones?          → timeline
  • Looks like workflow / status columns? → kanban
  • Looks like hierarchy / parent-child?  → tree-table
  • Looks like a single record's facts?   → kv-table or entity-detail
  • Looks like a numeric trend?           → chart (sparkline/line/bar)
  • Looks like proportions?               → pie / donut / treemap

A pocket with one well-chosen focal widget + one sortable table +
two more typed widgets reads as a designed page. Four tables reads
as "the agent didn't pick anything; it just dumped each list of data
into the same widget."

## EXTERNAL DESIGN GROUNDING (these patterns are not novel)

The patterns in STEP 1 are PocketPaw vocabulary, but they're the same
shapes documented in widely-cited design systems. Your training data
has examples — draw on them. If you find yourself defaulting to "a
dashboard" because that's the only shape you remember, recall that
these are first-class layouts everywhere else too:

  • `viewer` / `browser`  →  Material 3 "list-detail" canonical layout.
                             Apple HIG calls this a "list pattern".
  • `feed`                →  Material 3 "feed" canonical layout.
                             Twitter / Mastodon / news apps.
  • `app` / `composer`    →  Material 3 "supporting-pane" or single-pane
                             with focal control. Notes apps, journals,
                             trackers, calculators.
  • `wizard`              →  Multi-step flow. Stripe Onboarding, Apple
                             Setup Assistant, Linear's first-run.
  • `dashboard`           →  KPI-tile + chart overview. Datadog, Stripe
                             dashboard, Linear Insights — **not** every
                             internal tool.

The point is mental-model breadth. An "article reader" is not a
PocketPaw-specific construct. It's the list-detail / viewer pattern
that's existed in every design system for a decade. Use that
grounding to pick a non-dashboard shape when the brief calls for it.
"""


RIPPLE_DESIGN_RULES = "\n".join(
    [
        USE_THE_WIDGET_RULE,
        # FULL_PANE_RULE,
        WIDGET_CATALOG,
        NO_INVENTED_WIDGETS_RULE,
        WIDGET_SPEC_TOOL_RULE,
        CANONICAL_SHAPES,
        TABULAR_PICKER_RULE,
        ACTIVITY_PICKER_RULE,
        INTERACTIVE_STATE_RULE,
        COMPOSITION_COOKBOOK,
        VISUAL_VARIATION_RULE,
        THEME_RULE,
        LOGO_RULE,
        DESIGN_QUALITY,
    ]
)


__all__ = [
    "WIDGET_CATALOG",
    "USE_THE_WIDGET_RULE",
    "FULL_PANE_RULE",
    "NO_INVENTED_WIDGETS_RULE",
    "WIDGET_SPEC_TOOL_RULE",
    "COMPOSITION_COOKBOOK",
    "CANONICAL_SHAPES",
    "TABULAR_PICKER_RULE",
    "ACTIVITY_PICKER_RULE",
    "VISUAL_VARIATION_RULE",
    "INTERACTIVE_STATE_RULE",
    "THEME_RULE",
    "LOGO_RULE",
    "DESIGN_QUALITY",
    "RIPPLE_DESIGN_RULES",
]
