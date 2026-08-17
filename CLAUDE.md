# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PocketPaw is a self-hosted AI agent that runs locally and is controlled via Telegram, Discord, Slack, WhatsApp, or a web dashboard. The Python package is named `pocketpaw` (the internal/legacy name), while the public-facing name is `pocketpaw`. Python 3.11+ required.

### Two packages: `pocketpaw` (OSS) + `pocketpaw-ee` (enterprise)

The backend ships as **two installable wheels** (open-core split):

- **`pocketpaw`** (MIT) — the OSS core. Source in `src/pocketpaw/`, packaged by
  the root `pyproject.toml`. `pip install pocketpaw` gives a fully working local
  agent with no enterprise code on disk.
- **`pocketpaw-ee`** (FSL-1.1) — the enterprise layer (multi-tenant cloud, auth,
  rooms, billing, knowledge base, file storage, fleet, instinct, pocket
  specialist). Source in `ee/pocketpaw_ee/`, packaged by `ee/pyproject.toml`. It
  depends on `pocketpaw` and activates through the core's entry-point extension
  registry (`pocketpaw/extensions.py` + `pocketpaw/_registry.py`).

The OSS core never statically imports `pocketpaw_ee` — an import-linter contract
enforces it. Optional enterprise behaviour is reached at runtime via
entry-points. For a full development environment use `uv sync --dev --group ee`.

## Knowledge Base

A codebase wiki lives at `docs/wiki/` — auto-generated from AST analysis + LLM compilation. **Read the relevant wiki article before modifying a module.**

```bash
# Search the KB from terminal
cd /path/to/knowledge-base && kb search "GroupService" --scope paw-cloud

# Show a specific module's wiki
kb show group_service --scope paw-cloud

# Rebuild after big changes (also runs automatically via PostCommit hook)
kb build ./ee/pocketpaw_ee/cloud --scope paw-cloud --output docs/wiki/

# Check wiki health
kb lint --scope paw-cloud
```

Key wiki articles for the enterprise cloud module:
- `docs/wiki/index.md` — Full index with all articles
- `docs/wiki/group_service.md` — Chat group CRUD, membership, agents
- `docs/wiki/message_service.md` — Message CRUD, reactions, threads
- `docs/wiki/service.md` (workspace) — Workspace CRUD, members, invites
- `docs/wiki/agent_bridge.md` — Agent orchestration for cloud chat
- `docs/wiki/errors.md` — CloudError hierarchy

The wiki auto-rebuilds on commits that touch `ee/pocketpaw_ee/cloud/` files (via `.claude/hooks/kb-rebuild.sh`).

## Commands

```bash
# Install dev dependencies (OSS core only — no enterprise code, no pocketpaw_ee)
uv sync --dev

# Full dev install — OSS core + the pocketpaw-ee enterprise package (editable).
# Required to run tests/ee, tests/cloud, or anything touching ee/pocketpaw_ee.
uv sync --dev --group ee

# Run the app (web dashboard is the default — auto-starts all configured adapters)
uv run pocketpaw

# Run Telegram-only mode (legacy pairing flow)
uv run pocketpaw --telegram

# Run headless Discord bot
uv run pocketpaw --discord

# Run headless Slack bot (Socket Mode, no public URL needed)
uv run pocketpaw --slack

# Run headless WhatsApp webhook server
uv run pocketpaw --whatsapp

# Run multiple headless channels simultaneously
uv run pocketpaw --discord --slack

# Run in development mode (auto-reload on file changes)
uv run pocketpaw --dev

# CLI management commands (all support --json for scripting)
uv run pocketpaw status                     # Show agent status (--watch for live)
uv run pocketpaw health                     # Quick startup health check
uv run pocketpaw doctor                     # Full diagnostics with connectivity
uv run pocketpaw channels                   # List channel configured/autostart status
uv run pocketpaw channels start discord     # Start a channel adapter (needs running dashboard)
uv run pocketpaw channels stop slack        # Stop a channel adapter
uv run pocketpaw skills                     # List available skills
uv run pocketpaw sessions                   # List chat sessions
uv run pocketpaw sessions search <query>    # Search session content
uv run pocketpaw sessions delete <key>      # Delete a session
uv run pocketpaw memory                     # Show memory stats
uv run pocketpaw memory search <query>      # Search long-term memories
uv run pocketpaw config                     # Show config (secrets masked)
uv run pocketpaw config set <key> <value>   # Set a config value
uv run pocketpaw config validate            # Validate API keys
uv run pocketpaw config path                # Print config file path
uv run pocketpaw errors                     # Show recent errors (--limit, --search)
uv run pocketpaw logs                       # Show audit log (--follow to tail)
uv run pocketpaw update                     # Update to latest version via uv

# Run all tests (excluding E2E tests) — needs the full install: uv sync --dev --group ee
uv run pytest --ignore=tests/e2e

# Run only the OSS-core test scope (passes on an OSS-only `uv sync --dev`)
uv run pytest --ignore=tests/e2e --ignore=tests/cloud --ignore=tests/ee

# Run a single test file
uv run pytest tests/test_bus.py

# Run a specific test
uv run pytest tests/test_bus.py::test_publish_subscribe -v

# Run E2E tests (requires Playwright browsers - see below)
uv run pytest tests/e2e/ -v

# Install Playwright browsers (required for E2E tests, one-time setup)
# Linux/Mac:
uv run playwright install
# Windows (if above fails with trampoline error):
.venv\Scripts\python -m playwright install

# Lint
uv run ruff check .

# Format
uv run ruff format .

# Type check
uv run mypy .

# Run pre-commit hooks manually
pre-commit run --all-files

# Build package
python -m build
```

