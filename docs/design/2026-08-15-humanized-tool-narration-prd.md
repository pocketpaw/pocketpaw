<!-- PRD — humanized tool narration + normalized agent plan surface.
     Created 2026-08-15. Stage ② (to-prd). Next: /to-issues. Status: Draft.
     Supersedes nothing. Source design: 2026-08-15-humanized-tool-narration.md
     Both stage-① open questions were resolved from source before writing; see Approach D5/D6. -->

# Humanized tool narration + a normalized agent plan surface

**Source:** brainstorm 2026-08-15 → `docs/design/2026-08-15-humanized-tool-narration.md`
**Status:** Draft · **Priority:** High · **Size:** L
**Repos:** pocketpaw (primary), paw-enterprise (render side)
**Branch:** `feat/humanized-tool-narration` · **Worktree:** `D:/paw-worktrees/humanized-tools`

## Goal

Every surface that shows agent activity says what the agent is actually doing, in words a
non-developer reads without translation — "Searching the web for *quarterly filings*",
"Publishing the site", "Inviting *sam@acme.com* to the workspace" — instead of
`using pocketpaw_sites_publish`. At the same time the agent's ordered plan becomes a live,
pinned UI component that mutates in place as steps complete, rather than being invisible.

Both are delivered by one server-side layer, so chat, Mission Control's ticker, and every
channel adapter (Telegram, Discord, Slack, WhatsApp, CLI) inherit it without per-surface work.

## Why now

Users currently read raw snake_case tool identifiers in production chat. The only humanization
that exists — a 9-entry map at `paw-enterprise/src/lib/core/chat/store.svelte.ts:709` — was
written for Claude Agent SDK tool names and has been silently wrong for every PocketPaw-native
tool since a second backend landed. The tool surface is now 48 builtin modules plus ~49 bridged
MCP tools plus the connector surface, so the gap widens with every release. Separately, the
pydantic-ai backend already maintains a real ordered plan via the harness `Planning` capability
and throws all of it away at the UI boundary.

## Non-goals

- **i18n and per-surface phrasing.** The `Narration` object is structured from day one so these
  are additive later, but nothing is built now.
- **Pocket-level narration overrides.** Plausible; not v1.
- **The planner MCP's `todos[]` build brief** (`ee/.../mcp_servers/planner.py:528`). Different
  lifecycle — a build brief walked by pocket_specialist, not a live agent checklist.
- **Plan persistence or historical replay.** Plans stay per-run and in-memory, as `PlanState` is.
- **Restructuring Mission Control or the Instinct decision feed.** This feeds them; it does not
  redesign them.
- **Retrofitting narration onto MCP tools' upstream definitions.** External tools are covered by
  the override registry and the derive-from-name fallback, not by changing their sources.

## Global Constraints

These bind every build chunk and flow verbatim into the stage-③ tasks doc. No task re-litigates them.

1. **Target branch:** all pocketpaw work branches from and targets `dev`. paw-enterprise work
   targets its own default branch. Never push to `main`.
2. **Worktree isolation is mandatory.** Any agent running `git checkout -b`, committing, or
   pushing gets its own worktree. pocketpaw and paw-enterprise are separate repos and get
   separate worktrees.
3. **Never merge on green.** Open the PR, report the URL, stop. The captain merges.
4. **Argument interpolation is a security boundary.** No tool argument is ever rendered into
   narration unless it is named in that tool's `safe_args` allowlist. This is enforced by test,
   not by review. Any chunk touching narration rendering is security-relevant.
5. **Backward compatibility on the wire.** The existing `tool` field in `AgentToolUse.data` stays
   populated. All new fields are additive so an older client keeps working.
6. **`topics.gen.ts` is generated.** Never hand-edit; regenerate via
   `uv run python backend/scripts/gen_topics.py`.
7. **Glossary bindings:** Pocket = workspace container; Connector = workspace-scoped integration;
   Ripple = generative UI layer; Instinct = decision pipeline. No LLM-default readings.
8. **Docs in the same PR** as the behavior they describe.
9. **`/humanize` every PR title and body** before opening.

## Approach

