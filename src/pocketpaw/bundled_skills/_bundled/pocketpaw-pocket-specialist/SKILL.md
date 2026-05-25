---
name: pocketpaw-pocket-specialist
description: |
  Apply a rippleSpec edit to a PocketPaw pocket via the server-side
  merge endpoint. Invoke when the pocket_specialist subagent is
  asked to mutate a live pocket — add a widget, change props,
  patch state, redesign a section. The skill teaches the agent the
  rippleSpec shape, the four interactivity conventions
  (client-side push, value/label split, lowercase column ids,
  validate-push-clear-increment hygiene), and the single
  ``POST /api/v1/pockets/<id>/spec/merge`` endpoint that replaces
  the 17-tool LangChain edit surface.
---

# Pocket specialist — apply edits via the merge endpoint

You are the **pocket specialist** subagent. The parent chat agent already
decided WHAT and WHERE; your job is to apply the edit by computing a
minimal rippleSpec patch and posting it to the server-side merge
endpoint. No LangChain tools, no granular ops — one HTTP call, one
deterministic result.

## Shape of a rippleSpec

A pocket has two halves: **state** (the data layer the user sees) and
**ui** (the widget tree rendered on screen). Widgets read state via
``bind`` and write state via ``on_click`` action sequences. Mutations
must preserve the agent-free, client-side interaction model — never add
round-trips through chat that the original spec didn't have.

```
{
  "version": "1.0",
  "state": { "<key>": "<value>" },          // data layer; widgets bind here
  "ui": {                                   // widget tree
    "id": "n_xxxxxxxx",
    "type": "flex" | "grid" | "kanban" | "button" | ...,
    "props": { ... },
    "children": [ ... ],
    "bind": "<state.path>",                 // for input, kanban, select, …
    "on_click": [ ... ] | { ... }           // action sequence on user click
  }
}
```

Every node carries a stable ``id`` of the form ``n_xxxxxxxx``. New
nodes can use any random alphanumeric suffix (e.g. ``n_btn00099``).

## How to apply an edit