## Architecture

### Message Bus Pattern

The core architecture is an event-driven message bus (`src/pocketpaw/bus/`). All communication flows through three event types defined in `bus/events.py`:

- **InboundMessage** — user input from any channel (Telegram, WebSocket, CLI)
- **OutboundMessage** — agent responses back to channels (supports streaming via `is_stream_chunk`/`is_stream_end`)
- **SystemEvent** — internal events (tool_start, tool_result, thinking, error) consumed by the web dashboard Activity panel

### AgentLoop → AgentRouter → Backend

The processing pipeline lives in `agents/loop.py` and `agents/router.py`:

1. **AgentLoop** consumes from the message bus, manages memory context, and streams responses back
2. **AgentRouter** uses a registry-based system (`agents/registry.py`) to select and delegate to one of six backends based on `settings.agent_backend`:
   - `claude_agent_sdk` (default/recommended) — Official Claude Agent SDK with built-in tools (Bash, Read, Write, etc.). Uses `PreToolUse` hooks for dangerous command blocking. Lives in `agents/claude_sdk.py`.
   - `openai_agents` — OpenAI Agents SDK with GPT models and Ollama support. Lives in `agents/openai_agents.py`.
   - `google_adk` — Google Agent Development Kit with Gemini models and native MCP support. Lives in `agents/google_adk.py`.
   - `codex_cli` — OpenAI Codex CLI subprocess wrapper with MCP support. Lives in `agents/codex_cli.py`.
   - `opencode` — External server-based backend via REST API. Lives in `agents/opencode.py`.
   - `copilot_sdk` — GitHub Copilot SDK with multi-provider support. Lives in `agents/copilot_sdk.py`.
   - `deep_agents` — LangChain Deep Agents with LangGraph runtime, built-in planning/subagent tools, and multi-provider support. Lives in `agents/deep_agents.py`.
3. All backends implement the `AgentBackend` protocol (`agents/backend.py`) and yield standardized `AgentEvent` objects with `type`, `content`, and `metadata`
4. Legacy backend names (`pocketpaw_native`, `open_interpreter`, `claude_code`, `gemini_cli`) are mapped to active backends via `_LEGACY_BACKENDS` in the registry

### Channel Adapters

`bus/adapters/` contains protocol translators that bridge external channels to the message bus:

- `TelegramAdapter` — python-telegram-bot
- `WebSocketAdapter` — FastAPI WebSockets
- `DiscliAdapter` — `discord-cli-agent` subprocess wrapper (optional dep `pocketpaw[discord]`). Slash command `/paw` + DM/mention support. Stream buffering with edit-in-place (1.5s rate limit). Auto-registers a `pocketpaw-discord` MCP server on startup exposing Discord operations to all MCP-capable backends. Admin commands (`/converse`, `/setstatus`, etc.) require Administrator or Manage Server permission.
- `SlackAdapter` — slack-bolt Socket Mode (optional dep `pocketpaw[slack]`). Handles `app_mention` + DM events. No public URL needed. Thread support via `thread_ts` metadata.
- `WhatsAppAdapter` — WhatsApp Business Cloud API via `httpx` (core dep). No streaming; accumulates chunks and sends on `stream_end`. Dashboard exposes `/webhook/whatsapp` routes; standalone mode runs its own FastAPI server.

**Dashboard channel management:** The web dashboard (default mode) auto-starts all configured adapters on startup. Channels can be configured, started, and stopped dynamically from the Channels modal in the sidebar. REST API: `GET /api/channels/status`, `POST /api/channels/save`, `POST /api/channels/toggle`.

### Key Subsystems

