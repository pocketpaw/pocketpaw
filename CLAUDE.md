# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PocketPaw is a self-hosted AI agent that runs locally and is controlled via Telegram, Discord, Slack, WhatsApp, or a web dashboard. The Python package is named `pocketpaw` (the internal/legacy name), while the public-facing name is `pocketpaw`. Python 3.11+ required.

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
3. All backends implement the `AgentBackend` protocol (`agents/backend.py`) and yield standardized `AgentEvent` objects with `type`, `content`, and `metadata`
4. Legacy backend names (`pocketpaw_native`, `open_interpreter`, `claude_code`, `gemini_cli`) are mapped to active backends via `_LEGACY_BACKENDS` in the registry

### Channel Adapters

`bus/adapters/` contains protocol translators that bridge external channels to the message bus:

- `TelegramAdapter` — python-telegram-bot
- `WebSocketAdapter` — FastAPI WebSockets
- `DiscordAdapter` — discord.py (optional dep `pocketpaw[discord]`). Slash command `/paw` + DM/mention support. Stream buffering with edit-in-place (1.5s rate limit).
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

### Frontend

The web dashboard (`frontend/`) is vanilla JS/CSS/HTML served via FastAPI+Jinja2. No build step. Communicates with the backend over WebSocket for real-time streaming.

## Key Conventions

- **Async everywhere**: All agent, bus, memory, and tool interfaces are async. Tests use `pytest-asyncio` with `asyncio_mode = "auto"`
- **Protocol-oriented**: Core interfaces (`AgentProtocol`, `ToolProtocol`, `MemoryStoreProtocol`, `BaseChannelAdapter`) are Python `Protocol` classes for swappable implementations
- **Env vars**: All settings use `POCKETPAW_` prefix (e.g., `POCKETPAW_ANTHROPIC_API_KEY`)
- **API key required**: The `claude_agent_sdk` backend requires an `ANTHROPIC_API_KEY` when using the Anthropic provider. OAuth tokens from Free/Pro/Max plans are not permitted for third-party use per [Anthropic's policy](https://code.claude.com/docs/en/legal-and-compliance#authentication-and-credential-use). Ollama/local providers do not require an API key.
- **Ruff config**: line-length 100, target Python 3.11, lint rules E/F/I/UP
- **Entry point**: `pocketpaw.__main__:main`
- **Lazy imports**: Agent backends are imported inside `AgentRouter._initialize_agent()` to avoid loading unused dependencies
