<!-- Local task breakdown — humanized tool narration + normalized agent plan surface.
     Created 2026-08-15. Stage ③ (kept LOCAL, not filed in any repo).
     Source: docs/design/2026-08-15-humanized-tool-narration-prd.md
     Vertical slices, dependency-ordered. Next: /paw-execute. -->

# Humanized tool narration — Tasks (local, not filed)

## Status — wave 1 closed 2026-08-15

| Task | Status | PR | Evidence |
|---|---|---|---|
| HTN-0 | done | — | Rescoped HTN-4; findings inline below |
| HTN-1 | **PR open, awaiting captain** | [#1942](https://github.com/pocketpaw/pocketpaw/pull/1942) | 46 narration tests, 160 in sweep; security SOUND_WITH_FINDINGS, 6 fixed |
| HTN-4 | **PR open, awaiting captain** | [#1943](https://github.com/pocketpaw/pocketpaw/pull/1943) | 131 passed; correlation regression caught and fixed before PR |
| HTN-2 | ready | — | Carries inherited design + security notes below |
| HTN-3 | blocked on HTN-2 | — | See sequencing note |
| HTN-5 | ready | — | Unblocked by HTN-1 |
| HTN-6 | **blocked — needs captain** | — | Live `TodoWrite` payload capture; see HTN-0 finding 3 |
| HTN-7 | ready | — | Unblocked by HTN-1 |
| HTN-8 | first pass done | — | Re-audit when HTN-2/3 widen the surface |

**Neither PR is merged.** Per the never-merge-on-green gate, both wait on captain review.

Vertical slices from the PRD's 9 build chunks. The PRD's chunks were layered
(bridge → contract → frontend); they are re-cut here by shippable outcome, so the first
merge puts a real humanized phrase on a real screen instead of landing plumbing nobody can see.

**Global Constraints** (verbatim from the PRD — no task re-litigates these):

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

**Critical path:** HTN-0 → HTN-1 → HTN-5 → HTN-6.

```
HTN-0 ─▶ HTN-1 ─┬─▶ HTN-2 ─▶ HTN-3 ─▶ HTN-8
                │
                ├─▶ HTN-5 ─▶ HTN-6
                │             ▲
                ├─▶ HTN-7     │
                │             │
                └─▶ HTN-4 ────┘
```

**Cross-repo note:** HTN-1 and HTN-5 each span pocketpaw and paw-enterprise, so each ships **two
PRs** — backend first, frontend second, sequenced not stacked (separate repos). The slice is still
vertical: the unit of *value* is the visible phrase, not the layer.

---

## HTN-0 — Resolve the two sequencing unknowns · ~0.5 agent-hrs · **DONE (partial)**

> **Findings, 2026-08-15.**
> 1. **Streaming vs block path — ANSWERED.** Both fire. `claude_agent_sdk` delivers fully
>    assembled real arguments on the `AssistantMessage` (`_extract_tool_info` → `block.input`,
>    `claude_sdk.py:1156-1162`); pocketpaw suppresses that emission with the `_announced_tools`
>    guard at `:3243`. The arguments were never lost, so HTN-4 needs no `input_json_delta`
>    accumulation and is rescoped from ~1.5h/high-risk to ~0.5h/low-risk.
> 2. **Event ordering — DOWNGRADED to informational, not blocking.** The answer does not change
>    what gets built: `seq` is cheap and ships either way. It determines only whether `seq` is
>    load-bearing or belt-and-braces. HTN-5 proceeds without it.
> 3. **Live `TodoWrite` payload — STILL OPEN, needs the captain.** `TodoWrite` is a CLI-side
>    builtin bundled inside `claude_agent_sdk/_bundled/claude.exe`, so its shape is not readable
>    from source and requires a live default-backend run to capture. **HTN-6 is blocked on this**
>    and must not be written against an inferred payload.

**Slice:** the answers that decide whether HTN-4 is release-blocking and whether `seq` is
load-bearing. Cheap to run, expensive to guess wrong.

**Do:**
- Run a real chat turn on the default config and capture which `claude_sdk.py` path emits the
  `tool_use` event — the streaming path (`:3160`, `"input": {}`) or the block path (`:3251`,
  real args). This decides whether HTN-4 blocks the release or is a follow-up.
- Capture a live `TodoWrite` payload from that same run — HTN-6's normalizer must be written
  against a real payload, not an inferred one.
- Read the realtime delivery path and record whether per-run event ordering is guaranteed. If it
  is, `seq` in HTN-5 is belt-and-braces; if not, it is load-bearing.

**Done when:** a short findings note is appended to the PRD's Open questions section with the
captured `TodoWrite` payload pasted verbatim, and each of the three questions is marked resolved
with its answer. No code changes.

**Interfaces:**
- exposes: the real `TodoWrite` wire payload (consumed by HTN-6); a yes/no on ordering guarantees
  (consumed by HTN-5); a yes/no on whether the streaming path is live (re-prioritizes HTN-4)
- consumes: nothing

**Deps:** none.

---

## HTN-1 — One tool narrates, end to end · ~2.5 agent-hrs

**Slice:** a real `web_search` call renders as **"Searching the web for *quarterly filings*"** in
chat. The whole path proven on one tool before it is widened to a hundred.

**Do:**
- `src/pocketpaw/tools/narration.py` (new): the `Narration` frozen dataclass
  (`active` / `bare` / `safe_args`) and `render(narration, args) -> str`. Rendering rules:
  only `safe_args` fields interpolate; a missing/empty/redacted safe arg falls back to `bare`;
  values are newline-stripped and truncated to 80 chars.
- `src/pocketpaw/tools/protocol.py`: optional `narration` property on `BaseTool`, default `None`,
  alongside `trust_level`.
- Annotate **one** tool — `web_search` — as the reference implementation.
- `ee/.../cloud/shared/agent_bridge.py:582`: extract `metadata["input"]` alongside the tool name,
  render narration server-side, add a `narration` field to `AgentToolUse.data`. Keep `tool`
  populated (Global Constraint 5).
- `paw-enterprise/src/lib/core/chat/store.svelte.ts:706`: render `data.narration` when present,
  falling through to the existing behavior when absent. **Do not delete the `statusMap` yet** —
  it is the fallback until HTN-2 lands.
- Tests: renderer unit tests including the redaction and truncation paths; a bridge test asserting
  args survive and `narration` is populated.

**Done when:** a live `web_search` turn shows the interpolated phrase in chat, and a bridge test
asserts the emitted `AgentToolUse.data.narration` equals `"Searching the web for quarterly filings"`.

**Interfaces:**
- exposes: `Narration` dataclass and `render()` (consumed by HTN-2, HTN-3); `BaseTool.narration`
  property (consumed by HTN-3); `AgentToolUse.data.narration` wire field (consumed by HTN-7);
  tool arguments surviving the bridge (consumed by HTN-5)
- consumes: `AgentEvent.metadata["input"]` as already emitted by `pydantic_ai.py:2156`

**Deps:** HTN-0.
**PRs:** 2 — pocketpaw (backend), then paw-enterprise (render).

---

## HTN-2 — The unannotated tail stops showing raw identifiers · ~1 agent-hr

**Slice:** every tool that declares nothing still reads like English.
`pocketpaw_sites_publish` → "Publishing the site", not `using pocketpaw_sites_publish`.

> **Inherited from HTN-1, 2026-08-15 — read before starting.**
> HTN-1 shipped `_ANNOTATED_TOOLS` in `narration.py`: a hardcoded `tool name → (module, class)`
> map with exactly one entry. **It is a stopgap to DELETE, not an extension point to grow.** It
> maps to builtin *classes*, so MCP and connector tools — precisely what this task must cover —
> can never appear in it by construction.
>
> HTN-1 read `src/pocketpaw/tools/registry.py` in full and rejected it for three concrete
> reasons, so do not re-derive them: (1) `ToolRegistry` is instance-based (`__init__(self,
> policy=None)`) with no module-level singleton — `get_tool_registry` / `_global_registry` /
> `create_default_registry` all return zero hits across `src/`; (2) the bridge has no registry
> handle to reach — `_run_agent_response` only receives `instance` from `pool.get(agent_id)`, and
> `pool.py` exposes no tool registry; (3) registries are populated per-caller (e.g.
> `tools/cli.py:102` constructs and registers `WebSearchTool()` itself), and `builtin/__init__.py`
> uses lazy `__getattr__` over `_LAZY_IMPORTS` *specifically because* optional dependencies are
> missing in some installs — so walking every builtin to find narrations would import the world
> and can raise `ImportError`, just to phrase a status line.
>
> **The real fix is threading the agent's actual `ToolRegistry` through to the bridge.** That is
> this task's central design problem, not the verb lexicon.
>
> **From the HTN-1 security review, 2026-08-15 — three things this task inherits:**
>
> - **Read the narration off the live instance; never construct a tool to read a property.**
>   The registry already stores live instances (`tools/registry.py:47,52,61-63`), so use
>   `registry.get(name).narration`. HTN-1's `narration_for_tool` instantiates the class, which is
>   harmless for `WebSearchTool` but does not generalize: `ShellTool.__init__` calls
>   `get_settings()` (`tools/builtin/shell.py:22`), so a registry-wide version of that pattern
>   would construct settings — and whatever the credential store does on first load — on the event
>   loop, purely to phrase a status line.
>
> - **[MEDIUM] Narration must key on resolved tool IDENTITY, not the bare wire name.** Today the
>   lookup is a process-global name→class map with no namespace or ownership check, and
>   `registry.py:52` (`self._tools[tool.name] = tool`) overwrites silently on collision. On the
>   codex_cli backend the MCP branch (`agents/codex_cli.py:523-532`) emits the RAW unprefixed MCP
>   tool name while keeping `server` in a field the bridge never reads — so a user-added MCP server
>   exposing `web_search(query)` inherits the builtin's phrase, and every group member's ticker
>   asserts in the product's own voice that the agent is searching the web. Misattribution is the
>   core failure mode for a feature whose entire job is to say what the agent is doing. (On the
>   default `claude_sdk` backend MCP tools carry `mcp__<server>__<tool>` names, so there it
>   requires overriding a builtin name in the registry instead.) At minimum, refuse to narrate a
>   name that is not the builtin registry's own entry.
>
> - **Truncate before sanitizing, once large-arg tools are annotated.** HTN-1 sanitizes the full
>   untruncated value before capping at 80 chars. Negligible for `web_search.query`; it becomes
>   multi-MB copies per `tool_use` event on the response stream's own task the moment this task or
>   HTN-3 annotates `shell`'s command, a file write's content, or a connector payload. (A fix is
>   landing in HTN-1; verify it survived before annotating anything large.)
>
> **Sequencing: HTN-2 MUST land before HTN-3.** Until the registry lookup replaces
> `_ANNOTATED_TOOLS`, every tool HTN-3 annotates is dead code unless someone also hand-adds a line
> to that map. The dependency order below already reflects this; this note records why.

**Do:**
- Replace `_ANNOTATED_TOOLS` with a real lookup that reaches the agent's `ToolRegistry`, per the
  note above. Delete the stopgap map in the same change.
- Derive-from-name fallback in `narration.py`: strip vendor prefix, verb-first via a small
  deterministic lexicon (`publish`→Publishing, `create`→Creating, `search`→Searching,
  `invite`→Inviting, `delete`→Deleting, `update`→Updating, `list`→Listing, `send`→Sending).
  No LLM at runtime.
- Override registry for MCP and connector tools, which cannot self-declare.
- Fallback order: declared `Narration` → override registry → derive-from-name → `using <name>`.
- **Delete the 9-entry `statusMap`** at `store.svelte.ts:709` — the server now covers every case
  it did, and more.
- Tests: a table test over representative real tool names from the MCP and connector surfaces.

**Done when:** no tool in the top-20-by-call-volume renders a raw snake_case identifier, with a
test asserting the derived phrasing for a sample of unannotated tools; `statusMap` is gone.

**Interfaces:**
- exposes: the complete fallback chain (relied on by HTN-3 as the baseline it improves on)
- consumes: `render()` and the `Narration` contract from HTN-1

**Deps:** HTN-1.
**PRs:** 2 — pocketpaw (fallback), paw-enterprise (`statusMap` deletion).

---

## HTN-3 — The builtin surface speaks in the product's voice · ~2 agent-hrs

**Slice:** hand-quality phrasing across all builtin tools, replacing derived phrasing tool by tool.

**Do:**
- Run the seeded pass: a model drafts `Narration` for every builtin tool across the ~48 modules
  under `src/pocketpaw/tools/builtin/`, including a proposed `safe_args` per tool.
- **A human reviews the entire diff for voice and for allowlist correctness before commit.** The
  allowlist review is the security-relevant half and cannot be skimmed — see Global Constraint 4.
- Mechanical test: every builtin tool declares `Narration`, and every template references only
  fields named in that tool's `safe_args`. A template referencing a non-allowlisted field fails
  the suite rather than leaking at runtime.

**Done when:** the test passes with zero exemptions, and the reviewer has signed off on the diff.

**Interfaces:**
- exposes: annotated tools (no new contract); the mechanical test (guards HTN-8)
- consumes: `BaseTool.narration` from HTN-1; the fallback chain from HTN-2 as the pre-existing baseline

**Deps:** HTN-1, HTN-2.
**PRs:** 1 — pocketpaw. Large but mechanical diff; review is the real cost.

---

## HTN-4 — Narration works on the default backend · ~0.5 agent-hrs

> **Rescoped 2026-08-15 by HTN-0, from ~1.5h and "highest-risk" down to ~0.5h and low-risk.** The
> arguments are not lost and never needed reassembling — see below.

**Slice:** `claude_agent_sdk` — the shipped default — delivers tool arguments to the bridge, so
narration and the plan panel are not pydantic-ai-only features.

**Do:**
- `src/pocketpaw/agents/claude_sdk.py`: the completed `AssistantMessage` already carries fully
  assembled real arguments (`_extract_tool_info` → `block.input`, `:1156-1162`). They are dropped
  by the guard `if tool["name"] not in _announced_tools` at `:3243`, because the streaming path at
  `:3156` already added the name while emitting `"input": {}`.
- Let the args-bearing emission through. Likely minimal shape: the streaming path announces the
  name for a fast indicator without suppressing the later event; the `AssistantMessage` path emits
  with real arguments, upgrading narration from `bare` to `active`.
- **Do not** implement `input_json_delta` accumulation. The SDK assembles the input itself; that
  work is unnecessary.
- Tests: assert a streamed tool call produces a `tool_use` event carrying real `input`, and that
  narration upgrades from `bare` to `active` across the pair.

**Done when:** a streamed default-backend tool call reaches the bridge with populated `input`, with
a test covering the announce-then-upgrade sequence.

**Interfaces:**
- exposes: populated `metadata["input"]` on the default backend (consumed by HTN-6, and by HTN-1's
  bridge rendering for default-backend users)
- consumes: nothing new

**Deps:** HTN-0 (done).
**PRs:** 1 — pocketpaw. Still keep it alone in its PR: it changes event cardinality on the default
backend's hot path, so it deserves an isolated diff even though it is small.

---

## HTN-5 — The plan appears and mutates in place · ~3 agent-hrs

**Slice:** on pydantic-ai, the agent's `write_plan` output renders as a pinned panel that updates
as steps complete, with the current narration line as the sub-line of the `in_progress` item.

**Do:**
- `ee/.../cloud/_core/realtime/events.py`: add `AgentPlanUpdated` (`agent.plan_updated`) near
  `AgentToolUse` (`:382`).
- Regenerate topics: `uv run python backend/scripts/gen_topics.py` (Global Constraint 6).
- `ee/.../cloud/_core/realtime/audience.py:194`: broadcast the new topic.
- Normalizer for `write_plan` → the canonical shape: `{group_id, agent_id, run_id, seq, items:
  [{id, content, status}], progress: {completed, total}}`, status enum
  `pending|in_progress|completed|cancelled`.
- Bridge: route plan tools to the normalizer and emit `agent.plan_updated`; emit only on actual
  change (hash the item list) to absorb `write_plan` firing at both the start and end of each step.
- Per-run monotonic `seq`; the frontend ignores any event with a lower `seq` than the one it holds.
- paw-enterprise: the pinned panel. **Replaces state wholesale, never merges** — whole-list
  replacement is inherited from `write_plan`'s own semantics. Not chat messages: a plan re-printed
  per update is what makes transcripts unreadable.

**Done when:** a multi-step pydantic-ai turn shows the plan populating and ticking through
`in_progress` → `completed` in place, with no duplicate panels and no flicker on repeated
identical `write_plan` calls.

**Interfaces:**
- exposes: the `agent.plan_updated` event contract and the normalizer registry (consumed by HTN-6);
  the panel component (extended by HTN-6 only in that it gains more sources)
- consumes: tool arguments surviving the bridge (HTN-1); the ordering answer from HTN-0

**Deps:** HTN-1, HTN-0.
**PRs:** 2 — pocketpaw (event + normalizer), then paw-enterprise (panel).

---

## HTN-6 — The plan works on every backend · ~1 agent-hr

**Slice:** the panel populates for default-config users and deep_agents users, not just pydantic-ai.

**Do:**
- Normalizer for `TodoWrite` (claude_agent_sdk) written against the **payload captured in HTN-0**,
  not an inferred shape.
- Normalizer for `write_todos` (deep_agents) — three states; `cancelled` simply never emitted.
- Tests: one fixture per backend asserting all three normalize to a byte-identical canonical event
  for an equivalent plan.

**Done when:** the same three-step plan produces the same canonical event from all three backends,
and the panel is verified live on the default backend.

**Interfaces:**
- exposes: full backend coverage of the plan surface
- consumes: the normalizer registry and event contract from HTN-5; the captured payload from HTN-0;
  populated streaming args from HTN-4

**Deps:** HTN-5, HTN-4, HTN-0.
**PRs:** 1 — pocketpaw.

---

## HTN-7 — Mission Control's ticker uses the same narration · ~0.5 agent-hrs

**Slice:** the second ad-hoc narration implementation is retired, so operators and end users see
one consistent phrasing.

**Do:**
- `ee/.../cloud/activity/buffer.py::_summarise()`: prefer the `narration` field; retire the
  field-fishing chain (`summary` → `message` → `tool` → `tool_name` → `thought` → `name`).
- **Verify the live ticker rather than assuming narration is strictly better there** — operator
  phrasing needs are not identical to end-user phrasing needs.
- While in the file: correct the `:174` docstring, which claims the bus emits `agent.tool_start`.
  It is emitted nowhere (confirmed 2026-08-15).

**Done when:** the ticker renders narration for tool events, and a before/after comparison on a
real run shows no operator-facing regression.

**Interfaces:**
- exposes: nothing new
- consumes: `AgentToolUse.data.narration` from HTN-1

**Deps:** HTN-1.
**PRs:** 1 — pocketpaw.

---

## HTN-8 — Security audit of the interpolation path · ~0.5 agent-hrs

> **A first pass already ran against HTN-1 on 2026-08-15 — verdict SOUND_WITH_FINDINGS.** The
> allowlist held against every format-string escape hatch (`{q.__class__}`, `{q[0]}`, `{0}`,
> `{q!r}`, `{q:>99999999}`, nested specs, malformed and doubled braces); `_template_fields` was
> judged complete against CPython's own parser, and ANSI/terminal escape injection is genuinely
> blocked (both the 7-bit `\x1b[` and 8-bit `\x9b` introducers fall inside the stripped ranges).
> Findings 1-6 were fixed in HTN-1; the tool-identity MEDIUM moved to HTN-2 above.
>
> **Two corrections to this plan's stated threat model, from that review:**
> 1. **Narration does NOT currently reach any channel adapter or the CLI.** It rides one emit, and
>    `agent.tool_use` scopes its audience to chat-group members only
>    (`_core/realtime/audience.py:192-202`). The wider blast radius this plan assumed is the design
>    *intent*, not the current wiring — it becomes true only when a surface actually consumes the
>    field. Re-audit at that point, not before.
> 2. **HTN-1 incidentally closed a real pre-existing leak.** The old bridge read `event.content`
>    first, and on codex_cli that is `f"Running: {item.command}"` — so the ENTIRE shell command was
>    written to the `tool` field, unbounded and unsanitized, and emitted to every group member
>    (`agents/codex_cli.py:497-506`, and the same shape at `:511-512` / `:535-536` for file paths
>    and search queries). Those are the arguments that carry `Authorization: Bearer …` and
>    `export …_KEY=`. That field is now just `"shell"`.
>
> **What this task still owes:** re-audit once HTN-2/HTN-3 widen the surface from one tool to ~100
> and from builtins to MCP/connector tools, and close the layer-5 gap — nothing on this path emits
> an `AuditEvent`, though narration is the first place tool ARGUMENTS become user-visible.

**Slice:** independent confirmation that no argument path can leak a credential into a transcript
or a channel adapter.

**Do:**
- `security-auditor` review of the full path against the 7-layer stack: `safe_args` enforcement,
  the truncation and newline-stripping rules, the derive-from-name fallback (which must never
  interpolate anything), and the override registry.
- Adversarial cases specifically: `shell`, `pip_install`, and the connector tools — the three
  families whose arguments most plausibly carry secrets.

**Done when:** the auditor signs off, or files findings that are fixed and re-reviewed.
**This task gates merge of the narration feature** (Global Constraint 4).

**Interfaces:**
- exposes: sign-off
- consumes: the complete annotated surface from HTN-3

**Deps:** HTN-3.
**PRs:** review only — findings, if any, land as fixes on the relevant task's branch.

---

**Total:** ~12.5 agent-hours across 8 tasks and 11 PRs.

This exceeds the PRD's ~11-hour estimate by ~1.5h: the HTN-0 spike is new (the PRD carried those
questions as open rather than costed), and the cross-repo tasks each carry a second PR's overhead.
Recorded here rather than quietly absorbed.

**First shippable outcome:** HTN-0 → HTN-1 (~3h) puts a real humanized phrase on a real screen.
**First broadly useful outcome:** + HTN-2 (~4h) — every tool reads as English, no annotations needed.

**Out of scope (deferred, from the PRD's non-goals):**
- i18n and per-surface phrasing — `Narration` is structured so these are additive later.
  **Known consequence to revisit when i18n lands (measured 2026-08-15):** the security fix strips
  the whole Unicode `Cf` category to close the bidi-override and zero-width-padding findings, and
  U+200D ZERO WIDTH JOINER is `Cf`. So a ZWJ emoji sequence degrades to its components
  (👩‍💻 renders as 👩 💻), and ZWJ/ZWNJ conjunct control in Indic and Arabic scripts is stripped
  too. Ordinary non-ASCII is unaffected — Japanese, Devanagari and accented Latin all verified
  intact. This is the right trade for an 80-char status line whose arguments are model-authored,
  and narrowing the class to re-admit U+200D would reopen the zero-width padding bypass. Flagged
  so i18n work treats it as a known decision rather than a bug to "fix"
- Pocket-level narration overrides
- The planner MCP's `todos[]` build brief (`ee/.../mcp_servers/planner.py:528`) — different lifecycle
- Plan persistence and historical replay — plans stay per-run and in-memory
- Restructuring Mission Control or the Instinct decision feed
- Deleting the dead `AgentToolStart` event — unrelated cleanup, would widen blast radius

**Build location:** pocketpaw → `dev` (worktree `D:/paw-worktrees/humanized-tools`, branch
`feat/humanized-tool-narration`). paw-enterprise → its own default branch, **separate worktree
required** per Global Constraint 2.
