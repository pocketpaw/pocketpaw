---
name: pocketpaw-edit-pocket
description: |
  Edit an existing PocketPaw pocket — add / remove / rename / move /
  redesign widgets, or mutate state — by delegating to the pocket
  edit specialist. Invoke when the user asks to change something on
  the canvas they're currently looking at: "add a chart", "remove
  that card", "mark task 1 as done", "filter to overdue", "rebuild
  this as a kanban", "make this less cluttered". The chat agent
  decides WHAT and WHERE; the specialist applies the granular
  mutations via ``pocket_specialist__edit``. The skill body sits
  outside the chat agent's system prompt until invoked, keeping
  always-on context lean.
---

# Pocket Edit Workflow

The user is inside an existing pocket — its id is in the chat
context (look for a ``<current-pocket>`` block in the system prompt
or pass through from the chat surface). Your job: route the user's
message into one of three paths and delegate the mutation work to
the specialist when needed.

## STEP 1 — Pick the path (READ / EDIT / CHAT)

Three valid response paths. Decide before reaching for any tool.

### READ
The user wants information that's already on the canvas.

  ✓ "what's in this pocket?"
  ✓ "summarize the activity feed"
  ✓ "which deals are in negotiation?"
  ✓ "what does the chart show?"

→ Call ``mcp__pocketpaw_pocket__get_pocket`` once with the pocket id.
   Answer from the returned ``rippleSpec.ui`` / ``rippleSpec.state``.
   Do NOT call any mutation tools — this is read-only.

### EDIT
The user wants the canvas to change.

  ✓ "add a chart for revenue"
  ✓ "remove that card"
  ✓ "rename the table header"
  ✓ "mark task 1 as done"
  ✓ "filter to overdue only"
  ✓ "clear the draft"
  ✓ "rebuild this as a kanban"
  ✓ "make this less cluttered"
  ✓ "switch the layout to tabs"

→ Use the **EDIT DECISION TREE** below to pick the right delegation
   shape, then call ``mcp__pocketpaw_pocket_specialist__edit``.

### CHAT
The message doesn't reference the pocket / widgets / data.

  ✓ "what's the weather like?"
  ✓ "tell me a joke"
  ✓ "summarize this article" (where "this" is a link, not the pocket)

→ Reply directly. Do not call any pocket tool. The pocket on the
   canvas isn't part of this message.

## STEP 2 — EDIT DECISION TREE (Type A / B / C)

Edit work is a two-agent flow: **you** decide what + where, the
**specialist** applies the change. Your preparation determines how
deterministic the specialist's run is.

### Type A — Simple state edit (intent is self-contained)

The user names what they want in a way that needs NO lookup:

  - "mark task 1 as done"
  - "filter to overdue only"
  - "clear the draft"
  - "set the title to 'Q3 Pipeline'"
  - "add Bob to the team list"

These map cleanly to ``set_state`` / ``append_state`` / ``remove_state``
without knowing the widget tree. Delegate with **intent only**:

```json
{
  "pocket_id": "<id>",
  "intent": "<verbatim user request>"
}
```

### Type B — Structural / disambiguation edit (needs widget tree)

The user references a widget that could be one of several, or asks
for a structural change ("add a chart", "remove that card", "rename
the table header"). The specialist would have to guess otherwise.

→ Call ``mcp__pocketpaw_pocket__get_pocket`` FIRST to see the
   structure. Then either:

**(a) Target is unambiguous** — pass it along by id:

```json
{
  "pocket_id": "<id>",
  "intent": "<verbatim user request>",
  "pocket": <pocket payload from get_pocket>,
  "target_node_ids": ["n_chart00", "n_table42"]
}
```

**(b) Target is ambiguous** (multiple matches) — ask the user ONE
tight question and wait:

  > "There are two charts on the page — the revenue one at the top,
  > or the channel breakdown below?"

Once they answer, proceed with ``target_node_ids`` set.

``target_node_ids`` tells the specialist EXACTLY which nodes to
touch — it does not search. This is the deterministic path.

### Type C — Open-ended redesign

  - "rebuild this as a kanban"
  - "make this less cluttered"
  - "switch the layout to tabs"
  - "convert to a dashboard view"

The specialist will replan most of the spec.

→ Pass ``pocket`` (so the specialist sees current state) but NOT
   ``target_node_ids`` (targets are everywhere). The specialist
   applies several ops in sequence.

## STEP 3 — Call the specialist

```
mcp__pocketpaw_pocket_specialist__edit({
  pocket_id:        "<the current pocket id>",      // required
  intent:           "<verbatim user request>",      // required
  pocket:           <get_pocket payload>,           // optional, Type B/C
  target_node_ids:  ["n_chart00", "n_table42"]      // optional, Type B(a)
})
```

The tool schema validates all four fields. Backwards-compatible with
intent-only calls (Type A).

After the call returns, give the user a **one-line summary** drawn
from the specialist's ``ops`` array in the return. Don't re-list
every op — the canvas already shows the result.

  ✓ "Marked task 1 as done."
  ✓ "Added a revenue chart below the KPI strip."
  ✓ "Rebuilt as a kanban with three columns."

## Hard rules

- **NEVER** call ``mcp__pocketpaw_pocket__set_state``,
  ``set_node_prop``, ``add_node``, ``move_node``, ``remove_node``,
  ``replace_node``, ``patch_state``, ``update_pocket``, or any other
  granular mutation tool. They are not on your allowlist in chat
  mode. Use ``pocket_specialist__edit`` for every edit, no matter
  how small.
- **NEVER** call ``pocket_specialist__create`` for edits. That tool
  spawns a brand-new pocket; the user is inside an existing one.
  Use the ``pocketpaw-create-pocket`` skill for fresh canvases only.
- **NEVER** ask more than 1 disambiguation question per turn. The
  user came here to edit, not be interrogated.
- If ``pocket_specialist__edit`` returns an error, surface it to the
  user and stop. Do NOT improvise with shell, files, or HTTP.
- **Always pass the pocket_id**. The current pocket is the one in
  the ``<current-pocket>`` block — don't ask the user.

## When NOT to use this skill

- The user is on the chat surface BUT not inside a specific pocket
  (no ``<current-pocket>`` block in context). Either ask which
  pocket they mean OR redirect to ``pocketpaw-create-pocket`` if
  they want a new one.
- The user wants a brand-new pocket ("create a sales dashboard").
  That's the create skill, not the edit skill — different specialist.
- The user wants to delete the pocket entirely. That's a workspace-
  level operation, not an edit; use the ``pockets`` admin surface
  instead.

## Related tools (also available via MCP)

- ``mcp__pocketpaw_pocket__get_pocket`` — fetch the current pocket's
  rippleSpec (the chat agent uses this in Type B/C edits to surface
  structure to the specialist)
- ``mcp__pocketpaw_pocket__list_pockets`` — list workspace pockets,
  useful when the user mentions a pocket by name and you need to
  resolve to an id
- ``mcp__pocketpaw_pocket_specialist__create`` — sister tool for
  creating a NEW pocket (the ``pocketpaw-create-pocket`` skill
  wraps this)
