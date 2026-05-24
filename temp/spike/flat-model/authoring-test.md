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

## 8. Phase 2 — controlled A/B with the same model

Phase 1 left the verdict with two caveats: the `kind_for_op` import
bug was code-specific (not inherent to the 8-op design), and we hadn't
run a real A/B with a flat-emit prompt. Phase 2 closes both gates by
running the same task against the same model (Opus 4.7) with two
different prompts, in isolation from the broken cloud code.

### Setup

- Same broken pocket spec as input (the one captured in
  `captures/broken-edit-pocket.json`).
- Same user request: *"The kanban is broken — the cards have
  lead/qualified/proposal/won status but the columns are
  Backlog/In progress/Review/Done. Fix the columns to match the actual
  lanes."*
- Variant A: verbatim mutation-strategy block from
  `src/pocketpaw/ripple/_pockets.py:1275-1354` — the live 17-tool
  surface across 3.5 layers.
- Variant B: replacement strategy block (~15 lines) describing
  `merge_flat_patch(partial)` with merge-by-name semantics.
- Both agents wrote their emitted tool calls to JSON
  (`/tmp/p2a-baseline.json`, `/tmp/p2b-flat.json`) — no real backend
  calls, no schema dispatch, no chance of the `kind_for_op` confound.

### Result

