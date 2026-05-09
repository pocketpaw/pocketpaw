# Pocket Specialist — Design

**Date:** 2026-05-09
**Status:** Design approved, ready for implementation plan
**Stacks on:** PR #1083 (deepagents bump), PR #1084 (deep_agents optimizations)

## Problem

Pocket creation today happens inline inside the calling agent's main chat loop:
the LLM reads `POCKET_CREATION_PROMPT_MCP` (or `_CLI`) from its system prompt,
then orchestrates `list_pockets` → `create_pocket` / `update_pocket` itself
across multiple turns. That mixes spec generation into the user-facing chat
context, eats tokens on every call, leaves correctness up to the calling
agent, and ties pocket-creation quality to whichever backend the user picked
for chat (`claude_agent_sdk`, `codex_cli`, etc.).

We want a focused tool that any agent backend can invoke — the calling agent
hands over a brief, the specialist owns the entire flow (list → decide →
draft → validate → persist), and a fully-formed pocket comes back. The
specialist's LLM work is in-process where possible (default `deep_agents`
backend, no subprocess cold-start) and runs on a cheap fast model
(DeepSeek V4 Pro).

## Goals

- Backend-portable: callable from every agent backend (`claude_agent_sdk`,
  `codex_cli`, `opencode`, `openai_agents`, `google_adk`, `copilot_sdk`,
  `deep_agents`, `gemini_cli`).
- Single tool call from the caller's perspective — specialist owns
  list/decide/draft/validate/persist end-to-end.
- Specialist is always part of the system (no on/off toggle); tool
  availability is the only gate, governed by which backend the operator
  chose for the specialist runtime.
- Always ships output. Vague briefs and post-validation warnings produce
  best-effort pockets, never refusals. (Memory:
  `feedback_pocket_always_ships.md`.)
- Status events stream back to the user (listing, drafting, validating,
  persisting) for UX feedback during 5–15s runs.

## Non-goals

- Refining persisted pockets via a `pocket_specialist__refine` tool — out
  of scope for v1.
- Human-in-the-loop interrupts before persist (`interrupt_on=`).
- Streaming the specialist's full token output to the user; status events
  only.
- Parallel/batch specialist runs.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│ Calling agent (any backend)                                  │
│ — has the user's intent and any research already gathered    │
└────────────────────────────────┬─────────────────────────────┘
                                 │ tool call:
                                 │ pocket_specialist__create(brief, hints)
                                 │  (MCP)  or
                                 │ cloud_pocket_specialist_create <brief>
                                 │  (CLI shell command)
                                 ▼
┌──────────────────────────────────────────────────────────────┐
│ ee/agent/pocket_specialist/{mcp_tool.py, cli_tool.py}        │
│ — workspace_id / user_id from per-stream ContextVars         │
│ — emits "specialist:start" via realtime bus                  │
│ — calls runtime.run_specialist(...)                          │
└────────────────────────────────┬─────────────────────────────┘
                                 │
                                 ▼
┌──────────────────────────────────────────────────────────────┐
│ ee/agent/pocket_specialist/runtime.py                        │
│ — picks backend via POCKETPAW_POCKET_SPECIALIST_BACKEND      │
│   (default: deep_agents)                                     │
│ — creates an isolated AgentBackend instance with             │
│   pocket-specific tools attached                             │
│ — runs the LangGraph/loop with brief + system prompt         │
│ — emits status events at every phase transition              │
└────────────────────────────────┬─────────────────────────────┘
                                 │ tool calls inside the graph:
        ┌────────────────────────┼─────────────────────────┐
        ▼                        ▼                         ▼
┌──────────────┐        ┌──────────────┐         ┌──────────────────┐
│list_pockets  │        │validate_spec │         │persist_pocket    │
│ee.cloud.     │        │ee.cloud.     │         │ee.cloud.pockets  │
│  pockets.    │        │  ripple_     │         │  .service.       │
│  service.    │        │  validator.  │         │  agent_create /  │
│  agent_list  │        │  validate_   │         │  agent_update    │
│              │        │  ripple_spec │         │                  │
└──────────────┘        └──────────────┘         └──────────────────┘
```

Imports flow `ee/agent → ee/cloud`, never back. Specialist sits outside
`ee/cloud/` because it's an orchestration utility, not a cloud entity
with its own DB collection.

## Module layout

```
backend/ee/agent/                       # NEW top-level
└── pocket_specialist/
    ├── __init__.py                     # public re-exports
    ├── mcp_tool.py                     # MCP server registration
    │                                   #   pocketpaw_pocket_specialist
    │                                   # tool: pocket_specialist__create
    ├── cli_tool.py                     # shell command:
    │                                   #   cloud_pocket_specialist_create
    │                                   # for codex_cli / opencode / gemini_cli
    ├── runtime.py                      # run_specialist() — backend
    │                                   #   selection, tool wiring, event emit
    ├── tools.py                        # internal LangChain StructuredTool
    │                                   #   wrappers (list / validate / persist)
    ├── events.py                       # status event names + emit helpers
    └── settings.py                     # env-var resolution helpers