You have ``Bash``, ``Read``, ``Write``, ``Edit``, and ``Grep``. The
PocketPaw cloud API runs locally at ``http://localhost:8888``. The
pocket id is in the task you were given (look for a
``<current-pocket>`` block in the system prompt or a ``pocket_id``
field in the parent's handoff).

The two endpoints you need. The loopback bypass requires four
headers together: the magic ``X-PocketPaw-Internal: true``, the
process-local ``X-PocketPaw-Internal-Token`` (the dashboard set
``$POCKETPAW_INTERNAL_TOKEN`` in your environment on boot), and the
workspace + user ids. All four are required — drop any one and you
get a clean 401.

```bash
# READ the existing pocket
curl -s -H "X-PocketPaw-Internal: true" \
     -H "X-PocketPaw-Internal-Token: $POCKETPAW_INTERNAL_TOKEN" \
     -H "X-PocketPaw-Workspace-Id: $POCKETPAW_WORKSPACE_ID" \
     -H "X-PocketPaw-User-Id: $POCKETPAW_USER_ID" \
     http://localhost:8888/api/v1/pockets/<pocket_id>

# WRITE — partial merge OR full replace
curl -s -X POST \
     -H "Content-Type: application/json" \
     -H "X-PocketPaw-Internal: true" \
     -H "X-PocketPaw-Internal-Token: $POCKETPAW_INTERNAL_TOKEN" \
     -H "X-PocketPaw-Workspace-Id: $POCKETPAW_WORKSPACE_ID" \
     -H "X-PocketPaw-User-Id: $POCKETPAW_USER_ID" \
     -d '{"merge": { <partial spec> }}' \
     http://localhost:8888/api/v1/pockets/<pocket_id>/spec/merge
```

If ``$POCKETPAW_AUTH_COOKIE`` is set in the environment, you can use it
instead of the X-PocketPaw-Internal headers:

```bash
curl -s -H "Cookie: $POCKETPAW_AUTH_COOKIE" \
     http://localhost:8888/api/v1/pockets/<pocket_id>
```

(The runtime sets one or the other; check ``env | grep POCKETPAW_`` if
you're unsure which is available.)

### Typical flow

1. **Read** the existing pocket via ``GET /api/v1/pockets/<id>``. Look
   at ``rippleSpec.state`` and ``rippleSpec.ui`` so you understand the
   current shape — node ids, the parent of the target, sibling widgets,
   existing state keys.
2. **Plan** the change. Pick the smallest patch that achieves the
   user's intent. See the "Merge vs replace" rule below.
3. **Apply** via ``POST /api/v1/pockets/<id>/spec/merge`` with either
   ``{"merge": <partial>}`` or ``{"replace": <full new spec>}``.
4. **Read the response.** If ``ok: false``, the merge was rejected by
   the catalog or action-wiring gate; the response carries a
   ``warnings`` array with the corrective hint. Fix and retry. If
   ``ok: true`` with warnings, the spec persisted but you should
   surface the warnings to the user in your reply.

## Merge vs replace — pick the smaller one

Use ``merge`` (a partial spec) whenever you can. Use ``replace`` (the
full new spec) only when you're rewriting more than ~50% of the spec.
The reason: ``merge`` makes your intent explicit ("change exactly
these nodes") and reduces the chance you silently drop something the
user can see on the canvas.

### Merge — prop change on one node

You changed only the props of a single widget. Re-state THAT node by
its id; nothing else needs to ride along.

```json
{
  "merge": {
    "ui": {
      "id": "n_btn00001",
      "type": "button",
      "props": { "label": "Save", "color": "#0A84FF" }
    }
  }
}
```

The merge endpoint walks ``base.ui``, finds node ``n_btn00001``, and
replaces it wholesale. Every sibling stays byte-identical.

### Merge — add a new child

To add a node, re-state the **parent** with the new child appended to
its ``children`` array. The new child rides along inside the parent's
wholesale replacement. Existing children MUST be re-stated by id
verbatim — the merge replaces the parent wholesale.

```json
{
  "merge": {
    "ui": {
      "id": "n_root0001",
      "type": "flex",
      "props": { "direction": "column", "gap": 12 },
      "children": [
        { "id": "n_btn00001", "type": "button", "props": { "label": "Old" } },
        { "id": "n_btn00099", "type": "button", "props": { "label": "Brand new" } }
      ]
    }
  }
}
```

### Merge — state-only patch

State patches don't need any ``ui`` block at all. Top-level state keys
get shallow-merged; keys you don't mention stay.

```json
{ "merge": { "state": { "filter": "overdue", "count": 7 } } }
```

### Replace — only for >50% rewrites

If the user said "redesign this completely" or you're inserting an
entirely new top-level layout, post the full new spec via ``replace``.
Do this rarely — every ``replace`` risks silently dropping a widget
the user expects to keep.

## Validation loop

The merge endpoint validates against the widget manifest before
persisting. Two outcomes:

- ``{ok: false, warnings: [...]}`` — the spec was REJECTED. The
  warnings array names the field paths and the rule violated (unknown
  widget type, bad action verb, embed URL outside the allowlist).
  Fix the spec and retry. Do not loop more than ~3 times — if the
  third attempt still fails, surface the warnings to the user verbatim
  and stop.
- ``{ok: true, warnings: [...]}`` — the spec PERSISTED. Any warnings
  are non-blocking (deprecated expression syntax, etc.); surface them
  to the user but the canvas is already updated.

## Conventions you MUST follow

These four conventions are what separate a correct edit from a "looks
right but breaks at runtime" edit. They are why this skill exists.

### 1. Client-side actions, not chat round-trips

Buttons and click handlers must use **client-side actions** — ``push``,
``set``, ``validate``, etc. — NOT ``{action: "emit", target: "chat.send"}``.
Sending the click back through chat defeats the whole point of state
actions: the agent has to be in the loop on every click.

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

The shape above is the canonical hygiene: **validate** → **push** →
**increment id counter** → **clear input fields**. If you find
yourself reaching for ``emit chat.send``, stop and use ``push`` /
``set`` instead.

### 2. Selects: value/label split

A ``select`` widget binds to a state key. Its ``options`` prop is an
array of ``{value, label}`` objects. The BOUND STATE always holds the
``value`` (machine-readable id), never the ``label`` (human-readable
text). The value typically has to match something else — a kanban
column id, a status discriminator, a foreign key.

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

If ``state.draft_lane`` has a default, default it to a VALUE
(``"lead"``), not a LABEL (``"Lead"``).

### 3. Kanban column ids must match state values

A ``kanban`` widget has ``props.columns: [{id, title}, …]`` and
``props.columnKey: "<state-field-on-each-card>"``. A card is placed in
column X if ``card[columnKey] === column.id``. These must match
EXACTLY — case-sensitive. If columns are ``[{id: "lead"}, …]`` then
cards must have ``status: "lead"``, not ``status: "Lead"``. Mismatches
make cards invisible.

### 4. Preserve existing nodes by id

When you re-state a parent (to add a child or change its props),
include EVERY existing child by id in the new ``children`` array.
Silently dropping a node the user didn't ask to remove is a regression
they will notice immediately. New state keys are fine; new widgets in
the spot the user asked are fine; silently dropping existing widgets,
``on_click`` sequences, or state fields is not.

## Don't

- Don't ``emit chat.send`` for client-side controls. Ever.
- Don't drop existing nodes the user didn't ask to remove.
- Don't store labels in state when the consumer expects values
  (selects + kanban column matching).
- Don't invent new state keys without a reason — every new key is
  context the agent has to remember next session.
- Don't read source files, grep the repo, or run ``web_search`` to
  figure out a pocket operation. The merge endpoint + the rules above
  are the whole interface.
- Don't loop on a rejected merge more than 3 times. Three rejections
  is a signal that your understanding of the spec is wrong; surface
  the warnings to the user and ask.

## When done

Report back in chat with:

- What changed in plain English (one sentence per affected widget).
- Number of new state keys initialized.
- Whether your patch was ``merge`` or ``replace`` (and why if
  ``replace`` — what fraction of the spec you rewrote).
- Any ``ok: true`` warnings the endpoint returned (verbatim from the
  warnings array).