- **Memory** (`memory/`) — Session history + long-term facts, file-based storage in `~/.pocketpaw/memory/`. Protocol-based (`MemoryStoreProtocol`) for future backend swaps
- **Browser** (`browser/`) — Playwright-based automation using accessibility tree snapshots (not screenshots). `BrowserDriver` returns `NavigationResult` with a `refmap` mapping ref numbers to CSS selectors
- **Security** (`security/`) — Guardian AI (secondary LLM safety check) + append-only audit log (`~/.pocketpaw/audit.jsonl`)
- **Tools** (`tools/`) — `ToolProtocol` with `ToolDefinition` supporting both Anthropic and OpenAI schema export. Built-in tools in `tools/builtin/`
- **Bootstrap** (`bootstrap/`) — `AgentContextBuilder` assembles the system prompt from identity, memory, and current state
- **Config** (`config.py`) — Pydantic Settings with `POCKETPAW_` env prefix, JSON config at `~/.pocketpaw/config.json`. Channel-specific config: `discord_bot_token`, `discord_allowed_guild_ids`, `discord_allowed_user_ids`, `slack_bot_token`, `slack_app_token`, `slack_allowed_channel_ids`, `whatsapp_access_token`, `whatsapp_phone_number_id`, `whatsapp_verify_token`, `whatsapp_allowed_phone_numbers`
- **Soul** (`soul/`) -- Optional soul-protocol integration for persistent AI identity, psychology-informed memory, OCEAN personality, emotional state, and portable `.soul` files. Enable via `soul_enabled=true`. SoulManager handles lifecycle (birth/awaken/save), auto-saves periodically, recovers from corrupt files, and wires SoulBootstrapProvider into the system prompt. Soul tools (`soul_remember`, `soul_recall`, `soul_edit_core`, `soul_status`) auto-register with all backends when active. Can be toggled at runtime via the dashboard settings.
- **Bundled skills** (`bundled_skills/`) — AgentSkills-format SKILL.md files (under `_bundled/skills/<name>/`) that ship with PocketPaw. They reach the chat agent by two **independent** routes, because no single route covers every backend:
  1. **`~/.claude/skills/` mirror** (boot-time install, `auto_install_bundled_skills`). That path is one of the three `pocketpaw.skills.SKILL_PATHS` PocketPaw's own `SkillLoader` scans, so the desktop slash-command dispatcher resolves them on the non-SDK backends (codex_cli / openai_agents / deep_agents). **This mirror is invisible to the default `claude_agent_sdk` backend** — it launches with `setting_sources=[]` for persona isolation, which disables the SDK's filesystem skill discovery (verified 2026-06-03: a slash hits the SDK as an unknown command and the run returns with no assistant turn).
  2. **Local plugin** (`sdk_load_bundled_skills`). `_bundled/` is also a valid Claude Code local plugin (`.claude-plugin/plugin.json` + `skills/`); the `claude_agent_sdk` backend passes it via the SDK `plugins=` option, which loads regardless of `setting_sources`. This is the only route that reaches that backend — the bundled skills become invokable by slash command **and** natural-language intent without leaking the rest of `~/.claude` (CLAUDE.md, output styles) into the agent.

  Distinct from `pocketpaw/skills/` (the runtime loader/executor) — this module is the *shipping* side. Bundles `pocketpaw-create-pocket`, `pocketpaw-edit-pocket`, `pocketpaw-create-site`, `pocketpaw-pocket-planner`, `pocketpaw-pocket-specialist`, `foresight-create-sim`, the per-engine Paw Sites authoring brains (`pocketpaw-create-paw-site`, `pocketpaw-create-svelte-site`, `pocketpaw-create-react-site`, `pocketpaw-create-dynamic-site`), `pocketpaw-edit-react-site` (the react-track EDIT brain — a site already exists and needs changing, which is a different tool and a different failure mode from a create), and the engine-agnostic `pocketpaw-design-taste` they all compose with. Adding one: drop `_bundled/skills/<name>/SKILL.md` — the installer and the plugin both discover it by directory iteration — **then run `uv run pocketpaw atlas build` and commit the regenerated `src/pocketpaw/atlas/data/atlas.json`**. Directory iteration covers the two runtime routes, but atlas is a COMPILED artifact: `tests/atlas/test_widgets_skills.py::test_every_bundled_skill_has_a_valid_entry` requires one `skill:<slug>` entry per skill dir, so a new skill fails the suite until the artifact is rebuilt. Check `tests/atlas/test_eval.py` too — a new skill name enters the search corpus and can re-rank intents that have nothing to do with it. Opt out via `POCKETPAW_AUTO_INSTALL_BUNDLED_SKILLS=false` (mirror) and `POCKETPAW_SDK_LOAD_BUNDLED_SKILLS=false` (SDK plugin). See `docs/internal/2026-05-bundled-skills.md` for design + how to add a new skill.
- **Bundled KB** — *removed 2026-07-12.* PocketPaw previously shipped a `ripple-recipes` kb-go scope (hand-authored pattern recipes auto-installed to `~/.knowledge-base/` and retrieved at pocket-creation time). It was retired because the fixed recipes biased the agent's design toward a handful of canned compositions; design breadth now comes from live design references rather than a shipped scope. The general `_get_kb_context` injection in `bootstrap/context_builder.py` (workspace/agent/pocket KB) is unchanged — only the bundled ripple-recipes scope and its boot-time installer were removed.

### Frontend

The web dashboard (`frontend/`) is vanilla JS/CSS/HTML served via FastAPI+Jinja2. No build step. Communicates with the backend over WebSocket for real-time streaming.

## Key Conventions

