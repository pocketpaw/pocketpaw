# Ripple Mutation: Implementation Handover

*Companion to [`2026-05-pocket-specialist-and-ripple-mutation.md`](./2026-05-pocket-specialist-and-ripple-mutation.md). The vision doc says **what** and **why**; this doc says **how**, in PR-sized chunks Rohit can pick up directly.*

*Owner: Rohit. Reviewer: Prakash. Each section is self-contained — read top-to-bottom for context, or jump to the PR you're starting next.*

---

## Roadmap at a glance

| # | PR | Depends on | Sized | Goal in one sentence |
|---|---|---|---|---|
| 1 | Granular UI-tree mutations | #1069 merged | L | Replace whole-spec rewrites with surgical ID-targeted ops on `rippleSpec.ui`. |
| 2 | Inverse op log + undo/redo | PR 1 | M | Every committed op stores its inverse; `undo_op` / `redo_op` MCP tools roll forward/back. |
| 3 | Backend-agnostic specialist wrapper | #1069 merged | L | Lift `pocket_specialist` registration out of `claude_sdk.py` into a backend-agnostic factory; cover at least `openai_agents`, `google_adk`, `deep_agents`. |
| 4 | Vision-in-the-loop verification | PR 1 | M | After every specialist mutation cycle, snapshot the canvas and ask a vision model "did this land?" before returning success. |
| 5 | Instinct integration for pocket mutations | PR 1, ideally PR 2 | L | Each granular mutation flows through Instinct as a proposed action with audit trail and approval policy. |

PR 1 unblocks 2 / 4 / 5 — land it first. PR 3 is independent and can run in parallel with 1.

**Out of scope for this batch:** Streaming as op stream (becomes free once PR 1 lands — call it PR 1.5 if it comes naturally). Soul memory of design preferences. Named-statement spec view (long-term — needs a separate RFC).

---

## PR 1: Granular UI-tree mutations

### Goal
Replace `update_pocket(ripple_spec=<entire tree>)` as the primary mutation path with five surgical ops keyed on stable node IDs. The specialist no longer rewrites untouched panes to preserve them.

### Definition of done

- [ ] Every node in `rippleSpec.ui` carries an `id` field (auto-generated where missing on read).
- [ ] Five new MCP tools: `add_node`, `replace_node`, `set_node_prop`, `move_node`, `remove_node` — registered in `sdk_mcp_pocket.py`, surfaced on the specialist allowlist, **filtered off** the main agent allowlist.
- [ ] Server-side handlers in `ee/cloud/pockets/service.py` apply ops to `rippleSpec.ui` and persist the changed document.
- [ ] New SSE event payloads (`action: "node_added" | "node_replaced" | "node_prop_set" | "node_moved" | "node_removed"`) carry only the changed subtree, not the full pocket.
- [ ] Specialist prompt updated to **prefer** granular ops over `update_pocket` for surgical edits; `update_pocket` remains for whole-canvas rewrites and initial creation.
- [ ] Ripple renderer applies incremental mutations in place without remounting unchanged subtrees.
- [ ] Contract tests + the canonical "rename one row in a 100-row table" integration test (assertion: only one node in the spec changed; only one SSE frame emitted).
- [ ] Backwards compat: existing `update_pocket(ripple_spec=full)` still works, still emits `action: "replace"`. No existing pockets break.

### Stable IDs spec

```ts
// ripple/src/lib/schema/ui-spec.ts — UINode shape

UINode = {
  id: string;          // ← was optional; now REQUIRED at read time
  type: UINodeType;
  props?: Record<string, any>;
  children?: UINode[];
  // ... existing fields
};
```

**Generation rules:**

- Format: `n_<8-char-base32>` (e.g. `n_3kfa9zxq`). 40 bits of randomness — collision risk negligible at any pocket size we'll see.
- Generated server-side on read whenever a node lacks an `id`. Persisted on the next write.
- Migration is lazy: a pocket without IDs gets them on its next mutation. No big-bang migration script.
- Uniqueness scope: per-pocket. Cross-pocket collisions are fine.