backend/src/pocketpaw/agents/
├── router.py                           # MODIFIED: add
│                                       #   create_isolated_backend()
└── backend.py                          # MODIFIED: AgentBackend Protocol
                                        #   gains attach_specialist_tools()

backend/src/pocketpaw/config.py         # MODIFIED: add three Settings fields

backend/ee/ripple/_pockets.py           # MODIFIED: replace inline pocket
                                        #   creation block with delegation
                                        #   block in BOTH _MCP and _CLI prompts

backend/tests/ee/agent/
└── test_pocket_specialist/
    ├── __init__.py
    ├── test_runtime.py                 # backend selection, event order, persist invariant
    ├── test_tools.py                   # tool wrappers in isolation, ContextVar closure
    ├── test_mcp_tool.py                # end-to-end with mocked claude_agent_sdk
    └── test_cli_tool.py                # end-to-end with mocked codex_cli

backend/tests/cloud/
└── test_pocket_prompts_single_source.py  # EXTENDED: assert delegation block
                                          #   only in _pockets.py
```

Why no `prompts.py` under `pocket_specialist/`: per memory
`reference_pocket_prompts.md`, pocket prompts live ONLY in
`ee/ripple/_pockets.py`. The specialist imports them; never duplicates.

## Tool surface

### MCP

Server: `pocketpaw_pocket_specialist` (separate from existing
`pocketpaw_pocket` server — composable).

Tool ID: `mcp__pocketpaw_pocket_specialist__create`
Used by: `claude_agent_sdk`, `deep_agents`, `openai_agents`, `google_adk`,
and any other MCP-capable backend.

### CLI shell

Command: `cloud_pocket_specialist_create <brief> [--hints '<json>']`
Used by: `codex_cli`, `opencode`, `gemini_cli`, `copilot_sdk`,
and any backend that consumes shell commands instead of MCP.

Both surfaces marshal to the same `runtime.run_specialist()`.

### Input

```python
class PocketSpecialistCreateInput(BaseModel):
    brief: str = Field(
        ...,
        min_length=10,
        max_length=4000,
        description=(
            "Natural-language description of what the user wants. "
            "Include any research/context the calling agent has already "
            "gathered. The specialist owns the rest of the flow."
        ),
    )
    hints: PocketSpecialistHints | None = Field(default=None)

class PocketSpecialistHints(BaseModel):
    """Caller-supplied overrides for fields the user named explicitly."""
    name: str | None = None
    description: str | None = None
    color: str | None = None              # hex #RRGGBB
    icon: str | None = None               # lucide-react icon name
    target_pocket_id: str | None = None   # forces update path; skips
                                          # list-decide branch
```

### Output

```python
class PocketSpecialistCreateOutput(BaseModel):
    ok: bool                              # only false on infra failure
    action: Literal["created", "extended"]
    pocket: PocketSummary                 # id, name, description, type, icon, color
    warnings: list[str]                   # validator warnings on the persisted spec
    duration_ms: int
    backend_used: str                     # which backend ran the specialist
```

`ok=False` is reserved for infrastructure failures (Mongo down, backend
unable to initialize). It is NEVER set on LLM/spec quality issues — the
specialist always persists a best-effort pocket. (Memory:
`feedback_pocket_always_ships.md`.)

## Internal workflow

```
1. emit("specialist:start", {brief, hints})

2. branch on hints.target_pocket_id:
   if set:
       lock target, skip to step 4
   else:
       emit("specialist:listing")
       existing[] = list_pockets(workspace_id)
       LLM picks: extend existing[i] | create new
       emit("specialist:decided", {action, target?})

3. emit("specialist:drafting")
   LLM drafts rippleSpec from POCKET_CREATION_PROMPT_MCP +
       brief + hints + (existing target spec if extending)

4. validation loop (max settings.pocket_specialist_max_validation_retries):
       emit("specialist:validating", {iteration})
       warnings = validate_ripple_spec(draft)
       if not warnings: break
       emit("specialist:revising", {iteration, warning_count})
       LLM re-drafts with format_warnings_for_agent(warnings) appended
   # If still warnings after max iterations, persist anyway and return
   # them in output. Never block.

5. emit("specialist:persisting")
   if extending: pockets.service.agent_update(target_id, ...)
   else:        pockets.service.agent_create(workspace_id, user_id, ...)

6. emit("specialist:done", {pocket_id, action, duration_ms})
   return PocketSpecialistCreateOutput(ok=True, action, pocket, warnings, ...)
