# Pocket mutation SSE channel

The `pocket_mutation` SSE event is the cloud chat agent's narrow,
in-place patch signal for a pocket the user is currently looking at.
It rides on the same agent-stream as `chunk` / `tool_start` /
`stream_end`, and is what the paw-enterprise canvas re-renders from
without a full refetch.

## Producer

`ee/pocketpaw_ee/cloud/pockets/agent_context.py` is the only producer.
Every granular `*_for_agent` helper that mutates a pocket pushes one
`pocket_mutation` frame after the Beanie write succeeds. Two helpers
do the actual push:

- `_push_mutation_frame(op, view, payload)` — granular node / state ops
  use `PocketMutationFrame.to_wire()` to emit a flat dict.
- `_push_replace(view)` — full-document push for changes that don't
  map to a granular in-place op (top-level pocket fields, embedded
  widget add/remove, `rippleSpec.sources`, `rippleSpec.actions`).

Each push happens once per write. The coarse `pocket.updated` realtime
event the service layer emits in parallel is the refetch fallback for
clients that ignore the discriminated frame entirely.

## The `kind` discriminant (RFC 06 position #2)

Every frame carries a top-level `kind` field with one of three values:

| `kind`        | Emitted by                                              | Wire payload                                              |
|---------------|---------------------------------------------------------|-----------------------------------------------------------|
| `"structure"` | node / component-tree ops (`node_added`, `node_prop_set`, `node_moved`, `node_removed`, `node_replaced`, `node_prop_array_item_*`) | `op`, `node_id`, subtree, parent / index info             |
| `"data"`      | state-model ops (`state_set`, `state_appended`, `state_removed`, `state_patched`) | `op`, `path`, `value`, prior value                        |
| `"replace"`   | the full-document push (`_push_replace`): top-level pocket field updates, widget add/remove, `set_source` / `remove_source`, `set_action` / `remove_action` | `op == "replace"`, `pocket` (full resolved pocket view)   |

The split mirrors Ripple's two mutator modules (`spec-mutator.ts` for
structure, `state-mutator.ts` for data) and the A2UI
`updateComponents` / `updateDataModel` message families. A client that
branches on `kind` can:

- skip layout work entirely on `kind === "data"` (only data-bound
  widgets need to re-render);
- patch the touched subtree on `kind === "structure"`;
- swap the full pocket on `kind === "replace"`.

`"replace"` is a third value rather than folded into `"structure"`
because the wire payload genuinely is different — the whole pocket
document goes over the wire, not a subtree. Clients gate on it before
the structure-vs-data fork.

## Back-compat

The frame is purely additive:

- The legacy `action` key still rides on every frame (it equals `op`).
- The A2UI-style `family` key (`"updateComponents"` / `"updateDataModel"`)
  rides on granular structure / data frames for any consumer that
  already branches on it. It is omitted on `kind === "replace"`
  because the replace push isn't an A2UI granular op.
- Every payload key the historical un-discriminated frame carried at
  the top level still sits at the top level — `to_wire()` flattens
  `PocketMutationFrame.payload` next to the discriminants.

An op identifier with no known `kind` (a typo, a future op not yet
wired into `kind_for_op`) falls back to the legacy un-discriminated
flat frame in `_push_mutation_frame`. The canvas still gets the
signal; only the discriminant is missing — and the coarse
`pocket.updated` event still fires on the same write as the final
backstop.

## Follow-up

Client-side wiring is out of scope for this PR. The paw-enterprise
canvas continues to apply the frame the same way it did before — by
reading `action` + flat payload. The follow-up is to gate the canvas
on `kind` so structure changes re-run layout while data changes only
re-render data-bound widgets, mirroring Ripple's two-mutator split on
the consumer side too.