**Validation:**

- Add a Pydantic / Zod check in `_validate_ripple_spec` that every node has a string `id` matching `^n_[a-z0-9]{8}$` *or* is missing entirely (which means: the read path will assign one).
- Reject writes where two siblings share an ID — that's a real bug, not a missing ID.

### MCP tool surface

All five tools are namespaced under `mcp__pocketpaw_pocket__` and registered in `src/pocketpaw/agents/sdk_mcp_pocket.py` via the existing `@tool` pattern. Add their IDs to `POCKET_TOOL_IDS` and to `_POCKET_MUTATION_TOOL_IDS` in `claude_sdk.py` so the main agent can't call them.

```python
# Schema sketch — finalize naming with Rohit before coding.

add_node(
    pocket_id: str,
    parent_id: str,            # ID of the node to insert under
    spec: dict,                # the new UINode (id auto-assigned if absent)
    after_id: str | None = None,  # insert after this sibling; None = append
) -> {"ok": True, "node_id": str, "subtree": dict}
  | {"ok": False, "error": str}

replace_node(
    pocket_id: str,
    node_id: str,              # full subtree replacement; preserves id
    spec: dict,
) -> {"ok": True, "subtree": dict}
  | {"ok": False, "error": str}

set_node_prop(
    pocket_id: str,
    node_id: str,
    prop: str,                 # dot-path inside props; e.g. "label" or "data.rows"
    value: Any,
) -> {"ok": True, "subtree": dict, "old_value": Any}
  | {"ok": False, "error": str}

move_node(
    pocket_id: str,
    node_id: str,
    new_parent_id: str,
    after_id: str | None = None,
) -> {"ok": True, "subtree": dict}
  | {"ok": False, "error": str}

remove_node(
    pocket_id: str,
    node_id: str,
) -> {"ok": True, "removed_id": str}
  | {"ok": False, "error": str}
```

**Returns the changed subtree** (not the full pocket) so the specialist's reasoning context stays small. The SSE event carries the same payload — client applies it without a re-fetch.

**Errors that should fail loudly (not silently no-op):**

- `node_id` not found in spec → `error: "no node with id <x>"`.
- `parent_id` not found / `parent_id` not a container type → `error: "..."`.
- `after_id` not a child of `parent_id` → `error: "..."`.
- `set_node_prop` with a `prop` path that doesn't match the widget's manifest schema → `error: "unknown prop <x> for widget <type>"`. *(This is the silent-failure killer — manifest validation is already wired for `update_pocket`; reuse it per-op.)*

### Server-side wiring

Per `pocketpaw/CLAUDE.md` ee/cloud 4-file shape, the implementation lives in:

- `ee/cloud/pockets/service.py` — five new module-level `async def agent_<op>(ctx, body)` functions. Each: validate body, fetch document with `workspace=ctx.workspace_id`, walk `rippleSpec.ui` to find the target by ID, mutate, persist, emit event.
- `ee/cloud/pockets/dto.py` — five `<Op>Request` + `<Op>Response` DTOs. Reuse `model_validate` at entry per ee/cloud rule 6.
- `ee/cloud/pockets/agent_context.py` — five new `<op>_for_agent` MCP-shape wrappers (mirror the existing `update_pocket_for_agent` pattern).

**Walk + mutate primitives.** Add a small helper module — `ee/cloud/pockets/spec_ops.py` (new) — with pure functions:

```python
def find_by_id(node: dict, target_id: str) -> tuple[dict, list[int]] | None:
    """Return (node, path) where path is the index trail from root."""

def insert_after(parent: dict, after_id: str | None, child: dict) -> None:
    """Insert child into parent.children. None => append."""

def replace_at(node: dict, path: list[int], new_node: dict) -> None: ...
def remove_at(node: dict, path: list[int]) -> dict: ...  # returns removed
def set_prop(node: dict, prop_path: str, value: Any) -> Any: ...  # returns old
```