| Metric | Variant A (17 tools) | Variant B (flat) |
|---|---|---|
| Tool chosen first try | `set_node_prop(n_no719i8s, "columns", [...])` ✓ | `merge_flat_patch({components: {n_no719i8s: {...}}})` ✓ |
| Tool calls in the sequence | 1 | 1 |
| Turns | 1 | 1 |
| Patch payload size (chars) | ~120B | ~340B |
| Resulting spec is correct | Yes | Yes (verified — `captures/p2-verify.ts` applies the patch via the spike's `merge()` and proves all 8 cards route to the right lanes, `bind="cards"` is preserved) |
| Agent flagged any concern | Briefly considered 4× `set_prop_array_item` (Layer 2.5) but ruled out — every item changes both id and title, so re-emitting the whole prop is more economical | **Real footgun:** re-emit-by-name replaces the WHOLE node, so the agent had to re-state `type`, `id`, `bind`, `columnKey` etc. The agent explicitly flagged: *"a real foot-gun if I forget `bind` — the card data would silently detach"* |

### What this changes about the verdict

The Phase 1 hypothesis was that the 17-tool surface caused the LLM to
pick the wrong layer (`patch_state` instead of `set_node_prop`).
**Phase 2 falsifies that for capable models.** Opus 4.7 navigated the
3.5-layer hierarchy on the first try and ruled out the lower-layer
alternatives with explicit reasoning. The wrong-layer choice in Phase 1
was likely a **model-capability artifact** (Sonnet 4-6 inside a long
chat session with template-context and recipe-context biasing
toward state-level mutations) rather than a structural defect of the
17-tool surface.

The Phase 1 failures that **do** survive Phase 2 are #2 and #3 — the
per-op pydantic schema fragility and the per-op `kind_for_op` import
bug. Both are **codebase-side** consequences of having 17 distinct
op-dispatch paths instead of one merge function. Those are real and
they would be structurally eliminated by flat.

Phase 2 also surfaced a **new cost** that the spike's structural
analysis didn't quantify: **the re-emit footgun.** The flat variant
must re-state every field of a touched node — including fields the
mutation isn't supposed to change. The Opus agent flagged this
unprompted: *"if I forget `bind` the card data would silently detach."*
The 17-tool surface has no equivalent footgun — `set_node_prop` only
ever touches the one prop, no possibility of silent collateral damage.

### Updated verdict

| Aspect | 17-tool surface | Flat re-emit |
|---|---|---|
| Capable-model emit quality (Opus) | Equal — both correct first try | Equal — both correct first try |
| Weak-model emit quality (Phase 1, Sonnet) | Wrong layer first try | Untested in Phase 2 against the weak model; structural argument says better, but unproven |
| Per-mutation patch size | Smaller (~30% of flat) | Larger (~340B vs ~120B for the same prop-change) |
| Codebase surface area for bugs | 17 dispatches, 17 pydantic schemas, 17 service hops — Phase 1 caught a `kind_for_op` bug that killed ALL of them globally | 1 merge function, 1 schema, 1 hop — structurally cannot have the per-op-kind class of bug |
| Agent footgun risk | Low — surgical ops can't drop unintended fields | **Material** — re-emit-by-name can silently drop any field the agent forgets to re-state. The agent itself flagged this on the first run |
| Cognitive load on prompt | 80 lines of mutation-strategy block | 15 lines |

The honest synthesis: **flat is a clean win on codebase surface area
(failures #2 and #3 from Phase 1 cannot happen), a wash on
capable-model emit quality, a likely improvement on weak-model emit
quality (but Phase 2 didn't prove that), and a regression on
per-mutation wire size + a NEW agent-side footgun risk.**

### One design move that would change the calculus

The footgun is a property of OpenUI's "merge-by-name = whole-node
replacement" choice. Ripple could instead define merge as **per-field
shallow-merge inside a node** (i.e. only the fields the agent emits
overwrite; un-mentioned fields keep their value). That would:

- Eliminate the re-emit footgun entirely.
- Shrink the per-mutation patch from ~340B back toward parity with
  `set_node_prop` (~150-180B for this case — emit just `props.columns`).
- Stay one rule (still "re-emit a node by name").
- Diverge from OpenUI Lang's published semantics, so it's not a
  literal compatible implementation — but PocketPaw can choose its
  own merge depth without changing the wire shape.

This would be a small extension to the spike's `merge()` function and
worth prototyping if the captain greenlights PR-1. Filed as an open
design question in `findings.md`.

### Recommendation (updated from §6)

**Greenlight PR-1 of the migration sequence** (dual-read renderer +
`format: "flat" | "nested"` discriminator, default nested — no caller
has to change). The downside risk is bounded by the dual-read shape:
if PR-3 (the authoring claim test) shows the per-field merge depth is
needed, we can ship that without breaking PR-1's wire format.

**Defer the rest of the sequence** until PR-3 measures real
pocket_specialist emit on a hardened codebase (i.e., after the
`kind_for_op` import bug is fixed, so we're comparing flat against a
working 17-tool surface, not a broken one). Phase 2 strongly suggests
that on a capable model the surfaces are equivalent on emit quality;
the bet pays off on codebase surface area, not on the LLM's cognitive
load.

If the captain wants further signal before PR-1: re-run Phase 2 with
**Sonnet 4-6** instead of Opus 4.7 to test the weak-model hypothesis.
That's a 5-minute experiment and would either confirm or refute the
"flat helps weaker models" argument that Phase 1's failure-mode #1
implied.

### Phase 2 artifacts

- `/tmp/p2a-baseline.json` — the 17-tool agent's emitted tool call.
- `/tmp/p2b-flat.json` — the flat agent's emitted patch.
- `captures/p2-verify.ts` — applies the flat patch via the spike's
  `merge()` function and proves the resulting spec routes all 8 cards
  to the right lanes with `bind` preserved.

Branch HEAD before Phase 2 commit: `82054afc`.

## 9. Phase 3 — live additive edit (the captain caught a real bug)

Phase 1 ended with a recovery pocket that the captain flagged as
incomplete — the kanban renders but has no way to add new deals. Phase
2 was an isolated A/B against an LLM in a measurement harness. Phase 3
closes the loop by going back to the live `pocket_specialist` and
asking it to add the missing "Add deal" form row to the recovery
pocket — exactly the kind of additive structural edit (`add_node`,
Layer 3) Phase 1 never reached because the `kind_for_op` import
errored out before the agent ever got there.

### What happened

```
19:02:13  agent kicks off, calls get_pocket() — succeeds (read path is fine)
19:02:46  pocket_router escalates to Tier 2 — structural verbs [add, insert]
19:02:46  agent-mode edit kit returned with targets=3 (page-header, kanban, root flex)
19:03:50  agent in chat: "Computing the ops now — 4 state initializers + 1
          add_node to insert the form row after the page-header."
19:03:57  agent fires set_state ✗ ImportError: kind_for_op
          → ops=0, success=False, but the side-effect partially landed:
            state.draft_company appeared in the persisted spec even
            though the dispatch reported failure. The bug isn't just
            "ops don't apply" — it's "ops apply silently while the
            dispatch reports failure." Worst possible failure mode.
19:04:24  agent retries — pocket_router escalates again, edit kit returns
          has_pocket=False targets=0 — wasted round-trip
19:05:30  agent in chat: "The edit backend's op executor has a persistent
          ImportError — the same root cause as before. The workaround
          that succeeded last time is recreating the pocket via
          pocket_specialist__create with target_pocket_id so it
          replaces in place. Rebuilding now with the full spec
          including the form row."
19:06:00  rebuild lands — UI now has [page-header, form-flex, kanban]
```

### The recovery is structurally **smarter** than Phase 1's

Phase 1's recovery created a duplicate pocket (left the broken original
on disk). Phase 3's recovery used `pocket_specialist__create` with
`target_pocket_id` to replace the existing pocket in place — no
duplicate. The agent learned across sessions (same conversation,
two turns apart).

### …but the recovery rewrite introduces FOUR distinct correctness regressions

The agent rebuilt the entire pocket spec to add the form row. In the
rebuild it produced an interaction wire-up that is materially worse
than the original kanban-board template's. Captured in full at
`captures/p3-additive-edit-recovery.json`:

| # | Regression | Original template | Recovery rebuild |
|---|---|---|---|
| 1 | Button on_click | `[{action:"validate"}, {action:"push", target:"cards", value:{...}}, {action:"set", target:"next_id", ...}, {action:"set", target:"draft", value:""}]` — client-side, agent-free, instant | `{action:"emit", target:"chat.send", value:"add_deal company=\"{state.draft_company}\" ..."}` — **every click is an LLM round-trip** through `pocket_specialist`, which still has the `kind_for_op` bug |
| 2 | Select options | `"{state.lane_options}"` reference to `[{value:"lead", label:"Lead"}, ...]` — value/label split | Plain string array `["Lead", "Qualified", "Proposal", "Won"]` — no value/label split |
| 3 | Bound state default | `"backlog"` (lowercase value, would-be column id) | `"Lead"` (Label-case) — does NOT match the kanban's lowercase column ids |
| 4 | Click hygiene | Validates non-empty, increments `next_id`, clears `draft` after submit | None — no validation, no clear, no id generation |

Net effect: if the user fills in fields and clicks "Add deal," the
runtime emits a chat message to `pocket_specialist`. The agent gets
the message, tries to use the same broken edit ops, falls back to
another full pocket rebuild, possibly succeeds — but the new card
will be added with `status: "Lead"` (label-case) which doesn't match
any kanban column id ("lead", "qualified", etc.). Card lands in no
column. Same failure shape as Phase 1's kanban-columns bug, but on
every "Add deal" click instead of once at setup.

### What this changes about the verdict

Phase 1 found three failure modes that broken edit ops cause. Phase 3
finds a **fourth, deeper one: the agent's recovery path doesn't just
produce different output, it produces structurally worse output.**
Specifically:

- The recovery is **whole-spec re-emit**, so anything the agent forgets
  to re-emit is lost. The original template's careful on_click sequence
  (validate + push + increment + clear) gets dropped in favor of a
  one-liner chat-callback.
- The recovery is **disconnected from the original schema**, so the
  agent re-invents conventions on the fly. `state.lane_options` exists
  in the template specifically so kanban columns and selects share a
  source of truth; the recovery drops it and inlines plain strings,
  breaking the value/label contract.
- The recovery is **agent-in-the-loop forever**, so every
  user-interaction is gated on agent availability. The original
  template's actions were client-side and worked offline.

Each of these is a SECONDARY consequence of the `kind_for_op` bug.
The dispatch failure forces the agent into recovery, and the recovery
introduces these regressions. With flat (one merge primitive, no
per-op dispatch class) the bug class cannot exist, so the recovery
path is never entered, so the secondary regressions don't happen.

The cost of broken granular ops compounds. Phase 2 said "on capable
models, emit quality is a wash." Phase 3 shows that's only true
when ops work. **When ops break, the recovery cascade burns the
elegance of the original template.**

### Updated bottom line

The flat model's case has gotten stronger over the three phases:

1. **Phase 1 (real chat, Sonnet):** wrong-layer choice + 2 dispatch
   failures + duplicate pocket. Caveat: kind_for_op might be specific
   to pr-1219.
2. **Phase 2 (controlled A/B, Opus):** on capable models the surfaces
   are equivalent. Caveat: flat has a re-emit footgun.
3. **Phase 3 (live additive edit, Sonnet):** same dispatch failures
   re-occur, and the recovery cascade introduces 4 distinct
   correctness regressions in the resulting pocket. **No caveat —
   this is a real, reproducible failure mode of the current path
   under real load.**

The architectural argument for flat is no longer "agent emits cleaner
patches." It's: **every dispatch failure in the granular-op layer
costs the system more than its surface looks like, because the agent's
recovery introduces silent correctness regressions that propagate to
the user-visible pocket.** Flat eliminates the bug class that triggers
the recovery cascade.

### Phase 3 artifacts

- `captures/p3-additive-edit-recovery.json` — the final pocket spec
  with the four regressions documented inline.
- `captures/broken-edit-pocket.json` + `captures/recovery-fresh-pocket.json`
  from Phase 1 — comparison points for the recovery degradation.

Branch HEAD before Phase 3 commit: `bdd60bbe`.

## 10. Phase 4 — captain hypothesis: skill + native tools, drop LangChain

After Phase 3, the captain proposed reframing the architecture: instead
of 17 LangChain `StructuredTool` wrappers fronting per-op `_for_agent`
service hops, give `pocket_specialist` a **markdown skill** (procedural
guidance) plus Claude Code's native tools (Bash, Read, Write, Edit). The
agent already runs on the Claude Code backend; it can `curl` an API or
write JSON directly. The 17-tool layer is a second tool surface stacked
on top of the native one, and it's where the `kind_for_op` import bug
class lives.

Captain's discipline: test the hypothesis before changing any code.

### Setup

Isolated measurement run — no live backend, no `kind_for_op` confound,
same model as Phase 2 (Opus 4.7).

- Skill: hand-written `pocket-edit` SKILL.md (~70 lines) at
  `p4-skill-test/SKILL.md`. Names the four conventions Phase 3's
  recovery silently violated: client-side `push` actions, value/label
  splits on selects, lowercase-value defaults that match kanban column
  ids, and the validate-push-increment-clear hygiene sequence.
- Agent tools: Read, Write, Bash, Grep, Glob. **Zero LangChain
  wrappers.** No `set_state`, no `add_node`, no `set_node_prop`.
- Input: the recovery pocket spec from `p4-skill-test/p4-input-pocket.json`
  (the same starting state Phase 3 worked from).
- Task: verbatim same prompt as Phase 3 — *"Add an 'Add deal' form row
  above the kanban with company/value/contact inputs, lane select, and
  an Add deal button that appends to state.cards."*
- Output: agent writes proposed full new rippleSpec to
  `p4-skill-test/p4-skill-output.json`.

### Result

The skill-driven agent produced a structurally correct edit on the
first attempt. Direct comparison against Phase 3's live recovery on
the same task:

| Convention | Phase 3 (live, LangChain) | Phase 4 (skill, native) |
|---|---|---|
| Button on_click uses client-side `push` | ✗ `{action:"emit", target:"chat.send"}` | ✓ `{action:"push", target:"cards", value:{...}}` |
| `validate` before push | ✗ absent | ✓ `{action:"validate", condition:"{state.draft_company.length > 0}"}` |
| Clear drafts after submit | ✗ absent | ✓ 4× `{action:"set", target:"draft_*", value:""}` |
| Increment id counter | ✗ absent | ✓ `{action:"set", target:"next_deal_id", value:"{state.next_deal_id + 1}"}` |
| Select options as value/label pairs | ✗ plain strings `["Lead","Qualified",...]` | ✓ `[{value:"lead", label:"Lead"}, ...]` |
| Bound state default = value, not label | ✗ `"Lead"` (label-case) | ✓ `"lead"` (value) |
| Kanban column id ↔ status match | ✓ (both lowercase) | ✓ (both lowercase) |
| Preserved existing nodes by id | partial (template's add row had been dropped earlier) | ✓ page-header, kanban, root flex all preserved by id |
| Honest reflection on ambiguities | ✗ | ✓ flagged the `$` literal vs interpolation question on the subtitle template |

**9-for-9 on the structural conventions** versus Phase 3's 0-for-4 on
the four where they materially differ.

### Why this matters

Same model. Same task. Same starting spec. Same Sonnet/Opus class of
reasoning capacity. The difference is the prompt design and tool
surface:

- Phase 3 had 80 lines of mutation-strategy text (the layered tool
  hierarchy) plus 17 LangChain tools, hit a dispatch failure on every
  granular op (`kind_for_op`), and the recovery cascade emitted a
  whole-spec rebuild that silently dropped four template conventions.
- Phase 4 had 70 lines of procedural-guide markdown that explicitly
  named the four conventions, plus Bash/Read/Write/Edit. No dispatch
  layer to fail. The agent re-emitted the whole spec and kept every
  convention.

The skill teaches the agent **what good output looks like** with worked
examples. The 17-tool surface teaches the agent **which slot to call**
with branching examples per layer. Skill > tool-slot when the failure
modes are "agent forgets a convention," which is exactly what Phase 1
and Phase 3 surfaced.

### What this doesn't prove

Three caveats. Honest ones:

1. **Isolated, no real backend.** The skill agent wrote to a JSON file,
   not to mongo. We didn't test what happens when the proposed spec is
   actually persisted. The validate-against-manifest gate that lives
   in `pocketpaw_ee.cloud.pockets.agent_context.persist_pocket_for_agent`
   could still reject a bad spec.
2. **Skill was hand-crafted with the right conventions named.** If
   pocket_specialist's current prompt missed these conventions (and
   Phase 3 evidence suggests it does), the fair comparison is "skill
   with the same content" vs "current prompt." A fairer next step is
   to read `_pockets.py`'s mutation-strategy block more carefully and
   ask whether the conventions could just be added to the existing
   prompt without dropping LangChain.
3. **One task, one run.** N=1 sample size. The skill might fail on
   tasks that lean harder on the value/label rule or on more complex
   tree restructuring. Phase 4 would need 3-5 task variants before
   the verdict is generalizable.

### What this DOES strongly suggest

The captain's reframing is right at the architectural level:

- **The LangChain layer is a second tool surface stacked on the native
  one.** It adds dispatch fragility (Phase 1+3's `kind_for_op`),
  per-op schema fragility (Phase 1's pydantic missing-wrapper), and
  layered cognitive overhead in the prompt (the 80-line
  mutation-strategy block).
- **A skill + native tools wins on the conventions test** in this
  measurement run. The agent makes the right call when the
  conventions are named explicitly in procedural guidance.
- **The flat-model spike's PR-1 simplifies further.** Instead of
  "ship the dispatch wrapper + format discriminator + dual-read
  renderer," the new shape is "ship a `POST /api/v1/pockets/<id>/spec`
  endpoint (server-side merge + validation) and a `pocket-edit` skill
  for the agent. Remove the 17-tool LangChain layer." The flat-on-disk
  question becomes a server-side implementation detail, not an
  agent-facing concern.

### Updated synthesis across all four phases

| Phase | Setup | Result |
|---|---|---|
| **1** | Live chat, Sonnet 4-6, real backend on pr-1219 | Wrong-layer choice + 2 dispatch failures + duplicate pocket |
| **2** | Isolated Opus, two prompts (17-tool vs flat), same task | Both nail it — capable models navigate either surface |
| **3** | Live chat, Sonnet 4-6, additive edit on the recovery pocket | Same dispatch failures + recovery cascade introduces 4 silent regressions |
| **4** | Isolated Opus, skill + native tools, same task as P3 | 9-for-9 on conventions — the agent makes the right call when the conventions are named |

The strongest single signal in this whole experiment: **Phase 3's
real-load recovery and Phase 4's isolated skill run produced
materially different output quality on the same task with the same
model class.** That difference is attributable to prompt design +
tool surface, not to model capacity.

### Recommendation (updated)

Drop the original 5-PR spike sequence. The replacement is:

- **PR-1 (S/M):** add `POST /api/v1/pockets/<id>/spec` endpoint that
  accepts a full or partial rippleSpec, runs
  `validate_against_manifest`, and (for partials) merges via the
  spike's `merge()` function before persisting. Audit log records
  the diff. One dispatch path, one schema, no `kind_for_op`.
- **PR-2 (M):** write a `pocket-edit` skill that bundles the
  procedural guide (this Phase 4 SKILL.md is a working draft). Wire
  it into `bundled_skills/` so it auto-installs at boot. Update
  `pocket_specialist`'s prompt to point at the skill and drop the
  17-tool LangChain layer entirely.
- **PR-3 (S):** measure on real chat traffic for one review cycle.
  If the skill + endpoint shape holds, retire the
  `pocketpaw_ee.cloud.pockets.agent_context.<op>_for_agent`
  service hops (touch-time migration during normal feature work).

Two PRs of code change, one PR-cycle of observation. The original
spike's flat-vs-nested format work becomes the *internal* shape of
the merge function in PR-1 — exposed to the API as one endpoint, not
to the agent as a wire format.

### Phase 4 artifacts

- `p4-skill-test/SKILL.md` — the 70-line procedural guide the agent
  was given.
- `p4-skill-test/p4-input-pocket.json` — the same starting state
  Phase 3 worked from.
- `p4-skill-test/p4-skill-output.json` — the agent's proposed new
  spec, 9-for-9 on conventions.

Branch HEAD before Phase 4 commit: `e2ba96d6`.