- **Async everywhere**: All agent, bus, memory, and tool interfaces are async. Tests use `pytest-asyncio` with `asyncio_mode = "auto"`
- **Protocol-oriented**: Core interfaces (`AgentProtocol`, `ToolProtocol`, `MemoryStoreProtocol`, `BaseChannelAdapter`) are Python `Protocol` classes for swappable implementations
- **Env vars**: All settings use `POCKETPAW_` prefix (e.g., `POCKETPAW_ANTHROPIC_API_KEY`)
- **Soul config**: `POCKETPAW_SOUL_ENABLED=true`, `POCKETPAW_SOUL_NAME`, `POCKETPAW_SOUL_ARCHETYPE`, `POCKETPAW_SOUL_PATH`, `POCKETPAW_SOUL_AUTO_SAVE_INTERVAL`
- **Files-as-Knowledge config** (Phase 1): `POCKETPAW_EXTRACTION_CHAIN` (JSON list of adapter names, e.g. `'["gemini-flash","local"]'`), `POCKETPAW_EXTRACTION_PER_MIME` (JSON map of mime→adapter), `POCKETPAW_GEMINI_API_KEY` for the cloud captioning adapter; `POCKETPAW_KB_SCOPES` (JSON list, e.g. `'["workspace:w1","agent:a1"]'`) drives multi-scope KB injection in the agent system prompt. The legacy `POCKETPAW_KB_SCOPE` (single string) still works via a deprecation shim that copies it into `kb_scopes` on startup. Phase 3 (Stage 3.E): uploads can carry `pocket_id` (form field on `POST /api/v1/uploads`, query on `GET /api/v1/files`); the FileReady listener routes pocket uploads into `pocket:{id}` KB and the agent's `_get_kb_context` resolves scope priority `pocket > agent > workspace` via the per-request `KbContext` threaded from the cloud chat path.
- **Resumable chat runs config**: `POCKETPAW_REDIS_URL` (e.g.
  `redis://redis:6379/0`; Dragonfly / Valkey work as drop-in since they
  speak the Redis wire protocol). When unset, the transport falls back to
  an in-process buffer (Tier 0, dev-only — runs do not survive restart and
  the Tier 2 arq worker is unavailable). Production deploys must set it;
  startup logs a WARN if missing;
  `POCKETPAW_CLOUD_RUN_EXECUTOR` (`inprocess` default, `arq` for the Tier 2
  worker — added in the follow-up PR);
  `POCKETPAW_CLOUD_STREAM_TRANSPORT` (`redis` default — adapter selector for
  future non-Redis backends like NATS JetStream);
  `POCKETPAW_CLOUD_RUN_STREAM_TTL` (default `3600`, the Redis Stream retention
  after a run terminates);
  `POCKETPAW_CLOUD_RUN_JOB_TIMEOUT` (default `1800` = 30 min, the arq per-run
  job timeout — arq's own default is 300s, which cancels a long agent run
  mid-generation, so a big coding task halts after ~5 min; lift it for long
  runs, the 10-minute stale-run sweeper is the backstop). See `docs/plans/2026-05-22-resumable-chat-runs-design.md`.
  A background sweeper runs on cloud startup and every 5 minutes (hardcoded, not
  env-configurable), marking queued/running `ChatRunDoc`s older than 10 minutes as
  `interrupted` so runs abandoned by a backend restart surface a retry affordance
  instead of leaving clients subscribed forever.
- **Growth outbound config (`GROWTH_SENDING_DOMAIN`)**: the secondary domain the
  `/growth` engine sends cold outreach from — **required**, with no default. The
  dispatch worker fails closed when it is unset (nothing goes out), validates the
  from-address against it at send time, and refuses a value equal to the
  deployment's own host (`POCKETPAW_PUBLIC_BASE_URL`). Never point it at the apex:
  cold outreach draws spam complaints at rates transactional mail never sees, and
  the complaints land on the *sending* domain's reputation — a burnt secondary
  domain costs a DNS record and a warm-up, a burnt apex takes password resets,
  invoices and receipts with it. The provider credential itself is **not** an env
  var: it is per-workspace connector state on the workspace's `mailtrap` connector
  row (`MAILTRAP_API_TOKEN`, plus optional `MAILTRAP_FROM_EMAIL` /
  `MAILTRAP_FROM_NAME`, `MAILTRAP_REPLY_TO`, and `MAILTRAP_PROJECT_SENDERS` — a
  `{project_id: {from_email, from_name, reply_to}}` map so an agency sends as the
  client whose project owns the prospect, resolved per field with the workspace
  values as the fallback), so disabling the connector revokes sending immediately
  for every project at once.
  See `ee/pocketpaw_ee/cloud/growth/connector.py` and `docs/api-reference.md`.