```

### Persist-once invariant

The specialist MUST call `persist_pocket` exactly once before returning.
Enforced via:

1. The system prompt makes this explicit ("you have not finished until
   you call persist_pocket").
2. A runtime check in `run_specialist()`: if the LLM returns without
   having invoked `persist_pocket`, force-persist using the last draft
   (with whatever warnings it has) before returning. This is the
   safety net for "always ships output."

## Runtime selection

```python
async def run_specialist(
    brief: str,
    hints: PocketSpecialistHints | None,
    *,
    workspace_id: str,
    user_id: str,
    settings: Settings,
) -> PocketSpecialistCreateOutput:
    backend_name = settings.pocket_specialist_backend
    backend = AgentRouter.create_isolated_backend(
        backend_name,
        settings_override={
            f"{backend_name}_model": (
                settings.pocket_specialist_model
                or getattr(settings, f"{backend_name}_model")
            ),
        },
    )
    system_prompt = build_specialist_prompt(workspace_id, hints)
    tools = build_specialist_tools(workspace_id, user_id)
    backend.attach_specialist_tools(tools)

    persist_called = False
    captured_pocket: PocketSummary | None = None
    captured_warnings: list[str] = []

    async for event in backend.run(brief, system_prompt=system_prompt):
        # Normalize backend events → specialist status events,
        # emit to ee/cloud/_core/realtime bus.
        # Capture persist_pocket tool result when it fires.
        ...

    if not persist_called:
        # Safety net per "always ships output" rule.
        captured_pocket, captured_warnings = await force_persist_last_draft(...)

    return PocketSpecialistCreateOutput(
        ok=True,
        action=...,
        pocket=captured_pocket,
        warnings=captured_warnings,
        duration_ms=...,
        backend_used=backend_name,
    )
```

### `AgentRouter.create_isolated_backend()`

New helper on `agents/router.py`. Today the router caches one backend per
process; the specialist needs a fresh instance with its own settings
overrides so it doesn't pollute the user's chat-loop backend. Signature:

```python
@classmethod
def create_isolated_backend(
    cls,
    backend_name: str,
    settings_override: dict[str, Any] | None = None,
) -> AgentBackend:
    """Build a fresh, non-cached AgentBackend with optional settings overrides.

    Used for short-lived specialist runs that should not share state with
    the main chat backend.
    """
```

### `AgentBackend.attach_specialist_tools()`

New method on the `AgentBackend` Protocol in `agents/backend.py`. Each
backend implements differently:

| Backend | Implementation |
|---|---|
| `deep_agents` | Extends `_build_custom_tools()` cache with the specialist's StructuredTools. |
| `claude_agent_sdk` | Registers a per-invocation in-process MCP server exposing the tools. |
| `codex_cli` / `opencode` / `gemini_cli` | Registers the tools as shell commands the subprocess can call. |
| `openai_agents` / `google_adk` / `copilot_sdk` | Backend-specific dynamic tool registration. |

Backends that genuinely cannot accept dynamic tools at runtime are
excluded from the valid `pocket_specialist_backend` set; startup
validation rejects misconfiguration.

## Settings

```python
# Pocket Specialist Settings (config.py, mirrors deep_agents_* pattern)

pocket_specialist_backend: str = Field(
    default="deep_agents",
    description=(
        "Which agent backend runs the specialist's LLM work. Must be a "
        "registered backend name. Default deep_agents avoids subprocess "
        "cold-start."
    ),
)
pocket_specialist_model: str = Field(
    default="",
    description=(
        "Model override for the specialist run (empty = use the chosen "
        "backend's default model setting). provider:model format, e.g. "
        "'openai_compatible:deepseek-v4-pro' for cheap fast specs."
    ),
)
pocket_specialist_max_validation_retries: int = Field(
    default=3,
    description=(
        "Max draft → validate → revise iterations before persisting "
        "with remaining warnings."
    ),
)
```

Env vars: `POCKETPAW_POCKET_SPECIALIST_BACKEND`,
`POCKETPAW_POCKET_SPECIALIST_MODEL`,
`POCKETPAW_POCKET_SPECIALIST_MAX_VALIDATION_RETRIES`.

## Specialist-internal tools

Three LangChain `StructuredTool` wrappers in `pocket_specialist/tools.py`:

| Tool | Wraps | Purpose |
|---|---|---|
| `list_pockets` | `ee.cloud.pockets.service.agent_list(workspace_id, user_id)` | Compact list of existing pockets so the LLM can decide extend vs create. |
| `validate_spec` | `ee.cloud.ripple_validator.validate_ripple_spec` | Returns `{warnings: [], ok: bool}`. LLM uses this to refine before persisting. |
| `persist_pocket` | `ee.cloud.pockets.service.agent_create` or `agent_update` | Final write. Returns `PocketSummary`. MUST be called exactly once. |

Workspace/user IDs are closed over from request `ContextVars`; they are
NOT tool arguments, so the LLM cannot accidentally cross workspaces.

## Status events

Emitted through `ee/cloud/_core/realtime/bus.py`:

| Event | Payload |
|---|---|
| `specialist:start` | `{brief, hints, backend}` |
| `specialist:listing` | `{}` |
| `specialist:decided` | `{action: "extend" \| "create", target_pocket_id?}` |
| `specialist:drafting` | `{}` |
| `specialist:validating` | `{iteration}` |
| `specialist:revising` | `{iteration, warning_count}` |
| `specialist:persisting` | `{action}` |
| `specialist:done` | `{pocket_id, action, duration_ms, warning_count}` |

Frontend consumes these in the activity panel as a progress indicator.

## Calling-agent prompt changes

`ee/ripple/_pockets.py` — both `POCKET_CREATION_PROMPT_MCP` and
`POCKET_CREATION_PROMPT_CLI` get an unconditional STEP 0 that REPLACES
the existing inline `STEP 1..N` blocks:

```
STEP 0 — DELEGATE TO SPECIALIST:

