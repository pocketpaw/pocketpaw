# Contributing to PocketPaw

PocketPaw is an open-source AI agent that runs locally and connects to Telegram, Discord, Slack, WhatsApp, and a web dashboard. Python 3.11+, async everywhere, protocol-oriented.

We welcome contributions of all kinds: bug fixes, new tools, channel adapters, docs, tests.

## Branch strategy

> **All pull requests must target the `dev` branch.**
>
> PRs opened against `main` will be closed. The `main` branch is updated only via merge from `dev` when a release is ready.

## Before you start

- Search [existing issues](https://github.com/pocketpaw/pocketpaw/issues) to see if your bug or feature has already been reported.
- Check [open pull requests](https://github.com/pocketpaw/pocketpaw/pulls) to make sure someone isn't already working on the same thing.
- If an issue exists, comment on it to let others know you're picking it up.
- If no issue exists, open one first to discuss the approach before writing code.
- Issues labeled [`good first issue`](https://github.com/pocketpaw/pocketpaw/labels/good%20first%20issue) are a good starting point if you're new to the codebase.

## Setting up your environment

1. **Fork** the repository and clone your fork.
2. **Create a feature branch** off `dev`:
   ```bash
   git checkout dev
   git pull origin dev
   git checkout -b feat/your-feature
   ```
3. **Install dependencies** (requires [uv](https://docs.astral.sh/uv/)):
   ```bash
   uv sync --dev
   ```
4. **Run the app** to verify your setup:
   ```bash
   uv run pocketpaw
   ```
   The web dashboard should open at `http://localhost:8888`.

## Development commands

```bash
# Run the app (web dashboard)
uv run pocketpaw

# Run with auto-reload (watches *.py, *.html, *.js, *.css)
uv run pocketpaw --dev

# Run tests (skip e2e, they need Playwright browsers)
uv run pytest --ignore=tests/e2e

# Run a specific test file
uv run pytest tests/test_bus.py -v

# Lint
uv run ruff check .

# Format
uv run ruff format .

# Type check
uv run mypy .
```

## Project structure

```
src/pocketpaw/
  agents/            # Agent backends (Claude SDK, Native, Open Interpreter) + router
  bus/               # Message bus + event types
    adapters/        # Channel adapters (Telegram, Discord, Slack, WhatsApp, etc.)
  tools/
    builtin/         # 60+ built-in tools (Gmail, Spotify, web search, filesystem, etc.)
    protocol.py      # ToolProtocol interface (implement this for new tools)
    registry.py      # Central tool registry with policy filtering
    policy.py        # Tool access control (profiles, allow/deny lists)
  memory/            # Memory stores (file-based, mem0)
  security/          # Guardian AI, injection scanner, audit log
  mcp/               # MCP server configuration and management
  deep_work/         # Multi-step task decomposition and execution
  mission_control/   # Multi-agent orchestration
  daemon/            # Background tasks, triggers, proactive behaviors
  config.py          # Pydantic Settings with POCKETPAW_ env prefix
  credentials.py     # Fernet-encrypted credential store
  dashboard.py       # FastAPI server, WebSocket handler, REST APIs
  scheduler.py       # APScheduler-based reminders and cron jobs
frontend/            # Vanilla JS/CSS/HTML dashboard (no build step)
tests/               # pytest suite (130+ tests)
```

## Writing code

### Conventions

- **Async everywhere.** All agent, bus, memory, and tool interfaces are async.
- **Protocol-oriented.** Core interfaces (`AgentProtocol`, `ToolProtocol`, `MemoryStoreProtocol`, `BaseChannelAdapter`) are Python `Protocol` classes. Implement the protocol, don't subclass the concrete class.
- **Ruff config:** line-length 100, target Python 3.11, lint rules E/F/I/UP.
- **Lazy imports** for optional dependencies. Agent backends and tools with heavy deps are imported inside functions, not at module level.

### Adding a new tool

1. Create a file in `src/pocketpaw/tools/builtin/`.
2. Subclass `BaseTool` from `tools/protocol.py`.
3. Implement `name`, `description`, `parameters` (JSON Schema), and `execute(**params) -> str`.
4. Add the class to `tools/builtin/__init__.py` lazy imports.
5. Add the tool to the appropriate policy group in `tools/policy.py`.
6. Write tests.

### Adding a new channel adapter

1. Create a file in `src/pocketpaw/bus/adapters/`.
2. Extend `BaseChannelAdapter`.
3. Implement `_on_start()`, `_on_stop()`, and `send(message)`.
4. Use `self._publish_inbound()` to push incoming messages to the bus.
5. Add any new dependencies as optional extras in `pyproject.toml`.

### Security considerations

PocketPaw handles API keys, OAuth tokens, and shell execution. Keep these in mind:

- Never log or expose credentials. Use `credentials.py` for secret storage.
- New config fields that hold secrets must be added to the `SECRET_FIELDS` list in `credentials.py`.
- Shell-executing tools must respect the Guardian AI safety checks.
- New API endpoints need auth middleware.
- Test for injection patterns if your feature handles user input.

## Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add Spotify playback tool
fix: handle empty WebSocket message
docs: update channel adapter guide
refactor: simplify model router thresholds
test: add coverage for injection scanner
```

Keep the subject line under 72 characters. Add a body if the change needs explanation.

## Pull request checklist

- [ ] Branch is based on `dev` (not `main`)
- [ ] PR targets the `dev` branch
- [ ] Tests pass (`uv run pytest --ignore=tests/e2e`)
- [ ] Linting passes (`uv run ruff check .`)
- [ ] No secrets or credentials in the diff
- [ ] New config fields are added to `Settings.save()` dict
- [ ] New secret fields are added to `SECRET_FIELDS` in `credentials.py`
- [ ] New tools are registered in the appropriate policy group
- [ ] New optional dependencies are declared in `pyproject.toml` extras

## Code review

- PRs are reviewed by maintainers. We aim to respond within a few days.
- Small, focused PRs get reviewed faster than large ones.
- If your PR has been open for a week with no response, ping us in the issue.

## Reporting bugs

Open an issue with:
- What you expected to happen
- What actually happened
- Steps to reproduce
- Your OS, Python version, and PocketPaw version (`pocketpaw --version`)

## Questions

Open a [Discussion](https://github.com/pocketpaw/pocketpaw/discussions) or comment on a relevant issue. We're around.