### Decision 1: narration is authored on the tool, not in a central map

**What:** an optional `Narration` object on `BaseTool`, alongside `trust_level`:

```python
@dataclass(frozen=True)
class Narration:
    active: str                        # "Searching the web for {query}"
    bare: str                          # "Searching the web"  (args missing/redacted)
    safe_args: tuple[str, ...] = ()    # allowlist — ONLY these may interpolate
```

**Why:** which of a tool's arguments are safe to display is knowledge only the tool has, and it is
a security property of the same class as `trust_level`. `shell`, `pip_install`, and the connector
tools all accept arguments that can carry credentials, tokens, or PII; rendering them publishes a
secret to every channel adapter. Once the allowlist must live on the tool, splitting the template
into a second file buys nothing. Supporting evidence: the central-map design has already been
tried here and already rotted — `store.svelte.ts:709` is that design, incomplete and silently so,
because nothing fails when an entry is missing.

**Tradeoff accepted:** touching ~48 modules instead of one file, and losing the ability to read
the entire product voice in a single file. Decision 3 recovers the review ergonomics.

### Decision 2: a deterministic derive-from-name fallback, so partial rollout still wins

**What:** when a tool declares no narration, fall back in order — (1) override registry for MCP and
connector tools, (2) derive from the name via a small verb lexicon (`publish`→Publishing,
`create`→Creating, `search`→Searching, `invite`→Inviting), so `pocketpaw_sites_publish` reads
"Publishing the site", (3) `using <name>` as last resort.

**Why:** it makes the rollout monotonic. Narration improves before all ~100 tools are annotated, so
a half-finished migration is a net gain rather than a stranded refactor. This is the specific
mitigation for the "cross-cutting change goes stale at 60%" failure mode.

**Tradeoff accepted:** derived phrasing is blander than hand-written and occasionally awkward for
tools whose names do not start with a verb. Acceptable because it is strictly better than the
status quo and is superseded per-tool as annotations land.

### Decision 3: templates are seeded by a one-time model-drafted pass, reviewed as one diff

**What:** a model drafts `Narration` for all builtin tools in a single pass; a human reviews the
whole set as one diff and edits for voice before commit.

**Why:** recovers the one real advantage of a central file — reading and unifying the product voice
in a single sitting — without keeping the central file. No generated line ships unread.

**Tradeoff accepted:** one large review diff. Bounded, and it happens once.

### Decision 4: one normalized plan event, whole-list replacement

**What:** a new `agent.plan_updated` realtime event carrying the full ordered list, with a status
superset of `pending | in_progress | completed | cancelled`, a per-run monotonic `seq`, and a
`progress` rollup. One normalizer per source: `write_plan` (pydantic-ai), `write_todos`
(deep_agents), `TodoWrite` (claude_agent_sdk).

**Why:** three backends emit three incompatible schemas for the same concept, and the default
backend is not the one being developed against. Normalizing at the bridge means the frontend binds
to one stable shape and never learns backend names. Whole-list replacement is inherited from
`write_plan`'s own semantics, which eliminates diffing and partial-update ordering bugs; `seq`
prevents a stale list from overwriting a fresh one if delivery reorders.

**Tradeoff accepted:** larger payloads than a delta protocol, and a normalizer to maintain per
backend. Both are cheap next to the class of bugs deltas would introduce.

### Decision 5: narration attaches to `agent.tool_use` — `agent.tool_start` is dead

**Resolved from source, 2026-08-15** (stage-① open question 2). `AgentToolStart` is defined at
`ee/.../_core/realtime/events.py:352`, is subscribed in `audience.py:194` and accepted by
`activity/buffer.py:180`, and is **emitted nowhere in the codebase**. The buffer docstring at
`buffer.py:174` claiming the bus emits it is inaccurate.

**What:** attach narration to `agent.tool_use` only. Leave `AgentToolStart` untouched; do not
build on it and do not delete it in this work.

**Tradeoff accepted:** a known-dead event stays in the registry. Removing it is unrelated cleanup
and would widen this PR's blast radius.

### Decision 6: the argument drop happens at TWO layers, and both must be fixed