These stay pure (no DB, no events, no logging). The service wraps them with persistence + emission. Pure helpers are easy to test against fixture trees.

**Event emission** (per ee/cloud rule 9 — every write emits):

```python
# ee/cloud/pockets/service.py — sketch

async def agent_set_node_prop(ctx, body):
    body = SetNodePropRequest.model_validate(body)
    doc = await _PocketDoc.find_one(_id=body.pocket_id, workspace=ctx.workspace_id)
    if not doc:
        raise NotFound(f"pocket {body.pocket_id}")
    spec = doc.rippleSpec or {}
    found = spec_ops.find_by_id(spec.get("ui", {}), body.node_id)
    if not found:
        raise NotFound(f"no node with id {body.node_id}")
    node, _path = found
    old = spec_ops.set_prop(node, body.prop, body.value)
    await doc.save()
    await emit(NodePropChanged(
        pocket_id=body.pocket_id,
        node_id=body.node_id,
        prop=body.prop,
        old_value=old,
        new_value=body.value,
    ))
    return SetNodePropResponse(subtree=node, old_value=old)
```

**SSE push.** Update `_push_replace` in `agent_context.py` (or add a sibling `_push_op`):

```python
async def _push_op(action: str, pocket_id: str, payload: dict) -> None:
    push_pocket_mutation({
        "action": action,             # "node_added" | "node_replaced" | etc.
        "pocket_id": pocket_id,
        **payload,                    # subtree / removed_id / etc.
    })
```