- **Session supervisor config**: `POCKETPAW_SESSION_SUPERVISOR` (default OFF). When
  truthy (`1`/`true`/`yes`/`on`), the cloud chat executor drives every agent turn
  through the `SessionSupervisor` + the durable `(workspace, session, agent) ->
  cli_session_id` map (`ee/.../agent_sessions/runtime_service.py`) + the per-tenant
  `MongoSessionStore`, so the agent RESUMES its native CLI session (durable across
  restart, tenant-isolated) instead of replaying Mongo history into the prompt. OFF
  leaves `pool.run` byte-for-byte the legacy path, and any supervisor/store error
  during a turn falls back to legacy for that turn. v1 resumes cold from the store
  each turn (no warm hot-process reuse yet). Design:
  `docs/concepts/agent-session-management.mdx`; smoke:
  `docs/handoff/2026-06-30-session-supervisor-smoke.md`.
- **Per-tenant agent cwd jail (cloud, ART-2)**: In multi-tenant cloud each
  workspace's chat agent runs with `cwd = ~/.pocketpaw/workspaces/<workspace_id>/agent/<session_id>/`
  (resolved per-run from the `attach_agent_identity` ContextVars in
  `ee/pocketpaw_ee/cloud/agent_jail.py`) so one tenant's file ops never
  co-mingle in the shared home dir. It **fails closed**: a cloud run that
  reaches the backend with no resolvable `workspace_id` RAISES rather than
  falling back to `~`. OSS / dedicated installs (no cloud DB initialized) keep
  `settings.file_jail_path` unchanged. Override the jail root with
  `POCKETPAW_WORKSPACE_JAIL_ROOT` (default `~/.pocketpaw/workspaces`) to anchor
  it on a data volume.
- **Agent jail lifecycle (cloud, ART-3)**: bounds the ART-2 scratch jails so
  disk scales with active concurrency, not user count (the jail is pure scratch;
  durability lives in blob storage). Three knobs, all read from the environment
  in `ee/pocketpaw_ee/cloud/agent_jail.py`:
  `POCKETPAW_AGENT_JAIL_QUOTA_MB` (default `2048`) — per-workspace size cap
  measured at RUN-START; an over-quota run is rejected cleanly (a terminal
  `failed` run with an `agent.jail_over_quota` error frame, never an OOM/crash).
  `0` disables it.
  `POCKETPAW_AGENT_JAIL_TTL_GRACE_SECONDS` (default `3600`) — idle grace after
  which a jail with no active run is garbage-collected.
  `POCKETPAW_AGENT_JAIL_DISK_WATERMARK_PCT` (default `90`) — box disk-usage
  high-water mark; over it, the GC evicts least-recently-used IDLE jails first
  until back under. `0` disables watermark eviction.
  `POCKETPAW_AGENT_JAIL_GC_ENABLED` (default `true`) — escape hatch to disable
  the GC sweep entirely. The GC (`ee/pocketpaw_ee/cloud/agent_jail_gc.py`,
  `sweep_agent_jails`) runs on cloud startup and the same 5-minute heartbeat as
  the stale-run sweeper, and NEVER evicts a jail whose run is still queued or
  running (resolved via `run_service.find_active_run_scopes`; a retried
  interrupted run re-protects its jail by spawning a fresh queued run).
