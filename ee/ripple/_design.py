# ee/ripple/_design.py — Shared Ripple UI design rules.
# Licensed under FSL 1.1 — see ee/LICENSE.
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

WIDGET_CATALOG = """\
# WIDGET CATALOG

layout      flex, grid, card, container, tabs, accordion, split,
            master-detail, collapsible, separator, page-header, hero,
            section, app-shell, sidebar, breadcrumb
display     heading, text, badge, metric, stat, progress, progress-ring,
            avatar, image, markdown, code-block, code, kbd, icon,
            quote, highlight, definition-list, comparison-table,
            pros-cons, steps, status-dot, trend, link-preview, qr,
            diff, copy, chip, empty-state, loading
input       button, input, textarea, select, combobox, multi-select,
            checkbox, switch, radio-group, slider, rating, date-picker,
            time-picker, number-input, segmented, color-picker,
            file-upload, form, filter-bar
data        chart, table, data-grid, kanban, gantt, calendar, timeline,
            tree, tree-table, virtual-list, sparkline, gauge, funnel,
            heatmap, sankey, treemap
overlay     alert, callout, tooltip, popover, hover-card, dropdown-menu,
            toast, command-palette, context-menu, notification-center,
            error-state
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
  compare X vs Y / feature compare    → comparison-table
  sortable table / data grid          → data-grid
  audit log / activity log            → audit-log
  comments / discussion thread        → comment-thread
  command palette / cmd-k             → command-palette
  steps / how-to / wizard             → steps (NOT numbered text)
  pros vs cons                        → pros-cons
  file tree / folder tree             → tree
  hierarchical table                  → tree-table
  monitoring gauge / dial             → gauge
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
  big compare matrix                   → one full-pane `comparison-table`

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
  comparison X vs Y             → `comparison-table`
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
  inlined-shape data:   chart, table, kanban

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

`chart` — the prop is `type`, NOT `chartType`. The donut variant is
spelled `donut`, NOT `doughnut`:
  { "type": "chart", "props": {
      "type": "line",
      "data": [ { "label": "Jan", "value": 12000 },
                { "label": "Feb", "value": 15400 } ]
  }}
  - type: bar | line | area | pie | donut | candlestick | sparkline | heatmap | gauge | radar
  - data: [{label, value}] for most. candlestick: {label, open, high, low, close}.

`table` — `columns` are OBJECTS with `accessorKey`; `data` is an array
of OBJECTS keyed by accessorKey. NEVER pass `columns: [str]` +
`rows: [[]]` — the cells silently render empty:
  { "type": "table", "props": {
      "columns": [ { "accessorKey": "page",  "header": "Page" },
                   { "accessorKey": "views", "header": "Views" } ],
      "data": [ { "page": "/home", "views": "8,421" },
                { "page": "/pricing", "views": "5,312" } ]
  }}

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
  navigate / toast / emit / pin / unpin — host-handled events

## Toolkit — the expression language

Inside any string value the dispatcher resolves:

  {state.x}              — read state
  {state.x + 1}          — arithmetic (+ - * /)
  {state.x.length}       — property access
  {state.x > 0 ? a : b}  — ternary, comparisons, &&, ||, !
  {item} / {card} / {index}    — loop context
  {event}                — payload from on_change / on_submit

This is enough to build any add/remove/edit/toggle flow without
inventing new actions.

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
RIPPLE_DESIGN_RULES = "\n".join(
    [
        USE_THE_WIDGET_RULE,
        FULL_PANE_RULE,
        WIDGET_CATALOG,
        WIDGET_SPEC_TOOL_RULE,
        CANONICAL_SHAPES,
        INTERACTIVE_STATE_RULE,
        COMPOSITION_COOKBOOK,
        THEME_RULE,
        LOGO_RULE,
        DESIGN_QUALITY,
    ]
)


__all__ = [
    "WIDGET_CATALOG",
    "USE_THE_WIDGET_RULE",
    "FULL_PANE_RULE",
    "WIDGET_SPEC_TOOL_RULE",
    "COMPOSITION_COOKBOOK",
    "CANONICAL_SHAPES",
    "INTERACTIVE_STATE_RULE",
    "THEME_RULE",
    "LOGO_RULE",
    "DESIGN_QUALITY",
    "RIPPLE_DESIGN_RULES",
]