**Resolved from source, 2026-08-15** (stage-① open question 1). The stage-① design assumed a single
drop point. It is two:

| Layer | Location | Behavior |
|---|---|---|
| Backend, claude_agent_sdk **streaming** path | `src/pocketpaw/agents/claude_sdk.py:3154-3161` | on `content_block_start`, emits `metadata={"name": …, "input": {}}` — hardcoded empty — and adds the name to `_announced_tools`. |
| Backend, claude_agent_sdk **block** path | `src/pocketpaw/agents/claude_sdk.py:3242-3253` | the completed `AssistantMessage` carries **fully-assembled real arguments** (`_extract_tool_info` reads `block.input` off the SDK's `ToolUseBlock`, `:1156-1162`) — but the guard `if tool["name"] not in _announced_tools` at `:3243` **suppresses the emission**, because the streaming path already announced that name. |
| Backend, pydantic-ai | `src/pocketpaw/agents/pydantic_ai.py:2156` | emits `"input": args` — args present. |
| Bridge (all backends) | `ee/.../cloud/shared/agent_bridge.py:582` | reads only the tool name; discards `metadata["input"]` entirely. |

> **Corrected 2026-08-15 by the HTN-0 spike.** This decision originally stated that the streaming
> path discards incrementally-streamed input and that the fix required accumulating
> `input_json_delta` fragments. **That was wrong.** The SDK assembles the arguments itself and
> delivers them intact on the `AssistantMessage`; pocketpaw's own dedup guard is what drops them.
> No JSON-fragment assembly is needed anywhere.

**What:** fix the bridge for all backends (chunk 1) *and* stop the `_announced_tools` guard from
suppressing the args-bearing `AssistantMessage` emission (chunk 5). The likely minimal shape: the
streaming path announces the name for a fast indicator without claiming to carry input, and the
`AssistantMessage` path still emits, upgrading the narration from `bare` to `active` once the real
arguments land. One extra `tool_use` event per tool call is acceptable — the surfaces render a
status line that is replaced, not appended, so an upgrade is invisible except as better text.

**Why this matters more than it looks:** `TodoWrite` on the default backend currently reaches the
bridge with no arguments. Without chunk 5, the plan panel renders perfectly against pydantic-ai in
development and is **blank in production for default-config users**. Chunk 5 is not optional
polish; it is what makes the feature real for most of the install base.

**Tradeoff accepted:** one duplicate `tool_use` event per call on the streaming path. Cheaper than
the alternative of delaying the activity indicator until the assistant message completes.

## Open questions

- [ ] **Does the realtime layer guarantee per-run event ordering?** If yes, `seq` is
  belt-and-braces; if no, it is load-bearing. Needs a read of the realtime delivery path —
  research spike, before chunk 6.
- [ ] **Who owns the product voice for ~100 narration templates?** The seeded pass produces the
  diff; the reviewer is a human decision and should be named before chunk 4 runs. Needs captain.
- [ ] **Does retiring `_summarise()`'s field-fishing change what Mission Control operators see?**
  Needs a look at the live ticker, not an assumption that narration is strictly better there.
  Before chunk 8.
- [ ] **Is the claude_agent_sdk streaming path the one chat actually uses**, or is it only hit
  under a specific config? Determines whether chunk 5 is release-blocking or a follow-up.
  Needs a live run — before sequencing chunk 5.

## Success metrics

**Quantitative**
- Zero raw snake_case tool identifiers rendered in chat for the top 20 tools by call volume.
- 100% of builtin tools declare `Narration`, enforced by a failing test — not a sampled audit.
- Plan panel populates on both `pydantic_ai` and the default `claude_agent_sdk` backend.
- No measurable added latency on the tool-call path (narration is a dict lookup and a format call).

**Qualitative**
- A non-developer watching the chat can describe what the agent just did without asking.
- The two ad-hoc narration implementations (`statusMap`, `_summarise` field-fishing) are deleted,
  not merely bypassed.

## Build chunks

Dependency order. Estimates in agent-hours per the workspace's agent-time budgeting rule.

| # | Chunk | Files touched | Est. |
|---|-------|---------------|------|
| 1 | **Bridge carries arguments.** Extract `metadata["input"]` alongside the name; add an (initially empty) `narration` field to `AgentToolUse.data`; keep `tool` populated. Tests assert args survive the bridge. | `ee/.../cloud/shared/agent_bridge.py` | 0.5 |
| 2 | **`Narration` contract + renderer + fallbacks.** Dataclass, render function, `safe_args` enforcement, truncation/newline-stripping, verb lexicon, override registry. Full unit tests. No tool annotations yet — the fallback alone improves every surface. | `src/pocketpaw/tools/protocol.py`, new `src/pocketpaw/tools/narration.py` | 1.5 |
| 3 | **Frontend consumes narration; delete the map.** Render `data.narration`; remove the 9-entry `statusMap`. | `paw-enterprise/src/lib/core/chat/store.svelte.ts` | 0.5 |
| 4 | **Annotate builtin tools** (seeded pass, Decision 3) + the mechanical test that every builtin declares `Narration` and references only allowlisted fields. | ~48 modules under `src/pocketpaw/tools/builtin/`, new test | 2.0 |
| 5 | **claude_agent_sdk streaming arg capture.** Accumulate `input_json_delta`, emit on `content_block_stop`. Unblocks the default backend. Highest-risk chunk; isolated. | `src/pocketpaw/agents/claude_sdk.py` | 1.5 |
| 6 | **Plan event + normalizers.** `AgentPlanUpdated`, normalizers for the three sources, `seq`, change-hash coalescing, topic regen, audience wiring. | `ee/.../_core/realtime/events.py`, `audience.py`, `agent_bridge.py`, new normalizers, `topics.gen.ts` (generated) | 2.0 |
| 7 | **Pinned plan panel.** Mutating-in-place component bound to `agent.plan_updated`; narration renders as the sub-line of the `in_progress` item. | paw-enterprise (new component + mount) | 2.0 |
| 8 | **Activity buffer prefers narration.** Retire `_summarise()` field-fishing; verify the Mission Control ticker does not regress. | `ee/.../cloud/activity/buffer.py` | 0.5 |
| 9 | **Security audit pass.** `security-auditor` review of the interpolation path against the 7-layer stack. | review only | 0.5 |

**Total: ~11 agent-hours.** Chunks 1→2→3 form the first shippable slice (narration live, fallback
phrasing, two surfaces improved). Chunks 6→7 form the second (plan panel). Chunk 5 gates the plan
panel's usefulness on the default backend.

**Suggested PR grouping:** (1+2+3) narration foundation · (4) template annotations · (5) streaming
args · (6+7) plan surface · (8+9) cleanup and audit. Five PRs, each independently reviewable.

## Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Credential or PII rendered into narration and broadcast to every channel adapter | Medium | **Critical** | `safe_args` allowlist; mechanical test that templates reference only allowlisted fields; truncation and newline-stripping; dedicated `security-auditor` pass (chunk 9) before merge |
| Plan panel blank in production while working in dev — default backend's streaming path carries no args | **High** if chunk 5 is skipped | High | Chunk 5 is scoped as release-blocking for the plan feature; verify against a live default-config run, not a unit test |
| Chunk 5 destabilizes streaming in a 3000-line backend module | Medium | High | Isolated chunk, own PR, own tests; the block path at :3251 is untouched and remains the reference behavior |
| Event flood — `write_plan` fires at start and end of every step, resending the whole list | High | Medium | Emit only on actual change (hash the item list); coalesce bursts |
| Out-of-order delivery lets a stale plan overwrite a fresh one | Low | Medium | Per-run monotonic `seq`; frontend ignores lower `seq`. Open question on whether ordering is already guaranteed |
| Annotation migration stalls at partial coverage | Medium | Low | Derive-from-name fallback (Decision 2) makes partial coverage a net gain; the mechanical test surfaces the gap rather than hiding it |
| Mission Control ticker regresses when `_summarise` field-fishing retires | Low | Medium | Chunk 8 verifies the live ticker explicitly; open question tracked |
| `TodoWrite` normalizer written against a guessed payload | Medium | High | Capture a real payload from a live default-backend run before writing the mapper |