- **Artifact delivery + storage boot guard (cloud, ART-4)**: the agent's jail is
  pure scratch — durability lives in blob storage. The `deliver_artifact`
  in-process MCP tool (`ee/pocketpaw_ee/agent/mcp_servers/deliver.py`, server
  `pocketpaw_deliver`) lands a built file (or a zipped directory) from the jail
  into the tenant's blob storage via `EEUploadService.upload` and returns a real
  presigned download URL — so the agent shares a working link instead of a
  container path or a `127.0.0.1` preview server. Path safety is jail-scoped
  (rejects `..`, absolute paths out, symlinks out, another tenant's jail), and
  `POCKETPAW_DELIVER_MAX_MB` (default `100`) caps artifact size. Because this
  only works when uploads go to real object storage, `init_cloud_db` runs
  `verify_cloud_storage_backend()` (`ee/pocketpaw_ee/cloud/uploads/bootstrap.py`,
  mirroring the memory guard): in cloud, if `POCKETPAW_UPLOAD_ADAPTER` != `s3` it
  **WARNs loudly** (a local-disk deploy that would silently no-op delivery is
  visible in the boot logs), and `POCKETPAW_REQUIRE_S3_IN_CLOUD=1` escalates that
  to a hard boot failure. The multi-tenant-cloud signal both the jail and this
  guard read is `is_multi_tenant_cloud()` in `ee/pocketpaw_ee/cloud/shared/db.py`
  (one name for the `get_client() is not None` check).
- **In-process bus subscribers**: `pocketpaw_ee.cloud._core.realtime.bus.InProcessBus` exposes `subscribe(event_type, handler)` for cloud-side listeners (e.g. the `FileReady` → KB indexer wired in `ee/pocketpaw_ee/cloud/uploads/listeners.py`). Register subscribers from `mount_cloud()` after `init_realtime()` runs. Handler exceptions are logged and swallowed per-handler so one bad listener can't block the rest of the dispatch.
- **Memory backend (`POCKETPAW_MEMORY_BACKEND`)**: OSS self-hosted defaults to `"file"` (local JSON under `~/.pocketpaw/memory/`). The cloud forces `"mongodb"` via `register_default_backend()` (`ee/pocketpaw_ee/cloud/memory/bootstrap.py`) unless explicitly overridden. The cloud now **fails to boot** if the active store isn't `MongoMemoryStore` (`verify_cloud_memory_backend()` in `init_cloud_db`) — a deliberate guard so a misconfigured backend can never silently write chat history (files-surface chats included) to local disk. Don't set `POCKETPAW_MEMORY_BACKEND=file` on a cloud deployment.
- **API key required**: The `claude_agent_sdk` backend requires an `ANTHROPIC_API_KEY` when using the Anthropic provider. OAuth tokens from Free/Pro/Max plans are not permitted for third-party use per [Anthropic's policy](https://code.claude.com/docs/en/legal-and-compliance#authentication-and-credential-use). Ollama/local providers do not require an API key.
- **Ruff config**: line-length 100, target Python 3.11, lint rules E/F/I/UP
- **Entry point**: `pocketpaw.__main__:main`
- **Lazy imports**: Agent backends are imported inside `AgentRouter._initialize_agent()` to avoid loading unused dependencies

## pocketpaw_ee/cloud Code Rules

Applies to code under `ee/pocketpaw_ee/cloud/`. Local-runtime code (`src/pocketpaw/`)
uses different patterns; these rules don't apply there.

1. **Each entity has a 4-file shape.** `<entity>/{domain.py, dto.py, service.py, router.py}`. No `repositories.py`. The service IS the repository — Beanie writes are inline.

2. **Writes go through `<entity>/service.py`.** Never import Beanie document classes (`pocketpaw_ee.cloud.models.*`) from routers, DTOs, domains, channels, tools, or agents. Only `<entity>/service.py` may import its own `models.<entity>`.

3. **Domain enforces multi-tenancy at construction.** `domain.py` value objects are frozen with required tenancy fields (`workspace_id`, `scope`, etc.) — no defaults. Constructing a domain object without tenancy info is a type error.

4. **DTOs separate request and response.** `dto.py` defines distinct `<Op>Request` and `<Entity>Response` classes. Never reuse one model for both input and output — fields leak silently.

5. **Service signature.**
   ```python
   async def op(workspace_id: str, user_id: str, body: <RequestSchema>) -> dict:
   ```
   Module-level `async def`, not a class. Multi-tenancy via the explicit `workspace_id` parameter. `user_id` carries the viewer context for permission checks. Public APIs return wire dicts (`dict`) for legacy router compatibility — see `pockets/service.py` for the canonical shape, including the `_resolved_wire_dict` helper that produces the wire dict from a Beanie doc.

   *Note: a future migration may move toward a `RequestContext` value object that bundles `(workspace_id, user_id, viewer_metadata)`. New modules that anticipate this can mint a private `_context.py:RequestContext` (see `ee/pocketpaw_ee/calendar/_context.py` in PR #1132 for an example), but the mainline pattern remains the explicit parameter pair until pockets migrates.*

6. **Validate at entry.** First line of every service function: `body = <RequestSchema>.model_validate(body)`. FastAPI parses HTTP bodies; services re-parse for internal callers (bus handlers, MCP tools, CLI, jobs).

7. **Tenant filter on every read.** Every `_FooDoc.find(...)` / `find_one(...)` call includes `workspace=ctx.workspace_id` (or has an explicit `# global-read: <reason>` comment). Domain-level required fields catch construction-time leaks; this rule catches read-path leaks.

8. **Mapping via Pydantic, not hand-rolled helpers.** Use `Domain.model_validate(doc, from_attributes=True)` and `Response.model_validate(domain, from_attributes=True)` where field names align. When the wire format renames or transforms fields (e.g., camelCase ↔ snake_case, nested → flat), keep mapping as a private helper *in the same `service.py`* rather than a separate file.

9. **Emit on every write.** State-mutating service functions end with `await emit(<Event>(data=...))` — or have an explicit `# no-event: <reason>` comment on the line before return. Silent mutations desync downstream handlers (search index, soul memory, ripple invalidation).

10. **Errors via CloudError.** Use `_core.errors` subclasses (`NotFound`, `Forbidden`, `Conflict`, `ValidationError`, etc.). The canonical location is `ee/pocketpaw_ee/cloud/_core/errors.py`; `ee/pocketpaw_ee/cloud/shared/errors.py` is a transitional re-export shim from the 2026-04-27 cloud-restructure that remains for backwards compatibility. **New code should import from `pocketpaw_ee.cloud._core.errors` directly.** Some existing modules (including `pockets/service.py`) still import via the shared shim; that's tracked for touch-time migration. Never `raise HTTPException` in services or routers — `_core.http` maps `CloudError` to JSON.

11. **Prefer events over transactions.** Only money, identity, and permission flows reach for `session.start_transaction()`.

### Touch-time migration rule

When you touch any `ee/pocketpaw_ee/cloud/<entity>/*.py` file for any reason — bug fix, feature add, refactor — bring that entity onto the 4-file shape in the same PR:

1. Check whether `<entity>/{domain.py, dto.py, service.py, router.py}` exists with no `repositories.py`. If not, refactor on the way out.
2. Add `<Entity>Document` to the failing list in the `import-linter` contract; add `<entity>/router.py`, `<entity>/dto.py`, `<entity>/domain.py` to the source-modules list.
3. Ship the original change + the consolidation in the same PR.

`pockets/` is the canonical reference. Copy its shape.

### Atlas touch-time rule — the OS self-model must not drift

atlas (`src/pocketpaw/atlas/`) is the runtime OS self-model: the compiled corpus
agents query via `atlas_search` / `atlas_describe` and the always-on Paw OS
Primer. **A primitive, surface, or agent-facing capability that isn't in atlas is
undiscoverable to agents — and its CI drift check only proves the compiled
`atlas.json` matches the authored JSON, NOT that the authored facts match the
live OS.** Two whole subsystems (Fabric source-truth, the verify loop) shipped
after atlas was seeded and stayed invisible for weeks; three live routes were
once missing/stale while the check stayed green.

When you **add, rename, or remove a primitive, a user-facing surface/route, or
an agent-facing capability** — in the same PR:

1. Update `src/pocketpaw/atlas/authored/{primitives,surfaces,capabilities}.json`
   (all 10 `AtlasEntry` fields; primitives carry a `gist`; capabilities carry a
   `role:*` marker in `requires`; verify every route/fact against the real
   frontend routes, not just that it recompiles).
2. Recompile: `uv run pocketpaw atlas build`, then `atlas build --check` green;
   commit `src/pocketpaw/atlas/data/atlas.json`.
3. Pin the new intent(s) in `tests/atlas/eval_cases.json` (both directions — the
   new entry wins its intents, existing primitives still win theirs).

Routine refactors and bug fixes don't need atlas updates. If a subsystem ships
behind a rollout flag, keep the entry discoverable and let the overlay mark its
live `mode` (see `docs/atlas.md`) — never hide it.

---

## Prompt rows must carry the id the tools take

Applies to every prompt block that lists entities — cloud surface preambles
(`ee/pocketpaw_ee/cloud/surface/handlers/`) and the channel prompt layers
(`src/pocketpaw/prompt/`) alike.

**The rule:** if any tool declares a required `<kind>_id`, every prompt row that
lists `<kind>`s must carry that id.

**Never hand-roll a row.** Use `pocketpaw.prompt.entity.entity_line(label,
entity_id, **facts)`. `entity_id` is a required positional, so a row cannot
silently omit it; pass `None` where there genuinely is no id and it renders a
visible `id=?`.

**Why:** four handlers independently rendered `- {name} (…)` with no id while
`update_widget` required `widget_id`, so the prompt named widgets and pockets the
agent could not address. Two pockets called "Sales" rendered identical rows and
the tool call resolved to the wrong one silently. `rows.append(f"- {name} …")` is
the obvious thing to type, which is why this is a gate and not a review note.

**Enforcement** (`tests/cloud/surface/test_entity_id_contract.py`): an AST scan
fails any hand-rolled row; allow-listed modules pin their row *count*, so an
exempt file cannot grow a new one; and the set of addressable kinds is **derived
from the MCP tool schemas**, so adding a tool with a required `site_id` fails the
build until someone decides whether the sites preamble owes an id.

**No id to render?** Use `unaddressed_line("<kind>", label, **facts)` — it emits
no id, and the `<kind>` literal is *checked* against the tool schemas, so the
exemption fails the moment a tool starts requiring that id. Prefer it over adding
an allow-list entry.

**Ids render short.** `entity_line` shows the last 8 characters
(`id=…3f9a1c07`), and `ee/pocketpaw_ee/cloud/pockets/id_resolve.py` resolves a
tail back to the whole id, scoped to the workspace/pocket, erroring on ambiguity
rather than picking. The **tail**, not the head: an ObjectId starts with a
timestamp, so 12 widgets created together share their first 20 characters.

**Check the cap when you convert a list, and make the fixture realistic.** A test
entity with no `id` measures rows ~23 chars shorter than production, and the cap
test will pass while reporting headroom that isn't there.

---

## The prompt may not command a tool the agent doesn't have

Applies to every system-prompt block — `src/pocketpaw/ripple/_inline.py`, the
rule constants in `ee/pocketpaw_ee/cloud/chat/agent_service.py`, and any new
block you add.

**The rule:** before a prompt block names a tool, confirm the agent it reaches
actually has that tool. If the answer depends on the backend, gate the block on
`backend_name` — `build_behavior_instructions` already takes it.

**Why it fails silently.** A model handed an unsatisfiable instruction does not
raise; it improvises, and the improvisation looks like a normal reply. Two live
examples, both found by dumping the wire body rather than reading the code:

- `# MUST CALL BEFORE EMIT` made `get_inline_widget_help` mandatory and said "if
  the tool returns an error, OMIT the widget". The tool was on the
  `pocketpaw_widgets` MCP server, which only `agents/claude_sdk.py` builds — so
  on every other backend it did not error, it was absent, and the agent's only
  consistent move was to drop the widget. A block written to protect widget
  quality was destroying it.
- `<composio-auth-flow>` taught a four-step OAuth sequence on
  `initiate_connection` / `verify_connection`, gated on credentials alone.
  Composio builds tools for four backend kinds; this deploy runs a fifth. The
  agent was told it had Gmail/Slack/GitHub — and told the *user* so.

**Naming an existing-but-wrong tool is the same defect.** The MUST-CALL block
was satisfiable on the SDK backend and still wrong: `get_inline_widget_help`
returns hand-written design prose for 16 widgets, while `get_widget_spec` reads
the manifest and returns the prop schema. For `definition-list` — the block's own
cited failure — that is 18,623 chars without the answer versus 759 chars with it.

**Gate from one source of truth.** `providers.py` owns
`supports_composio_tools` / `supports_connection_tools` next to the code that
builds the tools, so adding a wrapper widens the prompt in the same commit. Never
hand-maintain a second backend list in the prompt layer.

**Enforcement** (`tests/test_prompt_names_only_real_tools.py`): every backticked
`tool(` call in the inline prompt must resolve in the runtime builtin registry —
the registry every backend gets, not the SDK's MCP surface. Per-backend gating is
tested next to the assembly in
`tests/cloud/test_agent_service_tools_context.py`.

**Bridging beats deleting.** When the instruction is right and the tool is
merely unreachable, add it to `tools/builtin/` and `tools/cli.py::_TOOLS` (see
`widget_spec.py`, and `flow_tool.py` before it) rather than dropping the rule.
Then classify it in `pydantic_ai._TENANT_SAFE_TOOLS` — an unclassified tool is
withheld, so registry presence alone still leaves the prompt lying.

**A tool result is prompt too.** A lookup miss that returns the whole catalog is
not "erring toward too much": `widget_help` answered any unknown type with all
58,765 chars of the design rulebook, which never contained the answer. Return the
miss, name what can answer it.

---

## A gate is not a gate until a mutation has been observed to break it

Applies to any test you are treating as protection — a contract test, a cap
assertion, a security check, a regression guard.

**Before claiming a test guards something, break the code on purpose and watch it
fail.** Use `scripts/mutate.py`:

```bash
uv run python scripts/mutate.py --plan tests/mutations/<area>.json
```

A plan is a JSON list of `{label, file, find, replace, tests}`. The script applies
each mutation, runs the tests, restores the file, and exits non-zero if any
mutation **escaped** (tests still passed).

**Why:** a passing test means the code and the test agree, which is also true when
both are wrong. That is not hypothetical here — `updatedAt` never updated and
every key on it reported "unchanged"; a `FunctionModel` double advertised native
tool search and a deferred-loading probe reported 0% saving; a cap fixture with no
`id` measured rows 23 chars short and kept passing; a positional-only test
asserted a `TypeError` that came from a missing argument, not from the property it
named. Each was found by mutation, not by review.

**How to apply:** when you add or change a gate, add its mutations to a plan under
`tests/mutations/` and run it. Docstrings in this repo name the mutation that
breaks each test — that convention is only worth anything if the mutation was
actually run, so run it. An escaping mutation is a bug in your test, not a
curiosity.

---

## Desktop Client (`client/`)

The Tauri 2.0 + SvelteKit desktop app lives in `client/`. It connects to the Python backend via REST/WebSocket.

### Commands

```bash
cd client && bun install               # Install deps (uses Bun, not npm)
cd client && bun run dev               # Vite dev server (http://localhost:1420)
cd client && bun run build             # Production build → client/build
cd client && bun run check             # Type check (svelte-kit sync + svelte-check)
cd client && bun run tauri dev         # Full desktop app (frontend + Tauri shell)
cd client && bun run tauri build       # Build desktop app
cd client && bun run tauri:android     # Android dev
cd client && bun run tauri:ios         # iOS dev
```

### Architecture

**SvelteKit 2 + Svelte 5** static SPA (adapter-static, no SSR) bundled into **Tauri 2.0** desktop app. Rust backend (`client/src-tauri/`) handles OAuth tokens, system tray, global hotkeys, notifications, and multi-window management.

**State management**: Svelte 5 runes (`$state`, `$derived`, `$effect`) in `client/src/lib/stores/`.

**API layer**: REST client (`client/src/lib/api/client.ts`) with Bearer auth + 401 refresh. WebSocket (`client/src/lib/api/websocket.ts`) for streaming with auto-reconnect.

**UI**: shadcn-svelte (bits-ui + Tailwind CSS 4) components. Custom window chrome.

### Conventions

- Bun for package management (not npm/yarn)
- TypeScript strict mode, Svelte 5 runes
- Tailwind CSS 4 with `@tailwindcss/vite`
- Tauri IPC commands in `client/src-tauri/src/commands.rs`
- Internal design docs in `client/internal-docs/`

See `client/CLAUDE.md` for full details.
