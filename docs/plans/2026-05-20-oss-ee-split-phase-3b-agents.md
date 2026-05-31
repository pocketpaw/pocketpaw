# OSS/EE Split — Phase 3b: `agents/` Cluster Extension Points

> Continuation of Phase 3 (`2026-05-18-oss-ee-split-phase-3-extension-points.md`).
> Phase 3a converted 8 single-/few-site surfaces; this phase clears the
> remaining `from pocketpaw_ee.*` imports in `src/pocketpaw/agents/` plus two
> stragglers (`dashboard_state.py`).

**Goal:** Remove every static `from pocketpaw_ee.*` import from `src/pocketpaw/`
except the documented `tools/cli.py` admin-CLI exception. Add the
`import-linter` enforcement contract and the OSS-only CI job.

## Surfaces and approach

| Core file(s) | Today | Phase 3b |
|---|---|---|
| `agents/pool.py`, `dashboard_state.py` | `pocketpaw_ee.cloud.models.agent.Agent`, `cloud.shared.errors.{NotFound,ValidationError}` | `ModelProvider.get_model("Agent")` (`pocketpaw.models`); errors → core `agents/errors.py` |
| `agents/loop.py` | `_create_pocket_and_session` imports `cloud.models.*` + `cloud.pockets` service/dto | move the function body to EE; core calls `PocketWriter` (`pocketpaw.pockets`) |
| `agents/sdk_mcp_tasks.py` | fully cloud (`cloud.tasks`, `cloud.chat`, `cloud._core`) | `git mv` → `ee/pocketpaw_ee/agent/mcp_servers/tasks.py`; EE `McpServerProvider` |
| `agents/sdk_mcp_planner.py` | fully cloud (`cloud.planner`, …) | `git mv` → `ee/pocketpaw_ee/agent/mcp_servers/planner.py`; EE `McpServerProvider` |
| `agents/sdk_mcp_pocket.py` | **mixed** — cloud `get_pocket`/`list_pockets` + core ripple `get_widget_spec`/`get_inline_widget_help` | **split**: widget tools → core `agents/sdk_mcp_widgets.py` (server `pocketpaw_widgets`); pocket tools → `ee/pocketpaw_ee/agent/mcp_servers/pockets.py` (server `pocketpaw_pocket`, unchanged) |
| `agents/codex_cli.py` | reads cloud identity ContextVars for subprocess env | `AgentExtension.subprocess_env()` (`pocketpaw.agent_extensions`) |
| `agents/tool_bridge.py` | appends `PocketSpecialistTool` for function-tool backends | `AgentExtension.agent_tools(backend)` |
| `agents/claude_sdk.py` | builds tasks/planner/pocket-specialist MCP servers via direct imports | discover cloud servers via `providers("pocketpaw.mcp_servers")`; build the core `pocketpaw_widgets` server directly |

## New / changed extension points (`pocketpaw/extensions.py`)

- `ModelProvider` — reused as-is (`get_model(name)`).
- `McpServerProvider` — reused as-is (`build_server()` + `tool_ids()`); EE registers
  four: `pocketpaw_tasks`, `pocketpaw_planner`, `pocketpaw_pocket`, `pocketpaw_pocket_specialist`.
- `AgentExtension` — **redefined** from the Phase 3a stub `install()` to
  `agent_tools(backend)` + `subprocess_env()`.
- `PocketWriter` — **new** (`pocketpaw.pockets`): `create_pocket_and_session(...)`.

## Decisions

- **`sdk_mcp_pocket.py` is split**, not moved whole. The two ripple widget-spec
  tools have no cloud dependency (ripple moved to core in Phase 2) and stay
  available in an OSS-only install. Tool ids for the two widget tools change
  prefix `pocketpaw_pocket` → `pocketpaw_widgets`; the cloud `get_pocket` /
  `list_pockets` ids are unchanged.
- **`pool.py` stays in core** — `dashboard_lifecycle.py` (core) consumes
  `get_agent_pool`. It looks up the `Agent` document itself via `get_model`.
- The MCP servers move into `ee/pocketpaw_ee/agent/mcp_servers/`, a sibling of
  the existing `agent/pocket_specialist/` namespace.

## Enforcement

- `import-linter` `forbidden` contract `OSS core may not import from EE`:
  `pocketpaw` may not statically import `pocketpaw_ee`, with documented
  `ignore_imports` for the `pocketpaw.tools.cli` admin-CLI exception.
- New CI job `oss-ee-boundary` (`.github/workflows/ci.yml`): runs
  `lint-imports` (static) plus `scripts/check_oss_boundary.py` — which blocks
  `pocketpaw_ee` at runtime and asserts every reworked core module still
  imports and the extension registry degrades to empty.
- The full standalone OSS-only *package build* (install `pocketpaw` with no
  `pocketpaw_ee` on disk at all) is deferred to Phase 4: it depends on the
  `pyproject.toml` split (`ee/pyproject.toml`), which Phase 4 performs. Doing
  it before that split would need a fragile file-copy hack; the import-linter
  contract + runtime boundary check already enforce the split.
