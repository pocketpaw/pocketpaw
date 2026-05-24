---
name: pocket-edit
description: Edit a PocketPaw pocket — change widgets, add controls, mutate state. Invoke when the user asks to change something on the canvas they're looking at.
---

# pocket-edit — edit a Ripple pocket cleanly

A Ripple pocket has two halves: **state** (the data layer the user sees) and
**ui** (the widget tree rendered on screen). Widgets read state via `bind`
and write state via `on_click` action sequences. Mutations should preserve
the agent-free, client-side interaction model — don't add round-trips
through chat that the original template didn't have.

## How to edit

You have Read, Write, and Bash. The pocketpaw cloud API runs at
`http://localhost:8888`. The pocket id is in the user's task. Cookie auth
is in the calling browser; for this measurement run you'll write your
proposed new spec to a JSON file and the experimenter will verify it.

Typical flow:

1. `Read` the current spec from the path the experimenter gave you.
2. Plan the change — decide which nodes need adding/replacing, which state
   keys need initializing.
3. `Write` the new full spec to the output path. Re-emit the whole thing,
   not a partial (the experimenter is testing the whole-spec emit path).

## Spec shape

```
{
  state: { <key>: <value> },                  // data layer; widgets bind to this
  ui: {                                       // widget tree
    type: "flex" | "grid" | "kanban" | ...,
    props: { ... },
    children: [ ... ],
    id: "n_xxxxxxxx",
    bind?: "<state.path>",                    // for widgets like input, kanban
    on_click?: [ ... ] | { ... }              // action sequence on user click
  }
}
```

Every node needs a stable `id` of the form `n_xxxxxxxx`. New nodes can use
any random alphanumeric suffix.

## Action sequences (what `on_click` should look like)

The whole point of state actions is they run **client-side** — no agent
involvement, no network round-trip per click. The original kanban-board
template's "Add card" button used this exact pattern:

```json
"on_click": [
  { "action": "validate", "condition": "{state.draft.length > 0}",
    "message": "Type a card title first" },
  { "action": "push", "target": "cards",
    "value": { "id": "c-{state.next_id}", "title": "{state.draft}",
               "status": "{state.draft_lane}", "assignee": "" } },
  { "action": "set", "target": "next_id", "value": "{state.next_id + 1}" },
  { "action": "set", "target": "draft", "value": "" }
]
```

Notice: `validate` → `push` → increment id → clear input. Each step is
one of Ripple's built-in actions. **Do NOT** wire a button's `on_click`
to `{action: "emit", target: "chat.send"}` — that defeats the entire
point of state actions and forces the agent into the loop on every
click. If you find yourself reaching for `emit chat.send`, stop and use
`push` / `set` instead.

## Selects and the value/label rule

A `select` widget binds to a state key. Its `options` prop is an array of
`{value, label}` objects. The BOUND STATE always holds the `value` (the
machine-readable id), not the `label` (the human-readable text). This
matters because the value typically has to match something else — e.g. a
kanban column's `id`, a fabric link key, or a CRUD discriminator.

```json
{
  "type": "select",
  "bind": "draft_lane",
  "props": {
    "options": [
      {"value": "lead",      "label": "Lead"},
      {"value": "qualified", "label": "Qualified"},
      {"value": "proposal",  "label": "Proposal"},
      {"value": "won",       "label": "Won"}
    ]
  }
}
```

If `state.draft_lane` defaults to anything, default it to a VALUE
(`"lead"`), not a LABEL (`"Lead"`). A common shared pattern: define
`state.lane_options` once, then reference it in the select via
`"options": "{state.lane_options}"`. That gives kanban columns and the
select a single source of truth for the lane vocabulary.

## Kanban column ids must match state's status values

A `kanban` widget has `props.columns: [{id, title}, ...]` and
`props.columnKey: "<state-field-on-each-card>"`. A card is placed in
column X if `card[columnKey] === column.id`. **These must match exactly.**
If columns are `[{id:"lead"}, ...]` then cards must have `status:"lead"`,
not `status:"Lead"`. Mismatches make cards invisible.

## When you add a new node, preserve the surrounding tree

You're going to rewrite the whole spec, so it's tempting to drop nodes
you don't think are needed. Don't. The user can see what's on the
canvas right now and will notice anything missing. Keep every existing
node by id, mutate only what the user asked you to. New state keys are
fine; new widgets in the spot the user asked are fine; silently dropping
existing widgets, on_click sequences, or state fields is not fine.

## Output

Write your proposed new full rippleSpec (the contents of the `rippleSpec`
field on the pocket — the thing with `state` and `ui`) as JSON to the
path the experimenter named. Don't include the pocket envelope (`name`,
`color`, `metadata`, etc.) — just the rippleSpec contents. Pretty-printed
is fine.

Then in your chat response back to the experimenter, give a short report:
- Number of new nodes added.
- Number of new state keys initialized.
- The exact `on_click` array on the new button (so the experimenter can
  see whether you used client-side actions or `emit chat.send`).
- Anything you noticed about the existing spec that surprised you.

## Don't

- Don't use `{action: "emit", target: "chat.send"}` for client-side
  controls. Ever.
- Don't drop existing nodes the user didn't ask to remove.
- Don't store labels in state when the bound widget consumer expects
  values (selects + kanban column matching is the canonical example).
- Don't invent new state keys without a reason — every new key is
  context the agent has to remember next session.
