<!-- design draft — humanized tool narration + normalized agent plan surface.
     Created 2026-08-15. Stage ① (paw-brainstorm). Next: /to-prd. Status: DRAFT.
     Covers two threads that share one root cause: the agent bridge discards tool
     arguments, so neither narration-with-context nor plan rendering is possible today. -->

# Design: humanized tool narration + a normalized agent plan surface

**Date:** 2026-08-15 · **Size:** L · **Stage:** ① brainstorm → next: `/to-prd`
**Repos:** pocketpaw (primary), paw-enterprise (render side)
**Branch:** `feat/humanized-tool-narration` · **Worktree:** `D:/paw-worktrees/humanized-tools`

## Problem

Three user-visible symptoms, one root cause.

1. **Users read raw tool identifiers.** The chat surface renders `using pocketpaw_sites_publish`,
   `using create_pocket`, `using web_search`. The only humanization that exists is a hardcoded
   9-entry `statusMap` at `paw-enterprise/src/lib/core/chat/store.svelte.ts:709`, keyed on
   *Claude Agent SDK* tool names (`WebSearch`, `Read`, `Bash`, `Glob`, …). Every PocketPaw-native
   tool — 48 builtin modules, ~49 bridged MCP tools, the whole connector surface — misses the map
   and falls through to `using ${tool}`.

2. **The agent's plan is invisible.** The pydantic-ai backend enables the harness `Planning`
   capability (`src/pocketpaw/agents/pydantic_ai.py:1286`), so the model maintains a real ordered
   plan via `write_plan`. None of it reaches the UI.

3. **Narration is implemented twice, ad hoc, and neither implementation is right.** Besides the
   frontend `statusMap`, `ee/pocketpaw_ee/cloud/activity/buffer.py::_summarise()` independently
   fishes a display string out of the event payload (`summary` → `message` → `tool` → `tool_name`
   → `thought` → `name`, else the raw event type) to feed Mission Control's ticker. The two do not
   know about each other and both degrade to raw tool names in the common case.

**Root cause.** `ee/pocketpaw_ee/cloud/shared/agent_bridge.py:582` extracts only the tool *name*
from the `tool_use` event and **drops the arguments**. Everything downstream is therefore forced
to author display text from a bare identifier, per surface, after the fact.

- For thread 1 this is why "Searching the web" can never become "Searching the web **for X**".
- For thread 2 it is fatal: `write_plan`'s entire payload *is* its arguments, so the frontend
  receives `{"tool": "write_plan"}` and nothing else. No component can render the plan today
  regardless of how it is written.

Fixing that one seam unblocks both threads. That is why they are one project, not two.

## Orientation findings

**Glossary terms used in their paw meanings:** Pocket (workspace container, not clothing),
Connector (workspace-scoped data integration), Ripple (generative UI layer), Instinct (decision
pipeline). No glossary term is used in its LLM-default sense here.

**Prior art in-repo — four plan/todo mechanisms, three incompatible schemas:**

| Source | Where | Shape | States |
|---|---|---|---|
| `write_plan` | `pydantic_ai_harness.planning`, enabled at `agents/pydantic_ai.py:1286` | `items: [{content, status}]` | pending, in_progress, completed, **cancelled** |
| `write_todos` / `read_todos` | `agents/deep_agents.py:533` (langchain `TodoListMiddleware`) | `[{content, status}]` | pending, in_progress, completed |
| `TodoWrite` | native to `claude_agent_sdk` — the **default** backend (`config.py:409`) | not referenced anywhere in pocketpaw source; passes through untouched | unverified |
| planner MCP `todos[]` | `ee/.../agent/mcp_servers/planner.py:528` | `{id, label, description, success_criteria[], preconditions[], depends_on[], tags[], estimated_minutes}` | n/a — build brief |

The first three are the same concept with three schemas. The fourth is a different lifecycle
(a build brief walked by pocket_specialist on "build it") and is a **non-goal** here.

`write_plan` uses **whole-plan replacement** — the model resends the entire ordered list on every
call, no indices, no deltas. That semantic is a gift: it maps directly onto a mutating pinned
panel with no diffing and no ordering logic. State lives in a per-run in-memory `PlanState`.

**Event plumbing.** `AgentEvent(type="tool_use")` in `src/pocketpaw/agents/protocol.py` is the
universal seam — all backends emit it, so one server-side narration layer covers every backend
*and* every channel adapter (Telegram, Discord, Slack, WhatsApp, CLI), which the current
frontend-only implementation cannot. `agent.tool_start` and `agent.tool_result` already exist in
`_core/realtime/events.py` and in the generated topic list; the chat store subscribes to neither.

