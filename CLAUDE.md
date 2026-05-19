# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PocketPaw is a self-hosted AI agent that runs locally and is controlled via Telegram, Discord, Slack, WhatsApp, or a web dashboard. The Python package is named `pocketpaw` (the internal/legacy name), while the public-facing name is `pocketpaw`. Python 3.11+ required.

## Knowledge Base

A codebase wiki lives at `docs/wiki/` — auto-generated from AST analysis + LLM compilation. **Read the relevant wiki article before modifying a module.**

```bash
# Search the KB from terminal
cd /path/to/knowledge-base && kb search "GroupService" --scope paw-cloud

# Show a specific module's wiki
kb show group_service --scope paw-cloud

# Rebuild after big changes (also runs automatically via PostCommit hook)
kb build ./ee/cloud --scope paw-cloud --output docs/wiki/

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

The wiki auto-rebuilds on commits that touch `ee/cloud/` files (via `.claude/hooks/kb-rebuild.sh`).

## Commands

```bash
# Install dev dependencies
uv sync --dev

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

# Run all tests (excluding E2E tests)
uv run pytest --ignore=tests/e2e

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

### Frontend

The web dashboard (`frontend/`) is vanilla JS/CSS/HTML served via FastAPI+Jinja2. No build step. Communicates with the backend over WebSocket for real-time streaming.

## Key Conventions

