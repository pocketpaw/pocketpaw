# Pocket Specialist & Ripple Mutation

*Status: living document. v1 captures what shipped on [#1069](https://github.com/pocketpaw/pocketpaw/pull/1069); subsequent sections lay out the design path the team has agreed to and the gaps that the next PRs need to close.*

*Owners: Rohit (current implementation), Prakash (direction). Reviewers: anyone touching `ee/ripple/`, `ee/cloud/pockets/`, or `src/pocketpaw/agents/sdk_mcp_pocket.py`.*

---

## TL;DR

Pocket creation and editing are no longer the main chat agent's job. They run on a dedicated **`pocket_specialist`** subagent, called as a tool from the main agent. Mutation tools (`create_pocket`, `update_pocket`, `add_widget`, `update_widget`, `remove_widget`) are filtered off the main agent's allowlist and live only on the specialist — architecture enforced structurally, not by instruction. Result on chat-mode prompt cost: ~12k → ~1.7k tokens per turn.

The shape we want long-term is the same shape Claude Code uses on files: the agent **reads** the current state, **edits** in place with surgical ops, **observes** the result, and the platform records every op for replay, undo, and Instinct review. PR #1069 builds the agent boundary; the **granular mutation surface** is the next move.

---

## Why this exists

Three problems collide on every Ripple-pocket interaction:

1. **Token economics.** The full pocket-mode prompt runs ~12k tokens. Every chat turn — even a one-line "what pockets do I have?" — paid for the entire creation + interaction prompt before this work landed. Most turns don't need it.
2. **Mutation fragility.** Today an "add a chart" request makes the agent: `get_pocket` → reason about the full tree → emit a complete new `ripple_spec` → `update_pocket(pocket_id, ripple_spec=<the entire thing>)`. The agent has to **rewrite untouched panes verbatim** to preserve them. Every rewrite is a chance to drop a binding, lose a handler, hallucinate a prop, or strip an interactive control.
3. **Architectural enforcement.** Telling the model "don't call `add_widget` for visible changes" via prompt alone is a request, not a guarantee. With wide tool access, models eventually find the wrong tool. Filter the tools off the allowlist and the wrong call stops being possible.

The pattern that solves all three is the same pattern Anthropic landed on for Claude Code: **agents that edit state should look like agents that edit files.** They `Read`, they `Edit`, they `Write` — surgical, observable, reversible, audit-trailable. PR #1069 is step one toward that for Ripple pockets.

---

## What shipped on PR #1069

### 1. The `pocket_specialist` subagent

A `claude_agent_sdk.AgentDefinition` registered on the `ClaudeAgentOptions.agents` map (`src/pocketpaw/agents/claude_sdk.py:_build_pocket_specialist_agent_def`). The main agent reaches it through the SDK's built-in `Agent` tool with `subagent_type="pocket_specialist"`. The specialist owns:

- The heavy `POCKET_CREATION_PROMPT_MCP` + `POCKET_INTERACTION_PROMPT_MCP` text (`ee/ripple/_pockets.py`).
- The full mutation tool whitelist: `create_pocket`, `update_pocket`, `add_widget`, `update_widget`, `remove_widget`.
- Read tools needed during edits: `get_pocket`, `list_pockets`, `get_widget_spec`.

### 2. Tool-allowlist filtering on the main agent

`src/pocketpaw/agents/claude_sdk.py:_POCKET_MUTATION_TOOL_IDS` is removed from `allowed_tools` before the `ClaudeAgentOptions` is constructed. The main agent literally cannot call mutation tools. `Agent` is added to `_TOOL_POLICY_MAP` (mapped to `shell` privilege) so subagent invocation passes the policy gate even on restrictive profiles.

### 3. `POCKET_DELEGATION_RULE`

A small block in the main chat agent's system prompt that teaches it *when* and *how* to delegate. It references the literal subagent name (`pocket_specialist`) — there's a contract test asserting both sides of the string match.

### 4. Slim inline-ripple prompt + on-demand catalog

`INLINE_RIPPLE_SYSTEM_PROMPT` cut from ~9k to ~1.7k tokens. The slim prompt names six core widgets (`text`, `heading`, `stat`, `button`, `table`, `flex`); everything else (charts, kanbans, sparklines, gauges, timelines) lives behind a new `get_inline_widget_help` MCP tool that returns the relevant catalog slice on demand (`ee/ripple/_inline_core.py`).

### 5. Cache-friendly system prompt assembly

`build_context_block` reordered so static content leads and dynamic `<scope>` / `<participants>` / `<current-pocket>` tags trail. The static prefix now hits Anthropic's prompt cache. Combined with the slim chat prompt, this is where the ~12k → ~1.7k chat-mode reduction comes from.

### 6. Ripple resolver — array methods + bracket indexing

`ee/cloud/ripple_resolver.py` (new) adds `.where(field, op, value)`, `.whereIn(field, [values])`, `.sortBy(field)`, `.limit(n)`, `.reverse()`, plus bracket indexing (`tasks[0]`, `state.tasks[state.selected_index]`). Fixes the **dead-binding pattern** where filter/sort controls couldn't actually filter or sort because expressions had no way to express the operation.

### 7. Ripple normalizer — dead-handler stripper

`ee/cloud/ripple_normalizer.py` strips `entity-detail.props.actions[]` items lacking handlers and lifts items using `on_click` instead of `actions` to the right field. Fixes the **dead-button pattern** where an action item rendered visually but did nothing on click.

### 8. Backend gating

The specialist mechanism is Claude-only — it relies on `ClaudeAgentOptions.agents` and the SDK's built-in `Agent` tool. Other backends (`openai_agents`, `google_adk`, `codex_cli`, `opencode`, `copilot_sdk`, `deep_agents`) fall back to the pre-split path with the heavy pocket prompt inlined. They still work — they just don't get the token savings or the architectural separation. Universal backend-agnostic version is the planned next step.

---

## How a mutation flows today

```
┌─ Main chat agent (slim prompt, no mutation tools) ────────────────┐
│                                                                   │
│  user: "add a status badge to the alice row that turns red on     │
│         overdue"                                                  │
│                                                                   │
│  agent reasons: this is pocket-mutation intent.                   │
│  agent calls: Agent(                                              │
│    subagent_type="pocket_specialist",                             │
│    description="add overdue badge",                               │
│    prompt="On pocket <id>: add a status badge..."                 │
│  )                                                                │
└────────────────────────┬──────────────────────────────────────────┘
                         │
┌────────────────────────▼──────────────────────────────────────────┐
│ pocket_specialist subagent (full pocket prompt, mutation tools)   │
│                                                                   │
│  1. get_pocket(pocket_id) → full document                         │
│  2. reason locally; build the FULL new ripple_spec                │
│  3. update_pocket(pocket_id, ripple_spec=<entire new tree>)       │
│  4. return short status to main agent                             │
└────────────────────────┬──────────────────────────────────────────┘
                         │
┌────────────────────────▼──────────────────────────────────────────┐
│ Main agent relays the specialist's status to the user.            │
└───────────────────────────────────────────────────────────────────┘
```

The architecture **separation** is good. The mutation **granularity** is the part we still need to fix.

---

## The MCP tool surface

Defined in `src/pocketpaw/agents/sdk_mcp_pocket.py`. Tools are registered on the in-process `pocketpaw_pocket` SDK MCP server and namespaced as `mcp__pocketpaw_pocket__<tool>`.

| Tool | Args | Returns | Where it lives |
|---|---|---|---|
| `get_pocket` | `pocket_id: str` | full pocket document | both agents |
| `list_pockets` | — | list of `{id, name, description, type, icon, color}` | both agents |
| `create_pocket` | `name, description, type, icon, color, ripple_spec` | `{ok, pocket, pocket_id}` | specialist only |
| `update_pocket` | `pocket_id, ripple_spec?, name?, description?, icon?, color?` | updated pocket document | specialist only |
| `add_widget` ⚠️ | `pocket_id, widget` | updated pocket | specialist only |
| `update_widget` ⚠️ | `pocket_id, widget_id, fields` | updated pocket | specialist only |
| `remove_widget` ⚠️ | `pocket_id, widget_id` | updated pocket | specialist only |
| `get_widget_spec` | `types: list[str]` | markdown reference per requested widget type | both agents |
| `get_inline_widget_help` | `types: list[str]` | catalog slice for chat-inline rendering | both agents |

⚠️ **`add_widget` / `update_widget` / `remove_widget` mutate the legacy embedded `widgets` array, not `rippleSpec.ui`.** The desktop client renders only from `rippleSpec.ui`, so writes to the legacy array are invisible. The system prompt explicitly tells the specialist not to use these for visible changes — they're held for backwards compatibility with older pockets.

**This is a key gap.** We have *names* like `add_widget` and `update_widget` already on the surface, but they target the wrong field. The next PR's job is to repurpose those names (or add new ones) to mutate `rippleSpec.ui` granularly with stable IDs. See [§ Next moves](#next-moves) below.

---

## Design principles

These are the rules the implementation already follows; document them so future PRs don't drift:

1. **Architectural enforcement beats instruction.** If a tool shouldn't be called from a context, remove it from that context's allowlist. Don't rely on "please don't call this" in the prompt.
2. **Read tools stay cheap and broadly available.** `get_pocket`, `list_pockets`, `get_widget_spec`, `get_inline_widget_help` are read-only and stay on the main agent so conversational queries don't pay subagent overhead.
3. **Heavy domain prompt lives with the agent that needs it.** The 10k-token pocket prompt rides with the specialist. The chat agent gets a 200-token delegation rule pointing at it.
4. **Tool definitions are the contract, not docs.** Each MCP tool carries a Zod-style schema and a docstring that doubles as the LLM-facing description. The specialist's behavior is shaped by the tool descriptions, not by separate prompt instructions about each tool.
5. **List before create.** Hard-coded gate in every creation prompt: the agent runs `list_pockets` first; same-name / same-intent pockets get extended via `update_pocket` rather than spawning duplicates. (Soft gate today — the prompt asks the agent to confirm with the user. Could harden later.)
6. **Interactive by default.** Every new pocket gets at least one in-canvas control wired to top-level `state` via `bind` + `on_click` action chains. Edits preserve interactivity instead of stripping it. The pattern is canonical and lives in `_INTERACTIVE_DEFAULT_BLOCK`.
7. **Real values only.** No "TBD", "...", or null in display content. Estimates get a `~` prefix. No widget without data is shipped.
8. **Two-pass UI for streaming.** Streaming partial JSON drops invalid enum values mid-stream rather than rendering a half-typed widget type. Lives in the renderer, not in the agent — but the agent benefits because partial output never visibly breaks.

---

## Open gaps (in priority order)

### Gap 1 — Granular UI-tree mutations *(biggest payoff)*

**Problem.** `update_pocket(ripple_spec=<full tree>)` is the only way to change anything visible. To rename one row in a table, the specialist re-emits the entire spec. That's wasteful and fragile — the rewrite is where bindings get lost.

**Fix.** Stable IDs on every UINode + ID-targeted mutation tools. The shape:

```
read_node(pocket_id, id?)              → returns the subtree (or whole spec)
list_nodes(pocket_id, parent_id?)      → flat list of {id, type, label?}
add_node(pocket_id, parent_id, spec, after_id?)
replace_node(pocket_id, id, spec)
set_node_prop(pocket_id, id, prop, value)
move_node(pocket_id, id, new_parent_id, after_id?)
remove_node(pocket_id, id)
```

Each call mutates `rippleSpec.ui` server-side, returns the changed subtree (not the full document), and emits a `pocket_widget_changed` SSE event the client can apply incrementally.

The names `add_widget` / `update_widget` / `remove_widget` are already on the MCP surface — repurposing them to target `rippleSpec.ui` (instead of the legacy embedded array) is the natural migration. The legacy-array versions get retired once no client depends on them.

**Why this matters.** Same shape as Claude Code's `Read` / `Edit` / `Write` on files. Same operational properties: idempotent, surgical, observable, audit-trailable. Compounds with every gap below.

### Gap 2 — Inverse op log → undo/redo

Once mutations are granular, every op has a defined inverse: `addNode ↔ removeNode`, `setProp(new) ↔ setProp(old)`, `moveNode(a → b) ↔ moveNode(b → a)`. Store the last N inverses in a per-pocket ring buffer and you get:

- Undo / redo on the canvas.
- Optimistic UI with rollback when a server write fails.
- "Agent made a wrong edit, retry from before that op" without re-streaming the whole conversation.

The Command pattern, applied properly. Not new tech — just discipline.

### Gap 3 — Vision-in-the-loop verification

After each mutation cycle, snapshot the rendered canvas (Playwright server-side, or `html2canvas` client-side) and feed the screenshot back to a vision-capable model with the question *"did the requested change actually happen?"*. Closes the loop on the most common silent-failure mode (typo'd prop names, wrong target IDs, `bind` strings that don't match state shape). The validator can be the same specialist or a smaller second pass — either way, the human stops being the only one who notices when an edit didn't visibly land.

### Gap 4 — Backend portability

The specialist mechanism is gated to `claude_agent_sdk` because it leans on `ClaudeAgentOptions.agents` + the built-in `Agent` tool. Other backends fall back to the inlined heavy prompt — they work, but they pay the full token cost and don't get the architectural separation.

Universal version probably looks like a backend-agnostic "subagent" wrapper that compiles to:
- `claude_agent_sdk` → `AgentDefinition` + `Agent` tool (today's path)
- `openai_agents` → handoff agent
- `google_adk` → sub-agent
- `deep_agents` → planning subagent
- everything else → an in-process synthetic subagent that's just a second `query()` call with the specialist's prompt + tool slice

Rohit flagged this as the planned next step. Concrete spec deserves its own RFC.

### Gap 5 — Streaming as op stream, not partial JSON

Today the specialist emits `update_pocket(ripple_spec=<huge JSON>)` and the renderer parses partial JSON to render progressively. With granular ops (gap 1), each tool call is its own atomic unit — the renderer applies them as they arrive, no partial-JSON gymnastics. This removes one of the messiest pieces of the current renderer.

### Gap 6 — Instinct integration

Every pocket mutation is, conceptually, a proposed action: the agent is going to change something the user sees. PocketPaw already has Instinct (the approval/audit layer at `ee/instinct/`). Wiring pocket mutations through Instinct gives:

- Captain (or end-user) review of mutations before they land — same gate as `send_email`.
- Reasoning-trace capture per mutation, not per turn.
- Outcome metering — was the rename right? Did the user revert it within 30 seconds?
- Audit log of *every* edit, queryable like any other Instinct event.

Default policy can be permissive (auto-approve) for low-risk ops; sensitive surfaces (workspace pockets, shared with other members) can require explicit approval. This composes cleanly with gap 2 — undo *is* a revert flow Instinct can offer.

### Gap 7 — Soul memory for design preferences

The specialist's soul records "captain prefers density over whitespace", "we use rounded corners not sharp", "this user reverted the last three pie charts to bar charts". Survives across sessions. Loads at the start of every specialist invocation. Makes the design system something the agent *learns* rather than something it *reads from a static prompt*.

### Gap 8 — Named-statement spec view *(long-term)*

OpenUI Lang ([thesysdev/openui](https://github.com/thesysdev/openui)) flattens the spec into a dictionary of named statements: `root = Stack([header, chart, tbl])`, `chart = PieChart(...)`. Incremental edits become trivial dictionary union by name; their measurement: 85% token reduction on followup edits.

We don't need to adopt the storage format to get most of the benefit. The path is:

1. Land stable IDs (gap 1). The IDs *are* the names.
2. Provide a "named-statement view" serializer the agent reads from. The view is the thin DSL; the storage stays JSON.
3. When (and only when) the token cost on long-session refinement hurts in real numbers, lift the view to be the source of truth.

This is *years*-out work; documenting the destination so we don't accidentally choose a path that closes the door.

---

## Next moves

The captain reviews PR scope; this is a proposal, not a commitment.

| # | PR | Scope | Depends on |
|---|---|---|---|
| 1 | `feat(ripple): granular UI-tree mutation tools` | Stable IDs on UINodes + new `add_node` / `replace_node` / `set_node_prop` / `move_node` / `remove_node` MCP tools targeting `rippleSpec.ui`. Repurpose existing `add_widget`/`update_widget`/`remove_widget` names. Specialist prompt updated to prefer granular ops over `update_pocket` for surgical edits. | #1069 |
| 2 | `feat(ripple): inverse op log + undo/redo` | Per-pocket ring buffer of inverse ops. New SSE event `pocket_op_committed` with op + inverse. Client-side undo/redo controls. | PR 1 |
| 3 | `feat(agents): backend-agnostic subagent wrapper` | Lift `pocket_specialist` registration into a backend-agnostic factory. Targets at minimum `openai_agents`, `google_adk`, `deep_agents`. | #1069 |
| 4 | `feat(ripple): vision verification on mutation` | After every specialist mutation cycle, snapshot the rendered canvas and run a vision check before returning success to the main agent. | PR 1 |
| 5 | `feat(instinct): pocket mutations as proposed actions` | Wire each pocket mutation through Instinct's proposal pipeline. Default policy permissive; per-pocket override. | PR 1 |

Tests track the contracts that already matter: `tests/test_pocket_specialist.py` asserts the delegation-rule string matches the registered subagent name; `tests/cloud/test_pocket_service_resolver.py` exercises the new resolver methods; `tests/test_sdk_mcp_inline_help.py` covers the catalog-slice tool. New PRs should add cross-file contract tests in the same style.

---

## Appendix A — File map

| File | Role |
|---|---|
| `src/pocketpaw/agents/sdk_mcp_pocket.py` | In-process MCP server. Tool definitions + handlers. |
| `src/pocketpaw/agents/claude_sdk.py` | Backend wiring. Specialist registration, allowlist filtering, `Agent` tool policy mapping. |
| `ee/cloud/pockets/agent_context.py` | The actual mutation logic (`fetch_pocket_for_agent`, `update_pocket_for_agent`, etc.). MCP server is a thin adapter over this. |
| `ee/cloud/pockets/service.py` | Beanie writes. Where the mutation actually persists. |
| `ee/ripple/_pockets.py` | All pocket-mode system prompts (creation × interaction × MCP × CLI). `POCKET_DELEGATION_RULE` lives here too. |
| `ee/ripple/_design.py` | The widget catalog + design rules. Spliced into every creation/interaction prompt. |
| `ee/ripple/_inline.py` / `_inline_core.py` | Slim chat-inline prompt + the catalog payload behind `get_inline_widget_help`. |
| `ee/cloud/ripple_resolver.py` | Expression evaluator. Owns `.where`, `.sortBy`, `.limit`, `.reverse`, bracket indexing. |
| `ee/cloud/ripple_normalizer.py` | Dead-handler stripper, `on_click → actions` lift. |
| `ee/cloud/ripple_sources.py` | `$source` marker hydration (`workspace.pockets`, `workspace.members`). |

## Appendix B — Pocket-mode prompt structure

Every pocket-mode prompt (creation or interaction, MCP or CLI variant) is assembled from the same blocks in the same order. Source: `ee/ripple/_pockets.py:_assemble_creation` / `_assemble_interaction`.

**Creation prompt:**
1. `_SCOPE_BLOCK` — what "pocket" means in this conversation
2. `_CANVAS_BLOCK` — `rippleSpec.ui` is the only renderable surface
3. `_LIST_BEFORE_CREATE_*` — the duplicate-avoidance gate
4. `_TOOLS_*` — tool surface description (MCP or CLI)
5. `_CREATION_OVERVIEW_*` — high-level "you're making a new pocket" framing
6. `_INTERACTIVE_DEFAULT_BLOCK` — controls + state + bind pattern
7. `_STATE_SOURCES_BLOCK` — `$source` markers for live workspace data
8. `_CREATION_EXAMPLES_*` — two minimal examples (todo app, revenue report)
9. `_RESEARCH_PROTOCOL` — multi-search research gate for display pockets
10. `RIPPLE_DESIGN_RULES` — the widget catalog + design quality bar (heavy)

**Interaction prompt:**
1. `_SCOPE_BLOCK`
2. `_CANVAS_BLOCK`
3. `_TOOLS_*`
4. `_WORKFLOW_INTERACTION_*` — read/write/chat classification + the "preserve untouched panes" rule
5. `_INTERACTIVE_DEFAULT_BLOCK`
6. `_STATE_SOURCES_BLOCK`
7. `RIPPLE_DESIGN_RULES`

The interaction prompt carries a literal `__POCKET_ID__` token that the specialist wiring substitutes at invocation time (`src/pocketpaw/agents/claude_sdk.py:_pocket_specialist_system_prompt`).

## Appendix C — Sequence: a small edit, end to end

```
User → Main agent (chat-mode):  "add a chart of completed tasks"

Main agent reasons: pocket-mutation intent. Calls:
    Agent(
      subagent_type="pocket_specialist",
      description="add completed-tasks chart",
      prompt="On pocket P-7f3: add a chart of completed tasks..."
    )

Specialist starts. System prompt = full pocket-mode interaction prompt
(POCKET_INTERACTION_PROMPT_MCP). Available tools: get_pocket,
list_pockets, get_widget_spec, create_pocket, update_pocket,
add_widget, update_widget, remove_widget.

Specialist calls: get_pocket(pocket_id="P-7f3")
  → returns full document including current rippleSpec.ui

Specialist calls: get_widget_spec(types=["chart"])
  → returns markdown reference for the chart widget

Specialist reasons locally; builds the FULL new ripple_spec preserving
every existing node and adding the chart node.

Specialist calls: update_pocket(
  pocket_id="P-7f3",
  ripple_spec=<entire new tree>
)
  → server persists, emits pocket_updated SSE, returns updated doc

Specialist returns to main agent: "Added a bar chart of completed tasks
to the dashboard."

Main agent relays to user.
```

In the post-Gap-1 world, the specialist's mutation step shrinks to:

```
Specialist calls: list_nodes(pocket_id="P-7f3", parent_id="dashboard-root")
  → flat list including {id: "dashboard-root", type: "flex", ...}

Specialist calls: add_node(
  pocket_id="P-7f3",
  parent_id="dashboard-root",
  spec={type: "chart", id: "completed-chart", props: {...}, ...},
  after_id: "tasks-table"
)
  → server inserts, emits pocket_op_committed, returns subtree
```

Two surgical calls, no full-spec rewrite. Same auditability. Same correctness. Far fewer ways to silently lose a binding on the way through.

---

## Cross-references

- [PR #1069](https://github.com/pocketpaw/pocketpaw/pull/1069) — implementation
- [thesysdev/openui](https://github.com/thesysdev/openui) — the named-statement / incremental-editing reference design (for Gap 8)
- `ripple/src/lib/schema/ui-spec.ts` — Ripple's UISpec v1.0 definition
- `ripple/src/lib/schema/universal-spec.ts` — Ripple's UniversalSpec v2.0 definition
- `ripple/src/lib/core/state-manager.svelte.ts` — runtime state container; eventual consumer of granular ops
- `ee/instinct/` — Instinct layer, eventual home for pocket-mutation review (Gap 6)