**Charter alignment.** Two universal principles from
`docs/roadmap/future-upgrades/engineering-patterns/workspace-charter.md` apply directly:
mechanical enforcement (a test that fails when a tool lacks narration) beats human review, and one
canonical reference beats phased propagation. The existing 9-entry `statusMap` is a worked example
of the failure mode this design exists to prevent.

**External research:** not run. The decision drivers here are all internal (existing schemas, the
bridge seam, the security property below); no external prior art would change the shape. Flagged
so a later reader knows it was a deliberate skip, not an oversight.

## Approach (chosen)

**Narrate server-side at the bridge, from tool-declared templates, and normalize every backend's
plan tool into one canonical event.**

### Why tool-declared over a central registry

A central map was considered and rejected. The deciding argument is not maintainability, it is
security: **which of a tool's arguments are safe to display is knowledge only the tool has.**
`shell`, `pip_install`, and the connector tools all accept arguments that can carry credentials,
tokens, or PII. Rendering `Running curl -H 'Authorization: Bearer sk-…'` into a chat transcript
publishes a secret to every channel adapter. That allowlist is the same class of knowledge as
`trust_level`, which already lives on `BaseTool`. Once it must live on the tool, splitting the
template into a second file buys nothing.

Supporting argument: the central-registry approach has already been tried in this codebase and has
already rotted. `store.svelte.ts:709` *is* that design at small scale — written for one backend,
silently wrong for every other since the day a second backend landed, and nothing fails when it is
incomplete. It just prints the raw name forever.

**Rejected — central registry only:** simpler and reviewable in one pass, but drifts silently and
cannot hold the security allowlist correctly.

**Rejected — LLM narration on the hot path:** per-call tokens, latency, nondeterministic phrasing,
and it can confidently describe a tool it has misread. Retained only as a **build-time** seeding
pass (below).

**Adopted as the authoring mechanism:** a one-time model-drafted pass proposes templates for all
~100 tools, reviewed by a human as a single diff and committed. This recovers the one real benefit
of a central file — reading the whole product voice top-to-bottom and making it consistent in one
sitting — without keeping the central file. No generated line ships unread.

## Design

### Contract 1 — `Narration` (authoring, colocated with the tool)

```python
@dataclass(frozen=True)
class Narration:
    active: str  # "Searching the web for {query}"
    bare: str  # "Searching the web"  (args missing/redacted)
    safe_args: tuple[str, ...] = ()  # allowlist — ONLY these may interpolate
```

Exposed as an optional property on `BaseTool` (default `None`), alongside `trust_level`.

**Rendering rules** (one function, server-side):

- Only fields listed in `safe_args` are interpolated. A template referencing a field outside
  `safe_args` is a **test failure**, not a runtime leak — the check is mechanical.
- A missing, empty, or redacted safe arg falls back to `bare`.
- Interpolated values are newline-stripped and truncated (80 chars) before rendering.

**Fallback chain** when a tool declares no narration:

1. Override registry — for MCP and connector tools, which are external and cannot self-declare.
2. **Derive from name** — strip the vendor prefix, verb-first via a small deterministic lexicon
   (`publish`→Publishing, `create`→Creating, `search`→Searching, `invite`→Inviting, …), so
   `pocketpaw_sites_publish` reads *"Publishing the site"* rather than
   *"using pocketpaw_sites_publish"*. No LLM at runtime.
3. `using <name>` — the current behavior, as the last resort.

Step 2 is what makes the rollout safe: narration improves **before** all ~100 tools are annotated,
so a half-finished migration is still a net gain and cannot strand.

`Narration` ships as a structured object from day one even though only three fields are populated,
so i18n, per-surface phrasing, and Pocket-level overrides become additive fields later rather than
a breaking change across 48 modules.

### Contract 2 — normalized plan event

New realtime event `agent.plan_updated`:

```json
{
  "group_id": "…", "agent_id": "…", "run_id": "…", "seq": 7,
  "items": [
    {"id": "1", "content": "Add the database migration", "status": "completed"},
    {"id": "2", "content": "Wire the endpoint",          "status": "in_progress"}
  ],
  "progress": {"completed": 1, "total": 2}
}
```

Status enum is the **superset**: `pending | in_progress | completed | cancelled`. Sources with
three states simply never emit `cancelled`.

One normalizer per source, all producing this shape:

- `write_plan` (pydantic-ai) — direct field map, all four states.
- `write_todos` (deep_agents) — direct map, three states.
- `TodoWrite` (claude_agent_sdk) — **wire shape unverified**; see Open questions.