- **Async everywhere**: All agent, bus, memory, and tool interfaces are async. Tests use `pytest-asyncio` with `asyncio_mode = "auto"`
- **Protocol-oriented**: Core interfaces (`AgentProtocol`, `ToolProtocol`, `MemoryStoreProtocol`, `BaseChannelAdapter`) are Python `Protocol` classes for swappable implementations
- **Env vars**: All settings use `POCKETPAW_` prefix (e.g., `POCKETPAW_ANTHROPIC_API_KEY`)
- **Soul config**: `POCKETPAW_SOUL_ENABLED=true`, `POCKETPAW_SOUL_NAME`, `POCKETPAW_SOUL_ARCHETYPE`, `POCKETPAW_SOUL_PATH`, `POCKETPAW_SOUL_AUTO_SAVE_INTERVAL`
- **Files-as-Knowledge config** (Phase 1): `POCKETPAW_EXTRACTION_CHAIN` (JSON list of adapter names, e.g. `'["gemini-flash","local"]'`), `POCKETPAW_EXTRACTION_PER_MIME` (JSON map of mime→adapter), `POCKETPAW_GEMINI_API_KEY` for the cloud captioning adapter; `POCKETPAW_KB_SCOPES` (JSON list, e.g. `'["workspace:w1","agent:a1"]'`) drives multi-scope KB injection in the agent system prompt. The legacy `POCKETPAW_KB_SCOPE` (single string) still works via a deprecation shim that copies it into `kb_scopes` on startup. Phase 3 (Stage 3.E): uploads can carry `pocket_id` (form field on `POST /api/v1/uploads`, query on `GET /api/v1/files`); the FileReady listener routes pocket uploads into `pocket:{id}` KB and the agent's `_get_kb_context` resolves scope priority `pocket > agent > workspace` via the per-request `KbContext` threaded from the cloud chat path.
- **In-process bus subscribers**: `ee.cloud._core.realtime.bus.InProcessBus` exposes `subscribe(event_type, handler)` for cloud-side listeners (e.g. the `FileReady` → KB indexer wired in `ee/cloud/uploads/listeners.py`). Register subscribers from `mount_cloud()` after `init_realtime()` runs. Handler exceptions are logged and swallowed per-handler so one bad listener can't block the rest of the dispatch.
- **API key required**: The `claude_agent_sdk` backend requires an `ANTHROPIC_API_KEY` when using the Anthropic provider. OAuth tokens from Free/Pro/Max plans are not permitted for third-party use per [Anthropic's policy](https://code.claude.com/docs/en/legal-and-compliance#authentication-and-credential-use). Ollama/local providers do not require an API key.
- **Ruff config**: line-length 100, target Python 3.11, lint rules E/F/I/UP
- **Entry point**: `pocketpaw.__main__:main`
- **Lazy imports**: Agent backends are imported inside `AgentRouter._initialize_agent()` to avoid loading unused dependencies

## ee/cloud Code Rules

Applies to code under `ee/cloud/`. Local-runtime code (`src/pocketpaw/`)
uses different patterns; these rules don't apply there.

1. **Each entity has a 4-file shape.** `<entity>/{domain.py, dto.py, service.py, router.py}`. No `repositories.py`. The service IS the repository — Beanie writes are inline.

2. **Writes go through `<entity>/service.py`.** Never import Beanie document classes (`ee.cloud.models.*`) from routers, DTOs, domains, channels, tools, or agents. Only `<entity>/service.py` may import its own `models.<entity>`.

3. **Domain enforces multi-tenancy at construction.** `domain.py` value objects are frozen with required tenancy fields (`workspace_id`, `scope`, etc.) — no defaults. Constructing a domain object without tenancy info is a type error.

4. **DTOs separate request and response.** `dto.py` defines distinct `<Op>Request` and `<Entity>Response` classes. Never reuse one model for both input and output — fields leak silently.

5. **Service signature.**
   ```python
   async def op(workspace_id: str, user_id: str, body: <RequestSchema>) -> dict:
   ```
   Module-level `async def`, not a class. Multi-tenancy via the explicit `workspace_id` parameter. `user_id` carries the viewer context for permission checks. Public APIs return wire dicts (`dict`) for legacy router compatibility — see `pockets/service.py` for the canonical shape, including the `_resolved_wire_dict` helper that produces the wire dict from a Beanie doc.

   *Note: a future migration may move toward a `RequestContext` value object that bundles `(workspace_id, user_id, viewer_metadata)`. New modules that anticipate this can mint a private `_context.py:RequestContext` (see `ee/calendar/_context.py` in PR #1132 for an example), but the mainline pattern remains the explicit parameter pair until pockets migrates.*

6. **Validate at entry.** First line of every service function: `body = <RequestSchema>.model_validate(body)`. FastAPI parses HTTP bodies; services re-parse for internal callers (bus handlers, MCP tools, CLI, jobs).

7. **Tenant filter on every read.** Every `_FooDoc.find(...)` / `find_one(...)` call includes `workspace=ctx.workspace_id` (or has an explicit `# global-read: <reason>` comment). Domain-level required fields catch construction-time leaks; this rule catches read-path leaks.

8. **Mapping via Pydantic, not hand-rolled helpers.** Use `Domain.model_validate(doc, from_attributes=True)` and `Response.model_validate(domain, from_attributes=True)` where field names align. When the wire format renames or transforms fields (e.g., camelCase ↔ snake_case, nested → flat), keep mapping as a private helper *in the same `service.py`* rather than a separate file.

9. **Emit on every write.** State-mutating service functions end with `await emit(<Event>(data=...))` — or have an explicit `# no-event: <reason>` comment on the line before return. Silent mutations desync downstream handlers (search index, soul memory, ripple invalidation).

10. **Errors via CloudError.** Use `_core.errors` subclasses (`NotFound`, `Forbidden`, `Conflict`, `ValidationError`, etc.). The canonical location is `ee/cloud/_core/errors.py`; `ee/cloud/shared/errors.py` is a transitional re-export shim from the 2026-04-27 cloud-restructure that remains for backwards compatibility. **New code should import from `ee.cloud._core.errors` directly.** Some existing modules (including `pockets/service.py`) still import via the shared shim; that's tracked for touch-time migration. Never `raise HTTPException` in services or routers — `_core.http` maps `CloudError` to JSON.

11. **Prefer events over transactions.** Only money, identity, and permission flows reach for `session.start_transaction()`.

### Touch-time migration rule

When you touch any `ee/cloud/<entity>/*.py` file for any reason — bug fix, feature add, refactor — bring that entity onto the 4-file shape in the same PR:

1. Check whether `<entity>/{domain.py, dto.py, service.py, router.py}` exists with no `repositories.py`. If not, refactor on the way out.
2. Add `<Entity>Document` to the failing list in the `import-linter` contract; add `<entity>/router.py`, `<entity>/dto.py`, `<entity>/domain.py` to the source-modules list.
3. Ship the original change + the consolidation in the same PR.

`pockets/` is the canonical reference. Copy its shape.

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
