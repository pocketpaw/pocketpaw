# Authoring test — pocket_specialist on real chat traffic

**Scope.** RFC 06 Position 1 spike. The spike's headline claim is that a
flat re-emit-by-name model collapses the agent's 8-op (actually 17-tool)
mutation surface into one rule, and that this collapse is the **real**
prize of the flat model — wire-size and patch-size are net negative, so
the bet only pays off if the agent's emit pipeline becomes meaningfully
simpler. This document captures one full session against the live
`pocket_specialist` to see whether that elegance argument survives
contact with the real model.

**Setup.** Local `pocketpaw serve` on `:8888` (branch `pr-1219`,
backend = `claude_agent_sdk`, model = `claude-sonnet-4-6`), live
`paw-enterprise` SvelteKit dashboard on `:5173`/`:1420`. Driven via
the MCP preview tools (real DOM, real chat WebSocket, real LLM). No
modifications to pocket_specialist's code or prompt — this is the
shape that ships today.

**Date.** 2026-05-24, ~16:28–16:32 IST.

## 1. What we asked for

One chat message:

> *Build a sales pipeline kanban dashboard with 4 columns: Lead,
> Qualified, Proposal, Won. Add 2 sample deal cards to each column
> showing company name, deal value in dollars, and contact person name.
> Use a clean enterprise look.*

This is deliberately representative — a layout the existing kanban
template gets ~80% right, with one structural delta (column labels and
ids) and bulk data fill (8 cards). It exercises the **edit path**, not
just the create path: the agent template-matches first, then has to
mutate.

## 2. What pocket_specialist actually did

Real-time chat transcript + server logs, condensed:

```
16:28:31  agent kicks off ("Template match: Kanban Board")
16:29:11  agent-mode template short-circuit → pocket created in 182ms
          → pocket_id=6a12d9ffae05f15284584408, kanban with default
            columns Backlog/In progress/Review/Done
16:29:33  pocket_router escalates request to Tier 2 (structural verb 'add'
          present → component-tree change → specialist owns structure)
16:30:02  agent emits patch_state({cards:[...], lane_options:[...]})
          ✗ pydantic rejects: 1 validation error for _PatchStateArgs
            field `partial` required — model forgot to wrap the payload
            in `{partial: {...}}`, emitted the dict bare
          → ops=0, success=False
16:30:14  agent retries patch_state with corrected shape
          ✗ server raises: cannot import name 'kind_for_op' from
            pocketpaw_ee.cloud.chat.agent_schemas
          → ops=0, success=False
16:30:24  agent tries set_state instead
          ✗ same import error
          → ops=0, success=False
16:30:44  agent tries set_node_prop on the kanban widget
          ✗ same import error
          → ops=0, success=False
16:30:56  pocket_router re-escalates ("structural verb 'add' present")
16:31:40  agent gives up on the edit path → calls create on a FRESH
          pocket with the entire correct spec inline
          → pocket_id=6a12da94ae05f15284584409 (duplicate, 1397B)
16:31:54  SDK stream finishes — 2375 events
```

Agent's own summary to the user (verbatim from the chat panel):

> *The agent-mode edit ops are hitting a backend import error on all op
> types. Trying subagent mode instead.* … *The edit backend has a
> runtime import error on all op types — I'll create a fresh pocket
> directly with the correct spec.*

## 3. The end state

Two pockets exist in the workspace, both named "Sales Pipeline":

| Pocket | Outcome | UI render |
|---|---|---|
| `6a12d9ffae05f15284584408` (broken edit) | `state.cards` + `state.lane_options` updated correctly via the first patch_state attempt's partial side-effect; kanban widget `props.columns` NEVER updated (every follow-up op failed) | Two empty columns "Backlog 0" + "In progress 0"; **all 8 cards invisible** because their `status` field ("lead"/"qualified"/…) doesn't match any column id in `props.columns` ("backlog"/"in_progress"/…) — they fall off the board entirely |
| `6a12da94ae05f15284584409` (recovery fresh) | Full spec emitted in one create call, sidestepping all edit ops | Correct: 4 columns Lead/Qualified/Proposal/Won, 8 cards visible and routed to the right columns |

JSON specs captured at `captures/broken-edit-pocket.json` and
`captures/recovery-fresh-pocket.json`. The diff is exactly two things:
the kanban node's `props.columns` array and the absence of the bulk
"add card" form (the template carried it; the fresh emit dropped it).

The user is left with one broken pocket cluttering their workspace and
a duplicate "Sales Pipeline" that works. In a real product session this
is a UX failure even though the agent technically recovered.

## 4. The surface area that produced this

From the parallel research agent's read of `pocket_specialist/tools.py`
and the prompt in `src/pocketpaw/ripple/_pockets.py`:

```
LAYER 1 — DATA (state)         LAYER 2 — APPEARANCE        LAYER 2.5 — PROP-ARRAY        LAYER 3 — STRUCTURE
  set_state                      set_node_prop               set_prop_array_item            add_node
  append_state                   replace_node                append_prop_array_item         move_node
  remove_state                                               remove_prop_array_item         remove_node
  patch_state                                                                              + 4 source/action binders
```