The renderer needs a corresponding switch — see [Renderer integration](#renderer-integration) below.

### Renderer integration (ripple repo)

Single biggest "watch out": the renderer today does `state.current = spec` on every update. That has to become "apply op in place to a stable mutable spec, let Svelte's reactivity handle the rest."

**Files to touch in `ripple/`:**

- `src/lib/streaming/stream-spec.svelte.ts` — add a new path that consumes incremental ops alongside the existing whole-spec parser. Whole-spec writes still work for `action: "replace"`.
- `src/lib/core/state-manager.svelte.ts` — already has path-based mutation for `state.*`. Extend with a separate ID-keyed index for `ui.*` nodes.
- `src/lib/Ripple.svelte` — when the incoming spec is keyed by ID, mutate in place rather than replacing `state.current`.
- New: `src/lib/core/spec-mutator.ts` — mirror of the server-side `spec_ops.py`. Pure functions over the in-memory spec. The renderer applies ops via these.

**Strategy:** maintain `Map<id, UINode>` alongside the tree. Ops mutate via the map (O(1) lookup) and Svelte's `$state` reactivity surfaces the change. Keyed `{#each}` with `id` as the key prevents row-thrash when arrays mutate.

### Specialist prompt updates

`ee/ripple/_pockets.py` — extend `_WORKFLOW_INTERACTION_MCP` with an "ops over whole-spec" preference rule:

```text
<mutation-strategy>
For surgical edits (renaming one cell, toggling one prop, adding one
widget, moving a card), use the granular ops:

  add_node, replace_node, set_node_prop, move_node, remove_node

These touch only the target node. Cheaper, safer, and the user's
focus / scroll position survive.

Reach for `update_pocket(ripple_spec=...)` only when:
  - You're rewriting the entire canvas (rare — usually "redesign this").
  - The user asked for a structural shift that touches more than ~30%
    of nodes.

Never use `update_pocket` to change one row, one prop, or one widget.
The ops above exist for exactly that.
</mutation-strategy>
```

Don't gut the existing "build the FULL updated tree locally" instruction — keep it as the fallback path. The new block sits *above* it as the preferred default.

### Tests

- `tests/cloud/test_spec_ops.py` (new) — pure unit tests for the walk+mutate helpers.
- `tests/cloud/test_pocket_granular_ops.py` (new) — integration: each of the 5 ops, success + each error class, ID assignment on first read of a legacy pocket, sibling-ID-collision rejection.
- `tests/test_pocket_specialist.py` — add: specialist can call all 5 new tools; main agent's allowlist excludes them.
- `tests/cloud/test_pocket_agent_context.py` — extend: SSE event shapes for each op.
- `tests/test_sdk_mcp_inline_help.py` — no change.
- **The canonical regression test:** start with a 100-row table pocket; have the specialist rename one row; assert (a) `agent_update` was *not* called, (b) `agent_set_node_prop` was called once, (c) the SSE payload's subtree is the single row, not the full table.

### Migration / backwards compat

| Layer | Behavior |
|---|---|
| Existing pockets | Lazy ID assignment on first read after deploy. No migration script. |
| `update_pocket(ripple_spec=full)` | Stays. Same `action: "replace"` SSE event. Specialist still reaches for it on whole-canvas rewrites. |
| Legacy `add_widget`/`update_widget`/`remove_widget` | **Stay (separately)** — they target the legacy embedded `widgets` array which the desktop client doesn't render. Don't repurpose their names. |
| Old clients | Receive the new `action` values they don't recognize. Renderer should ignore unknown `action`s and fall back to a refetch — define this contract in the renderer PR. |

**Decision flagged for Rohit:** *do* we repurpose `add_widget`/`update_widget`/`remove_widget` to target `rippleSpec.ui` (since the legacy array is dead) and rename the new ops accordingly, *or* introduce fresh names (`add_node` / `replace_node` / `set_node_prop` / `move_node` / `remove_node`)? My recommendation: fresh names — cleaner contract, no risk of confusion with old behavior, easier to delete the legacy tools later. Open question.

### Gotchas

- **`set_node_prop` and dot-paths.** Allowing `prop="data.rows"` is convenient but lets the specialist write into a Ripple-resolver expression context where it shouldn't. Constrain to schema-known prop paths via the manifest validator. If `data` is `"{state.tasks}"`, refuse to set `data.rows` directly — refuse with a clear error.
- **Move across parents vs reorder within a parent.** `move_node` covers both. Same op, same inverse.
- **Concurrency.** Two ops arriving in quick succession on the same pocket: today no concurrency control beyond Beanie's optimistic update. Granular ops won't make this worse, but if the agent issues a flurry of ops in parallel (which it might), we should serialize per-pocket. Add an `asyncio.Lock` keyed on `pocket_id` in `service.py` with a small per-pocket lock cache (LRU 256). If we ever ship CRDTs, the lock retires.
- **Validation timing.** `_validate_ripple_spec` is whole-spec today (manifest walk). For per-op validation, validate only the touched subtree. Cheap.
- **Widget catalog drift.** If a widget's prop set changes upstream, old pockets may have unknown props. Today we warn-and-pass; keep that behavior on per-op writes too.

---

## PR 2: Inverse op log + undo/redo

### Goal
Every committed op stores its inverse in a per-pocket ring buffer. New tools `undo_op` / `redo_op` roll the ledger forward/back. Falls out of PR 1 almost for free — the inverses are trivial to derive at write time.

### Definition of done

- [ ] Per-pocket op ledger persisted. Recommend a `pocket_ops` Mongo collection with `{pocket_id, seq, op, inverse, actor, ts}`. Capped at the last 100 ops per pocket via a TTL+seq policy.
- [ ] Each granular op (PR 1) writes a ledger entry with its computed inverse before returning.
- [ ] Two new MCP tools on the specialist: `undo_op(pocket_id)` (pops the most recent op, applies its inverse, returns the inverse subtree) and `redo_op(pocket_id)` (re-applies a previously undone op).
- [ ] Client-side undo/redo controls in the desktop client wired to these tools (or to dedicated REST endpoints — see Open question below).
- [ ] Tests asserting op + inverse round-trip restores the exact pre-state for every op type.

### Inverse op table

| Op | Inverse |
|---|---|
| `add_node(parent, spec, after)` | `remove_node(spec.id)` |
| `remove_node(id)` | `add_node(parent_id_at_remove, removed_subtree, after_id_at_remove)` |
| `replace_node(id, new)` | `replace_node(id, old)` |
| `set_node_prop(id, prop, new)` | `set_node_prop(id, prop, old)` |
| `move_node(id, new_parent, after)` | `move_node(id, old_parent, old_after)` |

Capture the "before" data at op-application time (in PR 1's service handlers) and write it into the ledger. Cheap — `find_by_id` is already running.

### Open questions

- **Tool vs. endpoint?** Undo/redo could be MCP tools the agent triggers ("undo my last change"), or REST endpoints the user triggers from a UI button. Probably both — start with the agent-facing tool, add the endpoint when the desktop client wants the UI affordance.
- **Branching.** What happens if user does op A, undoes it, then does op B? Standard editor behavior: B clears the redo stack. Document explicitly.
- **Cross-actor undo.** Two users in the same workspace editing the same pocket — should undo respect actor ID? Probably yes (each actor's undo only sees their own ops). Defer to a follow-up.

### Tests

- Per op type: apply → undo → assert spec equals pre-state byte-for-byte.
- Apply N ops → undo N times → assert spec equals seed.
- Apply A, undo A, apply B → redo stack is empty (assert redo errors with `no op to redo`).

---

## PR 3: Backend-agnostic specialist wrapper

### Goal
Lift the `pocket_specialist` registration out of `claude_sdk.py` so all backends benefit from the architectural separation (and the token savings). Today it's gated to `claude_agent_sdk` because it relies on the SDK's `agents=` map + built-in `Agent` tool.

### Definition of done

- [ ] New module `src/pocketpaw/agents/specialist.py` exposes a backend-agnostic `register_pocket_specialist(backend) -> SpecialistHandle` factory.
- [ ] Each backend's `run()` calls `register_pocket_specialist(self)` if available; the handle exposes `delegate(prompt) -> AsyncIterator[Event]`.
- [ ] Compiles to:
  - `claude_agent_sdk` → `AgentDefinition` + `Agent` tool (today's path; refactor to use the shared factory).
  - `openai_agents` → handoff agent.
  - `google_adk` → sub-agent / tool-as-agent.
  - `deep_agents` → planning subagent (LangChain Deep Agents already supports subagents natively).
  - `codex_cli` / `opencode` / `copilot_sdk` → in-process synthetic specialist: a second `query()` call with the specialist's prompt + tool slice, results streamed back into the main agent's transcript as tool-result.
- [ ] `POCKET_DELEGATION_RULE` stays generic (no backend-specific tool name). The delegation glue is each backend's responsibility.
- [ ] Per-backend tests covering: specialist invokable, mutation tools fired only from specialist, main agent's `_TOOL_POLICY_MAP` doesn't reference `Agent` directly (the factory abstracts it).

### Architecture sketch

```python
# src/pocketpaw/agents/specialist.py

@dataclass
class SpecialistDefinition:
    name: str
    description: str
    system_prompt: str
    tool_ids: list[str]            # MCP tool ids the specialist owns
    main_agent_filter: list[str]   # tool ids to filter off main agent

class SpecialistHandle(Protocol):
    async def delegate(self, prompt: str, *, context: dict | None = None) -> AsyncIterator[AgentEvent]:
        ...

def build_pocket_specialist_definition() -> SpecialistDefinition:
    """One source of truth for the specialist's prompt + tools."""
    ...

# Backend integration — each backend implements one of these.

class BackendSpecialistAdapter(Protocol):
    def register(self, defn: SpecialistDefinition) -> SpecialistHandle: ...
```

The synthetic-fallback adapter (for backends without native subagent support) is a small wrapper around the backend's own `run()` with a tighter system prompt and tool allowlist — at the cost of a second LLM round-trip but with the same architectural separation.

### Tests
- Per backend: invoke the specialist, assert mutation tools fired only inside the specialist's transcript, never as direct main-agent tool calls.
- Cross-backend contract test: every backend listed above returns a working `SpecialistHandle`.

### Gotchas
- `deep_agents` already has its own subagent primitives — use them, don't fight them.
- `codex_cli` and `opencode` are subprocess-based; "synthetic specialist" means an extra subprocess invocation. Cost is real; document it.

---

## PR 4: Vision-in-the-loop verification

### Goal
After every specialist mutation cycle, snapshot the rendered canvas and ask a vision model "did the requested change land?". Catches the silent-failure class (typo'd prop names, wrong target IDs, `bind` strings that miss state shape).

### Definition of done

- [ ] New module `ee/cloud/pockets/vision_verify.py`. Single entry point `verify_mutation(pocket_id, intent, mutation_summary) -> VerifyResult`.
- [ ] Snapshot mechanism: server-side Playwright (Chromium headless) hitting an internal render route that takes a pocket id and returns a static-rendered image. Already partially possible — the desktop client renders Ripple, so we can spin up a Playwright worker against a "render server" route. Concrete decision needed (see Open questions).
- [ ] Vision call: the active LLM provider's vision-capable variant. Reuse the `pocketpaw.llm.client` resolver; pick the smallest sufficient model (Haiku-tier).
- [ ] Specialist's flow: at end of mutation, call `verify_mutation`. If verify fails, return a structured error to the main agent that includes the user's original intent + the vision model's "what's missing" string. Main agent decides whether to re-delegate or surface to user.
- [ ] Configurable per-workspace: `verify_pocket_mutations: bool` setting (default `False` until rollout proven; flip to `True` after).
- [ ] Tests with mocked vision provider asserting (a) verify passes when the snapshot matches intent, (b) verify fails with structured error when snapshot doesn't, (c) the failure path doesn't break the SSE stream.

### Open questions

- **Where does the snapshot come from?** Three options:
  1. Server-side Playwright against a synthetic render route — most reliable, most infrastructure.
  2. Client round-trip: client takes `html2canvas`, posts back. No new infra; latency cost; fragile if user has the pocket closed.
  3. Skip image entirely — feed the resolved spec to a non-vision model and ask "does this match the intent?". Cheapest. Less accurate but probably 80% there.
  
  Recommend 3 for v1 (no infrastructure cost), 1 for v2 once we know it's worth the lift.

- **Cost.** A vision call per mutation cycle is real money. Either rate-limit (one verify per N ops) or only verify on completion of a multi-step specialist transcript, not after each tool call.

### Tests
- Mock vision provider returning "matches" → mutation completes, no error event.
- Mock provider returning "missing X" → main agent receives structured retry hint.
- Mock provider unavailable → mutation completes (best-effort verify), warning logged.

---

## PR 5: Instinct integration for pocket mutations

### Goal
Every granular mutation flows through Instinct (`ee/instinct/`) as a proposed action with: reasoning trace, fabric snapshot of the pocket pre-state, audit trail, and approval policy. Same gate as `send_email`.

### Definition of done

- [ ] New `MutationProposal` Instinct event type. Carries: pocket id, op, op args, inverse (from PR 2), specialist's reasoning trace, the user's verbatim intent.
- [ ] Each granular service-layer call routes through Instinct's proposal pipeline:
  - Auto-approve path: low-risk ops on user-owned pockets (current default).
  - Approval-required path: ops on workspace-shared pockets, ops marked sensitive by per-pocket setting.
- [ ] Instinct UI affordance for reviewing pending pocket mutation proposals (probably extends the existing Instinct panel — coordinate with that surface owner).
- [ ] Outcome metering: was the mutation reverted within N seconds? Did the user undo it? Counts as negative outcome. Plumbs into the existing Instinct outcome table.
- [ ] Tests: auto-approve path identical to current behavior; approval-required path correctly blocks until approval; rejection path applies the inverse op.

### Wiring

- `ee/cloud/pockets/service.py` — each granular service function ends not with a direct `emit(NodePropChanged(...))` but with `await instinct.propose(MutationProposal(...))`. Instinct decides whether to apply immediately (auto-approve) or queue.
- `ee/instinct/decisions.py` — extend the policy table with a new `pocket_mutation` action class.
- `ee/cloud/pockets/dto.py` — add `sensitivity: PocketSensitivity` field on the pocket document. Defaults to `private`. Workspace-shared pockets default to `team` which routes to approval.

### Tests
- End-to-end: low-risk op auto-approves and emits SSE within the same request.
- Approval-required op: SSE shows "pending approval"; second request approves; SSE then shows the mutation applied.
- Rejection path: rejected op applies inverse, no SSE for the original op.

---

## Out of scope (documenting so we don't forget)

These come after the five above. Tracking here so they're not lost:

- **Op streaming.** Once granular ops exist (PR 1), streaming the agent's tool calls **is** streaming the mutations. The renderer applies them as they arrive. No more partial-JSON parsing for spec writes. Worth a small follow-up PR after PR 1 + PR 2 stabilize.
- **Soul memory of design preferences.** Specialist's soul records "captain prefers density", "avoid pie charts", "use rounded corners". Loads at the start of every specialist invocation. Survives across sessions. Roadmap item, no PR yet.
- **Named-statement spec view (OpenUI Lang style).** The agent reads a flat-dictionary view; storage stays nested JSON. 85%-ish token savings on long-session refinement. Long-term — separate RFC required.
- **CRDTs for concurrent agent editing.** Don't need it today (per-pocket lock from PR 1 is enough). Door stays open.
- **Cross-actor undo.** Multi-user pockets where each actor's undo only sees their own ops. Follow-up to PR 2.

---

## Open questions for Prakash / Rohit

These need decisions before PR 1 starts. Recommend a 30-min sync.

1. **Tool naming.** Repurpose `add_widget`/`update_widget`/`remove_widget` (kill the legacy embedded array), or introduce fresh `add_node`/`replace_node`/`remove_node`? My recommendation: fresh names; retire legacy in a follow-up.
2. **Vision verification (PR 4).** Start with prose-only verify (no snapshot) or invest in Playwright render route? My recommendation: prose-only for v1.
3. **Instinct policy default (PR 5).** Auto-approve all pocket mutations by default, or require approval for any workspace-shared pocket? My recommendation: auto-approve everywhere for v1; ratchet down once Instinct UI is in place.
4. **Op ledger storage (PR 2).** New Mongo collection or embed in the pocket document? My recommendation: separate collection — pocket documents stay small, ledger TTLs cleanly.
5. **PR 1 size.** As specified above, PR 1 is L (server + tool registration + renderer + prompt + tests). Worth splitting into PR 1a (server + tools, renderer falls back to refetch on unknown actions) and PR 1b (renderer applies in place)? My recommendation: keep as one PR — the renderer-side change is small enough and ships together cleanly.

---

## Cross-references

- [Vision doc](./2026-05-pocket-specialist-and-ripple-mutation.md) — the "why" companion to this "how".
- [PR #1069](https://github.com/pocketpaw/pocketpaw/pull/1069) — the foundation this builds on.
- `pocketpaw/CLAUDE.md` § ee/cloud Code Rules — the 4-file shape every PR-1 code path follows.
- `ripple/src/lib/schema/ui-spec.ts` — UISpec schema; PR 1 makes `id` required at read time.
- `ee/cloud/pockets/agent_context.py` — current MCP-shape wrappers; pattern to mirror for granular ops.
- `ee/cloud/chat/agent_service.py:push_pocket_mutation` — SSE push site; new action types layer in here.