When the user wants a pocket and you have the brief, IMMEDIATELY call:

  pocket_specialist__create(brief, hints?)         # MCP backends
  cloud_pocket_specialist_create <brief> [hints]   # CLI backends

The specialist will list existing pockets, decide extend-vs-create,
draft, validate, and persist. You receive {ok, action, pocket, warnings}.

Do NOT call list_pockets / create_pocket / update_pocket directly.
The specialist owns the whole flow in one tool call.

After the specialist returns, surface any warnings to the user as
"I shipped it; want me to clean up X?" — do NOT block on warnings.
```

The legacy inline `STEP 1..N` blocks are deleted from this PR, since the
specialist is always part of the system. If `pocket_specialist_backend`
points at a backend that isn't installed, the tool registration logs a
startup warning and skips registration; the calling agent sees no pocket
tools at all (preferable to silent inline degradation).

## Testing strategy

| File | Coverage |
|---|---|
| `test_runtime.py` | Backend selection honors `pocket_specialist_backend`, model override applied, status events emit in expected order, persist-once invariant enforced (forced fallback when LLM omits `persist_pocket`), output schema well-formed. |
| `test_tools.py` | `list_pockets`/`validate_spec`/`persist_pocket` tool wrappers in isolation: workspace_id closure works, validator warnings round-trip, persist returns `PocketSummary`. |
| `test_mcp_tool.py` | End-to-end with mocked `claude_agent_sdk` backend: tool registers under `pocketpaw_pocket_specialist` MCP server, schema accepted, persisted pocket returned. |
| `test_cli_tool.py` | Same as above but for the shell-command surface used by codex_cli / opencode / gemini_cli. |
| `tests/cloud/test_pocket_prompts_single_source.py` (extended) | Assert (a) the new delegation block lives only in `_pockets.py`, never duplicated into `agent_service.py`; (b) the legacy `STEP 1..N` inline-creation blocks are gone from both prompt variants. |

Any existing tests that assert the presence of the legacy
`STEP 1..N` inline pocket-creation steps must be updated or removed in
this PR — they are no longer truth after the prompt rewrite. Audit
target: any test under `tests/` that string-matches against
`POCKET_CREATION_PROMPT_*` content.

No real-LLM tests — the specialist's LLM behavior is the calling agent's
contract, not ours; we test schema, plumbing, and event emission.

## Rollout

Single-phase ship. The PR lands and the new path is live for everyone.
Operator-visible behavior change: calling agents now delegate pocket
creation to the specialist tool instead of running the inline flow. The
default `pocket_specialist_backend=deep_agents` runs in-process; operators
can override per environment.

### Operator misconfiguration mode

If `pocket_specialist_backend` points at a backend that isn't installed
(e.g. `deep_agents` without `pip install pocketpaw[deep-agents]`), the
specialist's MCP/CLI tool registration logs a startup warning and skips
registration. Calling agents see no pocket tools at all — preferable to
silent inline degradation, since the user's "always enabled" decision
means we don't carry an inline fallback. Operators must fix their install
before pocket creation works. This is an explicit design choice, not an
oversight.

## References

- Memory `feedback_backend_portable.md` — specialist must work across all
  backends, not just `claude_agent_sdk`.
- Memory `reference_pocket_prompts.md` — pocket prompts live only in
  `ee/ripple/_pockets.py`; specialist imports them.
- Memory `feedback_pocket_always_ships.md` — never refuse, always
  persist.
- PR #1083 — `deepagents>=0.5.8` floor.
- PR #1084 — `deep_agents.py` Responses-API fix + skills/memory plumbing.