**Whole-list replacement semantics**, inherited from `write_plan`: the frontend *replaces* its
state, never merges. No diffing, no partial-update ordering bugs. `seq` is a per-run monotonic
counter so an out-of-order delivery cannot let a stale plan overwrite a fresh one.

### Contract 3 — bridge changes

At `ee/pocketpaw_ee/cloud/shared/agent_bridge.py:582`:

- Extract tool **arguments** alongside the name.
- If the tool is registered as a plan tool → run its normalizer and emit `agent.plan_updated`.
- Otherwise → render narration server-side and add a `narration` field to `AgentToolUse.data`.
- Keep the existing `tool` field populated for backward compatibility.

## Integration points

| Change | File |
|---|---|
| Add `AgentPlanUpdated` event | `ee/pocketpaw_ee/cloud/_core/realtime/events.py` (near `AgentToolUse`, :382) |
| Regenerate topic list | `uv run python backend/scripts/gen_topics.py` → `paw-enterprise/src/lib/core/shared/topics.gen.ts` (generated — do not hand-edit) |
| Broadcast the new topic | `ee/pocketpaw_ee/cloud/_core/realtime/audience.py:194` |
| Prefer `narration`, retire field-fishing | `ee/pocketpaw_ee/cloud/activity/buffer.py::_summarise()` |
| Consume `narration`; **delete** the 9-entry map | `paw-enterprise/src/lib/core/chat/store.svelte.ts:706-720` |
| New pinned plan panel | paw-enterprise (new component, bound to `agent.plan_updated`) |
| Channel adapters | no change — narration is server-side, so they inherit it |

The plan panel is a **pinned panel that mutates in place**, not chat messages. A plan re-printed
on every update is what makes agent transcripts unreadable. The thread-1 narration line renders as
the sub-line of the currently `in_progress` item: thread 1 answers "what is it doing right now",
thread 2 answers "where does that sit in the plan". One event contract underneath both, so the two
indicators cannot disagree on screen.

## Risks & reversal cost

1. **Credential/PII leak through narration.** The central risk. Mitigated by the `safe_args`
   allowlist plus a mechanical test that every template references only allowlisted fields, and by
   truncation. **Requires a `security-auditor` pass before merge** — this touches the 7-layer stack.
2. **Default-backend coverage.** `claude_agent_sdk` is the shipped default and its `TodoWrite`
   payload is unverified in this codebase. If the normalizer is wrong, the panel is blank for most
   users while working perfectly in dev on pydantic-ai. Verify against a real payload first.
3. **Event flood.** `write_plan` is called at the start *and* end of every step, and it resends the
   whole list each time. Mitigate by emitting only on actual change (hash the item list) and
   coalescing bursts.
4. **Ordering.** Whole-list replacement plus out-of-order delivery would let a stale plan overwrite
   a fresh one. `seq` addresses this, but whether the realtime layer already guarantees per-run
   ordering is unconfirmed.
5. **Mission Control regression.** Retiring `_summarise()`'s fallback changes what operators see in
   the ticker. Check the ticker explicitly rather than assuming narration is strictly better there.

**Reversal cost.** LOW for the bridge and event work — all fields are additive and `tool` stays
populated, so an old client keeps working. MEDIUM for `BaseTool.narration` across 48 modules,
though it is optional and additive, so a partial state is valid and revert is per-tool rather than
all-or-nothing.

## Open questions

1. **What does `claude_agent_sdk` actually put on the wire for `TodoWrite`?** Blocks the
   default-backend normalizer. Must be captured from a live run, not inferred.
2. **Does the bridge emit `agent.tool_use`, `agent.tool_start`, or both** for a pydantic-ai run?
   Both exist in the bus vocabulary and the activity buffer accepts all three; narration must
   attach to whichever actually fires, or it will be silently absent.
3. **Does the realtime layer guarantee per-run event ordering?** If yes, `seq` is belt-and-braces;
   if no, it is load-bearing.
4. **Who owns the product voice for ~100 templates?** The seeding pass produces the diff; the
   review pass is a human decision and should be named before the pass runs.

## Non-goals

- i18n and per-surface phrasing (structured `Narration` keeps the door open; not built now).
- Pocket-level narration overrides.
- The planner MCP's `todos[]` build brief — different lifecycle, not this surface.
- Plan persistence or historical replay. Plans stay per-run and in-memory, as `PlanState` is today.
- Replacing the Instinct decision feed or Mission Control's ticker. This feeds them; it does not
  restructure them.