**17 tools across 3.5 layers.** The system prompt explicitly tells the
model "ALWAYS reach for the LOWEST applicable layer" with worked
examples. That layered cognitive model is precisely the load that has
to be re-derived correctly per mutation — and in this session, the
model chose `patch_state` (Layer 1) first when the change it needed to
make ("kanban widget shows wrong column labels") was actually a Layer 2
prop-replace on `n_no719i8s.props.columns`. The first try was
*wrong layer*; the second try was *correct shape but the server-side
import bug killed it*; the third+ tries were *correct intent but the
same import bug killed them too*.

## 5. What flat re-emit-by-name would have done

The minimum sufficient mutation under the flat model would be the
single patch:

```json
{
  "components": {
    "n_no719i8s": {
      "type": "kanban",
      "id": "n_no719i8s",
      "bind": "cards",
      "props": {
        "columnKey": "status",
        "columns": [
          {"id": "lead", "title": "Lead"},
          {"id": "qualified", "title": "Qualified"},
          {"id": "proposal", "title": "Proposal"},
          {"id": "won", "title": "Won"}
        ]
      }
    }
  }
}
```

236 bytes. One shape. No layer choice. No per-op pydantic schema. No
`kind_for_op` dispatch. The pydantic mismatch in failure #1 is
structurally impossible — there is no `partial` wrapper to forget,
because the whole patch IS the partial. The import bug in failures
#2-4 is structurally impossible — there is no per-op kind to dispatch
on, because there is one op.

For comparison, the data updates that DID succeed (via the first
patch_state's partial side-effect) would also be just one re-emit in
flat:

```json
{
  "state": {
    "cards": [ /* the 8 cards */ ],
    "lane_options": [ /* the 4 lanes */ ]
  }
}
```

(Note: flat's `state` lives at the spec envelope level, not inside
`components` — same as today.)

Total flat session for the same outcome: **one merge call**, ~1400
bytes wire. Today's session: 4 failed ops + 1 fresh create + 2 stranded
pockets, ~2900 bytes of wire traffic across 2375 SDK events, ~3 minutes
of LLM time.

## 6. Honest verdict

**The authoring claim is real, and stronger than the spike originally
argued.** I went in expecting Phase 1 to show "current emit works fine,
flat is an aesthetic preference." It did not. Phase 1 surfaced THREE
distinct failure modes of the current path in a single 60-second
session:

1. **Cognitive load on the model.** The 3-layer hierarchy + 17 tools
   produced a wrong-layer first choice (patch_state when set_node_prop
   on the kanban was the right call).
2. **Per-op schema fragility.** Each tool has its own pydantic shape
   (e.g. `patch_state` needs `{partial: {...}}`, not bare dict). The
   model forgot the wrapper. With flat's one schema, the error
   surface is one schema.
3. **Per-op server-side dispatch fragility.** A single missing import
   (`kind_for_op`) broke *every* granular op type for the entire
   session. With flat's one merge function, there is no per-op
   dispatch and this class of bug cannot exist.

Failure #1 is a "the model is fallible" cost that flat materially
reduces. Failures #2 and #3 are "the codebase has surface area" costs
that flat structurally eliminates. The +17-25% wire-size tax and
+70-100% patch-size tax (per `findings.md` §3) are the price; the
"agent emits cleanly" win is what we trade them for.

**However**, two genuine caveats remain:

- This session has a confound: the `kind_for_op` import bug
  (`/Users/prakash-1/Documents/paw-workspace/pocketpaw/ee/pocketpaw_ee/cloud/chat/agent_schemas.py`)
  is a *current* broken state of `pr-1219`, not an inherent property
  of the 8-op design. A fixed import would have made ops 2-4 succeed.
  We don't know whether they would have produced correct output —
  only the second op (patch_state with wrapper) might have, and it
  still wouldn't have updated the kanban widget's `props.columns`
  because that's a Layer 2 op, not Layer 1.
- Phase 2 (modifying pocket_specialist's prompt to emit flat patches
  and re-running the same task) was not executed. The verdict above
  is grounded in Phase 1 + the prompt analysis — a "what flat would
  have done" reasoning, not "what flat actually did under the same
  LLM." Phase 2 remains the cleanest way to make the comparison
  ironclad if the captain wants the additional signal.

**Recommendation:** the spike's PR-3 (in `findings.md` §8 — the
authoring-claim test gate) is now well-motivated by real evidence. The
question for the captain is not "should we test the authoring claim"
— this session already showed the current path has real, observable
brittleness — but "is the spike's 5-PR migration the right way to fix
it, or is the cheaper move to harden the existing 17-tool surface
(fix the import, tighten the per-op schemas, train the model on layer
choice)?" Both are defensible; the answer depends on whether the
elegance/robustness trade survives once the migration cost is paid.

I'd land the spike branch as a research artifact regardless, and run
Phase 2 (real flat-emit A/B against the same kanban task) before
greenlighting PR-1.

## 7. Artifacts

- `captures/broken-edit-pocket.json` — the pocket the edit ops broke.
- `captures/recovery-fresh-pocket.json` — the fresh pocket the agent
  created when it gave up.
- `findings.md` §1–§9 — the structural analysis the spike built on.
- `flatten.ts`, `RippleRenderer.svelte`, `streaming-sim.ts` — the
  proof-of-concept code (60 bun tests pass).
- Server log of the four-failure sequence is in
  `paw-enterprise` running process's stderr (not captured here — it
  scrolls off; the timestamps in §2 are exact).

Branch: `spike/flat-component-model`. HEAD before this commit:
`088b7e94`.
